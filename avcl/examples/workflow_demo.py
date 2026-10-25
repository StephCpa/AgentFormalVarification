"""One checked workflow, two branches: recovered effect versus retained reserve."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from avcl.runtime import ReferenceKernel
from avcl.simulator import RefundService,Order
from avcl.types import Request,Capability,Approval
from avcl.contract import default_contract
from avcl.ir import FieldType,compile_predicate
from avcl.workflow import RefType,compile_workflow,load_workflow
from avcl.workflow_runtime import WorkflowExecutor,FaultPlan

def program():
    refs={"request":RefType("Candidate","refund"),"cap":RefType("Capability","refund"),
          "evidence":RefType("Evidence","refund"),"approval":RefType("Approval","refund")}
    values={"proceed":FieldType("Bool")}
    condition=compile_predicate({"tag":"Field","name":"proceed"},values,"R09")
    body=[{"tag":"Prepare","candidate":"request","capability":"cap","evidence":"evidence","approval":"approval","out":"permit"},
          {"tag":"If","condition":condition,"then":[
              {"tag":"Dispatch","permit":"permit","out":"operation"},
              {"tag":"Send","operation":"operation"},{"tag":"Reconcile","operation":"operation"}],
           "otherwise":[{"tag":"Drop","ref":"permit"}]}]
    return compile_workflow("refund-flow",refs,values,("Read","Write","Disclose","ResourceConsume"),body,default_contract(),
                             source="examples/workflow_demo.py:program")

def execute(flow,proceed):
    k=ReferenceKernel();service=RefundService({"order":Order("v1","original",100)})
    k.trusted.add_account("tenant:CNY",100);k.trusted.add_account("order:order:CNY",100)
    k.trusted.register_capability(Capability("cap","demo","alice",frozenset(["order"]),100,1000,1))
    req=Request("demo","alice","task","case1","order","v1","original","70","0")
    k.trusted.register_evidence(service.validate(req,"ev",1000));k.trusted.register_approval(Approval("ap",req.request_digest,1000))
    result=WorkflowExecutor(k,service).run(flow,"alice","run1",{"request":req,"cap":"cap","evidence":"ev","approval":"ap"},
        {"proceed":proceed},FaultPlan(drop_receipt_at=frozenset(["w0.1.then.1"])))
    k.assert_invariants();account=k.accounts()["tenant:CNY"]
    assert result.status=="Finished"
    assert (service.real_cost,account.spent,account.pending)==((70,70,0) if proceed else (0,0,70))
    return {"proceed":proceed,"actual_cost":service.real_cost,"spent":account.spent,"pending":account.pending,
            "result":result.report(k,"alice"),"kernel_events":[e.kind for e in k.events()]}

def main():
    if len(sys.argv)!=2: raise SystemExit("usage: python examples/workflow_demo.py OUTPUT_DIRECTORY")
    out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=False)
    flow=program();load_workflow(flow.to_json(),default_contract())
    (out/"workflow.ir.json").write_text(flow.to_json(),encoding="utf-8")
    (out/"analysis.json").write_text(json.dumps(flow.report(),indent=2),encoding="utf-8")
    reports=[execute(flow,flag) for flag in (True,False)]
    (out/"execution-report.json").write_text(json.dumps(reports,indent=2),encoding="utf-8")
    print(json.dumps(reports,indent=2))

if __name__=="__main__": main()
