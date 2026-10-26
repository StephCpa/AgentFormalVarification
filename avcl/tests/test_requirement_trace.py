"""Diagnostic provenance must follow the failed gate, not a generic API label."""
import ast
import json
from dataclasses import replace
from pathlib import Path
import unittest

from avcl.canonical import Reject
from avcl.contract import default_contract, compile_contract, Health
from avcl.ir import compile_predicate, FieldType
from avcl.requirements import CODE_REQUIREMENTS, requirements_for
from avcl.runtime import PolicyDenied
from .support import setup, authorize, prepare
from .test_contract_policy import amended, ceiling_clause
from .test_encoding_ir import field
from .test_delegation import provision, payload, authorization


class RequirementTraceTests(unittest.TestCase):
    def test_budget_failure_has_resource_requirement_and_rolls_back(self):
        k, service, _ = setup(limit=60)
        auth = authorize(k, service)
        before = k.accounts(), k.events(), dict(k._approvals), dict(k._dedup)
        with self.assertRaises(Reject) as caught:
            prepare(k, auth)
        self.assertEqual((caught.exception.code, caught.exception.requirement_ids),
                         ("BUDGET_EXCEEDED", ("R04",)))
        self.assertEqual((k.accounts(), k.events(), k._approvals, k._dedup), before)

    def test_delegation_budget_retains_its_specific_requirement(self):
        k, service, _ = setup()
        provision(k)
        with self.assertRaises(Reject) as caught:
            k.delegate("parent", payload(limit_minor="101"))
        self.assertEqual(caught.exception.requirement_ids, ("R04", "R11"))
        child = k.delegate("parent", payload(limit_minor="60"))["child_cap_ref"]
        with self.assertRaises(Reject) as caught:
            prepare(k, authorization(k, service, child, value=70))
        self.assertEqual((caught.exception.code, caught.exception.requirement_ids),
                         ("BUDGET_EXCEEDED", ("R04", "R11")))

    def test_failed_clauses_only_and_assumption_failure_are_distinguished(self):
        base = default_contract()
        passing = replace(ceiling_clause(100), id="passes", requirement_ids=("R04", "PASSING_ONLY"))
        failing = replace(ceiling_clause(60), requirement_ids=("R04", "LIMIT_RULE"))
        compiled = compile_contract(amended(base, extra=(passing, failing)))
        values = {"amount":70, "currency":"CNY"}
        health = {"remote-idempotency":Health("simulator", "refund", 0, 1000, "Healthy")}
        denied = PolicyDenied(compiled.evaluate("prepare", "refund", values, health, 0))
        self.assertEqual(denied.requirement_ids, ("LIMIT_RULE", "R04", "R09"))
        self.assertNotIn("PASSING_ONLY", denied.requirement_ids)
        broken = PolicyDenied(compile_contract(base).evaluate("dispatch", "refund", values, {}, 0),
                              "ASSUMPTION_BROKEN")
        self.assertEqual(broken.requirement_ids, ("R09", "R14"))

    def test_pure_diagnostic_preserves_source_requirement(self):
        predicate = compile_predicate(field("ok"), {"ok":FieldType("Bool")}, "_LOCAL_RULE")
        with self.assertRaises(Reject) as caught:
            predicate.evaluate({"ok":"not-bool"})
        self.assertEqual(caught.exception.requirement_ids, ("R09", "_LOCAL_RULE"))
        self.assertEqual(caught.exception.requirement_id, "_LOCAL_RULE")

    def test_admission_events_cover_checked_gates_and_retry_adds_no_event(self):
        k, service, _ = setup()
        auth = authorize(k, service)
        op = prepare(k, auth)
        op = k.dispatch("a", op.id, op.permit_ref)
        before = k.events()
        k.dispatch("a", op.id, op.permit_ref)
        prepare(k, auth)
        self.assertEqual(k.events(), before)
        events = [e for e in before if e.kind in {"Prepared", "DispatchAdmitted"}]
        self.assertEqual(len(events), 2)
        for event in events:
            details = dict(event.details)
            self.assertEqual(json.loads(details["requirement_ids"]),
                             ["R01", "R02", "R04", "R06", "R08", "R09", "R14"])
            self.assertEqual(details["contract_digest"], op.contract_digest)
            self.assertEqual(details["requirement_id"], "R09")

    def test_unknown_codes_do_not_fabricate_requirements_and_core_codes_are_reviewed(self):
        self.assertEqual(Reject("EXTENSION_NOT_REVIEWED").requirement_ids, ())
        self.assertEqual(requirements_for("INVALID_HTTP_REQUEST"), ())
        root = Path(__file__).resolve().parents[1] / "avcl"
        missing = []
        def literals(expr):
            if isinstance(expr, ast.Constant) and isinstance(expr.value,str):return [expr.value]
            if isinstance(expr, ast.IfExp):return literals(expr.body)+literals(expr.orelse)
            return []
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and
                        node.func.id in {"Reject","Diagnostic","WorkflowDiagnostic","error","fail"} and node.args):
                    for code in literals(node.args[0]):
                        if code not in CODE_REQUIREMENTS:
                            missing.append((path.name, node.lineno, code))
        self.assertEqual(missing, [], "Review new rejection codes against design section 2")
