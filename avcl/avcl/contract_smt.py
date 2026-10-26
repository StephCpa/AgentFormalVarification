"""Offline joint contract VCs and irreducible conflict diagnostics.

No signing, activation, health update or policy relaxation occurs here. A
deletion witness is for a hypothetical subset, never an authorization.
"""
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version, PackageNotFoundError
import platform
from time import monotonic
from types import SimpleNamespace

from .canonical import Reject, digest
from .checks import COMPILER
from .contract import Health, assumptions_valid, compile_contract
from .evaluation import WorkBudget, DEFAULT_WORK_LIMIT, WORK_MODEL
from .smt import _Encoding, _check, EncodingLimit, BUILD as ENCODER, Z3_PACKAGE, Z3_CORE

BUILD = "avcl/contract-smt/0.1"
PROPERTIES = ("input-domain-inhabited", "contract-checks-equivalent", "joint-allowed-input-exists")
SPEC = {"encoder": ENCODER, "contract": "avcl/contract/0.3",
        "scope": "ideal pure contract semantics on one tool/input/time, conditional on sufficient evaluation work; runtime budget not encoded",
        "publication": "same input for prepare and dispatch; all declared assumption intervals",
        "conflict": "inclusion-minimal relative to the fixed full input domain; not minimum cardinality"}


class _Queries:
    def __init__(self, z3, bundle, timeout_ms, rlimit, max_calls, total_ms):
        self.z = z3
        self.bundle = bundle
        self.timeout_ms, self.rlimit, self.max_calls = timeout_ms, rlimit, max_calls
        self.deadline = monotonic() + total_ms / 1000
        self.calls = 0

    def unavailable(self):
        if self.calls >= self.max_calls: return "solver_call_budget_exhausted"
        if monotonic() >= self.deadline: return "overall_time_budget_exhausted"
        return None

    def run(self, enc, formula, label):
        z = self.z
        index = len(self.bundle["queries"])
        item = {"id": "q" + str(index), "label": label, "query_sha256": None,
                "executed": False, "solver_status": "Unknown", "reason": self.unavailable()}
        self.bundle["queries"].append(item)
        if item["reason"]: return item, None
        try:
            timeout = max(1, min(self.timeout_ms, int((self.deadline - monotonic()) * 1000)))
            solver = z.Solver(ctx=enc.ctx)
            solver.set(timeout=timeout, rlimit=self.rlimit, random_seed=0)
            solver.add(formula)
            raw = (f"(set-option :timeout {timeout})\n(set-option :rlimit {self.rlimit})\n"
                   "(set-option :smt.random_seed 0)\n" + solver.to_smt2())
            item.update(smt2=raw, query_sha256=sha256(raw.encode()).hexdigest())
            self.calls += 1
            item["executed"] = True
            status, reason = _check(z, solver)
            item.update(solver_status=status, reason=reason, statistics=str(solver.statistics()))
            model = solver.model() if status == "Sat" else None
            if model is not None: item["model"] = model.sexpr()
            return item, model
        except z.Z3Exception as exc:
            item.update(solver_status="Error", reason=str(exc))
            return item, None


def _groups(contract):
    for tool in contract.tools:
        for selector, events in (("prepare", ("prepare",)), ("dispatch", ("dispatch",)),
                                 ("publication", ("prepare", "dispatch")), ("close", ("close",))):
            clauses = tuple(c for c in contract.clauses if c.tool == tool.id and set(events) & set(c.events))
            if clauses: yield tool.id, selector, events, clauses


def verify_contract_smt(contract, *, timeout_ms=5000, rlimit=1000000,
                        max_encoding_steps=100000, max_solver_calls=512, total_timeout_ms=30000,evaluation_limit=DEFAULT_WORK_LIMIT):
    """Revalidate the whole contract and check each event/publication selector."""
    for value, maximum in ((timeout_ms, 60000), (rlimit, 100000000), (max_encoding_steps, 1000000),
                            (max_solver_calls, 4096), (total_timeout_ms, 300000)):
        if type(value) is not int or not 1 <= value <= maximum: raise Reject("INVALID_SMT_LIMIT")
    WorkBudget(evaluation_limit)
    compiled = compile_contract(contract)
    contract = compiled.contract
    checks = dict(compiled.checks)
    base = {"contract_digest": contract.semantic_digest, "source_digest": contract.source_digest,
            "spec_digest": digest(SPEC, BUILD + "/spec"), "checker_build": COMPILER,
            "encoder_build": ENCODER, "contract_checker_build": BUILD,
            "tool_versions": {"python": platform.python_version(), "z3_package": None, "z3_core": None},
            "bounds": {"timeout_ms": str(timeout_ms), "rlimit": str(rlimit),
                       "max_encoding_steps": str(max_encoding_steps), "max_solver_calls": str(max_solver_calls),
                       "total_timeout_ms": str(total_timeout_ms), "max_witness_nodes": "10000",
                       "evaluation_work_model":WORK_MODEL,"evaluation_work_limit":str(evaluation_limit),"runtime_budget_encoded":False},
            "checked_at": datetime.now(timezone.utc).isoformat(), "runtime_obligations": ["enforce-evaluation-work-budget"],
            "independent_certificate_checked": False,
            "trust_assumptions": ["pure-ir-typing", "smt-value-encoding", "smt-ast-encoding", "smt-vm-encoding",
                                  "python-runtime", "z3-" + Z3_PACKAGE, "hypothetical-healthy-observations","evaluation-work-sufficient"],
            "scope": SPEC["scope"]}
    bundle = {"schema": BUILD, "contract_digest": contract.semantic_digest, "reports": [], "queries": [], "groups": []}
    dependency_error = None
    try:
        import z3
        base["tool_versions"].update(z3_package=version("z3-solver"), z3_core=z3.get_version_string())
        if version("z3-solver") != Z3_PACKAGE or z3.get_version_string() != Z3_CORE:
            dependency_error = "solver_version_mismatch"
    except (ImportError, PackageNotFoundError, OSError) as exc:
        dependency_error = str(exc)
    queries = None if dependency_error else _Queries(z3, bundle, timeout_ms, rlimit, max_solver_calls, total_timeout_ms)
    used_steps = 0
    health = {a.id: Health(a.authority, a.scope, a.valid_from, a.expires, "Healthy") for a in contract.assumptions}
    assumptions = {a.id: a for a in contract.assumptions}

    for tool, selector, events, clauses in _groups(contract):
        group_id = tool + "/" + selector
        assumed = sorted(assumptions if selector == "publication" else {a for c in clauses for a in c.assumptions})
        meta = {**base, "tool": tool, "selector": selector, "events": list(events),
                "observation_time": {"lower": "0", "upper_exclusive": str(10**18 - 1 if selector == "publication" else 10**18)},
                "requirement_ids": sorted({r for c in clauses for r in c.requirement_ids}),
                "assumption_ids": assumed, "clause_ids": [c.id for c in clauses],
                "ir_digests": {c.id: c.predicate.semantic_digest for c in clauses},
                "checks_sha256": {c.id: sha256(checks[c.id].to_json().encode()).hexdigest() for c in clauses}}
        group = {"id": group_id, "constraint_catalog": {}}
        bundle["groups"].append(group)

        def report(prop, status="Unknown", query=None, reason=None, **extra):
            item = {**meta, "property_id": group_id + "/" + prop, "status": status,
                    "solver_status": query["solver_status"] if query else None,
                    "query_id": query["id"] if query else None, "witness_ref": None,
                    "reason": reason if reason is not None else (query.get("reason") if query else None), **extra}
            if query:
                item["query_sha256"] = query["query_sha256"]
                if "witness" in query: item["witness_ref"] = "#/queries/" + query["id"][1:] + "/witness"
            bundle["reports"].append(item)
            return item

        problem = dependency_error or (queries.unavailable() if queries else None)
        if not problem and used_steps >= max_encoding_steps: problem = "encoding_step_budget_exhausted"
        if problem:
            for prop in PROPERTIES: report(prop, reason=problem)
            continue
        enc = None
        try:
            schema = {}
            for c in clauses:
                for name, t in c.predicate.fields:
                    if name in schema and schema[name] != t: raise Reject("CONFLICTING_CONTEXT_TYPE")
                    schema[name] = t
            meta["input_fields"] = {k: t.wire() for k, t in sorted(schema.items())}
            enc = _Encoding(z3, SimpleNamespace(fields=tuple(sorted(schema.items()))), max_encoding_steps - used_steps)
            now = z3.Int("observation_time", enc.ctx)
            domain = z3.And(now >= 0, now < (10**18 - 1 if selector == "publication" else 10**18),
                            enc.all(enc.domain(t, value) for t, value in enc.fields.values()))
            constraints = {}
            for name in assumed:
                a = assumptions[name]; key = "assumption/" + name
                constraints[key] = z3.And(now >= a.valid_from, now < a.expires)
                group["constraint_catalog"][key] = {"kind": "assumption-validity", "assumption_id": name,
                    "valid_from": str(a.valid_from), "expires": str(a.expires), "authority": a.authority,
                    "scope": a.scope, "monitor": a.monitor,
                    "dependent_clause_ids": [c.id for c in clauses if name in c.assumptions]}
            ast_values, vm_values, ast_definitions, vm_definitions = [], [], [], []
            for c in clauses:
                _, a, da = enc.ast(c.predicate.root)
                v, dv = enc.vm(checks[c.id])
                if c.rule == "forbid": a, v = z3.Not(a), z3.Not(v)
                key = "clause/" + c.id
                constraints[key] = z3.And(da, a)
                group["constraint_catalog"][key] = {"kind": "clause", "clause_id": c.id, "rule": c.rule,
                    "requirement_ids": list(c.requirement_ids), "semantic_digest": c.semantic_digest,
                    "source_map": dict(c.predicate.source_map)}
                active = enc.all(constraints["assumption/" + name] for name in c.assumptions)
                ast_values.append(z3.And(active, a)); vm_values.append(z3.And(active, v))
                ast_definitions.append(z3.Implies(active, da)); vm_definitions.append(z3.Implies(active, dv))
            extra_health = enc.all(constraints["assumption/" + name] for name in assumed)
            ast_value = z3.And(extra_health, enc.all(ast_values))
            vm_value = z3.And(extra_health, enc.all(vm_values))
            equivalent = z3.And(enc.all(ast_definitions), enc.all(vm_definitions), ast_value == vm_value)
        except (Reject, EncodingLimit, z3.Z3Exception, ValueError, TypeError, KeyError, IndexError, RecursionError) as exc:
            for prop in PROPERTIES: report(prop, reason="encoding_failed:" + str(exc))
            if enc: used_steps += enc.steps
            continue
        used_steps += enc.steps

        def witness(query, model):
            budget = [10000]
            values = {k: t.normalize(enc.decode(model, t, value, budget)) for k, (t, value) in enc.fields.items()}
            at = model.eval(now, model_completion=True).as_long()
            if not 0 <= at < 10**18: raise EncodingLimit("invalid_model_time")
            query["witness"] = {"values": {k: t.value_wire(values[k]) for k, t in schema.items()},
                                "time": str(at), "health": "hypothetical-declared-Healthy"}
            return values, at

        def replay_subset(ids, values, at):
            rows = {}
            ast_budget=WorkBudget(evaluation_limit);vm_budget=WorkBudget(evaluation_limit)
            for key in ids:
                if key.startswith("assumption/"):
                    name = key.split("/", 1)[1]
                    rows[key] = assumptions_valid(contract, (name,), health, at)
                else:
                    c = next(c for c in clauses if c.id == key.split("/", 1)[1])
                    env = {k: values[k] for k, _ in c.predicate.fields}
                    a, v = c.predicate.evaluate(env,budget=ast_budget), checks[c.id].evaluate(env,budget=vm_budget)
                    rows[key] = (not a and not v) if c.rule == "forbid" else (a and v)
            return rows

        def runtime_replay(values, at):
            rows = []
            errors = []
            for event in events:
                ast_allowed = True
                ast_budget=WorkBudget(evaluation_limit)
                for c in clauses:
                    if event not in c.events: continue
                    try:
                        ast_budget.charge(1+len(c.predicate.fields)+len(c.assumptions))
                        if not assumptions_valid(contract, c.assumptions, health, at):
                            ast_allowed = False; continue
                        value = c.predicate.evaluate({k: values[k] for k, _ in c.predicate.fields},budget=ast_budget)
                        ast_allowed = ast_allowed and (not value if c.rule == "forbid" else value)
                    except (Reject, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                        errors.append(str(exc)); ast_allowed = False
                actual = compiled.evaluate(event, tool, values, health, at,budget=WorkBudget(evaluation_limit))
                rows.append({"event": event, "ast_allowed": ast_allowed, "compiled": actual})
            valid = assumptions_valid(contract, tuple(assumed), health, at)
            return {"events": rows, "declared_assumptions_valid": valid, "ast_errors": errors,
                    "evaluation_work_exhausted": "UNKNOWN_EVALUATION" in errors or any(
                        r["reason"]=="UNKNOWN_EVALUATION" for row in rows for r in row["compiled"]["reports"]),
                    "ast_allowed": valid and all(r["ast_allowed"] for r in rows),
                    "compiled_allowed": valid and all(r["compiled"]["allowed"] for r in rows)}

        domain_ok = False
        formulas = (domain, z3.And(domain, z3.Not(equivalent)), z3.And(domain, enc.all(constraints.values())))
        for prop, formula in zip(PROPERTIES, formulas):
            q, model = queries.run(enc, formula, group_id + "/" + prop)
            result = None
            try:
                status, reason = "Unknown", None
                if q["solver_status"] == "Sat":
                    values, at = witness(q, model)
                    if prop == "input-domain-inhabited": domain_ok = True; status = "ProvedUnder"
                    else:
                        replay = runtime_replay(values, at); q["replay"] = replay
                        if replay["evaluation_work_exhausted"]:
                            reason="evaluation_work_exhausted_during_replay"
                        elif prop == "contract-checks-equivalent":
                            if replay["ast_errors"] or replay["ast_allowed"] != replay["compiled_allowed"]:
                                status = "Violated"
                            else: reason = "countermodel_did_not_replay"
                        elif replay["ast_allowed"] and replay["compiled_allowed"]:
                            status = "ProvedUnder"
                        else: reason = "joint_witness_did_not_replay"
                elif q["solver_status"] == "Unsat":
                    if prop == "input-domain-inhabited": status = "AssumptionBroken"
                    elif prop == "contract-checks-equivalent" and domain_ok: status = "ProvedUnder"
                    elif prop == "joint-allowed-input-exists" and domain_ok: status = "Violated"
                if prop != "input-domain-inhabited" and not domain_ok:
                    status, reason = "Unknown", "input_domain_not_confirmed"
                result = report(prop, status, q, reason)
                if prop == "joint-allowed-input-exists" and status == "Violated":
                    core = list(constraints)
                    retained_witnesses = {}
                    unsat_query = q["id"]
                    attempts = []
                    for candidate in list(core):
                        if queries.unavailable(): break
                        remaining = [key for key in core if key != candidate]
                        test, test_model = queries.run(enc, z3.And(domain, enc.all(constraints[k] for k in remaining)),
                                                       group_id + "/remove/" + candidate)
                        test["constraint_ids"] = remaining
                        attempts.append(test["id"])
                        if test["solver_status"] == "Unsat":
                            core = remaining; unsat_query = test["id"]
                        elif test["solver_status"] == "Sat":
                            try:
                                vals, stamp = witness(test, test_model)
                                test["subset_replay"] = replay_subset(remaining, vals, stamp)
                                if all(test["subset_replay"].values()):
                                    retained_witnesses[candidate] = (test["id"], vals, stamp)
                            except (EncodingLimit, Reject, z3.Z3Exception, ValueError, TypeError, KeyError, IndexError) as exc:
                                test["replay_error"] = str(exc)
                    necessity = {}
                    for key in core:
                        if key not in retained_witnesses: continue
                        query_id, vals, stamp = retained_witnesses[key]
                        remaining = [k for k in core if k != key]
                        rows = replay_subset(remaining, vals, stamp)
                        if all(rows.values()): necessity[key] = {"query_id": query_id, "final_subset_replay": rows}
                    result["conflict"] = {"constraint_ids": core, "unsat_query_id": unsat_query,
                        "minimality": "InclusionMinimal" if set(necessity) == set(core) else "Unconfirmed",
                        "minimum_cardinality_claimed": False, "necessity_witnesses": necessity,
                        "deletion_query_ids": attempts, "fixed_background": "full declared input domain and legal time",
                        "budget_reason": queries.unavailable()}
            except (EncodingLimit, Reject, z3.Z3Exception, ValueError, TypeError, KeyError, IndexError, RecursionError) as exc:
                if result is None:
                    report(prop, query=q, reason="query_or_replay_failed:" + str(exc))
                else:
                    result["conflict"] = {"constraint_ids": list(constraints), "unsat_query_id": q["id"],
                        "minimality": "Unconfirmed", "minimum_cardinality_claimed": False,
                        "reason": "conflict_diagnostic_failed:" + str(exc)}
    bundle["solver_calls"] = str(queries.calls if queries else 0)
    bundle["encoding_steps"] = str(used_steps)
    return bundle
