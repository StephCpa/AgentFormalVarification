"""Optional real-Z3 tests: explicit --smt runs fail if Z3 is unavailable."""
from dataclasses import replace
from hashlib import sha256
from itertools import product
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import z3

from avcl.canonical import Reject
from avcl.checks import compile_checks, load_checks
from avcl.ir import FieldType, Record, Some, Tagged, compile_predicate, refund_predicate
from avcl.smt import _Encoding, _check, _verify_checks, verify_smt, Z3_CORE
from avcl.smt_package import write_smt_evidence
from avcl.verification import domain, verify_finite
from tests.test_encoding_ir import const, field, call
from tests.test_ir_pipeline import get, typed


class SmtTests(unittest.TestCase):
    def test_work_exhaustion_during_sat_replay_is_unknown_not_a_counterexample(self):
        p=compile_predicate(call("Lt",field("x"),const(5)),{"x":FieldType("Int",0,10)})
        checks=compile_checks(p)
        mutant=replace(checks,instructions=tuple(replace(i,op="LE") if i.op=="LT" else i for i in checks.instructions))
        limited=_verify_checks(mutant,evaluation_limit=0)["reports"][1]
        self.assertEqual((limited["solver_status"],limited["status"]),("Sat","Unknown"))
        self.assertEqual(limited["reason"],"evaluation_work_exhausted_during_replay")
        reports=verify_smt(p,evaluation_limit=0)["reports"]
        self.assertEqual((reports[2]["solver_status"],reports[2]["status"]),("Sat","Unknown"))
        self.assertFalse(reports[1]["bounds"]["runtime_budget_encoded"])
        self.assertIn("evaluation-work-sufficient",reports[1]["assumption_ids"])
        self.assertIn("enforce-evaluation-work-budget",reports[1]["runtime_obligations"])

    def verified(self, predicate):
        bundle = verify_smt(predicate)
        self.assertEqual([r["status"] for r in bundle["reports"]], ["ProvedUnder"] * 3,
                         [(r["property_id"], r["status"], r.get("reason")) for r in bundle["reports"]])
        self.assertEqual([r["solver_status"] for r in bundle["reports"]], ["Sat", "Unsat", "Sat"])
        return bundle

    def test_large_real_refund_domain_and_witness(self):
        p = refund_predicate()
        self.assertEqual(verify_finite(p)[0]["status"], "Unknown")
        bundle = self.verified(p)
        witness = bundle["reports"][2]["witness"]
        values = {k: t.value_from_wire(witness[k]) for k, t in p.fields}
        self.assertTrue(p.evaluate(values))
        self.assertTrue(compile_checks(p).evaluate(values))
        self.assertEqual(bundle["reports"][0]["tool_versions"]["z3_core"], Z3_CORE)

    def test_all_deny_is_not_vacuous_success(self):
        p = compile_predicate(const(False), {"b": FieldType("Bool")})
        reports = verify_smt(p)["reports"]
        self.assertEqual([r["status"] for r in reports], ["ProvedUnder", "ProvedUnder", "Violated"])
        self.assertEqual(reports[-1]["solver_status"], "Unsat")

    def test_option_short_circuit_and_nested_guards(self):
        for tag in ("And", "Or", "If"):
            present = call("IsSome", field("x"))
            positive = call("Lt", const(0), call("Unwrap", field("x")))
            expr = call("If", present, positive, const(False)) if tag == "If" else (
                call(tag, present if tag == "And" else call("Not", present), positive))
            self.verified(compile_predicate(expr, {"x": FieldType("Option", element=FieldType("Int", 0, 3))}))
        inner = call("Unwrap", field("x"))
        expr = call("And", call("IsSome", field("x")), call("And", call("IsSome", inner),
                    call("Eq", call("Unwrap", inner), const(2))))
        self.verified(compile_predicate(expr, {"x": FieldType("Option", element=FieldType("Option", element=FieldType("Int", 0, 3)))}))

    def test_structured_domain_matches_enumeration(self):
        from examples.ir_pipeline import bounded_refund
        p = bounded_refund()
        self.assertEqual(verify_finite(p)[0]["checked_cases"], "120")
        self.verified(p)

    def test_unicode_above_native_string_alphabet_and_nominal_id(self):
        for t, value in ((FieldType("String"), "\U0010ffff\U00030000\0"),
                         (FieldType("Id", name="OrderId"), Tagged("OrderId", "\U0010ffff"))):
            p = compile_predicate(call("Eq", field("x"), typed(t, value)), {"x": t})
            bundle = self.verified(p)
            self.assertEqual(bundle["reports"][2]["witness"]["x"], t.value_wire(value))

    def test_input_domains_reject_surrogates_empty_id_and_overlong_strings(self):
        for t, invalid in ((FieldType("String"), "\ud800"), (FieldType("String"), "a" * 257),
                           (FieldType("Id", name="Id"), Tagged("Id", ""))):
            p = compile_predicate(const(True), {"x": t})
            enc = _Encoding(z3, p, 50000)
            _, x = enc.fields["x"]
            solver = z3.Solver(ctx=enc.ctx)
            solver.add(enc.domain(t, x), x == enc.literal(t, invalid))
            self.assertEqual(solver.check(), z3.unsat)

    def test_set_equality_is_extensional_including_nested_records(self):
        element = FieldType("Record", fields=(("s", FieldType("Set", element=FieldType("Int", 0, 2), max_size=2)),))
        t = FieldType("Set", element=element, max_size=2)
        value = frozenset([Record((("s", frozenset([1, 2])),))])
        p = compile_predicate(call("Eq", field("x"), typed(t, value)), {"x": t})
        self.verified(p)
        enc = _Encoding(z3, p, 50000)
        base = enc.literal(element, next(iter(value)))
        duplicate = z3.Concat(z3.Unit(base), z3.Unit(base))
        solver = z3.Solver(ctx=enc.ctx)
        solver.add(z3.Not(enc.equal(t, duplicate, enc.literal(t, value))))
        self.assertEqual(solver.check(), z3.unsat)

    def test_integer_arithmetic_and_if_join_do_not_wrap_or_narrow(self):
        bound = 10**18 - 1
        x = field("x")
        doubled = call("MulConst", const(2), x)
        computed = call("If", field("b"), call("Add", x, x), call("Sub", doubled, const(0)))
        p = compile_predicate(call("Eq", computed, doubled), {"x": FieldType("Int", -bound, bound), "b": FieldType("Bool")})
        self.verified(p)

    def test_set_refinements_and_conditional_capacity_joins(self):
        small = FieldType("Set", element=FieldType("Int", 0, 2), max_size=1)
        large = FieldType("Set", element=FieldType("Int", 0, 2), max_size=2)
        chosen = call("If", field("b"), field("small"), field("large"))
        p = compile_predicate(call("Eq", chosen, field("large")),
                              {"b": FieldType("Bool"), "small": small, "large": large})
        self.verified(p)
        checks = compile_checks(p); enc = _Encoding(z3, p, 50000)
        _, ast, da = enc.ast(p.root); vm, dv = enc.vm(checks)
        for flag in (False, True):
            env = {"b": flag, "small": frozenset([1]), "large": frozenset([1, 2])}
            solver = z3.Solver(ctx=enc.ctx)
            for k, t in p.fields: solver.add(enc.fields[k][1] == enc.literal(t, env[k]))
            solver.add(z3.Or(z3.Not(da), z3.Not(dv), ast != p.evaluate(env), vm != checks.evaluate(env)))
            self.assertEqual(solver.check(), z3.unsat)

    def test_nested_option_record_set_membership(self):
        inner = FieldType("Set", element=FieldType("Bool"), max_size=2)
        record = FieldType("Record", fields=(("value", FieldType("Option", element=inner)),))
        collection = FieldType("Set", element=record, max_size=2)
        value = Record((("value", Some(frozenset([True, False]))),))
        p = compile_predicate(call("InSet", typed(record, value), field("rows")), {"rows": collection})
        self.verified(p)

    def test_translation_matches_concrete_small_domain_each_input(self):
        # Independent enumeration checks truth and definedness of BOTH translations.
        option = FieldType("Option", element=FieldType("Int", -1, 1))
        schema = {"x": option, "s": FieldType("Set", element=FieldType("Int", -1, 1), max_size=2)}
        p = compile_predicate(call("If", call("IsSome", field("x")),
                              call("InSet", call("Unwrap", field("x")), field("s")), const(False)), schema)
        checks = compile_checks(p); enc = _Encoding(z3, p, 50000)
        _, ast, da = enc.ast(p.root); vm, dv = enc.vm(checks)
        for values in product(*(domain(t) for _, t in p.fields)):
            env = dict(zip((k for k, _ in p.fields), values))
            solver = z3.Solver(ctx=enc.ctx)
            for (k, t), value in zip(p.fields, values): solver.add(enc.fields[k][1] == enc.literal(t, value))
            solver.add(z3.Or(z3.Not(da), z3.Not(dv), ast != p.evaluate(env), vm != checks.evaluate(env)))
            self.assertEqual(solver.check(), z3.unsat, env)

    def test_bad_compiler_mutants_have_replayed_counterexamples(self):
        samples = [
            (compile_predicate(call("Lt", field("x"), const(1)), {"x": FieldType("Int", 0, 2)}), "LT", "LE"),
            (compile_predicate(call("Eq", call("Sub", field("x"), const(1)), const(0)), {"x": FieldType("Int", 0, 2)}), "SUB", "ADD"),
            (compile_predicate(call("And", call("IsSome", field("x")), call("Eq", call("Unwrap", field("x")), const(1))),
                               {"x": FieldType("Option", element=FieldType("Int", 0, 2))}), "JF_KEEP", "JT_KEEP")]
        for p, old, new in samples:
            checks = compile_checks(p)
            code = tuple(replace(i, op=new) if i.op == old else i for i in checks.instructions)
            mutant = replace(checks, instructions=code)
            r = _verify_checks(mutant)["reports"][1]
            self.assertEqual((r["status"], r["solver_status"]), ("Violated", "Sat"), r)
            self.assertIn("replay", r)
            with self.assertRaises(Reject): load_checks(mutant.to_json(), p)

    def test_limit_unknown_and_solver_timeout_error_are_not_proofs(self):
        p = refund_predicate()
        self.assertTrue(all(r["status"] == "Unknown" for r in verify_smt(p, max_encoding_steps=1)["reports"]))
        self.assertTrue(any(r["status"] == "Unknown" for r in verify_smt(p, rlimit=1)["reports"]))
        for status in ("Timeout", "Unknown"):
            with patch("avcl.smt._check", return_value=(status, "injected solver outcome")):
                result = verify_smt(p)
            self.assertTrue(all(r["status"] == "Unknown" and r["solver_status"] == status for r in result["reports"]))
        with patch("avcl.smt._check", side_effect=z3.Z3Exception("injected failure")):
            self.assertTrue(all(r["status"] == "Unknown" and r["solver_status"] == "Error" for r in verify_smt(p)["reports"]))

    def test_model_decode_failure_is_not_promoted_from_sat(self):
        with patch("avcl.smt._Encoding.decode", side_effect=ValueError("injected decode failure")):
            reports = verify_smt(refund_predicate())["reports"]
        self.assertTrue(all(r["status"] == "Unknown" for r in reports))
        self.assertEqual(reports[0]["solver_status"], "Sat")
        self.assertEqual(reports[2]["solver_status"], "Sat")

    def test_timeout_classification_preserves_unknown_reason(self):
        class UnknownSolver:
            def check(self): return z3.unknown
            def reason_unknown(self): return "timeout"
        self.assertEqual(_check(z3, UnknownSolver()), ("Timeout", "timeout"))

    def test_wrong_forward_jump_and_partial_final_stack_find_errors(self):
        p = compile_predicate(call("If", field("b"), const(True), const(False)), {"b": FieldType("Bool")})
        checks = compile_checks(p)
        code = tuple(replace(i, arg=i.arg + 1) if i.op == "JF_POP" else i for i in checks.instructions)
        r = _verify_checks(replace(checks, instructions=code))["reports"][1]
        # A malformed stack is an encoding failure, never an equivalence proof.
        self.assertEqual(r["status"], "Unknown")
        self.assertIn("invalid_vm_result", r["reason"])
        code = tuple(replace(i, arg=0) if i.op == "JUMP" else i for i in checks.instructions)
        self.assertTrue(all(r["status"] == "Unknown" for r in _verify_checks(replace(checks, instructions=code))["reports"]))

    def test_unconfirmed_domain_blocks_vacuous_equivalence(self):
        p = compile_predicate(const(True), {"b": FieldType("Bool")})
        with patch("avcl.smt._check", return_value=("Unsat", None)):
            reports = verify_smt(p)["reports"]
        self.assertEqual([r["status"] for r in reports], ["AssumptionBroken", "Unknown", "Unknown"])

    def test_invalid_bounds_version_and_forged_ir_fail_closed(self):
        p = refund_predicate()
        for limits in ({"timeout_ms": 0}, {"rlimit": True}, {"max_encoding_steps": 1000001}):
            with self.assertRaises(Reject): verify_smt(p, **limits)
        with patch("avcl.smt.version", return_value="future"):
            self.assertTrue(all(r["status"] == "Unknown" for r in verify_smt(p)["reports"]))
        with self.assertRaises(Reject): verify_smt(replace(p, root=replace(p.root, tag="ToolCall")))

    def test_query_export_hashes_and_independent_query_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "evidence"
            bundle = write_smt_evidence(root, refund_predicate())
            manifest = json.loads((root / "manifest.json").read_text())
            for name, expected in manifest["files"].items(): self.assertEqual(sha256((root / name).read_bytes()).hexdigest(), expected)
            for q in bundle["queries"]:
                raw = (root / (q["property_id"] + ".smt2")).read_bytes()
                self.assertEqual(sha256(raw).hexdigest(), q["query_sha256"])
                solver = z3.Solver()
                solver.from_string(raw.decode())
                self.assertEqual(str(solver.check()).lower(), q["solver_status"].lower())
            with self.assertRaises(FileExistsError): write_smt_evidence(root, refund_predicate())

    def test_cli_separates_proved_violated_and_unknown_exit_codes(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "verify_smt.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, p, flags, expected in [
                ("proved", refund_predicate(), [], 0),
                ("violated", compile_predicate(const(False), {"b": FieldType("Bool")}), [], 1),
                ("unknown", refund_predicate(), ["--max-encoding-steps", "1"], 2),
            ]:
                source = root / (name + ".json"); source.write_bytes(p.to_json().encode())
                result = subprocess.run([sys.executable, "-X", "utf8", str(script), str(source), str(root / name), *flags],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertTrue((root / name / "manifest.json").is_file())
            for name, raw in (("invalid-utf8", b"\xff"), ("oversized", b" " * 65537)):
                source = root / (name + ".json"); source.write_bytes(raw)
                result = subprocess.run([sys.executable, str(script), str(source), str(root / name)],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 2)
                self.assertIn("IR rejected", result.stderr)
                self.assertFalse((root / name).exists())


if __name__ == "__main__": unittest.main()
