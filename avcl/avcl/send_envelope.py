"""Authenticated local refund send envelopes; not production key isolation.

The receiver gets a purpose/tenant/receiver-derived HMAC key, never the policy
authority key. Key provisioning is host-only. HMAC is shared-key authentication,
not a public signature or non-repudiation mechanism.
"""
from dataclasses import dataclass,fields
import hashlib
import hmac
import re

from .canonical import Reject,canonical_bytes,digest,strict_loads,amount
from .contract import moment
from .types import Request

FORMAT="avcl/send-envelope/0.1"
RECEIVER="refund-simulator"
KEY_ID="local-sender-v1"
SIGN_DOMAIN=b"avcl/send-authentication/0.1\x00"
KEY_DOMAIN=b"avcl/send-key/0.1\x00"
FIELDS=frozenset("format action profile key_id tenant receiver operation_id request request_digest binding contract_digest epoch issued_at not_after attempt signature".split())


def _key(kernel):
    return hmac.new(kernel._policy_authority._key,KEY_DOMAIN+canonical_bytes(
        {"tenant":kernel.tenant,"receiver":RECEIVER}),hashlib.sha256).digest()


def seal_attempt(kernel,op,now,attempt,profile):
    """Internal transaction helper; only sending.admit_attempt may issue traffic."""
    body={"format":FORMAT,"action":"send","profile":profile,"key_id":KEY_ID,
          "tenant":kernel.tenant,"receiver":RECEIVER,"operation_id":op.id,
          "request":op.request.payload(),"request_digest":op.request.request_digest,
          "binding":op.binding,"contract_digest":op.contract_digest,"epoch":str(op.epoch),
          "issued_at":str(now),"not_after":str(op.expires),"attempt":str(attempt)}
    signature=hmac.new(_key(kernel),SIGN_DOMAIN+canonical_bytes(body),hashlib.sha256).hexdigest()
    return canonical_bytes({**body,"signature":signature}).decode("utf-8")


def envelope_digest(raw):
    return digest(strict_loads(raw),"avcl/send-envelope-digest/0.1")


@dataclass(frozen=True)
class RemoteCommand:
    id: str
    request: Request
    binding: str
    expires: int
    admitted: bool=True


class SendVerifier:
    def __init__(self,key,tenant,receiver=RECEIVER,key_id=KEY_ID,receipt_signer=None):
        if type(key) is not bytes or len(key)!=32 or any(type(v) is not str or not v or len(v)>256 for v in (tenant,receiver,key_id)):
            raise Reject("INVALID_SEND_VERIFIER")
        self._key=key
        self.tenant=tenant
        self.receiver=receiver
        self.key_id=key_id
        self._receipt_signer=receipt_signer

    def verify(self,raw,now):
        from .sending import PROFILE,MAX_ATTEMPTS
        obj=strict_loads(raw)
        if type(obj) is not dict or set(obj)!=FIELDS:raise Reject("INVALID_SEND_ENVELOPE")
        signature=obj["signature"]
        if type(signature) is not str or re.fullmatch(r"[0-9a-f]{64}",signature) is None:
            raise Reject("INVALID_SEND_AUTHENTICATION")
        body={k:v for k,v in obj.items() if k!="signature"}
        expected=hmac.new(self._key,SIGN_DOMAIN+canonical_bytes(body),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature,expected):raise Reject("INVALID_SEND_AUTHENTICATION")
        if (obj["format"]!=FORMAT or obj["action"]!="send" or obj["profile"]!=PROFILE or
                obj["key_id"]!=self.key_id or obj["tenant"]!=self.tenant or obj["receiver"]!=self.receiver):
            raise Reject("SEND_AUTHORITY_SCOPE_MISMATCH")
        if type(obj["request"]) is not dict or set(obj["request"])!={f.name for f in fields(Request)}:
            raise Reject("INVALID_SEND_ENVELOPE")
        request=Request(**obj["request"]);request.validate()
        if request.tenant!=self.tenant or request.request_digest!=obj["request_digest"]:
            raise Reject("SEND_REQUEST_BINDING_MISMATCH")
        for name in ("binding","contract_digest"):
            if type(obj[name]) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}",obj[name]) is None:
                raise Reject("INVALID_SEND_ENVELOPE")
        if type(obj["operation_id"]) is not str or re.fullmatch(r"op-[0-9a-f]{32}",obj["operation_id"]) is None:
            raise Reject("INVALID_SEND_ENVELOPE")
        issued,expires,attempt,epoch=(amount(obj[name]) for name in ("issued_at","not_after","attempt","epoch"))
        if not (issued<expires and 1<=attempt<=MAX_ATTEMPTS and epoch>0):raise Reject("INVALID_SEND_ENVELOPE")
        if not moment(now):raise Reject("INVALID_REMOTE_CLOCK")
        if now<issued:raise Reject("SEND_ENVELOPE_NOT_YET_VALID")
        if now>=expires:raise Reject("REMOTE_ACCEPTANCE_CLOSED")
        return RemoteCommand(obj["operation_id"],request,obj["binding"],expires)


def provision_verifier(kernel):
    """Host-only in-process provisioning, intentionally not a public API route."""
    from .receipt_envelope import ReceiptSigner,key_for
    return SendVerifier(_key(kernel),kernel.tenant,receipt_signer=ReceiptSigner(key_for(kernel),kernel.tenant))
