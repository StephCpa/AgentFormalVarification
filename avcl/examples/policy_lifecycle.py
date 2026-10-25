"""Policy activation, old-permit rejection, and commitment-preserving rollback."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from avcl.canonical import Reject
from avcl.runtime import ReferenceKernel
from avcl.simulator import RefundService,Order
from avcl.types import Request,Capability,Approval
from avcl.ir import FieldType,compile_predicate
from avcl.contract import Clause,default_contract,build_contract

def main():
    if len(sys.argv)!=2: raise SystemExit("usage: python examples/policy_lifecycle.py OUTPUT_DIRECTORY")
    output=Path(sys.argv[1]);output.mkdir(parents=True,exist_ok=False)
    k=ReferenceKernel();remote=RefundService({"order":Order("v1","original",100)})
    k.trusted.add_account("tenant:CNY",100);k.trusted.add_account("order:order:CNY",100)
    def prepare(value,case):
        cap="cap-"+case
        k.trusted.register_capability(Capability(cap,"demo","alice",frozenset(["order"]),100,1000,k.epoch))
        request=Request("demo","alice","task",case,"order","v1","original",str(value),"0")
        k.trusted.register_evidence(remote.validate(request,"ev-"+case,1000))
        k.trusted.register_approval(Approval("ap-"+case,request.request_digest,1000))
        return k.prepare("alice",request,cap,"ev-"+case,"ap-"+case,"req-"+case)
    old=prepare(70,"case1")
    p=compile_predicate({"tag":"Le","args":[{"tag":"Field","name":"amount"},{"tag":"Const","value":60}]},
                         {"amount":FieldType("Int")},"R04")
    base=default_contract()
    clause=Clause("refund-ceiling","safety","requires","refund",("prepare","dispatch"),("R04",),(),True,p)
    new=build_contract(base.module_id,2,base.tools,base.validators,base.assumptions,base.clauses+(clause,),(("tenant:CNY",100),))
    def seal(contract):
        return k.trusted.seal_policy(contract,900,contract.requirements(),
            frozenset(c.id for c in contract.clauses if c.kind=="safety"),{"refund":{"amount":1,"currency":"CNY"}})
    envelope=seal(new)
    (output/"contract.ir.json").write_text(new.to_json(),encoding="utf-8")
    (output/"signed-release.json").write_text(envelope,encoding="utf-8")
    active=k.trusted.activate_policy(envelope)
    try: k.dispatch("alice",old.id,old.permit_ref)
    except Reject as exc: old_rejection=exc.code
    else: raise AssertionError("old policy permit admitted")
    k.abort_prepared("alice",old.id)
    op=prepare(60,"case2");op=k.dispatch("alice",op.id,op.permit_ref);k.trusted.observe(remote.send(op))
    too_low=build_contract(new.module_id,3,new.tools,new.validators,new.assumptions,new.clauses,(("tenant:CNY",59),))
    before=(k.epoch,k.policy,k.accounts(),k.events())
    try: k.trusted.activate_policy(seal(too_low))
    except Reject as exc: budget_rejection=exc.code
    else: raise AssertionError("underfunded policy activated")
    assert before==(k.epoch,k.policy,k.accounts(),k.events())
    assert old_rejection=="STALE_PERMIT" and budget_rejection=="POLICY_BUDGET_BELOW_COMMITMENT"
    assert remote.real_cost==60 and old.contract_digest in k._policy_snapshots
    k.assert_invariants()
    report={"old_contract_digest":old.contract_digest,"active_contract_digest":active,
            "old_permit_result":old_rejection,"new_refund_actual_cost":remote.real_cost,
            "budget_59_update":budget_rejection,"failed_update_left_state_unchanged":True,
            "active_epoch":k.epoch,"scope":"in-memory local HMAC authority; no durable publication guarantee"}
    (output/"activation-report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report,indent=2))

if __name__=="__main__": main()
