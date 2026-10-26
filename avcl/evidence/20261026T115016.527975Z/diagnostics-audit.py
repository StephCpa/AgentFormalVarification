"""Read-only post-verification audit; run from avcl with the project interpreter."""
import hashlib
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
from avcl.gateway import RuntimeGateway
from avcl.requirements import CODE_REQUIREMENTS

report = json.loads((HERE / "verification.json").read_text(encoding="utf-8"))
mismatches = [name for name, expected in report["source_sha256"].items()
              if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected]
counts = {}
for stage in report["stages"]:
    log = (HERE / stage["log"]).read_text(encoding="utf-8")
    match = re.search(r"^Ran (\d+) tests? in", log, re.M)
    if match:
        counts[stage["name"]] = int(match[1])
probes = {code: RuntimeGateway.error(code, "diagnostic-audit")["error"]["requirement_ids"]
          for code in ("BUDGET_EXCEEDED", "POLICY_DENIED", "ASSUMPTION_BROKEN", "UNREVIEWED_EXTENSION")}
expected = {"BUDGET_EXCEEDED":["R04"], "POLICY_DENIED":["R09"],
            "ASSUMPTION_BROKEN":["R14"], "UNREVIEWED_EXTENSION":[]}
result = {
    "schema":"avcl/requirement-diagnostics-audit/0.1",
    "verification_passed":report["passed"],
    "source_files_checked":len(report["source_sha256"]),
    "source_hash_mismatches":mismatches,
    "design_baselines_unchanged":all(d["unchanged"] for d in report["design_baselines"]),
    "stages":len(report["stages"]),
    "test_counts":counts,
    "error_response_probes":probes,
    "probes_match_expected":probes == expected,
    "reviewed_error_codes":len(CODE_REQUIREMENTS),
    "postgres_full_suite_requested":report["postgres_full_suite_requested"],
    "m1_release_ready":False,
    "remaining":["runtime evaluation work budget and Unknown propagation",
                 "durable denial log and review of remaining language-extension diagnostics",
                 "full-design completion and requirement-by-requirement release audit"],
}
(HERE / "diagnostics-audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
sys.exit(0 if report["passed"] and not mismatches and probes == expected else 1)
