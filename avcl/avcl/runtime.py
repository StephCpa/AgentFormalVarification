"""Serial, in-memory reference transactions.

TrustedPlane methods model an external authority/validator/adapter. They must
never be exposed to an Agent. RLock is reference atomicity, not durability.
"""
from dataclasses import replace
from threading import RLock
from uuid import uuid4
import secrets
from .canonical import Reject, amount, digest, canonical_bytes
from .requirements import merge_requirements
from .evaluation import WorkBudget, DEFAULT_WORK_LIMIT
from .types import (Request, Capability, Evidence, Approval, Account, Reservation,
                    Operation, Execution, Settlement, Observation, Event)
from .ir import refund_predicate,FieldType
from .checks import compile_checks,load_checks
from .contract import default_contract,Health,moment
from .policy import PolicyAuthority
from . import delegation


class ReferenceKernel:
    def __init__(self, tenant="demo", policy="refund-policy/0.1", clock=None, *, evaluation_limit=DEFAULT_WORK_LIMIT):
        self._evaluation_limit=WorkBudget(evaluation_limit).limit
        self.tenant, self.policy, self.epoch = tenant, policy, 1
        self._clock = clock or (lambda: 0)  # Explicit deterministic test clock.
        self._lock = RLock()
        self._accounts = {}
        self._caps, self._evidence, self._approvals = {}, {}, {}
        self._subjects,self._grants,self._delegation_requests={},{},{}
        self._operations, self._permits, self._business = {}, {}, {}
        self._dedup, self._observations = {}, {}
        self._events = []
        self._event_sequence_allocator = None  # Host-local transactional allocator.
        self._send_owners = {}  # operation -> (subject, run_id, workflow_digest)
        self._workflow_send_context = None  # Host-local, never durable authority by itself.
        self._next_id = 0
        self._quarantined = False
        self._before_prepare = None  # Optional host admission guard, inside the same transaction.
        self._predicate = refund_predicate()
        generated = compile_checks(self._predicate)
        self._checks = load_checks(generated.to_json(),self._predicate)
        initial = default_contract()
        self._health = {"remote-idempotency":Health("simulator","refund",0,1000,"Healthy")}
        self._policy_authority = PolicyAuthority(initial.tools,initial.validators)
        envelope = self._policy_authority.seal(initial,self._health,0,1000,initial.requirements(),
            frozenset(c.id for c in initial.clauses if c.kind=="safety"),{"refund":{"amount":1,"currency":"CNY"}})
        self._release = self._policy_authority.load(envelope,self._health,0)
        self._policy_snapshots = {initial.semantic_digest:self._release}

    @property
    def trusted(self):
        """Host setup API; access control is an external process-boundary task."""
        return TrustedPlane(self)

    def _event(self, kind, operation_id=None, **details):
        if "requirement_ids" in details:
            details["requirement_ids"] = canonical_bytes(list(merge_requirements(details["requirement_ids"]))).decode("utf-8")
        sequence=self._event_sequence_allocator() if self._event_sequence_allocator is not None else len(self._events)+1
        self._events.append(Event(sequence, kind, operation_id,
                                  tuple(sorted((k, str(v)) for k, v in details.items()))))

    def _admission_requirements(self, event, request, capability_ref):
        clauses = self._release.compiled.contract.clauses
        return merge_requirements(
            ("R01", "R02", "R04", "R06", "R08", "R09", "R14"),
            (r for c in clauses if c.tool == request.tool and event in c.events for r in c.requirement_ids),
            ("R11",) if capability_ref in self._grants else ())

    def events(self):
        with self._lock:
            return tuple(self._events)

    def accounts(self):
        with self._lock:
            return dict(self._accounts)

    def _lookup(self, records, ref):
        if type(ref) is not str or ref not in records:
            raise Reject("UNTRUSTED_REFERENCE")
        return records[ref]

    def _owned(self, subject, operation_id):
        op = self._lookup(self._operations, operation_id)
        if op.request.subject != subject:
            raise Reject("AUTH_DENIED")
        return op

    def get(self, subject, operation_id):
        with self._lock:
            return self._owned(subject, operation_id)

    def delegate(self,subject,raw):
        with self._lock:return delegation.delegate(self,subject,raw)

    def _authorize(self, request, cap_ref, evidence_ref, approval_ref, now, event="prepare"):
        budget=WorkBudget(self._evaluation_limit)
        if self._quarantined:
            raise Reject("ASSUMPTION_BROKEN")
        if not self._checks.evaluate({"amount": amount(request.amount_minor), "currency": request.currency},budget=budget):
            raise Reject("POLICY_DENIED")
        cap = self._lookup(self._caps, cap_ref)
        ev = self._lookup(self._evidence, evidence_ref)
        approval = self._lookup(self._approvals, approval_ref)
        if (request.tenant != self.tenant or cap.tenant != self.tenant or
                cap.subject != request.subject or cap.epoch != self.epoch or now >= cap.expires):
            raise Reject("AUTH_DENIED")
        if request.order not in cap.orders or amount(request.amount_minor) > cap.max_amount:
            raise Reject("SCOPE_DENIED")
        delegation.admission(self,request,cap_ref)
        if (ev.request_digest != request.request_digest or ev.predicate != "RefundEligible" or
                ev.issuer != "order-service" or now >= ev.expires):
            raise Reject("STALE_EVIDENCE")
        if getattr(self,"_check_evidence",None) is not None:self._check_evidence(evidence_ref)
        if approval.request_digest != request.request_digest or now >= approval.expires:
            raise Reject("APPROVAL_MISMATCH")
        if not self._release.issued_at <= now < self._release.expires:
            raise Reject("POLICY_EXPIRED_OR_NOT_YET_VALID")
        report=self._release.compiled.evaluate(event,request.tool,
            {"amount":amount(request.amount_minor),"max_fee":amount(request.max_fee_minor),"currency":request.currency},self._health,now,budget=budget)
        if not report["allowed"]:
            failed=[r for r in report["reports"] if not r["passed"]]
            code=("ASSUMPTION_BROKEN" if any(r["status"]=="AssumptionBroken" for r in failed) else
                  "UNKNOWN_EVALUATION" if any(r["reason"]=="UNKNOWN_EVALUATION" for r in failed) else "CONTRACT_DENIED")
            raise PolicyDenied(report,code)
        if getattr(self,"_check_task",None) is not None:self._check_task(request)
        return cap, ev, approval, budget.report()

    def _binding(self, op):
        return digest({"request": op.request.payload(), "operation_id": op.id,
                       "permit_ref": op.permit_ref, "policy": op.policy,
                       "ir_digest": op.ir_digest,
                       "contract_digest": op.contract_digest,
                       "epoch": str(op.epoch), "expires": str(op.expires),
                       "capability_ref": op.capability_ref, "evidence_ref": op.evidence_ref,
                       "approval_ref": op.approval_ref,
                       "bounds": [{"account": r.account, "upper": str(r.upper)}
                                  for r in op.reservations]}, "avcl/permit/0.1")

    def prepare(self, subject: str, request: Request, capability_ref: str,
                evidence_ref: str, approval_ref: str, client_request_id: str,
                ttl: int = 60) -> Operation:
        if type(request) is not Request:
            raise Reject("INVALID_REQUEST")
        request.validate()
        if subject != request.subject or request.tenant != self.tenant:
            raise Reject("AUTH_DENIED")
        if type(ttl) is not int or not 0 < ttl <= 3600:
            raise Reject("INVALID_TTL")
        if type(client_request_id) is not str or not 0 < len(client_request_id) <= 128:
            raise Reject("INVALID_REQUEST_ID")
        submission = digest({"request": request.payload(), "capability": capability_ref,
                             "evidence": evidence_ref, "approval": approval_ref, "ttl": str(ttl)},
                            "avcl/prepare/0.1")
        key = (subject, client_request_id)
        with self._lock:
            if key in self._dedup:
                prior_digest, operation_id = self._dedup[key]
                if submission != prior_digest:
                    raise Reject("IDEMPOTENCY_CONFLICT")
                return self._owned(subject, operation_id)
            now = self._clock()
            cap, ev, approval, evaluation_work = self._authorize(request, capability_ref, evidence_ref, approval_ref, now)
            if request.business_key in self._business:
                existing = self._operations[self._business[request.business_key]]
                if existing.request.request_digest == request.request_digest:
                    return self._owned(subject, existing.id)
                raise Reject("BUSINESS_ACTION_CONFLICT")
            if approval.operation_id is not None:
                raise Reject("APPROVAL_CONSUMED")
            if self._before_prepare is not None:self._before_prepare()
            value = amount(request.amount_minor)
            bounds = {"tenant:CNY": value + amount(request.max_fee_minor),
                      "order:" + request.order + ":CNY": value}
            bounds.update({account:value+amount(request.max_fee_minor) for account in delegation.admission(self,request,capability_ref)})
            updated = dict(self._accounts)
            for account_id, upper in bounds.items():
                account = self._lookup(self._accounts, account_id)
                if account.breached or account.spent + account.pending + upper > account.limit:
                    raise Reject("BUDGET_EXCEEDED", requirement_ids=("R11",) if account_id.startswith("delegation:") else ())
                updated[account_id] = replace(account, pending=account.pending + upper)
            # Commit all state only after every check has succeeded.
            number = self._next_id + 1
            operation_id, permit_ref = "op-"+uuid4().hex, "permit-"+secrets.token_hex(32)
            op = Operation(operation_id, permit_ref, request, capability_ref, evidence_ref,
                           approval_ref, self.policy, self.epoch,
                           min(now + ttl, cap.expires, ev.expires, approval.expires,self._release.expires), "",
                           Execution.PREPARED, Settlement.UNSETTLED,
                           tuple(Reservation(a, u, remaining=u) for a, u in sorted(bounds.items())))
            op = replace(op, ir_digest=self._predicate.semantic_digest,contract_digest=self._release.compiled.contract.semantic_digest)
            op = replace(op, binding=self._binding(op))
            self._next_id = number
            self._accounts = updated
            self._operations[op.id] = op
            self._permits[permit_ref] = op.id
            self._business[request.business_key] = op.id
            self._approvals[approval_ref] = replace(approval, operation_id=op.id)
            self._dedup[key] = (submission, op.id)
            self._event("Prepared", op.id, binding=op.binding, ir_digest=op.ir_digest, requirement_id="R09",
                        contract_digest=op.contract_digest,
                        evaluation_work=canonical_bytes(evaluation_work).decode("utf-8"),
                        requirement_ids=self._admission_requirements("prepare", request, capability_ref))
            return op

    def dispatch(self, subject, operation_id, permit_ref):
        with self._lock:
            op = self._owned(subject, operation_id)
            if self._lookup(self._permits, permit_ref) != op.id or permit_ref != op.permit_ref:
                raise Reject("BINDING_MISMATCH")
            if op.admitted:
                return op
            if op.state != Execution.PREPARED:
                raise Reject("INVALID_TRANSITION")
            now = self._clock()
            if (now >= op.expires or op.epoch != self.epoch or op.policy != self.policy or
                    op.ir_digest != self._predicate.semantic_digest or
                    op.contract_digest != self._release.compiled.contract.semantic_digest):
                raise Reject("STALE_PERMIT")
            _,_,_,evaluation_work=self._authorize(op.request, op.capability_ref, op.evidence_ref, op.approval_ref, now,"dispatch")
            if self._binding(op) != op.binding:
                raise Reject("BINDING_MISMATCH")
            if any(self._accounts[r.account].breached for r in op.reservations):
                raise Reject("ASSUMPTION_BROKEN")
            op = replace(op, admitted=True, state=Execution.DISPATCHED, revision=op.revision + 1)
            self._operations[op.id] = op
            self._event("DispatchAdmitted", op.id, epoch=self.epoch, policy=self.policy, binding=op.binding,
                        ir_digest=op.ir_digest, requirement_id="R09",contract_digest=op.contract_digest,
                        evaluation_work=canonical_bytes(evaluation_work).decode("utf-8"),
                        requirement_ids=self._admission_requirements("dispatch", op.request, op.capability_ref))
            return op

    def abort_prepared(self, subject, operation_id):
        with self._lock:
            op = self._owned(subject, operation_id)
            if op.state == Execution.ABORTED:
                return op
            if op.admitted or op.state != Execution.PREPARED:
                raise Reject("NO_EFFECT_NOT_ESTABLISHED")
            for r in op.reservations:
                a = self._accounts[r.account]
                self._accounts[r.account] = replace(a, pending=a.pending - r.remaining)
            op = replace(op, state=Execution.ABORTED, settlement=Settlement.FINAL,
                         reservations=tuple(replace(r, remaining=0) for r in op.reservations),
                         revision=op.revision + 1)
            self._operations[op.id] = op
            self._event("AbortedBeforeDispatch", op.id)
            return op

    def assert_invariants(self):
        with self._lock:
            for ref in self._grants:delegation.chain(self,ref,False)
            for name, a in self._accounts.items():
                assert a.spent >= 0 and a.pending >= 0
                assert a.pending == sum(r.remaining for op in self._operations.values()
                                        for r in op.reservations if r.account == name)
                assert a.spent == sum(r.charged for op in self._operations.values()
                                     for r in op.reservations if r.account == name)
                if not a.breached:
                    assert a.spent + a.pending <= a.limit
            admitted = [e.operation_id for e in self._events if e.kind == "DispatchAdmitted"]
            assert len(admitted) == len(set(admitted))
            for op in self._operations.values():
                if op.capability_ref in self._grants:
                    accounts={g.account for _,g in delegation.chain(self,op.capability_ref,False)}
                    assert {r.account for r in op.reservations}==accounts|{"tenant:CNY","order:"+op.request.order+":CNY"}
                    total=next(r for r in op.reservations if r.account=="tenant:CNY")
                    assert all((r.upper,r.charged,r.remaining)==(total.upper,total.charged,total.remaining) for r in op.reservations if r.account in accounts)
                assert op.admitted == (op.id in admitted)
                assert op.binding == self._binding(op)
                assert op.contract_digest in self._policy_snapshots
                if op.state == Execution.UNKNOWN:
                    assert op.obligation_due is not None
                for r in op.reservations:
                    assert r.remaining >= 0 and r.charged >= 0


class PolicyDenied(Reject):
    def __init__(self,report,code="CONTRACT_DENIED"):
        super().__init__(code, requirement_ids=(r for row in report["reports"] if not row["passed"]
                                               for r in row["requirement_ids"]))
        self.report=report


class TrustedPlane:
    def __init__(self, kernel):
        self.k = kernel

    def send_verifier(self):
        from .send_envelope import provision_verifier
        with self.k._lock:return provision_verifier(self.k)

    def require_authenticated_receipts(self,operation_id):
        from .receipt_envelope import require
        with self.k._lock:require(self.k,operation_id)

    def register_subject(self,subject):
        with self.k._lock:return delegation.register_subject(self.k,subject)

    def disable_subject(self,subject):
        with self.k._lock:return delegation.disable_subject(self.k,subject)

    def enable_delegation(self,*args,**kwargs):
        with self.k._lock:return delegation.enable(self.k,*args,**kwargs)

    def revoke_capability(self,cap_ref):
        with self.k._lock:return delegation.revoke(self.k,cap_ref)

    @staticmethod
    def _text(value):
        return type(value) is str and 0 < len(value) <= 256 and not any(0xD800 <= ord(c) <= 0xDFFF for c in value)

    def add_account(self, name, limit):
        with self.k._lock:
            if not self._text(name) or type(limit) is not int or limit < 0 or name in self.k._accounts:
                raise Reject("INVALID_ACCOUNT")
            self.k._accounts[name] = Account(limit)

    def register_capability(self, capability):
        with self.k._lock:
            if (type(capability) is not Capability or
                    not all(self._text(v) for v in (capability.ref,capability.tenant,capability.subject)) or
                    type(capability.orders) is not frozenset or not capability.orders or
                    not all(self._text(v) for v in capability.orders) or
                    any(type(v) is not int or v < 0 for v in (capability.max_amount,capability.expires,capability.epoch)) or
                    capability.ref in self.k._caps):
                raise Reject("INVALID_CAPABILITY")
            self.k._caps[capability.ref] = capability

    def register_evidence(self, evidence):
        with self.k._lock:
            if (type(evidence) is not Evidence or
                    not all(self._text(v) for v in (evidence.ref,evidence.request_digest,evidence.issuer,evidence.predicate)) or
                    type(evidence.expires) is not int or evidence.expires < 0 or evidence.ref in self.k._evidence):
                raise Reject("INVALID_EVIDENCE")
            if evidence.issuer != "order-service" or evidence.predicate != "RefundEligible":
                raise Reject("UNREGISTERED_VALIDATOR")
            self.k._evidence[evidence.ref] = evidence

    def register_approval(self, approval):
        with self.k._lock:
            if (type(approval) is not Approval or not self._text(approval.ref) or not self._text(approval.request_digest) or
                    type(approval.expires) is not int or approval.expires < 0 or
                    approval.ref in self.k._approvals or approval.operation_id is not None):
                raise Reject("INVALID_APPROVAL")
            self.k._approvals[approval.ref] = approval

    def revoke(self):
        with self.k._lock:
            self.k.epoch += 1
            self.k._event("AuthorityRevoked", epoch=self.k.epoch)

    def seal_policy(self,contract,expires,reviewed_requirements,runtime_clauses,witnesses):
        """Explicit trusted review inputs; never expose this method to an Agent."""
        with self.k._lock:
            return self.k._policy_authority.seal(contract,self.k._health,self.k._clock(),expires,
                                                reviewed_requirements,runtime_clauses,witnesses)

    def activate_policy(self,envelope):
        with self.k._lock:
            release=self.k._policy_authority.load(envelope,self.k._health,self.k._clock())
            old=self.k._release.compiled.contract;new=release.compiled.contract
            if new.module_id!=old.module_id or new.policy_version<=old.policy_version:
                raise Reject("NONMONOTONIC_POLICY_VERSION")
            new_clauses={c.id:c for c in new.clauses}
            old_assumptions={a.id:a for a in old.assumptions};new_assumptions={a.id:a for a in new.assumptions}
            for c in old.clauses:
                if c.frozen and (c.id not in new_clauses or c.semantic_digest!=new_clauses[c.id].semantic_digest or
                        any(new_assumptions.get(a)!=old_assumptions[a] for a in c.assumptions)):
                    raise Reject("FROZEN_CLAUSE_CHANGED")
            if new.tools!=old.tools or new.validators!=old.validators: raise Reject("UNSUPPORTED_RUNTIME_DEPENDENCIES")
            supported={"amount":FieldType("Int"),"max_fee":FieldType("Int"),"currency":FieldType("String")}
            for event in ("prepare","dispatch"):
                for name,t in new.input_types("refund",event).items():
                    if name not in supported or t!=supported[name]: raise Reject("UNSUPPORTED_RUNTIME_CONTEXT")
            accounts=dict(self.k._accounts)
            for name,limit in new.account_limits:
                account=self.k._lookup(accounts,name)
                if any(grant.account==name for grant in self.k._grants.values()) and limit!=account.limit:
                    raise Reject("DELEGATION_QUOTA_IMMUTABLE")
                if account.breached or account.spent+account.pending>limit: raise Reject("POLICY_BUDGET_BELOW_COMMITMENT")
                accounts[name]=replace(account,limit=limit)
            # Publish only after every dependency, clause and account has passed.
            self.k._accounts=accounts
            self.k._release=release
            self.k._policy_snapshots[new.semantic_digest]=release
            self.k.policy=f"{new.module_id}/{new.policy_version}"
            self.k.epoch+=1
            self.k._event("PolicyActivated",policy=self.k.policy,contract_digest=new.semantic_digest,
                          envelope_digest=release.envelope_digest,epoch=self.k.epoch)
            return new.semantic_digest

    def set_assumption_health(self,name,health):
        with self.k._lock:
            declarations={a.id:a for a in self.k._release.compiled.contract.assumptions}
            if (type(name) is not str or name not in declarations or type(health) is not Health or
                    type(health.status) is not str or health.status not in {"Healthy","Broken","Unknown"} or
                    health.authority!=declarations[name].authority or health.scope!=declarations[name].scope or
                    not moment(health.valid_from) or not moment(health.expires) or health.valid_from>=health.expires):
                raise Reject("INVALID_ASSUMPTION_HEALTH")
            self.k._health[name]=health
            self.k._event("AssumptionHealthChanged",assumption=name,status=health.status,expires=health.expires)

    def unknown(self, operation_id):
        with self.k._lock:
            op = self.k._lookup(self.k._operations, operation_id)
            if op.state == Execution.UNKNOWN:
                return op
            if op.state != Execution.DISPATCHED:
                raise Reject("INVALID_TRANSITION")
            op = replace(op, state=Execution.UNKNOWN, revision=op.revision + 1,
                         obligation_due=self.k._clock() + 86400)
            self.k._operations[op.id] = op
            self.k._event("Unknown", op.id, due=op.obligation_due)
            return op

    def _incident(self, op, observation, reason):
        self.k._quarantined = True
        self.k._operations[op.id] = replace(op, settlement=Settlement.DISPUTED,
                                           revision=op.revision + 1)
        self.k._event("AssumptionBroken", op.id, observation=observation.id, reason=reason)
        return self.k._operations[op.id]

    def observe(self, observation, expected_operation_id=None):
        from . import receipt_envelope
        if expected_operation_id is not None and not self._text(expected_operation_id):raise Reject("INVALID_OBSERVATION_BINDING")
        with self.k._lock:
            authenticated=type(observation) is str
            try:
                if authenticated:observation=receipt_envelope.verify(self.k,observation,expected_operation_id)
                elif type(observation) is Observation:
                    if expected_operation_id is not None and observation.operation_id!=expected_operation_id:raise Reject("UNEXPECTED_ADAPTER_OBSERVATION")
                    if receipt_envelope.required(self.k,observation.operation_id):raise Reject("AUTHENTICATED_RECEIPT_REQUIRED")
                return self._observe_trusted(observation)
            except Reject as exc:
                if authenticated or exc.code=="AUTHENTICATED_RECEIPT_REQUIRED":receipt_envelope.rejected(self.k,expected_operation_id,exc.code)
                raise

    def _observe_trusted(self,observation):
        from .observation_fact import encode_fact, fact_digest, encode_effective_costs
        if (type(observation) is not Observation or
                not all(self._text(v) for v in (observation.id,observation.operation_id,observation.request_digest)) or
                type(observation.kind) is not Execution or
                type(observation.final_cost) is not bool or type(observation.terminal_effect) is not bool or
                type(observation.cumulative_costs) is not tuple or
                any(type(pair) is not tuple or len(pair) != 2 or not self._text(pair[0]) or
                    type(pair[1]) is not int or pair[1] < 0 for pair in observation.cumulative_costs)):
            raise Reject("UNTRUSTED_OBSERVATION")
        with self.k._lock:
            op = self.k._lookup(self.k._operations, observation.operation_id)
            if observation.id in self.k._observations:
                prior=self.k._observations[observation.id]
                # A cost vector is indexed by account, not by serialization order.
                # Preserve frozen records; normalize only for semantic comparison.
                if replace(prior,cumulative_costs=tuple(sorted(prior.cumulative_costs))) == replace(
                        observation,cumulative_costs=tuple(sorted(observation.cumulative_costs))):
                    return op
                self.k._event("ConflictingObservation", op.id, observation=observation.id,
                              fact=encode_fact(observation), prior_fact_digest=fact_digest(encode_fact(prior)))
                return self._incident(op, observation, "same_id_different_content")
            if (not op.admitted or observation.request_digest != op.request.request_digest or
                    observation.kind not in (Execution.CONFIRMED, Execution.REJECTED, Execution.PARTIAL)):
                raise Reject("INVALID_OBSERVATION_BINDING")
            costs = dict(observation.cumulative_costs)
            if len(costs) != len(observation.cumulative_costs):raise Reject("INVALID_COST_VECTOR")
            costs=delegation.expand_costs(self.k,op,costs)
            if (set(costs) != {r.account for r in op.reservations} or
                    any(type(v) is not int or v < 0 for v in costs.values())):
                raise Reject("INVALID_COST_VECTOR")
            if observation.kind in (Execution.REJECTED, Execution.CONFIRMED) and not observation.terminal_effect:
                raise Reject("TERMINAL_EFFECT_NOT_ESTABLISHED")
            # Serialize evidence before changing the reference state. Raw costs
            # retain submission order; effective costs include derived quotas.
            fact=encode_fact(observation);effective_costs=encode_effective_costs(costs)
            self.k._observations[observation.id] = observation
            self.k._event("ObservationRecorded", op.id, observation=observation.id,
                          fact=fact,effective_costs=effective_costs)
            if (op.state in (Execution.CONFIRMED, Execution.REJECTED) and op.state != observation.kind):
                return self._incident(op, observation, "contradictory_terminal_effect")
            if op.state == Execution.PARTIAL and observation.kind == Execution.REJECTED:
                return self._incident(op, observation, "partial_effect_cannot_be_erased")
            if any(costs[r.account] < r.charged for r in op.reservations):
                return self._incident(op, observation, "decreasing_cumulative_cost")
            if (op.settlement == Settlement.FINAL and op.state == observation.kind and
                    all(costs[r.account] == r.charged for r in op.reservations)):
                # This new evidence is subsumed by the established final fact.
                # Retain the raw observation and audit event, not renewed exposure.
                self.k._event("ObservationSubsumed", op.id, observation=observation.id,
                              reason="same_final_effect_and_cost")
                return op
            # A terminal business rejection can still carry invocation fees.
            # Terminal fees alone cannot free exposure to a possible late effect.
            finalize = observation.final_cost and observation.terminal_effect
            breached = False
            reservations = []
            for r in op.reservations:
                c = costs[r.account]
                remaining = 0 if finalize else max(0, r.upper - c)
                delta = c - r.charged
                violates = delta + remaining > r.remaining
                breached |= violates
                a = self.k._accounts[r.account]
                self.k._accounts[r.account] = replace(a, spent=a.spent + delta,
                    pending=a.pending - r.remaining + remaining, breached=a.breached or violates)
                reservations.append(replace(r, charged=c, remaining=remaining))
            op = replace(op, state=observation.kind,
                         settlement=(Settlement.DISPUTED if op.settlement == Settlement.DISPUTED
                                     else Settlement.FINAL if finalize else Settlement.PARTIAL),
                         reservations=tuple(reservations), revision=op.revision + 1)
            self.k._operations[op.id] = op
            if breached:
                return self._incident(op, observation, "cost_bound_exceeded_or_cost_after_final")
            self.k._event("Settled" if finalize else "PartiallySettled", op.id, observation=observation.id)
            return op
