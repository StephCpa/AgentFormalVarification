"""Two child Agents share a root; lost receipt and ancestor revocation persist."""
from pathlib import Path
from uuid import uuid4
import json
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from psycopg import sql
from avcl.postgres import PostgresKernel
from avcl.simulator import RefundService,Order
from avcl.types import Capability,Request,Approval,Settlement
from avcl.contract import default_contract
from avcl.workflow import RefType,compile_workflow
from avcl.workflow_postgres import DurableWorkflowExecutor

def main():
    if len(sys.argv)!=2:raise SystemExit("usage: delegation_demo.py OUTPUT_DIRECTORY")
    out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=False)
    dsn=json.loads((ROOT/".runtime"/"local-postgres.json").read_text(encoding="utf-8"))["dsn"]
    schema="avcl_delegate_"+uuid4().hex;k=PostgresKernel(dsn,schema=schema);k.initialize()
    try:
        k.trusted.add_account("tenant:CNY",100)
        for order in ("a","b"):k.trusted.add_account("order:"+order+":CNY",100)
        for subject in ("parent","one","two"):k.trusted.register_subject(subject)
        k.trusted.register_capability(Capability("root","demo","parent",frozenset({"a","b"}),100,900,1))
        root_account=k.trusted.enable_delegation("root",100)
        service=RefundService({name:Order("v1","original-"+name,100) for name in ("a","b")})
        parent_body=[]
        for subject,order in (("one","a"),("two","b")):
            parent_body.append({"tag":"Delegate","capability":"root","child_subject":subject,"out":subject+"_cap","attenuations":{
                "orders":[order],"tools":["refund"],"effects":["Write","Disclose","ResourceConsume"],
                "max_amount_minor":"60","limit_minor":"80","expires":"800","can_delegate":False}})
        parent_program=compile_workflow("parent-delegation",{"root":RefType("Capability","refund")},{},("Read","ResourceConsume"),parent_body,default_contract())
        parent_result=DurableWorkflowExecutor(k).run(parent_program,"parent","parent-run",{"root":"root"},{})
        assert parent_result.status=="Finished"
        grants={dict(step)["child_subject"]:dict(step) for step in parent_result.steps}
        authorizations={}
        for subject,order in (("one","a"),("two","b")):
            request=Request("demo",subject,"task-"+subject,"refund-"+subject,order,"v1","original-"+order,"60","0")
            evidence=service.validate(request,"evidence-"+subject,800);k.trusted.register_evidence(evidence)
            k.trusted.register_approval(Approval("approval-"+subject,request.request_digest,800))
            authorizations[subject]={"r":request,"c":grants[subject]["child_cap_ref"],"e":evidence.ref,"a":"approval-"+subject}
        child_program=compile_workflow("child-refund",{n:RefType(t,"refund") for n,t in (("r","Candidate"),("c","Capability"),("e","Evidence"),("a","Approval"))},
            {},("Read","Write","Disclose","ResourceConsume"),[
                {"tag":"Prepare","candidate":"r","capability":"c","evidence":"e","approval":"a","out":"p"},
                {"tag":"Dispatch","permit":"p","out":"op"},{"tag":"Send","operation":"op"},{"tag":"Reconcile","operation":"op"}],default_contract())
        executor=DurableWorkflowExecutor(k,service)
        executor.start(child_program,"one","one-run",authorizations["one"],{})
        executor.advance("one","one-run","worker")
        two=executor.run(child_program,"two","two-run",authorizations["two"],{})
        denied=two.error;assert two.status=="Halted" and denied=="BUDGET_EXCEEDED"
        executor.advance("one","one-run","worker")
        io=executor.advance("one","one-run","worker");service.send(io["operation"])
        executor.finish_io("one","one-run",io["node_id"],"worker",io["generation"],None)
        k.trusted.revoke_capability("root")
        pending=k.accounts()[root_account].pending
        restored=PostgresKernel(dsn,schema=schema)
        resumed=DurableWorkflowExecutor(restored,service).resume("one","one-run")
        final=restored.get("one",io["operation"].id);restored.assert_invariants()
        assert resumed.status=="Finished"
        assert pending==60 and final.settlement==Settlement.FINAL and service.real_cost==60
        assert restored.accounts()[root_account].spent==60 and restored.accounts()[root_account].pending==0
        assert restored.accounts()["tenant:CNY"].spent==60
        (out/"parent.workflow.ir.json").write_text(parent_program.to_json(),encoding="utf-8")
        (out/"child.workflow.ir.json").write_text(child_program.to_json(),encoding="utf-8")
        report={"schema":"avcl/delegation-demo/0.2","passed":True,"grants":grants,"sibling_prepare_rejected":denied,
                "pending_after_ancestor_revoke":pending,"actual_cost":service.real_cost,"effect_count":len(service.effects),
                "parent_workflow":parent_result.report(restored,"parent"),"child_workflows":{"one":resumed.report(restored,"one"),"two":two.report(restored,"two")},
                "final_reservations":[dict(r.__dict__) for r in final.reservations],
                "events":[{"sequence":e.sequence,"kind":e.kind,"operation_id":e.operation_id,"details":dict(e.details)} for e in restored.events()]}
        (out/"execution-report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(json.dumps({k:report[k] for k in ("passed","sibling_prepare_rejected","pending_after_ancestor_revoke","actual_cost","effect_count")},indent=2))
    finally:
        with k._connect() as c:c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

if __name__=="__main__":main()
