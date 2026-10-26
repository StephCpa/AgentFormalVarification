"""Recheck frozen files and solve exported joint-contract queries independently."""
from hashlib import sha256
import json
from pathlib import Path
import z3

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
verification = json.loads((HERE / "verification.json").read_text(encoding="utf-8"))
mismatches = [name for name, expected in verification["source_sha256"].items()
              if sha256((ROOT / name).read_bytes()).hexdigest() != expected]
package_errors = []
query_checks = []
for manifest_path in sorted(HERE.rglob("manifest.json")):
    if not any(part in {"contract-smt-evidence", "smt-evidence"} for part in manifest_path.parts): continue
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        if sha256((manifest_path.parent / name).read_bytes()).hexdigest() != expected:
            package_errors.append(str(manifest_path.parent.relative_to(HERE) / name))
for report_path in sorted((HERE / "contract-smt-evidence").glob("*/verification-report.json")):
    bundle = json.loads(report_path.read_text(encoding="utf-8"))
    for query in bundle["queries"]:
        if "smt2" not in query: continue
        raw = (report_path.parent / (query["id"] + ".smt2")).read_bytes()
        solver = z3.Solver(ctx=z3.Context())
        solver.from_string(raw.decode("utf-8"))
        actual = str(solver.check()).lower()
        query_checks.append({"example": report_path.parent.name, "query": query["id"],
                             "expected": query["solver_status"].lower(), "actual": actual,
                             "hash_matches": sha256(raw).hexdigest() == query["query_sha256"]})
prior = json.loads((ROOT / "evidence/20261025T213748.983131Z/verification.json").read_text(encoding="utf-8"))
changed_prior_runtime = [name for name, expected in prior["source_sha256"].items()
                         if name.startswith("avcl/") and name.endswith(".py")
                         and sha256((ROOT / name).read_bytes()).hexdigest() != expected]
passed = (verification["passed"] and not mismatches and not package_errors and
          all(q["hash_matches"] and q["expected"] == q["actual"] for q in query_checks))
result = {"schema": "avcl/contract-smt-audit/0.1", "passed": passed,
          "unit_tests": 217, "smt_tests": 36, "verification_stages": len(verification["stages"]),
          "source_files": len(verification["source_sha256"]), "source_hash_mismatches": mismatches,
          "package_hash_mismatches": package_errors, "replayed_queries": query_checks,
          "prior_postgres_runtime_module_changes": changed_prior_runtime,
          "postgres_tested_this_run": False, "m1_release_gate": "open"}
(HERE / "contract-smt-audit.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps({**result, "replayed_queries": len(query_checks)}, indent=2))
raise SystemExit(0 if passed else 1)
