"""Admission capacity reserves future reconciliation slots before new effects.

This bounds admitted active work, not total retained history or reality-driven
incident records. Existing reconciliation and settlement are never evicted.
"""
from .canonical import Reject
from .types import Settlement

DEFAULT_LIMIT=1000

def snapshot(db,c,k):
    policy=c.execute("SELECT max_operations,max_obligations,max_workflows FROM capacity_policy WHERE tenant_id=%s",(db.tenant,)).fetchone()
    if policy is None:raise Reject("MISSING_CAPACITY_POLICY")
    active={op.id for op in k._operations.values() if op.settlement!=Settlement.FINAL}
    obligations={r["operation_id"] for r in c.execute("SELECT operation_id FROM obligation WHERE tenant_id=%s AND resolution IS NULL",(db.tenant,))}
    workflows=c.execute("SELECT count(*) AS n FROM workflow_run WHERE tenant_id=%s AND status IN ('Running','WaitingIO','WaitingCapacity')",(db.tenant,)).fetchone()["n"]
    usage={"operations":len(active),"obligation_slots":len(active|obligations),"open_obligations":len(obligations),"workflows":workflows}
    return {"limits":dict(policy),"usage":usage,
        "operation_headroom":max(0,min(policy["max_operations"]-len(active),policy["max_obligations"]-len(active|obligations))),
        "workflow_headroom":max(0,policy["max_workflows"]-workflows),
        "over_capacity":len(active)>policy["max_operations"] or len(active|obligations)>policy["max_obligations"] or workflows>policy["max_workflows"]}

def check_new_operation(db,c,k):
    report=snapshot(db,c,k)
    if report["operation_headroom"]==0:raise Reject("OPERATION_CAPACITY_EXCEEDED")

def check_new_workflow(db,c,k):
    if snapshot(db,c,k)["workflow_headroom"]==0:raise Reject("WORKFLOW_CAPACITY_EXCEEDED")

def configure(db,c,k,operations,obligations,workflows):
    if any(type(v) is not int or not 0<=v<=1000000 for v in (operations,obligations,workflows)):raise Reject("INVALID_CAPACITY_POLICY")
    usage=snapshot(db,c,k)["usage"]
    if operations<usage["operations"] or obligations<usage["obligation_slots"] or workflows<usage["workflows"]:raise Reject("CAPACITY_BELOW_COMMITMENT")
    c.execute("UPDATE capacity_policy SET max_operations=%s,max_obligations=%s,max_workflows=%s WHERE tenant_id=%s",(operations,obligations,workflows,db.tenant))
    k._event("CapacityPolicyChanged",operations=operations,obligations=obligations,workflows=workflows,requirement_id="R10")
    return snapshot(db,c,k)
