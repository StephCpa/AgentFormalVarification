"""Host-only audit export and offline verification; never mounts an HTTP route."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from avcl.canonical import Reject
from avcl.audit_export import MAX_BYTES,load_bundle,encode_bundle,verify_bundle,checkpoint,number


def read(path,*,profile=False):
    with Path(path).open("rb") as stream:data=stream.read(MAX_BYTES+1)
    if len(data)>MAX_BYTES:raise Reject("AUDIT_EXPORT_TOO_LARGE")
    raw=data.decode("utf-8",errors="strict")
    return json.loads(raw) if profile else load_bundle(raw)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest="command",required=True)
    export=commands.add_parser("export")
    export.add_argument("--profile",default=str(ROOT/".runtime/local-postgres.json"))
    export.add_argument("--schema",default="avcl");export.add_argument("--tenant",required=True)
    export.add_argument("--after",type=int);export.add_argument("--limit",type=int,default=100)
    export.add_argument("--previous-checkpoint");export.add_argument("--output",required=True)
    verify=commands.add_parser("verify")
    verify.add_argument("input");verify.add_argument("--tenant",required=True)
    verify.add_argument("--after",type=int);verify.add_argument("--previous-checkpoint")
    verify.add_argument("--expected-checkpoint")
    verify.add_argument("--check-payloads",action="store_true",help="Require current legacy event payload schema; does not perform semantic replay")
    args=parser.parse_args()
    try:
        previous=checkpoint(read(args.previous_checkpoint)) if args.previous_checkpoint else None
        after=args.after if args.after is not None else number(previous["sequence"]) if previous else 0
        if args.command=="export":
            from avcl.postgres import PostgresKernel
            profile=read(args.profile,profile=True)
            if type(profile) is not dict or type(profile.get("dsn")) is not str:raise Reject("INVALID_AUDIT_BUNDLE")
            db=PostgresKernel(profile["dsn"],tenant=args.tenant,schema=args.schema)
            bundle=db.trusted.export_audit(after=after,limit=args.limit,expected_previous=previous)
            # Exclusive creation preserves prior evidence and caller checkpoints.
            with Path(args.output).open("x",encoding="utf-8",newline="\n") as stream:stream.write(encode_bundle(bundle)+"\n")
            print(json.dumps({"status":"Exported","checkpoint":bundle["checkpoint"],"event_count":str(len(bundle["records"]))}))
            return 0
        expected=checkpoint(read(args.expected_checkpoint)) if args.expected_checkpoint else None
        report=verify_bundle(read(args.input),expected_tenant=args.tenant,expected_after=after,
                             expected_previous=previous,expected_checkpoint=expected,check_payloads=args.check_payloads)
        print(json.dumps(report,ensure_ascii=False))
        return 0 if report["status"]=="VerifiedAgainstCheckpoint" else 2
    except Reject as exc:
        print(json.dumps({"status":"Rejected","code":exc.code}));return 1
    except Exception:
        # Includes connection failures. Do not echo DSNs, credentials or repr.
        print(json.dumps({"status":"Unavailable","code":"AUDIT_IO_UNAVAILABLE"}));return 1


if __name__=="__main__":sys.exit(main())
