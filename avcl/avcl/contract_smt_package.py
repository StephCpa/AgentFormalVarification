"""Freeze unsigned joint-policy evidence; never write an activation envelope."""
from hashlib import sha256
import json
from pathlib import Path

from .contract import compile_contract
from .contract_smt import verify_contract_smt


def write_contract_smt_evidence(destination, contract, **limits):
    compiled = compile_contract(contract)
    bundle = verify_contract_smt(compiled.contract, **limits)
    files = {"contract.ir.json": compiled.contract.to_json(),
             "checks.json": json.dumps({key: json.loads(check.to_json()) for key, check in compiled.checks},
                                       ensure_ascii=False, indent=2) + "\n",
             "verification-report.json": json.dumps(bundle, ensure_ascii=False, indent=2) + "\n"}
    for query in bundle["queries"]:
        if "smt2" in query: files[query["id"] + ".smt2"] = query["smt2"]
    manifest = {"schema": "avcl/contract-smt-evidence/0.1", "status": "unsigned-offline-evidence",
                "contract_digest": compiled.contract.semantic_digest,
                "files": {name: sha256(body.encode("utf-8")).hexdigest() for name, body in files.items()}}
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    root = Path(destination); root.mkdir(parents=True, exist_ok=False)
    for name, body in files.items(): (root / name).write_bytes(body.encode("utf-8"))
    return bundle
