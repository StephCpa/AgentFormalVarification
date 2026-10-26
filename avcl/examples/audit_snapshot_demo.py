"""Export and independently check a real PostgreSQL demonstration history."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from psycopg import sql
from avcl.postgres import PostgresKernel
from avcl.types import Request,Capability,Approval
from avcl.simulator import RefundService,Order
from avcl.canonical import Reject
from avcl.storage_codec import loads,dumps
from avcl.audit_export import build_bundle,encode_bundle,verify_bundle
from avcl.observation_fact import decode_fact,decode_effective_costs


def main(destination):
    out=Path(destination);out.mkdir(parents=True,exist_ok=False)
    dsn=json.loads((ROOT/".runtime/local-postgres.json").read_text(encoding="utf-8"))["dsn"]
    schema="avcl_demo_"+uuid4().hex;k=PostgresKernel(dsn,schema=schema)
    try:
        k.initialize();service=RefundService({"order":Order("v1","original",100)})
        k.trusted.add_account("tenant:CNY",100);k.trusted.add_account("order:order:CNY",100)
        k.trusted.register_capability(Capability("cap","demo","alice",frozenset({"order"}),100,1000,1))
        request=Request("demo","alice","task","business","order","v1","original","70","0")
        k.trusted.register_evidence(service.validate(request,"evidence",1000))
        k.trusted.register_approval(Approval("approval",request.request_digest,1000))
        op=k.prepare("alice",request,"cap","evidence","approval","request")
        op=k.dispatch("alice",op.id,op.permit_ref);k.trusted.observe(service.send(op))
        before=k.events();full=k.trusted.export_audit(limit=256)
        (out/"full.json").write_text(encode_bundle(full)+"\n",encoding="utf-8")
        (out/"checkpoint.json").write_text(json.dumps(full["checkpoint"],indent=2)+"\n",encoding="utf-8")
        reports=[];after=0;previous=None
        while True:
            page=k.trusted.export_audit(after=after,limit=2,expected_previous=previous)
            (out/f"page-{len(reports)}.json").write_text(encode_bundle(page)+"\n",encoding="utf-8")
            reports.append(verify_bundle(page,expected_tenant="demo",expected_after=after,expected_previous=previous,check_payloads=True))
            if len(page["records"])<2:break
            previous=page["checkpoint"];after=int(previous["sequence"])
        verified=verify_bundle(full,expected_tenant="demo",expected_checkpoint=full["checkpoint"],check_payloads=True)
        facts=[]
        for event in before:
            if event.kind=="ObservationRecorded":
                details=dict(event.details)
                facts.append({"sequence":str(event.sequence),"fact":decode_fact(details["fact"]),
                              "effective_costs":decode_effective_costs(details["effective_costs"])})
        (out/"observation-facts.json").write_text(json.dumps({"format":"avcl/observation-audit-view/0.1",
            "tenant":"demo","facts":facts,"event_truth":"not_verified"},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        payloads=[r["payload"] for r in full["records"]]
        payloads[0]=dumps(replace(loads(payloads[0]),kind="Rewritten"))
        rewritten=build_bundle("demo",payloads)
        unanchored=verify_bundle(rewritten,expected_tenant="demo")
        try:verify_bundle(rewritten,expected_tenant="demo",expected_checkpoint=full["checkpoint"])
        except Reject as exc:rejected=exc.code
        else:raise AssertionError("rewritten history must fail the original checkpoint")
        assert rejected=="AUDIT_CHECKPOINT_MISMATCH" and k.events()==before
        assert service.real_cost==70 and k.accounts()["tenant:CNY"].spent==70
        report={"passed":True,"scope":"real PostgreSQL snapshot; demonstration checkpoints from this same run, not an external trust service; no semantic replay proof",
                "verification":verified,"pages":reports,"rewritten_without_anchor":unanchored["status"],
                "rewritten_against_original_anchor":rejected,"database_events_unchanged":True,
                "artifacts_sha256":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.glob('*.json'))}}
        (out/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(json.dumps({"passed":True,"events":len(before),"pages":len(reports),"tamper_rejected":rejected}))
    finally:
        with k._connect() as c:c.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


if __name__=="__main__":main(sys.argv[1])
