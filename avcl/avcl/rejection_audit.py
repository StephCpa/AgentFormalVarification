"""Bounded authenticated rejection events, written under the tenant guard.

Context is created only by trusted host code after authentication. This module
is not an identity boundary against arbitrary Python or database access.
"""
from dataclasses import dataclass
import re
from uuid import uuid4
from .canonical import Reject
from .storage_codec import dumps
from . import audit


def text(value,limit):
    return type(value) is str and 0<len(value)<=limit and all(ord(c)>=32 and not 0xD800<=ord(c)<=0xDFFF for c in value)


@dataclass(frozen=True)
class AuthenticatedContext:
    actor: str
    action: str
    request_digest: str
    trace_id: str
    operation_ref: str|None=None

    def validate(self):
        if (not text(self.actor,256) or not text(self.action,64) or not text(self.trace_id,128) or
            type(self.request_digest) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}",self.request_digest) is None or
            (self.operation_ref is not None and not text(self.operation_ref,256))):
            raise Reject("INVALID_REJECTION_CONTEXT")
        return self


def configure(db,conn,k,total_limit,subject_limit,window_seconds):
    if (type(total_limit) is not int or not 1<=total_limit<=1000000 or
        type(subject_limit) is not int or not 1<=subject_limit<=1000000 or
        type(window_seconds) is not int or not 1<=window_seconds<=86400):
        raise Reject("INVALID_AUDIT_POLICY")
    row=conn.execute("SELECT recorded FROM rejection_audit_policy WHERE tenant_id=%s",(k.tenant,)).fetchone()
    if row is None or total_limit<row["recorded"]:raise Reject("INVALID_AUDIT_POLICY")
    conn.execute("UPDATE rejection_audit_policy SET total_limit=%s,subject_limit=%s,window_seconds=%s WHERE tenant_id=%s",
                 (total_limit,subject_limit,window_seconds,k.tenant))
    k._event("RejectionAuditConfigured",total_limit=total_limit,subject_limit=subject_limit,
             window_seconds=window_seconds,requirement_ids=("R14",))


def record(db,conn,k,context,error):
    """Baseline k must be freshly loaded after business savepoint rollback."""
    policy=conn.execute("SELECT * FROM rejection_audit_policy WHERE tenant_id=%s",(k.tenant,)).fetchone()
    if policy is None:raise Reject("AUDIT_UNAVAILABLE")
    row=conn.execute("SELECT * FROM rejection_audit_subject WHERE tenant_id=%s AND subject=%s",
                     (k.tenant,context.actor)).fetchone()
    now=k._clock();start=now;count=0
    if row is not None and now<row["window_start"]+policy["window_seconds"]:
        start=row["window_start"];count=row["recorded"]
    if policy["recorded"]>=policy["total_limit"] or count>=policy["subject_limit"]:
        raise Reject("AUDIT_CAPACITY_EXCEEDED")
    op=k._operations.get(context.operation_ref)
    if op is not None and op.request.subject!=context.actor:op=None
    event_id="event-"+uuid4().hex
    k._event("ActionRejected",op.id if op else None,event_id=event_id,actor=context.actor,
             action=context.action,request_digest=context.request_digest,trace_id=context.trace_id,
             operation_revision=str(op.revision) if op else "null",
             policy_digest=k._release.compiled.contract.semantic_digest,epoch=k.epoch,
             code=error.code,requirement_ids=error.requirement_ids,time=now,
             budget_deltas="{}",causal_parents="[]",evidence_refs="[]")
    event=k._events[-1];payload=dumps(event)
    if len(payload.encode("utf-8"))>16384:raise Reject("AUDIT_RECORD_TOO_LARGE")
    audit.persist(conn,k)
    conn.execute("UPDATE rejection_audit_policy SET recorded=recorded+1 WHERE tenant_id=%s",(k.tenant,))
    db._upsert(conn,"rejection_audit_subject",("tenant_id","subject"),
               {"tenant_id":k.tenant,"subject":context.actor,"window_start":start,"recorded":count+1})
    return event_id,event.sequence
