"""Bounded audit pages and an offline hash-chain integrity verifier.

Integrity against a caller's independently retained checkpoint is distinct
from authenticity of event contents or semantic execution replay.
"""
import hashlib
import json
import re
from .canonical import Reject,digest
from .audit import MAX_SEQUENCE,decode_event

FORMAT="avcl/audit-page/0.1"
MAX_RECORDS=256
MAX_BYTES=16*1024*1024


def tenant_id(value):
    if type(value) is not str or not 0<len(value)<=256:raise Reject("INVALID_AUDIT_BUNDLE")
    try:value.encode("utf-8",errors="strict")
    except UnicodeError:raise Reject("INVALID_AUDIT_BUNDLE") from None
    return value


def number(value):
    if type(value) is not str or re.fullmatch(r"0|[1-9][0-9]{0,18}",value) is None:raise Reject("INVALID_AUDIT_BUNDLE")
    result=int(value)
    if result>MAX_SEQUENCE:raise Reject("INVALID_AUDIT_BUNDLE")
    return result


def hash_value(value):
    if type(value) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}",value) is None:raise Reject("INVALID_AUDIT_BUNDLE")
    return value


def checkpoint(value):
    if type(value) is not dict or set(value)!={"tenant","sequence","digest"}:raise Reject("INVALID_AUDIT_BUNDLE")
    tenant_id(value["tenant"]);number(value["sequence"]);hash_value(value["digest"])
    return value


def anchor(tenant,sequence,head):return {"tenant":tenant,"sequence":str(sequence),"digest":head}
def genesis(tenant):return digest({"tenant":tenant},"avcl/audit-genesis/0.1")


def link(tenant,sequence,previous,payload):
    payload_digest="sha256:"+hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest({"tenant":tenant,"sequence":str(sequence),"previous":previous,"payload_digest":payload_digest},"avcl/audit-link/0.1")


def require_anchor(expected,actual):
    if checkpoint(expected)!=actual:raise Reject("AUDIT_CHECKPOINT_MISMATCH")


def encode_bundle(bundle):
    try:raw=json.dumps(bundle,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)
    except (TypeError,ValueError,RecursionError):raise Reject("INVALID_AUDIT_BUNDLE") from None
    try:size=len(raw.encode("utf-8",errors="strict"))
    except UnicodeError:raise Reject("INVALID_AUDIT_BUNDLE") from None
    if size>MAX_BYTES:raise Reject("AUDIT_EXPORT_TOO_LARGE")
    return raw


def load_bundle(raw):
    if type(raw) is not str:raise Reject("INVALID_AUDIT_BUNDLE")
    try:
        if len(raw.encode("utf-8",errors="strict"))>MAX_BYTES:raise Reject("AUDIT_EXPORT_TOO_LARGE")
        def pairs(items):
            obj={}
            for key,value in items:
                if key in obj:raise Reject("INVALID_AUDIT_BUNDLE")
                obj[key]=value
            return obj
        def bad_number(_):raise Reject("INVALID_AUDIT_BUNDLE")
        return json.loads(raw,object_pairs_hook=pairs,parse_int=bad_number,parse_float=bad_number,parse_constant=bad_number)
    except (UnicodeError,json.JSONDecodeError,RecursionError):raise Reject("INVALID_AUDIT_BUNDLE") from None


def build_bundle(tenant,payloads,*,after=0,limit=100,expected_previous=None):
    tenant_id(tenant)
    if (type(after) is not int or not 0<=after<=MAX_SEQUENCE or type(limit) is not int or
        not 1<=limit<=MAX_RECORDS or after>len(payloads)):raise Reject("INVALID_AUDIT_RANGE")
    head=genesis(tenant);previous=head;records=[]
    for sequence,payload in enumerate(payloads[:min(after+limit,len(payloads))],1):
        event=decode_event(payload)
        if event.sequence!=sequence:raise Reject("CORRUPT_AUDIT_SEQUENCE")
        head=link(tenant,sequence,head,payload)
        if sequence<=after:previous=head
        else:records.append({"sequence":str(sequence),"payload":payload,"digest":head})
    if expected_previous is not None:require_anchor(expected_previous,anchor(tenant,after,previous))
    result={"format":FORMAT,"tenant":tenant,"after_sequence":str(after),"previous_digest":previous,
            "records":records,"checkpoint":anchor(tenant,after+len(records),head)}
    encode_bundle(result)
    return result


def verify_bundle(bundle,*,expected_tenant,expected_after=0,expected_previous=None,expected_checkpoint=None,check_payloads=False):
    if type(check_payloads) is not bool:raise Reject("INVALID_AUDIT_BUNDLE")
    tenant_id(expected_tenant)
    if type(expected_after) is not int or not 0<=expected_after<=MAX_SEQUENCE:raise Reject("INVALID_AUDIT_RANGE")
    if type(bundle) is not dict or set(bundle)!={"format","tenant","after_sequence","previous_digest","records","checkpoint"}:
        raise Reject("INVALID_AUDIT_BUNDLE")
    if bundle["format"]!=FORMAT:raise Reject("INVALID_AUDIT_BUNDLE")
    tenant=tenant_id(bundle["tenant"]);after=number(bundle["after_sequence"])
    if tenant!=expected_tenant or after!=expected_after:raise Reject("AUDIT_SCOPE_MISMATCH")
    previous=hash_value(bundle["previous_digest"]);records=bundle["records"]
    if type(records) is not list or len(records)>MAX_RECORDS or after+len(records)>MAX_SEQUENCE:raise Reject("INVALID_AUDIT_BUNDLE")
    if after==0 and previous!=genesis(tenant):raise Reject("AUDIT_CHAIN_MISMATCH")
    if expected_previous is not None:require_anchor(expected_previous,anchor(tenant,after,previous))
    head=previous;structured_observations=opaque_observations=0
    for sequence,record in enumerate(records,after+1):
        if type(record) is not dict or set(record)!={"sequence","payload","digest"}:raise Reject("INVALID_AUDIT_BUNDLE")
        if number(record["sequence"])!=sequence:raise Reject("AUDIT_CHAIN_MISMATCH")
        event=decode_event(record["payload"])
        if event.sequence!=sequence:raise Reject("AUDIT_CHAIN_MISMATCH")
        if check_payloads:
            from .audit_payload import validate_payload
            validate_payload(event)
            if event.kind in ("ObservationRecorded","ConflictingObservation"):
                if "fact" in dict(event.details):structured_observations+=1
                else:opaque_observations+=1
        head=link(tenant,sequence,head,record["payload"])
        if hash_value(record["digest"])!=head:raise Reject("AUDIT_CHAIN_MISMATCH")
    current=anchor(tenant,after+len(records),head)
    if checkpoint(bundle["checkpoint"])!=current:raise Reject("AUDIT_CHAIN_MISMATCH")
    if expected_checkpoint is not None:require_anchor(expected_checkpoint,current)
    encode_bundle(bundle)
    report={"format":"avcl/audit-integrity/0.1","status":"VerifiedAgainstCheckpoint" if expected_checkpoint is not None else
            "ContinuationConsistent" if expected_previous is not None else "InternallyConsistent",
            "tenant":tenant,"after_sequence":str(after),"event_count":str(len(records)),"checkpoint":current,
            "semantic_replay":"not_performed","event_truth":"not_verified"}
    if check_payloads:
        from .audit_payload import PROFILE
        report["payload_schema"]={"profile":PROFILE,"status":"Conforms","checked_events":str(len(records)),
                                  "structured_observation_events":str(structured_observations),
                                  "opaque_observation_events":str(opaque_observations)}
    return report
