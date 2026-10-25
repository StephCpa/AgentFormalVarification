"""Real PostgreSQL obligation lifecycle with an independent free-query simulator."""
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4
import json
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from psycopg import sql
from avcl.postgres import PostgresKernel
from avcl.supervision import PostgresSupervisor
from avcl.simulator import RefundService,Order
from avcl.types import Request,Capability,Approval

def scenario(dsn,name):
    schema="avcl_demo_"+uuid4().hex;now=[0]
    k=PostgresKernel(dsn,schema=schema,clock=lambda:now[0]);k.initialize()
    try:
        service=RefundService({"order":Order("v1","original",100)})
        k.trusted.add_account("tenant:CNY",100);k.trusted.add_account("order:order:CNY",100)
        k.trusted.register_capability(Capability("cap","demo","alice",frozenset({"order"}),100,1000,1))
        request=Request("demo","alice","task","business","order","v1","original","70","0")
        k.trusted.register_evidence(service.validate(request,"evidence",1000))
        k.trusted.register_approval(Approval("approval",request.request_digest,1000))
        op=k.prepare("alice",request,"cap","evidence","approval","request")
        op=k.dispatch("alice",op.id,op.permit_ref)
        if name!="accepted-handoff":service.send(op)  # Deliberately lose the original receipt.
        op=k.trusted.unknown(op.id)
        supervisor=PostgresSupervisor(k)
        if name=="late-reconciliation":now[0]=op.obligation_due+10
        outcome=supervisor.run_once("worker",service)
        if name=="accepted-handoff":
            assert outcome["status"]=="Deferred"
            now[0]=31;supervisor.register_receiver("test-human-team",1000);claim=supervisor.claim("worker")
            outcome={"status":supervisor.accept_handoff(op.id,"worker",claim["generation"],"test-human-team","test-receipt")}
        row=k.status()["obligations"][0];account=k.accounts()["tenant:CNY"]
        if name=="accepted-handoff":assert row["resolution"]=="Escalated" and account.pending==70 and service.real_cost==0
        else:assert row["resolution"]=="FinalEffect" and account.spent==70 and account.pending==0 and service.real_cost==70
        assert row["status"]==("Breached" if name=="late-reconciliation" else "Satisfied")
        k.assert_invariants()
        return {"scenario":name,"outcome":outcome,"logical_time":now[0],"obligation":row,
                "account":asdict(account),"remote_real_cost":service.real_cost,
                "operation_execution":str(k.get("alice",op.id).state),"events":[asdict(e) for e in k.events()]}
    finally:
        with k._connect() as c:c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

def main():
    if len(sys.argv)!=2:raise SystemExit("usage: python examples/supervision_demo.py OUTPUT_DIRECTORY")
    output=Path(sys.argv[1]);output.mkdir(parents=True,exist_ok=False)
    dsn=json.loads((ROOT/".runtime"/"local-postgres.json").read_text(encoding="utf-8"))["dsn"]
    results=[scenario(dsn,name) for name in ("timely-reconciliation","accepted-handoff","late-reconciliation")]
    report={"scope":"real PostgreSQL; logical test time; free read-only simulator query; host-verified synthetic handoff ACK",
            "scenarios":results,"passed":True}
    (output/"supervision-report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"passed":True,"scenarios":[{"name":r["scenario"],"status":r["obligation"]["status"],"resolution":r["obligation"]["resolution"],"pending":r["account"]["pending"]} for r in results]},indent=2))

if __name__=="__main__":main()
