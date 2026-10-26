from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import z3

from avcl.canonical import Reject
from avcl.contract import Assumption, Clause, Health, Tool, build_contract, compile_contract, default_contract
from avcl.contract_smt import verify_contract_smt
from avcl.contract_smt_package import write_contract_smt_evidence
from avcl.smt import _Encoding, EncodingLimit
from avcl.ir import FieldType, compile_predicate
from avcl.policy import PolicyAuthority
from avcl.smt import verify_smt
from tests.test_encoding_ir import const, field, call


def predicate(expr, fields=None):
    return compile_predicate(expr, {"x": FieldType("Int", 0, 10)} if fields is None else fields, "R15")


def clause(name, expr, *, rule="requires", events=("prepare", "dispatch"), assumptions=(), tool="test", fields=None):
    return Clause(name, "completion" if events == ("close",) else "safety", rule, tool, events,
                  ("R15",), assumptions, False, predicate(expr, fields))


def contract(clauses, assumptions=(), tools=None):
    if tools is None: tools = (Tool("test", "sim/1", ("Read",)),)
    return build_contract("joint-policy", 1, tools, (), assumptions, tuple(clauses))


def report(bundle, selector="prepare", prop="joint-allowed-input-exists", tool="test"):
    return next(r for r in bundle["reports"] if r["property_id"] == tool + "/" + selector + "/" + prop)


class ContractSmtTests(unittest.TestCase):
    def test_joint_sat_witness_with_insufficient_work_is_unknown(self):
        c=contract([clause("one",call("Lt",field("x"),const(5)))])
        limited=verify_contract_smt(c,evaluation_limit=0)
        r=report(limited)
        self.assertEqual((r["solver_status"],r["status"]),("Sat","Unknown"))
        self.assertEqual(r["reason"],"evaluation_work_exhausted_during_replay")
        self.assertFalse(r["bounds"]["runtime_budget_encoded"])
        self.assertIn("evaluation-work-sufficient",r["trust_assumptions"])
        compiled=compile_contract(c)
        mutant=replace(compiled,checks=tuple((name,replace(check,instructions=tuple(
            replace(i,op="LE") if i.op=="LT" else i for i in check.instructions))) for name,check in compiled.checks))
        with patch("avcl.contract_smt.compile_contract",return_value=mutant):
            r=report(verify_contract_smt(c,evaluation_limit=0),prop="contract-checks-equivalent")
        self.assertEqual((r["solver_status"],r["status"]),("Sat","Unknown"))

    def test_real_default_contract_has_conditional_joint_witness(self):
        c = default_contract()
        bundle = verify_contract_smt(c)
        self.assertEqual(len(bundle["reports"]), 9)
        self.assertTrue(all(r["status"] == "ProvedUnder" for r in bundle["reports"]), bundle["reports"])
        r = report(bundle, "publication", tool="refund")
        q = next(q for q in bundle["queries"] if q["id"] == r["query_id"])
        self.assertTrue(q["replay"]["compiled_allowed"])
        self.assertEqual(q["witness"]["health"], "hypothetical-declared-Healthy")
        # Necessary witness checks can feed a separate host authority; analysis itself never seals.
        schema = c.input_types("refund", "prepare")
        values = {k: t.value_from_wire(q["witness"]["values"][k]) for k, t in schema.items()}
        at = int(q["witness"]["time"])
        health = {a.id: Health(a.authority, a.scope, a.valid_from, a.expires, "Healthy") for a in c.assumptions}
        authority = PolicyAuthority(c.tools, c.validators)
        envelope = authority.seal(c, health, at, at + 1, c.requirements(), frozenset(c.id for c in c.clauses), {"refund": values})
        self.assertEqual(authority.load(envelope, health, at).compiled.contract.semantic_digest, c.semantic_digest)
        broken = {k: replace(v, status="Broken") for k, v in health.items()}
        with self.assertRaises(Reject): authority.load(envelope, broken, at)

    def test_individually_satisfiable_clauses_can_jointly_conflict(self):
        clauses = [clause("a-low", call("Lt", field("x"), const(3))),
                   clause("b-high", call("Lt", const(5), field("x"))), clause("z-unrelated", const(True))]
        for c in clauses: self.assertEqual(verify_smt(c.predicate)["reports"][-1]["status"], "ProvedUnder")
        bundle = verify_contract_smt(contract(clauses))
        for selector in ("prepare", "dispatch", "publication"):
            r = report(bundle, selector)
            self.assertEqual((r["status"], r["solver_status"]), ("Violated", "Unsat"))
            core = r["conflict"]
            self.assertEqual(core["constraint_ids"], ["clause/a-low", "clause/b-high"])
            self.assertEqual(core["minimality"], "InclusionMinimal")
            self.assertEqual(set(core["necessity_witnesses"]), set(core["constraint_ids"]))
            self.assertFalse(core["minimum_cardinality_claimed"])

    def test_prepare_dispatch_separate_witnesses_do_not_imply_publication(self):
        c = contract([clause("prepare-low", call("Lt", field("x"), const(3)), events=("prepare",)),
                      clause("dispatch-high", call("Lt", const(5), field("x")), events=("dispatch",))])
        bundle = verify_contract_smt(c)
        self.assertEqual(report(bundle, "prepare")["status"], "ProvedUnder")
        self.assertEqual(report(bundle, "dispatch")["status"], "ProvedUnder")
        self.assertEqual(report(bundle, "publication")["status"], "Violated")

    def test_forbid_polarity_and_completion_are_not_admission(self):
        c = contract([clause("requires", call("Lt", const(5), field("x"))),
                      clause("forbid", call("Lt", const(5), field("x")), rule="forbid"),
                      clause("complete", const(True), rule="success", events=("close",))])
        bundle = verify_contract_smt(c)
        self.assertEqual(report(bundle)["status"], "Violated")
        self.assertEqual(report(bundle, "close")["status"], "ProvedUnder")
        c = contract([clause("safe", const(True)), clause("unreachable-completion", const(False), rule="success", events=("close",))])
        bundle = verify_contract_smt(c)
        self.assertEqual(report(bundle, "publication")["status"], "ProvedUnder")
        self.assertEqual(report(bundle, "close")["status"], "Violated")

    def test_assumption_intervals_produce_a_named_minimal_conflict(self):
        assumptions = (Assumption("early", "authority", "scope", 0, 5, "monitor"),
                       Assumption("late", "authority", "scope", 5, 10, "monitor"))
        c = contract([clause("one", const(True), assumptions=("early",)),
                      clause("two", const(True), assumptions=("late",))], assumptions)
        bundle = verify_contract_smt(c)
        core = report(bundle)["conflict"]
        self.assertEqual(core["constraint_ids"], ["assumption/early", "assumption/late"])
        self.assertEqual(core["minimality"], "InclusionMinimal")
        self.assertTrue(all(bundle["queries"][int(v["query_id"][1:])]["solver_status"] == "Sat"
                            for v in core["necessity_witnesses"].values()))

    def test_publication_includes_even_unused_assumption_dependencies(self):
        assumptions = (Assumption("unused-early", "authority", "scope", 0, 5, "monitor"),
                       Assumption("unused-late", "authority", "scope", 5, 10, "monitor"))
        c = contract([clause("safe", const(True))], assumptions)
        bundle = verify_contract_smt(c)
        self.assertEqual(report(bundle, "prepare")["status"], "ProvedUnder")
        self.assertEqual(report(bundle, "publication")["status"], "Violated")

    def test_tools_have_independent_input_contexts(self):
        c = contract([clause("a-low", call("Lt", field("x"), const(3)), tool="a"),
                      clause("b-high", call("Lt", const(5), field("x")), tool="b")],
                     tools=(Tool("a", "1", ("Read",)), Tool("b", "1", ("Read",))))
        bundle = verify_contract_smt(c)
        self.assertTrue(all(r["status"] == "ProvedUnder" for r in bundle["reports"]))

    def test_small_policy_corpus_matches_exhaustive_runtime_nonvacuity(self):
        expressions = [const(False), const(True), field("b"), call("Not", field("b"))]
        assumptions = (Assumption("early", "authority", "scope", 0, 2, "monitor"),
                       Assumption("late", "authority", "scope", 1, 3, "monitor"))
        health = {a.id: Health(a.authority, a.scope, a.valid_from, a.expires, "Healthy") for a in assumptions}
        for first in expressions:
            for second in expressions:
                for rule in ("requires", "forbid"):
                    c = contract([clause("first", first, events=("prepare",), assumptions=("early",), fields={"b": FieldType("Bool")}),
                                  clause("second", second, rule=rule, assumptions=("late",), fields={"b": FieldType("Bool")})], assumptions)
                    compiled = compile_contract(c)
                    bundle = verify_contract_smt(c)
                    for selector, events in (("prepare", ("prepare",)), ("dispatch", ("dispatch",)), ("publication", ("prepare", "dispatch"))):
                        allowed = False
                        for at in range(4):
                            for flag in (False, True):
                                dependency_ok = selector != "publication" or all(a.valid_from <= at < a.expires for a in assumptions)
                                allowed |= dependency_ok and all(compiled.evaluate(event, "test", {"b": flag}, health, at)["allowed"] for event in events)
                        self.assertEqual(report(bundle, selector)["status"], "ProvedUnder" if allowed else "Violated")

    def test_different_event_schemas_block_common_publication_context(self):
        c = contract([clause("prepare", const(True), events=("prepare",), fields={"x": FieldType("Bool")}),
                      clause("dispatch", const(True), events=("dispatch",))])
        bundle = verify_contract_smt(c)
        self.assertEqual(report(bundle, "prepare")["status"], "ProvedUnder")
        r = report(bundle, "publication")
        self.assertEqual(r["status"], "Unknown")
        self.assertIn("CONFLICTING_CONTEXT_TYPE", r["reason"])

    def test_resource_limit_does_not_claim_a_minimal_core(self):
        c = contract([clause("deny", const(False)), clause("unused", const(True))])
        bundle = verify_contract_smt(c, max_solver_calls=3)
        r = report(bundle)
        self.assertEqual(r["status"], "Violated")
        self.assertEqual(r["conflict"]["minimality"], "Unconfirmed")
        self.assertEqual(bundle["solver_calls"], "3")
        self.assertEqual(report(bundle, "dispatch")["status"], "Unknown")
        limited = verify_contract_smt(c, max_encoding_steps=1)
        self.assertTrue(all(r["status"] == "Unknown" for r in limited["reports"]))

    def test_deadline_and_unexecuted_queries_are_explicit(self):
        c = contract([clause("allow", const(True))])
        with patch("avcl.contract_smt.monotonic", side_effect=[0.0] + [10.0] * 100):
            bundle = verify_contract_smt(c, total_timeout_ms=1)
        self.assertEqual(bundle["solver_calls"], "0")
        self.assertTrue(all(r["status"] == "Unknown" and r["reason"] == "overall_time_budget_exhausted" for r in bundle["reports"]))
        with tempfile.TemporaryDirectory() as tmp:
            bundle = write_contract_smt_evidence(Path(tmp) / "budget", c, max_solver_calls=2)
            q = bundle["queries"][-1]
            self.assertFalse(q["executed"])
            self.assertIsNone(q["query_sha256"])
            self.assertNotIn("smt2", q)
            self.assertEqual(report(bundle)["status"], "Unknown")

    def test_failed_deletion_model_keeps_unsat_without_claiming_minimality(self):
        c = contract([clause("deny", const(False))])
        original = _Encoding.decode
        calls = [0]
        def decode(enc, model, t, value, budget):
            calls[0] += 1
            if calls[0] == 2: raise EncodingLimit("injected deletion decode limit")
            return original(enc, model, t, value, budget)
        with patch("avcl.contract_smt._Encoding.decode", new=decode):
            bundle = verify_contract_smt(c)
        r = report(bundle)
        self.assertEqual(r["status"], "Violated")
        self.assertEqual(r["conflict"]["minimality"], "Unconfirmed")
        self.assertEqual(len(bundle["reports"]), 9)

    def test_unknown_unsat_domain_and_errors_do_not_create_proofs(self):
        c = contract([clause("allow", const(True))])
        with patch("avcl.contract_smt._check", return_value=("Unsat", None)):
            result = verify_contract_smt(c)
        self.assertEqual(report(result, prop="input-domain-inhabited")["status"], "AssumptionBroken")
        self.assertEqual(report(result)["status"], "Unknown")
        for status in ("Unknown", "Timeout"):
            with patch("avcl.contract_smt._check", return_value=(status, "injected")):
                self.assertTrue(all(r["status"] == "Unknown" for r in verify_contract_smt(c)["reports"]))
        with patch("avcl.contract_smt._check", side_effect=z3.Z3Exception("injected")):
            self.assertTrue(all(r["status"] == "Unknown" for r in verify_contract_smt(c)["reports"]))
        with patch("z3.Solver", side_effect=z3.Z3Exception("injected construction failure")):
            bundle = verify_contract_smt(c)
        self.assertTrue(all(r["status"] == "Unknown" and r["solver_status"] == "Error" for r in bundle["reports"]))

    def test_compiler_disagreement_replays_in_real_contract_evaluator(self):
        c = contract([clause("less", call("Lt", field("x"), const(3)))])
        compiled = compile_contract(c)
        mutated = replace(compiled, checks=tuple((name, replace(check, instructions=tuple(
            replace(i, op="LE") if i.op == "LT" else i for i in check.instructions))) for name, check in compiled.checks))
        with patch("avcl.contract_smt.compile_contract", return_value=mutated):
            bundle = verify_contract_smt(c)
        r = report(bundle, prop="contract-checks-equivalent")
        self.assertEqual((r["status"], r["solver_status"]), ("Violated", "Sat"))
        q = bundle["queries"][int(r["query_id"][1:])]
        self.assertNotEqual(q["replay"]["ast_allowed"], q["replay"]["compiled_allowed"])

    def test_invalid_contract_and_options_fail_closed(self):
        c = contract([clause("allow", const(True))])
        for limits in ({"max_solver_calls": 0}, {"total_timeout_ms": True}, {"max_encoding_steps": -1}):
            with self.assertRaises(Reject): verify_contract_smt(c, **limits)
        with self.assertRaises(Reject): verify_contract_smt(replace(c, clauses=()))
        with patch("avcl.contract_smt.version", return_value="future"):
            self.assertTrue(all(r["status"] == "Unknown" for r in verify_contract_smt(c)["reports"]))

    def test_frozen_queries_and_conflict_can_be_rechecked(self):
        c = contract([clause("deny", const(False)), clause("unrelated", const(True))])
        before = c.to_json()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "evidence"
            bundle = write_contract_smt_evidence(root, c)
            manifest = json.loads((root / "manifest.json").read_text())
            for name, expected in manifest["files"].items(): self.assertEqual(sha256((root / name).read_bytes()).hexdigest(), expected)
            for q in bundle["queries"]:
                solver = z3.Solver()
                raw = (root / (q["id"] + ".smt2")).read_bytes()
                self.assertEqual(sha256(raw).hexdigest(), q["query_sha256"])
                solver.from_string(raw.decode())
                self.assertEqual(str(solver.check()).lower(), q["solver_status"].lower())
            with self.assertRaises(FileExistsError): write_contract_smt_evidence(root, c)
            source = Path(tmp) / "contract.json"; source.write_bytes(c.to_json().encode())
            script = Path(__file__).resolve().parents[1] / "scripts" / "verify_smt.py"
            result = subprocess.run([sys.executable, "-X", "utf8", str(script), "--contract", str(source), str(Path(tmp) / "cli")],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(c.to_json(), before)


if __name__ == "__main__": unittest.main()
