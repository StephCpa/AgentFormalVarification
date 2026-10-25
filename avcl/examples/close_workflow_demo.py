"""Validate-to-close traces with independent refund effects and ledger."""
from pathlib import Path
from uuid import uuid4
import json
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from psycopg import sql
from avcl.postgres import PostgresKernel
from avcl.contract import default_contract,build_contract,Clause
from avcl.ir import compile_predicate,FieldType
from avcl.workflow import compile_workflow,RefType
from avcl.workflow_postgres import DurableWorkflowExecutor
from avcl.workflow_runtime import FaultPlan
from avcl.tasks import TaskAuthority,PostgresTasks
from avcl.validation import ValidationAuthority
from avcl.types import Request,Capability,Approval
from avcl.simulator import RefundService,Order

def execute(dsn,name,out):
    schema="avcl_close_"+uuid4().hex;k=PostgresKernel(dsn,schema=schema);k.initialize()
    try:
        base=default_contract()
        success=compile_predicate({"tag":"Field","name":"confirmed"},{"confirmed":FieldType("Bool")},"R10")
        completion=Clause("refund-success","completion","success","refund",("close",),("R10",),(),False,success)
        contract=build_contract(base.module_id,2,base.tools,base.validators,base.assumptions,base.clauses+(completion,))
        envelope=k.trusted.seal_policy(contract,900,contract.requirements(),frozenset(c.id for c in contract.clauses if c.kind=="safety"),{"refund":{"amount":1,"currency":"CNY"}})
        k.trusted.activate_policy(envelope)
        k.trusted.add_account("tenant:CNY",100);k.trusted.add_account("order:a:CNY",100)
        k.trusted.register_capability(Capability("cap","demo","alice",frozenset({"a"}),100,1000,k.status()["epoch"]))
        service=RefundService({"a":Order("v1","original-a",100)})
        request=Request("demo","alice","task","case","a","v1","original-a","70","0")
        k.trusted.register_approval(Approval("ap",request.request_digest,1000))
        order,fee=service.read_order("a")
        context=ValidationAuthority(k).register_order_snapshot("alice","a",order,fee,500)
        with k._connect() as c:
            initial_evidence=c.execute(sql.SQL("SELECT count(*) AS n FROM {}.evidence").format(sql.Identifier(schema))).fetchone()["n"]
        assert initial_evidence==0
        task=TaskAuthority(k).register("alice","task",frozenset({"case"}))
        refs={"r":RefType("Candidate","refund"),"c":RefType("Capability","refund"),"ctx":RefType("Context","refund"),
              "a":RefType("Approval","refund"),"task":RefType("Task","refund")}
        body=[{"tag":"Validate","candidate":"r","context":"ctx","validator":"order-service","out":"e"},
              {"tag":"Prepare","candidate":"r","capability":"c","evidence":"e","approval":"a","out":"p"},
              {"tag":"Dispatch","permit":"p","out":"op"},{"tag":"Send","operation":"op"},
              {"tag":"Close","task":"task","out":"initial_close"}]
        if name=="recovered":body.extend([{"tag":"Reconcile","operation":"op"},{"tag":"Close","task":"task","out":"final_close"}])
        program=compile_workflow("task-close-demo",refs,{},("Read","Write","Disclose","ResourceConsume"),body,contract)
        sends=[];original_send=service.send
        def send(op):sends.append(op.id);return original_send(op)
        service.send=send
        result=DurableWorkflowExecutor(k,service).run(program,"alice","run",{"r":request,"c":"cap","ctx":context,"a":"ap","task":task},{},
            FaultPlan(drop_receipt_at=frozenset({"w0.3"}) if name!="confirmed" else frozenset()))
        closed=[dict(step)["result"] for step in result.steps if dict(step)["command"]=="Close"]
        expected={"confirmed":["Completed"],"pending":["Pending"],"recovered":["Pending","Completed"]}[name]
        assert result.status=="Finished" and closed==expected
        account=k.accounts()["tenant:CNY"];current=PostgresTasks(k).get("alice",task)
        assert current["status"]==expected[-1]
        assert service.real_cost==70 and len(sends)==1
        validations=sum(event.kind=="CandidateValidated" for event in k.events())
        assert validations==1
        assert (account.spent,account.pending)==((0,70) if name=="pending" else (70,0))
        (out/(name+".workflow.ir.json")).write_text(program.to_json(),encoding="utf-8")
        return {"scenario":name,"workflow":result.report(k,"alice"),"current_task":current,
                "close_steps":closed,"actual_cost":service.real_cost,"spent":account.spent,"pending":account.pending,
                "send_invocations":len(sends),"initial_evidence_count":initial_evidence,"validation_count":validations}
    finally:
        with k._connect() as c:c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

def main():
    if len(sys.argv)!=2:raise SystemExit("usage: close_workflow_demo.py OUTPUT_DIRECTORY")
    out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=False)
    dsn=json.loads((ROOT/".runtime"/"local-postgres.json").read_text(encoding="utf-8"))["dsn"]
    reports=[execute(dsn,name,out) for name in ("confirmed","pending","recovered")]
    (out/"execution-report.json").write_text(json.dumps(reports,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"passed":True,"scenarios":[{"name":r["scenario"],"close_steps":r["close_steps"],"pending":r["pending"],"send_invocations":r["send_invocations"]} for r in reports]},indent=2))

if __name__=="__main__":main()
