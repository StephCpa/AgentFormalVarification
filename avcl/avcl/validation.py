"""Pure refund validation over host-registered snapshots; no tool calls or grants.

Only the fixed order-service / RefundEligible / sim/0.1 implementation is
available. The active contract must also register that exact validator tuple.
Snapshot authenticity is a host integration obligation, not inferred from JSON.
"""
import secrets
from .canonical import Reject,amount,canonical_bytes,digest,strict_loads
from .types import Request,Evidence
from .simulator import Order
from .storage_codec import dumps

VALIDATOR="order-service"
VERSION="sim/0.1"
PREDICATE="RefundEligible"
CANDIDATE_FIELDS=frozenset(Request.__dataclass_fields__)-{"tenant","subject"}

def valid_text(value):
    return type(value) is str and 0<len(value)<=256 and all(ord(c)>=32 and not 0xD800<=ord(c)<=0xDFFF for c in value)

def validator(k,identifier,version=VERSION):
    if identifier!=VALIDATOR or version!=VERSION or not any(
        v.id==identifier and v.version==version and v.predicate_id==PREDICATE
        for v in k._release.compiled.contract.validators):raise Reject("UNREGISTERED_VALIDATOR")

def context(k,c,subject,ref):
    row=c.execute("SELECT s.*,h.snapshot_id AS current_ref FROM order_snapshot s JOIN order_context_head h USING(tenant_id,order_id) WHERE s.tenant_id=%s AND s.id=%s AND s.subject=%s",
                  (k.tenant,ref,subject)).fetchone()
    if row is None:raise Reject("AUTH_DENIED")
    if row["current_ref"]!=ref or not row["issued_at"]<=k._clock()<row["expires"]:raise Reject("STALE_EVIDENCE")
    return row

class ValidationAuthority:
    """Host-only snapshot ingestion. Never exposed as an HTTP route."""
    def __init__(self,kernel):self.db=kernel

    def register_order_snapshot(self,subject,order_id,order,fee,expires):
        if (not valid_text(subject) or not valid_text(order_id) or type(order) is not Order or
            not valid_text(order.version) or not valid_text(order.destination) or
            type(order.remaining) is not int or type(fee) is not int or type(expires) is not int):raise Reject("INVALID_CONTEXT")
        amount(str(order.remaining));amount(str(fee))
        ref="context-"+secrets.token_hex(32)
        payload=canonical_bytes({"version":order.version,"destination":order.destination,"remaining":str(order.remaining),"fee":str(fee)}).decode()
        def register(k,c):
            if k._clock_rollback:raise Reject("CLOCK_ROLLBACK")
            if expires<=k._clock():raise Reject("INVALID_CONTEXT")
            c.execute("INSERT INTO order_snapshot VALUES(%s,%s,%s,%s,%s,%s,%s)",(k.tenant,ref,subject,order_id,payload,k._clock(),expires))
            c.execute("INSERT INTO order_context_head VALUES(%s,%s,%s) ON CONFLICT(tenant_id,order_id) DO UPDATE SET snapshot_id=EXCLUDED.snapshot_id",(k.tenant,order_id,ref))
            k._event("OrderContextRegistered",context_ref=ref,subject=subject,order=order_id,requirement_id="R02")
        self.db._transaction("context-register",register)
        return ref

    def revoke_order_context(self,order_id):
        if not valid_text(order_id):raise Reject("INVALID_CONTEXT")
        def revoke(k,c):
            row=c.execute("UPDATE order_context_head SET snapshot_id=NULL WHERE tenant_id=%s AND order_id=%s RETURNING order_id",(k.tenant,order_id)).fetchone()
            if row is None:raise Reject("UNTRUSTED_REFERENCE")
            k._event("OrderContextRevoked",order=order_id,requirement_id="R02")
        self.db._transaction("context-revoke",revoke)

def check_evidence(k,c,evidence_ref):
    """Legacy host evidence has no snapshot binding; public validation always does."""
    row=c.execute("SELECT * FROM candidate_validation WHERE tenant_id=%s AND evidence_ref=%s",(k.tenant,evidence_ref)).fetchone()
    if row is None:return
    validator(k,row["validator_id"],row["validator_version"])
    context(k,c,row["subject"],row["context_ref"])
    if not row["issued_at"]<=k._clock()<row["expires"]:raise Reject("STALE_EVIDENCE")

def validate_candidate(k,c,subject,body,trace_id=None):
    if set(body)!={"client_request_id","candidate","validator_id","context_refs"}:raise Reject("INVALID_REQUEST")
    candidate=body["candidate"];refs=body["context_refs"];request_id=body["client_request_id"]
    if (not valid_text(request_id) or len(request_id)>128 or not valid_text(body["validator_id"]) or
        type(candidate) is not dict or set(candidate)!=CANDIDATE_FIELDS or
        any(not valid_text(v) for v in candidate.values()) or
        type(refs) is not list or len(refs)!=1 or not valid_text(refs[0])):raise Reject("INVALID_REQUEST")
    request=Request(tenant=k.tenant,subject=subject,**candidate);request.validate()
    validator(k,body["validator_id"])
    submitted=digest(body,"avcl/candidate-validation/0.1")
    previous=c.execute("SELECT * FROM candidate_validation WHERE tenant_id=%s AND subject=%s AND request_id=%s",(k.tenant,subject,request_id)).fetchone()
    if previous:
        if previous["input_digest"]!=submitted:raise Reject("IDEMPOTENCY_CONFLICT")
        check_evidence(k,c,previous["evidence_ref"])
        return project(previous)
    snapshot=context(k,c,subject,refs[0]);facts=strict_loads(snapshot["payload"])
    if (snapshot["order_id"]!=request.order or facts["version"]!=request.order_version or
        facts["destination"]!=request.destination or amount(request.amount_minor)>amount(facts["remaining"]) or
        amount(request.max_fee_minor)<amount(facts["fee"])):raise Reject("INELIGIBLE")
    now=k._clock();expires=min(snapshot["expires"],k._release.expires,now+300)
    if not k._release.issued_at<=now<expires:raise Reject("STALE_EVIDENCE")
    checked_ref="checked-"+secrets.token_hex(32);evidence_ref="evidence-"+secrets.token_hex(32)
    k.trusted.register_evidence(Evidence(evidence_ref,request.request_digest,VALIDATOR,PREDICATE,expires))
    c.execute("INSERT INTO checked_request VALUES(%s,%s,%s,%s,%s,%s)",(k.tenant,checked_ref,subject,dumps(request),evidence_ref,expires))
    row={"tenant_id":k.tenant,"subject":subject,"request_id":request_id,"input_digest":submitted,
         "checked_ref":checked_ref,"evidence_ref":evidence_ref,"context_ref":refs[0],
         "validator_id":VALIDATOR,"validator_version":VERSION,"issued_at":now,"expires":expires}
    c.execute("INSERT INTO candidate_validation VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",tuple(row.values()))
    k._event("CandidateValidated",checked_ref=checked_ref,evidence_ref=evidence_ref,context_ref=refs[0],
             request_digest=request.request_digest,subject=subject,request_id=request_id,requirement_id="R02",
             **({"trace_id":trace_id} if trace_id is not None else {}))
    return project(row)

def project(row):
    return {"checked_ref":row["checked_ref"],"evidence_refs":[row["evidence_ref"]],"predicate_id":PREDICATE,
            "validator_id":row["validator_id"],"validator_version":row["validator_version"],
            "status":"RuntimeSatisfied","scope":"registered order snapshot only; no authorization",
            "not_after_logical":str(row["expires"])}
