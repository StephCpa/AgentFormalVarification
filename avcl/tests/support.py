from avcl.runtime import ReferenceKernel
from avcl.types import Request, Capability, Approval
from avcl.simulator import RefundService, Order


class Clock:
    def __init__(self): self.now = 0
    def __call__(self): return self.now


def setup(limit=100, order_limit=100, fee=0):
    clock = Clock()
    k = ReferenceKernel(clock=clock)
    k.trusted.add_account("tenant:CNY", limit)
    service = RefundService({"a": Order("v1", "original-a", order_limit),
                             "b": Order("v1", "original-b", order_limit)}, fee=fee)
    for name in ("a", "b"):
        k.trusted.add_account("order:" + name + ":CNY", order_limit)
        k.trusted.register_capability(Capability("cap-" + name, "demo", name,
            frozenset({name}), 10000, 1000, k.epoch))
    return k, service, clock


def authorize(k, service, name="a", value=70, fee=0, case=None):
    case = case or "case-" + name
    request = Request("demo", name, "task-" + name, case, name, "v1", "original-" + name, str(value), str(fee))
    ev = service.validate(request, "ev-" + case, 1000)
    k.trusted.register_evidence(ev)
    k.trusted.register_approval(Approval("ap-" + case, request.request_digest, 1000))
    return request, "cap-" + name, ev.ref, "ap-" + case


def prepare(k, authorization, request_id="req", **kwargs):
    request, cap, evidence, approval = authorization
    return k.prepare(request.subject, request, cap, evidence, approval, request_id, **kwargs)
