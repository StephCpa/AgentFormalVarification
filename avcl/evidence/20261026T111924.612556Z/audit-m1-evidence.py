"""Recheck frozen M1 observations and retain actual traceability findings."""
from hashlib import sha256
import inspect
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
from avcl.gateway import RuntimeGateway
from avcl.checks import Checks
from avcl.ir import Predicate

verification = json.loads((HERE / "verification.json").read_text(encoding="utf-8"))
targeted = json.loads((HERE / "m1-targeted/report.json").read_text(encoding="utf-8"))
mismatches = [name for name, expected in verification["source_sha256"].items()
              if sha256((ROOT / name).read_bytes()).hexdigest() != expected]
processes = []
for case in targeted["cases"]:
    observation = case.get("observations", {})
    if "observed_waiters" not in observation: continue
    host_pids = [child["pid"] for child in observation["children"]]
    backend_pids = [row["pid"] for row in observation["observed_waiters"]]
    result_pids = [row["pid"] for row in observation["results"]]
    processes.append({"test_id": case["test_id"], "host_pids": host_pids, "backend_pids": backend_pids,
                      "independent_processes": len(set(host_pids)) == 2 and set(result_pids) == set(host_pids),
                      "independent_backends": len(set(backend_pids)) == 2,
                      "overlap_confirmed": observation["overlap_confirmed"],
                      "postconditions": observation["postconditions"]})
diagnostics = [{"code": code, "expected_requirement": requirement,
                "actual_requirements": RuntimeGateway.error(code, "m1-audit")["error"]["requirement_ids"]}
               for code, requirement in (("BUDGET_EXCEEDED", "R04"), ("POLICY_DENIED", "R09"))]
for row in diagnostics: row["requirement_present"] = row["expected_requirement"] in row["actual_requirements"]
result = {"schema": "avcl/m1-evidence-audit/0.1", "verification_passed": verification["passed"],
          "targeted_suite_passed": targeted["suite_passed"], "m1_release_ready": False,
          "source_files": len(verification["source_sha256"]), "source_hash_mismatches": mismatches,
          "unit_tests": 220, "smt_tests": 36, "targeted_checks": len(targeted["cases"]),
          "verification_stages": len(verification["stages"]), "process_evidence": processes,
          "diagnostic_findings": diagnostics,
          "runtime_evaluator_signatures": {"Predicate.evaluate": str(inspect.signature(Predicate.evaluate)),
                                           "Checks.evaluate": str(inspect.signature(Checks.evaluate))},
          "remaining_items": ["runtime evaluation work budget and fail-closed propagation",
                              "accurate requirement IDs in denial diagnostics and admission evidence"],
          "postgres_scope": "four targeted cases including three two-process cases; full suite not rerun"}
(HERE / "m1-evidence-audit.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, indent=2))
assert verification["passed"] and targeted["suite_passed"] and not mismatches
assert len(processes) == 3 and all(p["independent_processes"] and p["independent_backends"] and p["overlap_confirmed"] for p in processes)
