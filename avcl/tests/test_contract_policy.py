import json
import unittest
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from avcl.canonical import Reject
from avcl.ir import FieldType,compile_predicate
from avcl.contract import (Clause,Health,Tool,default_contract,build_contract,compile_contract,load_contract)
from avcl.policy import PolicyAuthority
from avcl.runtime import PolicyDenied
from avcl.types import Execution,Capability
from .test_encoding_ir import const,field,call
from .support import setup,authorize,prepare

def amended(base,version=2,extra=(),limits=(),clauses=None,assumptions=None,tools=None):
    return build_contract(base.module_id,version,tools or base.tools,base.validators,
        assumptions if assumptions is not None else base.assumptions,
        clauses if clauses is not None else base.clauses+extra,limits)

def ceiling_clause(limit=60):
    p=compile_predicate(call("Le",field("amount"),const(limit)),{"amount":FieldType("Int")},"R04")
    return Clause("refund-ceiling","safety","requires","refund",("prepare","dispatch"),("R04",),(),True,p)

def seal(k,c,expires=900,witness=None):
    if witness is None: witness={"amount":1,"currency":"CNY"}
    return k.trusted.seal_policy(c,expires,c.requirements(),frozenset(x.id for x in c.clauses if x.kind=="safety"),{"refund":witness})

class ContractPolicyTests(unittest.TestCase):
    def assertCode(self,code,fn):
        with self.assertRaises(Reject) as cm: fn()
        self.assertEqual(cm.exception.code,code)

    def test_contract_roundtrip_preserves_semantics_sources_and_order(self):
        c=default_contract();loaded=load_contract(c.to_json())
        self.assertEqual(c.to_json(),loaded.to_json())
        self.assertEqual(c.semantic_digest,loaded.semantic_digest)
        self.assertEqual(c.source_digest,loaded.source_digest)
        reordered=amended(c,version=1,tools=(replace(c.tools[0],effects=tuple(reversed(c.tools[0].effects))),))
        self.assertEqual(c.semantic_digest,reordered.semantic_digest)

    def test_contract_loader_rejects_duplicates_unknown_fields_and_broken_mapping(self):
        base=default_contract();obj=json.loads(base.to_json())
        obj["clauses"].append(obj["clauses"][0])
        self.assertCode("DUPLICATE_OR_INVALID_DECLARATION",lambda:load_contract(json.dumps(obj)))
        obj=json.loads(base.to_json());obj["silently_ignored_rule"]=True
        self.assertCode("INVALID_CONTRACT_DOCUMENT",lambda:load_contract(json.dumps(obj)))
        self.assertCode("DUPLICATE_KEY",lambda:load_contract('{"format":"a","format":"b"}'))
        bad=replace(base.clauses[0],requirement_ids=("R01",))
        self.assertCode("REQUIREMENT_MAPPING_MISMATCH",lambda:amended(base,clauses=(bad,)))

    def test_signed_check_payload_cannot_be_changed(self):
        k,_,_=setup();blob=seal(k,amended(default_contract()));obj=json.loads(blob)
        obj["checks"]["baseline-refund"]["instructions"][0]["value"]="-1"
        self.assertCode("INVALID_POLICY_SIGNATURE",lambda:k.trusted.activate_policy(json.dumps(obj)))

    def test_expired_policy_does_not_release_pending_operation(self):
        k,s,clock=setup();c=amended(default_contract());k.trusted.activate_policy(seal(k,c,expires=20))
        auth=authorize(k,s);k.trusted.register_capability(Capability("fresh","demo","a",frozenset(["a"]),100,1000,2))
        op=prepare(k,(auth[0],"fresh",auth[2],auth[3]));clock.now=20
        self.assertCode("STALE_PERMIT",lambda:k.dispatch("a",op.id,op.permit_ref))
        self.assertEqual(k.accounts()["tenant:CNY"].pending,70);k.assert_invariants()

    def test_completion_is_separate_and_forbid_inverts_the_predicate(self):
        base=default_contract();schema={"amount":FieldType("Int")}
        banned=Clause("not-five","safety","forbid","refund",("prepare","dispatch"),("R02",),(),False,
                      compile_predicate(call("Eq",field("amount"),const(5)),schema,"R02"))
        completion=Clause("business-success","completion","success","refund",("close",),("R09",),(),False,
                          compile_predicate(field("done"),{"done":FieldType("Bool")},"R09"))
        cc=compile_contract(amended(base,extra=(banned,completion)))
        health={"remote-idempotency":Health("simulator","refund",0,1000,"Healthy")}
        self.assertTrue(cc.evaluate("dispatch","refund",{"amount":1,"currency":"CNY","done":False},health,0)["allowed"])
        report=cc.evaluate("dispatch","refund",{"amount":5,"currency":"CNY"},health,0)
        self.assertFalse(report["allowed"])
        self.assertEqual({r["clause_id"]:r["status"] for r in report["reports"]},
                         {"baseline-refund":"RuntimeSatisfied","not-five":"Violated"})
        self.assertFalse(cc.evaluate("close","refund",{"done":False},health,0)["allowed"])
        self.assertTrue(cc.evaluate("close","refund",{"done":True},health,0)["allowed"])

    def test_unknown_or_unsupported_event_cannot_implicitly_authorize(self):
        base=default_contract();cc=compile_contract(base)
        self.assertFalse(cc.evaluate("dispatch","other",{}, {},0)["allowed"])
        bad=replace(base.clauses[0],kind="bounded_obligation")
        self.assertCode("UNSUPPORTED_CLAUSE_KIND",lambda:amended(base,clauses=(bad,)))
        bad=replace(base.clauses[0],events=("prepare",))
        self.assertCode("UNCOVERED_TOOL_EVENT",lambda:amended(base,clauses=(bad,)))
        bad=replace(base.clauses[0],kind="completion",rule="success")
        self.assertCode("COMPLETION_IS_NOT_AUTHORIZATION",lambda:amended(base,clauses=(bad,)))

    def test_signature_rejects_content_change_wrong_key_and_malformed_mac(self):
        k,_,_=setup();c=amended(default_contract());blob=seal(k,c)
        tampered=json.loads(blob);tampered["contract"]["policy_version"]="3"
        self.assertCode("INVALID_POLICY_SIGNATURE",lambda:k.trusted.activate_policy(json.dumps(tampered)))
        foreign=PolicyAuthority(c.tools,c.validators)
        self.assertCode("INVALID_POLICY_SIGNATURE",lambda:foreign.load(blob,k._health,0))
        tampered=json.loads(blob);tampered["signature"]="非签名"
        self.assertCode("UNSUPPORTED_SIGNED_POLICY",lambda:k.trusted.activate_policy(json.dumps(tampered)))

    def test_missing_review_or_dynamic_gate_cannot_be_signed(self):
        k,_,_=setup();c=amended(default_contract())
        self.assertCode("INCOMPLETE_REQUIREMENT_REVIEW",lambda:k.trusted.seal_policy(c,900,frozenset(),frozenset(["baseline-refund"]),{}))
        self.assertCode("HARD_CLAUSE_WITHOUT_APPROVED_CHECK",lambda:k.trusted.seal_policy(c,900,c.requirements(),frozenset(),{}))
        self.assertCode("MISSING_NONVACUITY_WITNESS",lambda:k.trusted.seal_policy(c,900,c.requirements(),frozenset(["baseline-refund"]),{}))

    def test_cross_clause_contradiction_has_no_acceptable_witness(self):
        k,_,_=setup();c=amended(default_contract(),extra=(ceiling_clause(0),))
        for value in (0,1,70):
            self.assertCode("INVALID_NONVACUITY_WITNESS",lambda:seal(k,c,witness={"amount":value,"currency":"CNY"}))

    def test_underdeclared_tool_effects_are_rejected(self):
        k,_,_=setup();base=default_contract()
        c=amended(base,tools=(Tool("refund","sim/0.1",("Read",)),))
        self.assertCode("TOOL_DEPENDENCY_MISMATCH",lambda:seal(k,c))

    def test_failed_activation_changes_no_budget_epoch_policy_or_events(self):
        k,s,_=setup();prepare(k,authorize(k,s))
        old=(k.accounts(),k.epoch,k.policy,k.events(),tuple(k._policy_snapshots))
        c=amended(default_contract(),limits=(("tenant:CNY",200),("order:a:CNY",69)))
        blob=seal(k,c)
        self.assertCode("POLICY_BUDGET_BELOW_COMMITMENT",lambda:k.trusted.activate_policy(blob))
        self.assertEqual((k.accounts(),k.epoch,k.policy,k.events(),tuple(k._policy_snapshots)),old)
        k.assert_invariants()

    def test_valid_update_keeps_old_prepared_snapshot_and_blocks_old_admission(self):
        k,s,_=setup();op=prepare(k,authorize(k,s));original=op.contract_digest
        c=amended(default_contract(),extra=(ceiling_clause(),),limits=(("tenant:CNY",100),))
        self.assertEqual(k.trusted.activate_policy(seal(k,c)),c.semantic_digest)
        self.assertEqual(k.epoch,2)
        self.assertCode("STALE_PERMIT",lambda:k.dispatch("a",op.id,op.permit_ref))
        self.assertEqual(k.get("a",op.id).contract_digest,original)
        self.assertIn(original,k._policy_snapshots)
        self.assertEqual(k.accounts()["tenant:CNY"].pending,70)
        k.abort_prepared("a",op.id);k.assert_invariants()

    def test_new_policy_gate_is_executed_on_future_prepare(self):
        k,s,_=setup();c=amended(default_contract(),extra=(ceiling_clause(),))
        k.trusted.activate_policy(seal(k,c))
        auth=authorize(k,s)
        k.trusted.register_capability(Capability("new-cap","demo","a",frozenset(["a"]),100,1000,k.epoch))
        auth=(auth[0],"new-cap",auth[2],auth[3])
        with self.assertRaises(PolicyDenied) as cm: prepare(k,auth)
        self.assertEqual(cm.exception.code,"CONTRACT_DENIED")
        failure=[r for r in cm.exception.report["reports"] if not r["passed"]][0]
        self.assertEqual((failure["clause_id"],failure["requirement_ids"]),("refund-ceiling",["R04"]))
        self.assertEqual(k.accounts()["tenant:CNY"].pending,0)

    def test_already_admitted_operation_can_reconcile_under_original_policy(self):
        k,s,_=setup();op=prepare(k,authorize(k,s));op=k.dispatch("a",op.id,op.permit_ref)
        c=amended(default_contract(),extra=(ceiling_clause(),));k.trusted.activate_policy(seal(k,c))
        final=k.trusted.observe(s.send(op))
        self.assertEqual(final.state,Execution.CONFIRMED)
        self.assertEqual(final.contract_digest,op.contract_digest)
        self.assertEqual(k.accounts()["tenant:CNY"].spent,70);k.assert_invariants()

    def test_rollback_version_and_frozen_clause_removal_rejected(self):
        k,_,_=setup();base=default_contract()
        self.assertCode("NONMONOTONIC_POLICY_VERSION",lambda:k.trusted.activate_policy(seal(k,base)))
        replaced=replace(base.clauses[0],id="replacement",frozen=False)
        c=amended(base,clauses=(replaced,))
        self.assertCode("FROZEN_CLAUSE_CHANGED",lambda:k.trusted.activate_policy(seal(k,c)))
        changed=replace(base.assumptions[0],expires=2000)
        c=amended(base,assumptions=(changed,))
        self.assertCode("FROZEN_CLAUSE_CHANGED",lambda:k.trusted.activate_policy(seal(k,c)))

    def test_unsupported_context_cannot_be_published_to_refund_runtime(self):
        k,_,_=setup();p=compile_predicate(field("extra"),{"extra":FieldType("Bool")},"R09")
        clause=Clause("extra","safety","requires","refund",("prepare","dispatch"),("R09",),(),False,p)
        c=amended(default_contract(),extra=(clause,))
        blob=seal(k,c,witness={"amount":1,"currency":"CNY","extra":True})
        self.assertCode("UNSUPPORTED_RUNTIME_CONTEXT",lambda:k.trusted.activate_policy(blob))

    def test_assumption_is_rechecked_at_dispatch_and_expiry(self):
        for health in [Health("simulator","refund",0,1000,"Broken"),Health("simulator","refund",0,20,"Healthy")]:
            k,s,clock=setup();op=prepare(k,authorize(k,s))
            k.trusted.set_assumption_health("remote-idempotency",health);clock.now=30
            self.assertCode("ASSUMPTION_BROKEN",lambda:k.dispatch("a",op.id,op.permit_ref))
            self.assertEqual(k.accounts()["tenant:CNY"].pending,70);k.assert_invariants()

    def test_assumption_failure_blocks_policy_activation_but_not_read_only_state(self):
        k,s,_=setup();op=prepare(k,authorize(k,s));blob=seal(k,amended(default_contract()))
        k.trusted.set_assumption_health("remote-idempotency",Health("simulator","refund",0,1000,"Unknown"))
        self.assertCode("ASSUMPTION_BROKEN",lambda:k.trusted.activate_policy(blob))
        self.assertEqual(k.get("a",op.id),op)

    def test_expired_signature_cannot_be_activated_and_permit_is_clamped(self):
        k,s,clock=setup();c=amended(default_contract());blob=seal(k,c,expires=20)
        clock.now=20;self.assertCode("POLICY_EXPIRED_OR_NOT_YET_VALID",lambda:k.trusted.activate_policy(blob))
        clock.now=0;k.trusted.activate_policy(blob)
        auth=authorize(k,s)
        k.trusted.register_capability(Capability("fresh","demo","a",frozenset(["a"]),100,1000,2))
        op=prepare(k,(auth[0],"fresh",auth[2],auth[3]))
        self.assertEqual(op.expires,20)

    def test_policy_activation_and_admission_share_one_order(self):
        k,s,_=setup();op=prepare(k,authorize(k,s));blob=seal(k,amended(default_contract()));barrier=Barrier(2)
        def dispatch():
            barrier.wait()
            try: return k.dispatch("a",op.id,op.permit_ref).admitted
            except Reject as exc: return exc.code
        def activate(): barrier.wait();return k.trusted.activate_policy(blob)
        with ThreadPoolExecutor(2) as pool:
            first=pool.submit(dispatch);second=pool.submit(activate);result=first.result();second.result()
        kinds=[e.kind for e in k.events()]
        if result is True: self.assertLess(kinds.index("DispatchAdmitted"),kinds.index("PolicyActivated"))
        else: self.assertEqual(result,"STALE_PERMIT");self.assertNotIn("DispatchAdmitted",kinds)
        k.assert_invariants()
