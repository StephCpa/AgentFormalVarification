"""Fixed refund sender profile. Call only inside the kernel transaction/lock.

The frozen permit expiry is ALSO this prototype's packet acceptance deadline.
It never cancels an already accepted effect. Retries have zero additional fees;
paid retries and production clock/network bounds require a different contract.
"""
from .canonical import Reject
from .contract import moment
from .types import Execution
from .send_envelope import seal_attempt,envelope_digest

PROFILE = "refund-send/0.1"
MAX_ATTEMPTS = 3
SAFETY_MARGIN = 1
REMOTE_WINDOW_REJECTIONS = frozenset({"REMOTE_ACCEPTANCE_CLOSED", "REMOTE_RETENTION_TOO_SHORT"})


def admit_attempt(kernel, op, **details):
    if not op.admitted:
        raise Reject("NOT_ADMITTED")
    registered=kernel._operations.get(op.id)
    if registered is None or not registered.admitted or registered.binding!=op.binding:
        raise Reject("UNTRUSTED_OPERATION")
    if kernel._binding(op) != op.binding:
        raise Reject("BINDING_MISMATCH")
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
