"""Run named M1 evidence checks. This does not decide or issue an M1 release."""
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "integration"))

CASES = [
    (["R01"], "tests.test_m1_boundaries.M1BoundaryTests.test_T01_objects_and_unregistered_refs_cannot_create_authority"),
    (["R01", "R08"], "tests.test_workflow.WorkflowTests.test_candidate_and_operation_cannot_be_authority_or_permit"),
    (["R02"], "tests.test_m1_boundaries.M1BoundaryTests.test_T02_fresh_evidence_does_not_rebind_an_old_approval"),
    (["R01", "R02"], "tests.test_runtime.ProtocolTests.test_wrong_permit_or_subject_rejected"),
    (["R04"], "test_m1_processes.M1ProcessTests.test_T04_two_processes_contend_for_70_and_60_of_100"),
    (["R04"], "tests.test_runtime.ProtocolTests.test_T19_second_account_failure_is_atomic"),
    (["R04"], "test_postgres.PersistenceTests.test_second_account_rejection_rolls_back_every_row"),
    (["R05"], "tests.test_runtime.ProtocolTests.test_T05_lost_receipt_preserves_exposure_and_replay_is_idempotent"),
    (["R05"], "tests.test_runtime.ProtocolTests.test_timeout_before_effect_retains_bound"),
    (["R05"], "tests.test_review_regressions.ReviewRegressions.test_final_knowledge_survives_late_weaker_authenticated_facts"),
    (["R08"], "test_m1_processes.M1ProcessTests.test_T08_two_processes_consume_one_permit_once"),
    (["R02", "R04", "R08"], "test_m1_processes.M1ProcessTests.test_two_processes_retry_same_prepare_without_duplicate_reservation"),
    (["R08"], "tests.test_workflow.WorkflowTests.test_join_does_not_resurrect_resource_consumed_in_one_branch"),
    (["R09"], "tests.test_encoding_ir.PredicateTests.test_T09_effectful_and_nonlinear_nodes_rejected"),
    (["R09"], "tests.test_encoding_ir.PredicateTests.test_invalid_dead_branch_is_checked_before_evaluation"),
    (["R09"], "tests.test_ir_pipeline.IRPipelineTests.test_enum_and_id_are_nominal_not_untyped_strings"),
    (["R09"], "tests.test_ir_pipeline.IRPipelineTests.test_option_guards_and_short_circuit_on_both_backends"),
    (["R09"], "tests.test_ir_pipeline.IRPipelineTests.test_runtime_results_do_not_imply_static_proof"),
    (["R09"], "tests.test_ir_pipeline.IRPipelineTests.test_unenumerable_domain_is_unknown_without_sampling_promotion"),
    (["R09"], "tests.test_evaluation_budget.EvaluationBudgetTests.test_structural_comparison_and_membership_are_not_single_unit_shortcuts"),
    (["R09"], "tests.test_evaluation_budget.EvaluationBudgetTests.test_shared_clause_budget_and_forbid_never_allow_unknown"),
    (["R04", "R08", "R09"], "tests.test_evaluation_budget.EvaluationBudgetTests.test_prepare_and_dispatch_fail_atomically_after_first_check_consumes_budget"),
    (["R04", "R09"], "test_evaluation_budget.DurableEvaluationBudgetTests.test_prepare_work_exhaustion_rolls_back_every_durable_projection"),
    (["R04", "R08", "R09"], "test_evaluation_budget.DurableEvaluationBudgetTests.test_dispatch_work_exhaustion_keeps_permit_reservations_and_no_outbox"),
]


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.active = {}

    def startTest(self, test):
        self.active[test.id()] = {"test_id": test.id(), "status": "Running", "started": time.monotonic()}
        super().startTest(test)

    def mark(self, test, status):
        if test.id() in self.active:
            self.active[test.id()]["status"] = status
        else:
            # unittest reports setUpClass failures without startTest/stopTest.
            self.records.append({"test_id": test.id(), "status": status, "seconds": 0,
                                 "fixture_failure": True})
    def addSuccess(self, test): self.mark(test, "Passed"); super().addSuccess(test)
    def addFailure(self, test, err): self.mark(test, "Failed"); super().addFailure(test, err)
    def addError(self, test, err): self.mark(test, "Error"); super().addError(test, err)
    def addSkip(self, test, reason): self.mark(test, "Skipped"); super().addSkip(test, reason)

    def addSubTest(self, test, subtest, err):
        if err: self.mark(test, "Failed")
        super().addSubTest(test, subtest, err)

    def stopTest(self, test):
        record = self.active.pop(test.id())
        record["seconds"] = round(time.monotonic() - record.pop("started"), 3)
        if hasattr(test, "acceptance_evidence"): record["observations"] = test.acceptance_evidence
        self.records.append(record)
        super().stopTest(test)


def main():
    if len(sys.argv) != 2: raise SystemExit("usage: python scripts/m1_checks.py NEW_OUTPUT_DIRECTORY")
    output = Path(sys.argv[1]); output.mkdir(parents=True, exist_ok=False)
    requested = {name: requirements for requirements, name in CASES}
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in requested)
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=RecordedResult).run(suite)
    actual = {r["test_id"] for r in result.records}
    for record in result.records: record["requirement_ids"] = requested.get(record["test_id"], [])
    passed = result.wasSuccessful() and actual == set(requested) and all(r["status"] == "Passed" for r in result.records)
    files = set()
    for case in requested:
        module = case.rsplit(".", 2)[0]
        if module in sys.modules: files.add(Path(sys.modules[module].__file__).resolve())
    report = {"schema": "avcl/m1-targeted-evidence/0.1", "suite_passed": passed,
              "release_decision": "not_issued", "checked_at": datetime.now(timezone.utc).isoformat(),
              "scope": "named authority, binding, budget, Unknown, affine and pure-IR checks; targeted real PostgreSQL",
              "case_count": len(result.records), "cases": result.records,
              "missing_test_ids": sorted(set(requested) - actual), "unexpected_test_ids": sorted(actual - set(requested)),
              "test_source_sha256": {p.relative_to(ROOT).as_posix(): sha256(p.read_bytes()).hexdigest() for p in sorted(files)},
              "limits": ["not a full M1 requirement audit", "not a full PostgreSQL integration run",
                         "no mechanical Python/SQL refinement", "no production identity or egress-isolation claim"]}
    (output / "tests.log").write_text(log.getvalue(), encoding="utf-8")
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(log.getvalue())
    print("M1 targeted evidence: " + str(output))
    return 0 if passed else 1


if __name__ == "__main__": sys.exit(main())
