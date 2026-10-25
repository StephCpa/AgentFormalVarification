"""Persisted workflow/ledger traces; actual kill points are tested in integration/."""
from pathlib import Path
from uuid import uuid4
import json
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from psycopg import sql
from avcl.postgres import PostgresKernel
from avcl.workflow_postgres import DurableWorkflowExecutor
from avcl.workflow_runtime import FaultPlan
from avcl.simulator import RefundService,Order
from avcl.types import Request,Capability,Approval
from examples.workflow_demo import program

def execute(dsn,flow,name):
    schema="avcl_flow_"+uuid4().hex;now=[0]
    k=PostgresKernel(dsn,schema=schema,clock=lambda:now[0]);k.initialize()
    try:
        service=RefundService({"order":Order("v1","original",100)})
        k.trusted.add_account("tenant:CNY",100);k.trusted.add_account("order:order:CNY",100)
        k.trusted.register_capability(Capability("cap","demo","alice",frozenset({"order"}),100,1000,1))
        request=Request("demo","alice","task","case","order","v1","original","70","0")
        k.trusted.register_evidence(service.validate(request,"ev",1000));k.trusted.register_approval(Approval("ap",request.request_digest,1000))
        refs={"request":request,"cap":"cap","evidence":"ev","approval":"ap"};executor=DurableWorkflowExecutor(k,service)
        sends=[];send=service.send
        def tracked_send(op):sends.append(op.id);return send(op)
        service.send=tracked_send
        if name=="uncertain-send-recovery":
            executor.start(flow,"alice","run",refs,{"proceed":True})
            for _ in range(3):executor.advance("alice","run","old-worker")
            job=executor.advance("alice","run","old-worker");service.send(job["operation"])
            # The effect persists independently; its receipt/checkpoint is lost.
            now[0]=31
            fresh=PostgresKernel(dsn,schema=schema,clock=lambda:now[0])
            result=DurableWorkflowExecutor(fresh,service).resume("alice","run","replacement")
        else:
            result=executor.run(flow,"alice","run",refs,{"proceed":name!="drop-permit"},FaultPlan(drop_receipt_at=frozenset({"w0.1.then.1"})))
        account=k.accounts()["tenant:CNY"]
        expected=(0,0,70,0) if name=="drop-permit" else (70,70,0,1)
        assert (service.real_cost,account.spent,account.pending,len(sends))==expected
        assert result.status=="Finished";k.assert_invariants()
        with k._connect() as c:
            c.execute(sql.SQL("SET LOCAL search_path={},pg_catalog").format(sql.Identifier(schema)))
            run=c.execute("SELECT pc,revision,status FROM workflow_run").fetchone()
            intents=c.execute("SELECT node_id,kind,status,generation FROM workflow_io ORDER BY node_id").fetchall()
        return {"scenario":name,"result":result.report(k,"alice"),"checkpoint":run,"io_intents":intents,
            "send_invocations":len(sends),"real_cost":service.real_cost,"spent":account.spent,"pending":account.pending}
    finally:
        with k._connect() as c:c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

def main():
    if len(sys.argv)!=2:raise SystemExit("usage: python examples/durable_workflow_demo.py OUTPUT_DIRECTORY")
    output=Path(sys.argv[1]);output.mkdir(parents=True,exist_ok=False)
    dsn=json.loads((ROOT/".runtime"/"local-postgres.json").read_text(encoding="utf-8"))["dsn"]
    flow=program();reports=[execute(dsn,flow,name) for name in ("lost-receipt","drop-permit","uncertain-send-recovery")]
    (output/"workflow.ir.json").write_text(flow.to_json(),encoding="utf-8")
    (output/"execution-report.json").write_text(json.dumps(reports,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"passed":True,"scenarios":[{"name":r["scenario"],"send_invocations":r["send_invocations"],"spent":r["spent"],"pending":r["pending"]} for r in reports]},indent=2))

if __name__=="__main__":main()
