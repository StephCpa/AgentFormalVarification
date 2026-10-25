"""Explicit task close, frozen host scope, and fact-driven completion projection.

Closing never cancels an operation, releases money, or removes an obligation.
Historical close receipts remain immutable when later facts change the status.
"""
from uuid import uuid4
from .canonical import Reject,canonical_bytes,strict_loads,amount
from .ir import FieldType
from .types import Execution,Settlement
from .supervision import sync_obligations

TERMINAL={"Completed","Rejected","Escalated","Failed"}
CONTEXT={name:FieldType("Bool") for name in ("confirmed","rejected","partial","aborted","settled")}
CONTEXT.update({name:FieldType("Int") for name in ("amount","max_fee","charged_principal","charged_total")})
CONTEXT["currency"]=FieldType("String")

def text(value,limit=256):
    return type(value) is str and 0<len(value)<=limit and all(ord(c)>=32 and not 0xD800<=ord(c)<=0xDFFF for c in value)

def encode(value):return canonical_bytes(value).decode()

def task_operations(k,row):
    return sorted((op for op in k._operations.values() if op.request.subject==row["subject"] and op.request.task==row["task_key"]),key=lambda op:op.id)

def admission(k,c,request):
    row=c.execute("SELECT * FROM task_run WHERE tenant_id=%s AND subject=%s AND task_key=%s",(k.tenant,request.subject,request.task)).fetchone()
    if row is None:return  # Legacy operations do not acquire a completion guarantee.
    if row["sealed"]:raise Reject("TASK_CLOSED")
    if request.business_key not in strict_loads(row["expected_keys"]):raise Reject("TASK_SCOPE_MISMATCH")

def evaluate(k,c,row):
    ops=task_operations(k,row);expected=set(strict_loads(row["expected_keys"]))
    matched={op.request.business_key for op in ops};missing=sorted(expected-matched)
    obligations={r["operation_id"]:r for r in c.execute("SELECT * FROM obligation WHERE tenant_id=%s",(k.tenant,)) if r["operation_id"] in {op.id for op in ops}}
    reports=[];outcomes=[]
    for op in ops:
        costs={r.account:r.charged for r in op.reservations}
        values={"confirmed":op.state==Execution.CONFIRMED,"rejected":op.state==Execution.REJECTED,
                "partial":op.state==Execution.PARTIAL,"aborted":op.state==Execution.ABORTED,
                "settled":op.settlement==Settlement.FINAL,"amount":amount(op.request.amount_minor),
                "max_fee":amount(op.request.max_fee_minor),"charged_principal":costs.get("order:"+op.request.order+":CNY",0),
                "charged_total":costs.get("tenant:CNY",0),"currency":op.request.currency}
        completion=k._policy_snapshots[row["contract_digest"]].compiled.evaluate("close",op.request.tool,values,k._health,k._clock())
        result="RuntimeSatisfied" if completion["allowed"] else "Violated" if any(r["status"]=="Violated" for r in completion["reports"]) else "Unknown"
        outcomes.append(result)
        reports.append({"operation_id":op.id,"revision":str(op.revision),"execution_state":str(op.state),
                        "settlement_state":str(op.settlement),"completion_status":result,
                        "failed_clause_count":str(sum(not r["passed"] for r in completion["reports"]))})
    unresolved=[op for op in ops if op.settlement!=Settlement.FINAL or op.state not in {Execution.CONFIRMED,Execution.REJECTED,Execution.PARTIAL,Execution.ABORTED}]
    open_obligations=[r for r in obligations.values() if r["resolution"] is None]
    breached=[r for r in obligations.values() if r["breached_at"] is not None]
    handoffs={r["operation_id"] for r in obligations.values() if r["resolution"]=="Escalated" and c.execute(
        "SELECT 1 FROM handoff_receipt WHERE tenant_id=%s AND operation_id=%s AND receiver=%s AND receipt_id=%s",
        (k.tenant,r["operation_id"],r["owner"],r["evidence_ref"])).fetchone() is not None}
    reason="close_not_requested";status="Running"
    if row["close_requested"]:
        if any(op.settlement==Settlement.DISPUTED for op in ops):status,reason="Failed","disputed_effect_or_cost"
        elif breached:status,reason="Failed","obligation_breach"
        elif matched-expected:status,reason="Failed","unexpected_business_action"
        elif missing or not ops:status,reason="Pending","missing_expected_action"
        elif unresolved or open_obligations:
            if unresolved and all(op.id in handoffs for op in unresolved) and not open_obligations:status,reason="Escalated","verified_responsibility_transfer"
            else:status,reason="Pending","unresolved_effect_cost_or_obligation"
        elif all(op.state in {Execution.REJECTED,Execution.ABORTED} for op in ops):status,reason="Rejected","no_business_effect"
        elif any(op.state!=Execution.CONFIRMED for op in ops):status,reason="Failed","business_effect_incomplete"
        elif any(outcome=="Violated" for outcome in outcomes):status,reason="Failed","completion_clause_violated"
        elif any(outcome!="RuntimeSatisfied" for outcome in outcomes):status,reason="Pending","completion_not_established"
        elif any(r.remaining for op in ops for r in op.reservations):status,reason="Pending","remaining_reservation"
        else:status,reason="Completed","all_completion_requirements_satisfied"
    return {"status":status,"reason":reason,"expected_count":str(len(expected)),"matched_count":str(len(matched)),
            "missing_business_keys":missing,"operations":reports,"open_obligation_count":str(len(open_obligations)),
            "breached_obligation_count":str(len(breached)),"verified_handoff_count":str(len(handoffs))}

def refresh(db,c,k,row):
    report=evaluate(k,c,row);payload=encode(report)
    sealed=row["sealed"] or report["status"] in TERMINAL
    if payload!=row["report"] or sealed!=row["sealed"]:
        c.execute("UPDATE task_run SET status=%s,report=%s,sealed=%s,revision=revision+1 WHERE tenant_id=%s AND id=%s",
                  (report["status"],payload,sealed,k.tenant,row["id"]))
        k._event("TaskAssessmentChanged",task_ref=row["id"],previous=row["status"],status=report["status"],
                 revision=row["revision"]+1,contract_digest=row["contract_digest"],requirement_id="R10")
        row={**row,"status":report["status"],"report":payload,"sealed":sealed,"revision":row["revision"]+1}
    return row

def sync(db,c,k):
    for row in c.execute("SELECT * FROM task_run WHERE tenant_id=%s",(k.tenant,)).fetchall():refresh(db,c,k,row)

def owned(k,c,subject,task_ref):
    row=c.execute("SELECT * FROM task_run WHERE tenant_id=%s AND subject=%s AND id=%s",(k.tenant,subject,task_ref)).fetchone()
    if row is None:raise Reject("TASK_NOT_FOUND")
    return row

def project(row):
    return {"task_ref":row["id"],"task_key":row["task_key"],"contract_digest":row["contract_digest"],
            "revision":str(row["revision"]),"close_requested":row["close_requested"],"sealed":row["sealed"],**strict_loads(row["report"])}

def close(db,k,c,subject,task_ref,request_id,trace_id=None):
    if not text(request_id,128):raise Reject("INVALID_REQUEST_ID")
    row=owned(k,c,subject,task_ref)
    prior=c.execute("SELECT 1 FROM task_close_request WHERE tenant_id=%s AND subject=%s AND task_id=%s AND request_id=%s",
                    (k.tenant,subject,task_ref,request_id)).fetchone()
    if not row["close_requested"]:
        c.execute("UPDATE task_run SET close_requested=true WHERE tenant_id=%s AND id=%s",(k.tenant,task_ref))
        row={**row,"close_requested":True}
    sync_obligations(db,c,k);row=refresh(db,c,k,row);result=project(row)
    if prior is None:
        c.execute("INSERT INTO task_close_request VALUES(%s,%s,%s,%s,%s)",(k.tenant,subject,task_ref,request_id,encode(result)))
        k._event("TaskCloseRequested",task_ref=task_ref,request_id=request_id,status=row["status"],
                 **({"trace_id":trace_id} if trace_id is not None else {}))
    return result

class TaskAuthority:
    def __init__(self,kernel):self.db=kernel

    def register(self,subject,task_key,expected_business_keys):
        if (not text(subject) or not text(task_key) or type(expected_business_keys) is not frozenset or
            not 1<=len(expected_business_keys)<=32 or any(not text(v) for v in expected_business_keys)):raise Reject("INVALID_TASK_SCOPE")
        keys=encode(sorted(expected_business_keys))
        def register(k,c):
            prior=c.execute("SELECT * FROM task_run WHERE tenant_id=%s AND subject=%s AND task_key=%s",(k.tenant,subject,task_key)).fetchone()
            contract=k._release.compiled.contract
            if prior:
                if prior["expected_keys"]!=keys or prior["contract_digest"]!=contract.semantic_digest:raise Reject("TASK_SCOPE_CONFLICT")
                return prior["id"]
            completion=[clause for clause in contract.clauses if clause.kind=="completion" and clause.tool=="refund"]
            if not 1<=len(completion)<=16:raise Reject("MISSING_OR_UNSUPPORTED_COMPLETION")
            if any(CONTEXT.get(name)!=t for clause in completion for name,t in clause.predicate.fields):raise Reject("UNSUPPORTED_COMPLETION_CONTEXT")
            if any(op.request.subject==subject and op.request.task==task_key for op in k._operations.values()):raise Reject("TASK_ALREADY_STARTED")
            ref="task-"+uuid4().hex
            c.execute("INSERT INTO task_run(tenant_id,subject,id,task_key,contract_digest,expected_keys,report) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                      (k.tenant,subject,ref,task_key,contract.semantic_digest,keys,"{}"))
            k._event("TaskRegistered",task_ref=ref,subject=subject,task_key=task_key,contract_digest=contract.semantic_digest)
            return ref
        return self.db._transaction("task-register",register)

class PostgresTasks:
    def __init__(self,kernel):self.db=kernel
    def close(self,subject,task_ref,request_id):
        return self.db._transaction("task-close",lambda k,c:close(self.db,k,c,subject,task_ref,request_id))
    def get(self,subject,task_ref):
        def get(k,c):
            row=owned(k,c,subject,task_ref)
            sync_obligations(self.db,c,k)
            return project(refresh(self.db,c,k,row))
        return self.db._transaction("task-get",get)
