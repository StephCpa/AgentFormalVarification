"""Fixed refund sender profile. Call only inside the kernel transaction/lock.

The frozen permit expiry is ALSO this prototype's packet acceptance deadline.
It never cancels an already accepted effect. Retries have zero additional fees;
paid retries and production clock/network bounds require a different contract.
"""
from contextlib import contextmanager
from .canonical import Reject
from .contract import moment
from .types import Execution
from .send_envelope import seal_attempt,envelope_digest

PROFILE = "refund-send/0.1"
MAX_ATTEMPTS = 3
SAFETY_MARGIN = 1
REMOTE_WINDOW_REJECTIONS = frozenset({"REMOTE_ACCEPTANCE_CLOSED", "REMOTE_RETENTION_TOO_SHORT"})


def bind_workflow(kernel, op, subject, run_id, workflow_digest):
    """Host interpreter only: bind during Prepare, never implicitly transfer."""
    with kernel._lock:
        if op.id in kernel._send_owners:raise Reject("WORKFLOW_PERMIT_NOT_FRESH")
        kernel._send_owners[op.id]=(subject,run_id,workflow_digest)


@contextmanager
def workflow_send_context(kernel, op, subject, run_id, workflow_digest, node_id):
    """Interpreter enters only after resolving its current executable Send node.

    The durable interpreter also commits workflow_io and send_attempt in this
    transaction. This is host mediation, not a sandbox against arbitrary Python.
    """
    with kernel._lock:
        if kernel._send_owners.get(op.id)!=(subject,run_id,workflow_digest):
            raise Reject("WORKFLOW_SEND_NOT_AUTHORIZED")
        if kernel._workflow_send_context is not None:raise Reject("NESTED_SEND_CONTEXT")
        kernel._workflow_send_context=(op.id,subject,run_id,workflow_digest,node_id)
        try:yield
        finally:kernel._workflow_send_context=None


def admit_attempt(kernel, op, **details):
    if not op.admitted:
        raise Reject("NOT_ADMITTED")
    registered=kernel._operations.get(op.id)
    if registered is None or not registered.admitted or registered.binding!=op.binding:
        raise Reject("UNTRUSTED_OPERATION")
    if kernel._binding(op) != op.binding:
        raise Reject("BINDING_MISMATCH")
    owner=kernel._send_owners.get(op.id)
    if owner is not None:
        context=kernel._workflow_send_context
        if context is None or context[:4]!=(op.id,*owner):
            raise Reject("WORKFLOW_SEND_NOT_AUTHORIZED")
        details={**details,"send_owner":"workflow","subject":owner[0],"run_id":owner[1],
                 "workflow_digest":owner[2],"node_id":context[4],"binding":op.binding}
    else:
        details={**details,"send_owner":"direct","binding":op.binding}
    now = kernel._clock()
    if not moment(now):
        raise Reject("INVALID_CLOCK")
    attempts = [e for e in kernel._events if e.kind == "SendAttemptAdmitted" and e.operation_id == op.id]
    times = [int(dict(e.details)["at"]) for e in kernel._events
             if e.kind in {"SendAttemptAdmitted", "SendBlocked"} and "at" in dict(e.details)]
    if times and now < max(times):
        raise Reject("CLOCK_ROLLBACK")
    reason = None
    if now + SAFETY_MARGIN >= op.expires:
        reason = "SEND_WINDOW_CLOSED"
    elif len(attempts) >= MAX_ATTEMPTS:
        reason = "SEND_ATTEMPTS_EXHAUSTED"
    if reason:
        if op.state == Execution.DISPATCHED:
            kernel.trusted.unknown(op.id)
        kernel._event("SendBlocked", op.id, reason=reason, at=now, not_after=op.expires,
                      profile=PROFILE, requirement_id="R05")
        return False
    envelope=seal_attempt(kernel,op,now,len(attempts)+1,PROFILE)
    kernel._event("SendAttemptAdmitted", op.id, at=now, not_after=op.expires,
                  attempt=len(attempts)+1, profile=PROFILE,
                  envelope_digest=envelope_digest(envelope), **details)
    return envelope
