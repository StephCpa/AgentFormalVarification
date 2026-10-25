"""Emit non-secret database/runtime provenance for a verification bundle."""
from pathlib import Path
import hashlib
import json
import platform
import psycopg
ROOT=Path(__file__).resolve().parents[1]
profile=json.loads((ROOT/".runtime"/"local-postgres.json").read_text(encoding="utf-8"))
with psycopg.connect(profile["dsn"]) as conn:
    assert conn.info.server_version==180006
    settings={key:conn.execute("SHOW "+key).fetchone()[0] for key in
              ("fsync","synchronous_commit","full_page_writes","server_encoding","listen_addresses")}
    assert all(settings[key]=="on" for key in ("fsync","synchronous_commit","full_page_writes"))
    version=conn.execute("SELECT version()").fetchone()[0]
manifest=ROOT/".runtime"/"postgresql-18.6"/"toolchain-manifest.json"
provenance=json.loads(manifest.read_text(encoding="utf-8"))
base=manifest.parent
for name,expected in provenance["files"].items():
    if hashlib.sha256((base/name).read_bytes()).hexdigest()!=expected:raise RuntimeError("Database toolchain hash mismatch: "+name)
print(json.dumps({"server":version,"server_version":180006,"psycopg":psycopg.__version__,
    "python":platform.python_version(),"settings":settings,"binary_source":provenance["source"],
    "toolchain_manifest_sha256":hashlib.sha256(manifest.read_bytes()).hexdigest(),
    "verified_binary_files":len(provenance["files"]),
    "provenance_limit":"HTTPS official distributor ZIP, ZIP CRC and local SHA256; no separately published vendor SHA256"},indent=2))
