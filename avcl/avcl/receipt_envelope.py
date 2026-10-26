"""Authenticated observations, independent of send authorization and deadlines."""
import hashlib
import hmac
import re
from .canonical import Reject,canonical_bytes,strict_loads,amount,cumulative_cost,digest
from .contract import moment
from .types import Observation,Execution
from .send_envelope import RECEIVER

FORMAT="avcl/receipt-envelope/0.1"
KEY_ID="local-receipt-v1"
KEY_DOMAIN=b"avcl/receipt-key/0.1\x00"
MAC_DOMAIN=b"avcl/receipt-authentication/0.1\x00"
RAW_RECORD="avcl/observation-input/0.1"
FIELDS=set("format key_id tenant issuer operation_id request_digest binding observed_at observation_id kind cumulative_costs final_cost terminal_effect signature".split())


def key_for(kernel):
    return hmac.new(kernel._policy_authority._key,KEY_DOMAIN+canonical_bytes(
        {"tenant":kernel.tenant,"receiver":RECEIVER}),hashlib.sha256).digest()


def required(kernel,operation_id):
    return any(e.kind=="AuthenticatedReceiptRequired" and e.operation_id==operation_id for e in kernel._events)


def require(kernel,operation_id):
    kernel._lookup(kernel._operations,operation_id)
    if not required(kernel,operation_id):kernel._event("AuthenticatedReceiptRequired",operation_id,requirement_id="R02")


def rejected(kernel,expected_operation_id,code,raw_digest=None):
    kernel._quarantined=True
    op=kernel._operations.get(expected_operation_id)
    if op is not None and op.state==Execution.DISPATCHED:kernel.trusted.unknown(op.id)
    kernel._event("ReceiptAuthenticationRejected",expected_operation_id,reason=code,
                  raw_digest=raw_digest or "unavailable",requirement_id="R14")


class ReceiptSigner:
    def __init__(self,key,tenant,issuer=RECEIVER):
        if type(key) is not bytes or len(key)!=32 or any(type(v) is not str or not v or len(v)>256 for v in (tenant,issuer)):
            raise Reject("INVALID_RECEIPT_SIGNER")
        self._key=key;self.tenant=tenant;self.issuer=issuer

    def seal(self,observation,binding,observed_at):
        if type(observation) is not Observation or not moment(observed_at):raise Reject("INVALID_RECEIPT_SOURCE")
        costs=dict(observation.cumulative_costs)
        if len(costs)!=len(observation.cumulative_costs):raise Reject("INVALID_COST_VECTOR")
        if any(type(v) is not int for v in costs.values()):raise Reject("INVALID_CUMULATIVE_COST")
        for value in costs.values():cumulative_cost(str(value))
        body={"format":FORMAT,"key_id":KEY_ID,"tenant":self.tenant,"issuer":self.issuer,
              "operation_id":observation.operation_id,"request_digest":observation.request_digest,
              "binding":binding,"observed_at":str(observed_at),"observation_id":observation.id,
              "kind":str(observation.kind),"cumulative_costs":{k:str(v) for k,v in costs.items()},
              "final_cost":observation.final_cost,"terminal_effect":observation.terminal_effect}
        signature=hmac.new(self._key,MAC_DOMAIN+canonical_bytes(body),hashlib.sha256).hexdigest()
        return canonical_bytes({**body,"signature":signature}).decode("utf-8")


def verify(kernel,raw,expected_operation_id=None):
    obj=strict_loads(raw)
    if type(obj) is not dict or set(obj)!=FIELDS:raise Reject("INVALID_RECEIPT_ENVELOPE")
    signature=obj["signature"]
    if type(signature) is not str or re.fullmatch(r"[0-9a-f]{64}",signature) is None:raise Reject("INVALID_RECEIPT_AUTHENTICATION")
    body={k:v for k,v in obj.items() if k!="signature"}
    expected=hmac.new(key_for(kernel),MAC_DOMAIN+canonical_bytes(body),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature,expected):raise Reject("INVALID_RECEIPT_AUTHENTICATION")
    if obj["format"]!=FORMAT or obj["key_id"]!=KEY_ID or obj["tenant"]!=kernel.tenant or obj["issuer"]!=RECEIVER:
        raise Reject("RECEIPT_AUTHORITY_SCOPE_MISMATCH")
    if expected_operation_id is not None and obj["operation_id"]!=expected_operation_id:raise Reject("UNEXPECTED_ADAPTER_OBSERVATION")
    op=kernel._lookup(kernel._operations,obj["operation_id"])
    if not op.admitted or obj["binding"]!=op.binding or obj["request_digest"]!=op.request.request_digest:
        raise Reject("INVALID_OBSERVATION_BINDING")
    observed=amount(obj["observed_at"])
    if not moment(kernel._clock()) or observed>kernel._clock():raise Reject("RECEIPT_FROM_FUTURE")
    # No receive-time expiry: an old authenticated final fact remains evidence.
    if type(obj["observation_id"]) is not str or not 0<len(obj["observation_id"])<=256:
        raise Reject("INVALID_RECEIPT_ENVELOPE")
    if type(obj["final_cost"]) is not bool or type(obj["terminal_effect"]) is not bool:raise Reject("INVALID_RECEIPT_ENVELOPE")
    try:kind=Execution(obj["kind"])
    except (ValueError,TypeError):raise Reject("INVALID_RECEIPT_ENVELOPE") from None
    if kind not in (Execution.CONFIRMED,Execution.REJECTED,Execution.PARTIAL):raise Reject("INVALID_RECEIPT_ENVELOPE")
    costs=obj["cumulative_costs"]
    if type(costs) is not dict or set(costs)!={"tenant:CNY","order:"+op.request.order+":CNY"}:raise Reject("INVALID_COST_VECTOR")
    observation=Observation(obj["observation_id"],op.id,op.request.request_digest,kind,
        tuple((account,cumulative_cost(value)) for account,value in sorted(costs.items())),obj["final_cost"],obj["terminal_effect"])
    require(kernel,op.id)
    kernel._event("ReceiptAuthenticated",op.id,envelope_digest=digest(obj,"avcl/receipt-envelope-digest/0.1"),
                  observation=observation.id,observed_at=observed,requirement_id="R02")
    return observation
