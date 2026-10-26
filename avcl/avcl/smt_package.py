"""Export unsigned offline SMT evidence; never a runtime authorization package."""
from hashlib import sha256
import json
from pathlib import Path

from .checks import compile_checks
from .smt import verify_smt


def write_smt_evidence(destination, predicate, **limits):
    checks = compile_checks(predicate)
    bundle = verify_smt(checks.predicate, **limits)
    files = {"core.ir.json": checks.predicate.to_json(), "checks.json": checks.to_json(),
             "verification-report.json": json.dumps(bundle, ensure_ascii=False, indent=2) + "\n"}
    for query in bundle["queries"]:
        files[query["property_id"] + ".smt2"] = query["smt2"]
    manifest = {"schema": "avcl/smt-evidence/0.1", "status": "unsigned-offline-evidence",
                "ir_digest": checks.predicate.semantic_digest,
                "files": {name: sha256(body.encode("utf-8")).hexdigest() for name, body in files.items()}}
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=False)
    for name, body in files.items():
        (root / name).write_bytes(body.encode("utf-8"))
    return bundle
