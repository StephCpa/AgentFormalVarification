"""Unsigned, single-predicate candidate bundles; not signed policy releases.

Host callers supply an independently trusted semantic digest when loading.
Local hashes detect substitution within that boundary, not publisher identity.
No manifest evidence status is treated as authority to execute an action.
"""
from pathlib import Path
import hashlib
from .canonical import Reject,canonical_bytes,strict_loads
from .ir import load_predicate
from .checks import compile_checks,load_checks,COMPILER
from .verification import verify_finite

FILES={"core.ir.json","checks.json","source-map.json","verification-report.json"}
FORMAT="avcl/predicate-candidate/0.2"

def sha(data): return "sha256:"+hashlib.sha256(data).hexdigest()

def candidate_files(predicate):
    checks=compile_checks(predicate);predicate=checks.predicate
    contents={"core.ir.json":predicate.to_json().encode("utf-8"),
              "checks.json":checks.to_json().encode("utf-8"),
              "source-map.json":canonical_bytes(dict(predicate.source_map)),
              "verification-report.json":canonical_bytes(verify_finite(predicate))}
    manifest={"format":FORMAT,"compiler":COMPILER,"release_state":"unsigned-candidate",
              "ir_digest":predicate.semantic_digest,"source_digest":predicate.source_digest,
              "files":{name:sha(data) for name,data in sorted(contents.items())}}
    contents["manifest.json"]=canonical_bytes(manifest)
    return contents

def write_candidate(path,predicate):
    contents=candidate_files(predicate)
    destination=Path(path);destination.mkdir(parents=True,exist_ok=False)
    for name,data in contents.items(): (destination/name).write_bytes(data)
    return destination

def load_candidate(path,expected_ir_digest):
    root=Path(path)
    if not root.is_dir() or root.is_symlink() or {p.name for p in root.iterdir()}!=FILES|{"manifest.json"}:
        raise Reject("INVALID_PACKAGE_FILES")
    data={}
    for name in FILES|{"manifest.json"}:
        file=root/name
        if not file.is_file() or file.is_symlink() or file.stat().st_size>65536: raise Reject("INVALID_PACKAGE_FILES")
        data[name]=file.read_bytes()
    try: manifest=strict_loads(data["manifest.json"].decode("utf-8"))
    except UnicodeError: raise Reject("INVALID_PACKAGE_ENCODING") from None
    if (type(manifest) is not dict or set(manifest)!={"format","compiler","release_state","ir_digest","source_digest","files"} or
            manifest["format"]!=FORMAT or manifest["compiler"]!=COMPILER or manifest["release_state"]!="unsigned-candidate" or
            type(manifest["files"]) is not dict or set(manifest["files"])!=FILES):
        raise Reject("INVALID_PACKAGE_MANIFEST")
    if type(expected_ir_digest) is not str or manifest["ir_digest"]!=expected_ir_digest: raise Reject("UNTRUSTED_POLICY_DIGEST")
    if any(sha(data[name])!=manifest["files"][name] for name in FILES): raise Reject("PACKAGE_HASH_MISMATCH")
    try:
        predicate=load_predicate(data["core.ir.json"].decode("utf-8"))
        source=strict_loads(data["source-map.json"].decode("utf-8"))
        strict_loads(data["verification-report.json"].decode("utf-8"))
        checks=load_checks(data["checks.json"].decode("utf-8"),predicate)
    except UnicodeError: raise Reject("INVALID_PACKAGE_ENCODING") from None
    if predicate.semantic_digest!=expected_ir_digest or predicate.source_digest!=manifest["source_digest"] or source!=dict(predicate.source_map):
        raise Reject("PACKAGE_BINDING_MISMATCH")
    return checks
