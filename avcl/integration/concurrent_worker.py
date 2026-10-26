"""Owned test child: a single prepare/dispatch against the private test cluster."""
import json
import os
from pathlib import Path
import sys
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from avcl.canonical import Reject,digest
from avcl.rejection_audit import AuthenticatedContext
from avcl.postgres import PostgresKernel
from avcl.storage_codec import dumps, loads


def main():
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    profile = json.loads((ROOT / ".runtime/local-postgres.json").read_text(encoding="utf-8"))
    if job["method"] not in {"prepare", "dispatch", "audited-prepare"}: raise ValueError("unsupported test operation")
    dsn = make_conninfo(profile["dsn"], application_name=job["application_name"])
    kernel = PostgresKernel(dsn, schema=job["schema"], clock=lambda: job["now"])
    result = {"pid": os.getpid(), "application_name": job["application_name"]}
    try:
        args=loads(job["args"])
        if job["method"]=="audited-prepare":
            context=AuthenticatedContext(args[0],"prepare",digest({"request":args[1].request_digest}),job["application_name"])
            operation=kernel._transaction("prepare",lambda k,c:k.prepare(*args),authenticate=lambda k,c:context)
        else:operation = getattr(kernel, job["method"])(*args)
        result.update(status="accepted", operation=dumps(operation))
    except Reject as exc:
        result.update(status="rejected", code=exc.code)
    print(json.dumps(result), flush=True)


if __name__ == "__main__": main()
