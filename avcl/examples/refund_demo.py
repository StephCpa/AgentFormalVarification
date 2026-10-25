"""A receipt is lost after a real effect; reservation prevents a second spend."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from avcl.canonical import Reject
from avcl.runtime import ReferenceKernel
from avcl.simulator import RefundService,Order
from avcl.types import Request,Capability,Approval


def main():
    kernel=ReferenceKernel()
    service=RefundService({name:Order("v1","original-"+name,100) for name in ("a","b")})
    kernel.trusted.add_account("tenant:CNY",100)
    for name in ("a","b"):
        kernel.trusted.add_account("order:"+name+":CNY",100)
        kernel.trusted.register_capability(Capability("cap-"+name,"demo",name,frozenset([name]),100,1000,1))
    def prepare(name,value):
        req=Request("demo",name,"task","case-"+name,name,"v1","original-"+name,str(value),"0")
        kernel.trusted.register_evidence(service.validate(req,"ev-"+name,1000))
        kernel.trusted.register_approval(Approval("ap-"+name,req.request_digest,1000))
        return kernel.prepare(name,req,"cap-"+name,"ev-"+name,"ap-"+name,"request-"+name)
    op=prepare("a",70)
    op=kernel.dispatch("a",op.id,op.permit_ref)
    service.send(op)  # The effect happened; its receipt was lost.
    kernel.trusted.unknown(op.id)
    before=kernel.accounts()["tenant:CNY"]
    try:
        prepare("b",60)
    except Reject as exc:
        rejected=exc.code
    else:
        raise AssertionError("second spend must not be authorized")
    kernel.trusted.observe(service.send(op))  # Same stable operation id.
    after=kernel.accounts()["tenant:CNY"]
    kernel.assert_invariants()
    assert (before.spent,before.pending,rejected,after.spent,after.pending,len(service.effects)) == (0,70,"BUDGET_EXCEEDED",70,0,1)
    print(json.dumps({"scenario":"lost_receipt_after_remote_effect","budget":100,
                      "unknown":{"spent":before.spent,"pending":before.pending,"actual":service.real_cost},
                      "second_request_60":rejected,"after_reconcile":{"spent":after.spent,"pending":after.pending},
                      "remote_effect_count":len(service.effects)},indent=2))


if __name__=="__main__": main()
