"""Closed, authenticated runtime operations. No control-plane/adapter routes.

Opaque bearer credentials are host-issued; only their hashes persist. The
HTTP transport is loopback-only, not a substitute for production TLS/identity.
"""
from dataclasses import dataclass
from uuid import uuid4
import hashlib
import re
import secrets
from psycopg import sql
import psycopg
from .canonical import Reject,canonical_bytes,strict_loads,digest
from .storage_codec import dumps,loads
from .types import Request,Execution
from .postgres import PostgresKernel

SCOPES=frozenset({"validate","prepare","dispatch","get","reconcile","cancel","close","delegate"})
TOKEN=re.compile(r"avcl_[A-Za-z0-9_-]{43}\Z")

def text_id(value,limit=256):return type(value) is str and 0<len(value)<=limit and all(ord(c)>=32 and not 0xD800<=ord(c)<=0xDFFF for c in value)
def token_hash(token):return hashlib.sha256(b"avcl/api-token/0.1\0"+token.encode("ascii")).hexdigest()

@dataclass(frozen=True,repr=False)
class IssuedCredential:
    id: str
    token: str

class GatewayAuthority:
    """Host-only provisioning; never mounted as an HTTP resource."""
    def __init__(self,kernel):self.db=kernel

    def issue(self,subject,scopes,expires):
        if not text_id(subject) or type(scopes) is not frozenset or not scopes or not scopes<=SCOPES or type(expires) is not int:raise Reject("INVALID_API_CREDENTIAL")
        issued=IssuedCredential("credential-"+uuid4().hex,"avcl_"+secrets.token_urlsafe(32))
        def issue(k,c):
            if expires<=k._clock():raise Reject("INVALID_API_CREDENTIAL")
            c.execute("INSERT INTO api_credential VALUES(%s,%s,%s,%s,%s,%s,false)",
                (token_hash(issued.token),issued.id,self.db.tenant,subject,canonical_bytes(sorted(scopes)).decode(),expires))
            k._event("ApiCredentialIssued",credential=issued.id,subject=subject,expires=expires,requirement_id="R01")
        self.db._transaction("api-credential-issue",issue)
        return issued

    def revoke(self,credential_id):
        def revoke(k,c):
            row=c.execute("SELECT revoked FROM api_credential WHERE tenant_id=%s AND id=%s",(self.db.tenant,credential_id)).fetchone()
            if row is None:raise Reject("UNTRUSTED_REFERENCE")
            if not row["revoked"]:
                c.execute("UPDATE api_credential SET revoked=true WHERE tenant_id=%s AND id=%s",(self.db.tenant,credential_id))
                k._event("ApiCredentialRevoked",credential=credential_id,requirement_id="R01")
        self.db._transaction("api-credential-revoke",revoke)

    def register_checked(self,request,evidence_ref,expires):
        if type(request) is not Request or type(expires) is not int:raise Reject("INVALID_CHECKED_REQUEST")
        request.validate();ref="checked-"+secrets.token_hex(32)
        def register(k,c):
            evidence=k._lookup(k._evidence,evidence_ref)
            if request.tenant!=k.tenant or evidence.request_digest!=request.request_digest or not k._clock()<expires<=evidence.expires:raise Reject("INVALID_CHECKED_REQUEST")
            c.execute("INSERT INTO checked_request VALUES(%s,%s,%s,%s,%s,%s)",(k.tenant,ref,request.subject,dumps(request),evidence_ref,expires))
            k._event("CheckedRequestRegistered",checked_ref=ref,subject=request.subject,request_digest=request.request_digest,requirement_id="R02")
        self.db._transaction("checked-register",register)
        return ref

class RuntimeGateway:
    def __init__(self,dsn,schema="avcl",clock=None):self.template=PostgresKernel(dsn,schema=schema,clock=clock)

    def _locate(self,hashed):
        with self.template._connect() as c:
            row=c.execute(sql.SQL("SELECT tenant_id FROM {}.api_credential WHERE token_hash=%s").format(sql.Identifier(self.template.schema)),(hashed,)).fetchone()
            if row is None:raise Reject("INVALID_CREDENTIAL")
            return row["tenant_id"]

    @staticmethod
    def route(method,path):
        task=re.fullmatch(r"/v1/tasks/(task-[0-9a-f]{32})(/close)?",path)
        if task:
            ref,suffix=task.groups()
            if method=="GET" and suffix is None:return "task-get",ref
            if method=="POST" and suffix=="/close":return "close",ref
        if method=="POST" and path=="/v1/candidates/validate":return "validate",None
        if method=="POST" and path=="/v1/capabilities/delegate":return "delegate",None
        if method=="POST" and path=="/v1/operations/prepare":return "prepare",None
        found=re.fullmatch(r"/v1/operations/(op-[0-9a-f]{32})(?:/(dispatch|reconcile|cancel))?",path)
        if found:
            operation,action=found.groups()
            if method=="GET" and action is None:return "get",operation
            if method=="POST" and action is not None:return action,operation
        raise Reject("ENDPOINT_NOT_FOUND")

    @staticmethod
    def owned(k,subject,operation):
        try:return k.get(subject,operation)
        except Reject as exc:
            if exc.code in {"AUTH_DENIED","UNTRUSTED_REFERENCE"}:raise Reject("OPERATION_NOT_FOUND") from None
            raise

    @staticmethod
    def project(op,include_permit=False):
        result={"operation_id":op.id,"execution_state":str(op.state),"settlement_state":str(op.settlement),
                "revision":str(op.revision),"contract_digest":op.contract_digest,
                "reservations":[{"account":r.account,"upper":str(r.upper),"charged":str(r.charged),"remaining":str(r.remaining)} for r in op.reservations],
                "obligation_refs":["reconcile:"+op.id] if op.obligation_due is not None else []}
        if include_permit:result["permit_ref"]=op.permit_ref
        return result

    def handle(self,method,path,authorization,raw_body=""):
        trace="trace-"+uuid4().hex;request_id=None
        try:
            if type(authorization) is not str or not authorization.startswith("Bearer "):raise Reject("INVALID_CREDENTIAL")
            token=authorization[7:]
            if TOKEN.fullmatch(token) is None:raise Reject("INVALID_CREDENTIAL")
            hashed=token_hash(token);tenant=self._locate(hashed)
            action,operation=self.route(method,path)
            if action in {"get","task-get"}:
                if raw_body:raise Reject("INVALID_REQUEST")
                body={}
            elif action in {"validate","delegate"}:
                body=strict_loads(raw_body)
                if type(body) is not dict:raise Reject("INVALID_REQUEST")
                request_id=body.get("client_request_id")
                if not text_id(request_id,128):raise Reject("INVALID_REQUEST")
            else:
                body=strict_loads(raw_body)
                shapes={"prepare":{"client_request_id","checked_ref","capability_ref","approval_refs","business_action_key"},
                        "dispatch":{"client_request_id","permit_ref"},"reconcile":{"client_request_id","reason"},"cancel":{"client_request_id","cancel_kind"},"close":{"client_request_id"}}
                if type(body) is not dict or set(body)!=shapes[action]:raise Reject("INVALID_REQUEST")
                for key,value in body.items():
                    if key=="approval_refs":
                        if type(value) is not list or len(value)!=1 or not text_id(value[0]):raise Reject("INVALID_REQUEST")
                    elif not text_id(value,128 if key=="client_request_id" else 256):raise Reject("INVALID_REQUEST")
                request_id=body["client_request_id"]
            db=PostgresKernel(self.template.dsn,tenant,self.template.schema,self.template.clock)
            submitted=digest({"method":method,"path":path,"body":body},"avcl/api-request/0.1")
            def execute(k,c):
                # Recheck after taking the same authority guard as revocation.
                auth=c.execute("SELECT subject,scopes,expires,revoked FROM api_credential WHERE tenant_id=%s AND token_hash=%s",(tenant,hashed)).fetchone()
                if auth is None or auth["revoked"] or auth["expires"]<=k._clock():raise Reject("INVALID_CREDENTIAL")
                if ("get" if action=="task-get" else action) not in strict_loads(auth["scopes"]):raise Reject("API_SCOPE_DENIED")
                subject=auth["subject"]
                if action in {"task-get","close"}:
                    from . import tasks
                    if action=="close":return tasks.close(db,k,c,subject,operation,request_id,trace)
                    row=tasks.owned(k,c,subject,operation)
                    tasks.sync_obligations(db,c,k)
                    return tasks.project(tasks.refresh(db,c,k,row))
                if action=="get":return self.project(self.owned(k,subject,operation))
                if action in {"validate","delegate","prepare","dispatch"} and k._clock_rollback:raise Reject("CLOCK_ROLLBACK")
                if action=="delegate":return k.delegate(subject,canonical_bytes(body).decode("utf-8"))
                if action=="validate":
                    from .validation import validate_candidate
                    return validate_candidate(k,c,subject,body,trace)
                prior=c.execute("SELECT digest,operation_id FROM api_request WHERE tenant_id=%s AND subject=%s AND endpoint=%s AND request_id=%s",(tenant,subject,path,request_id)).fetchone()
                if prior:
                    if prior["digest"]!=submitted:raise Reject("IDEMPOTENCY_CONFLICT")
                    return self.project(self.owned(k,subject,prior["operation_id"]),action=="prepare")
                if action=="prepare":
                    checked=c.execute("SELECT * FROM checked_request WHERE tenant_id=%s AND id=%s AND subject=%s",(tenant,body["checked_ref"],subject)).fetchone()
                    if checked is None:raise Reject("AUTH_DENIED")
                    if checked["expires"]<=k._clock():raise Reject("STALE_EVIDENCE")
                    request=loads(checked["payload"])
                    if request.subject!=subject or request.tenant!=tenant:raise Reject("AUTH_DENIED")
                    if request.business_key!=body["business_action_key"]:raise Reject("BINDING_MISMATCH")
                    op=k.prepare(subject,request,body["capability_ref"],checked["evidence_ref"],body["approval_refs"][0],request_id)
                else:
                    op=self.owned(k,subject,operation)
                    if action=="dispatch":op=k.dispatch(subject,op.id,body["permit_ref"])
                    elif action=="cancel":
                        if body["cancel_kind"]!="before_dispatch":raise Reject("UNSUPPORTED_GUARANTEE")
                        op=k.abort_prepared(subject,op.id)
                    elif action=="reconcile":
                        if not op.admitted:raise Reject("INVALID_TRANSITION")
                        if op.state==Execution.DISPATCHED:op=k.trusted.unknown(op.id)
                        c.execute("UPDATE obligation SET next_attempt=LEAST(next_attempt,%s) WHERE tenant_id=%s AND operation_id=%s AND resolution IS NULL AND worker IS NULL",(k._clock(),tenant,op.id))
                        k._event("ReconciliationRequested",op.id,request_id=request_id,reason=body["reason"],requirement_id="R10")
                c.execute("INSERT INTO api_request VALUES(%s,%s,%s,%s,%s,%s)",(tenant,subject,path,request_id,submitted,op.id))
                k._event("ApiRequestAccepted",op.id,subject=subject,action=action,request_id=request_id,trace_id=trace,requirement_id="R01")
                return self.project(op,action=="prepare")
            result=db._transaction("api-"+action,execute,action!="get")
            return (202 if action in {"dispatch","reconcile"} else 200),{"trace_id":trace,"request_id":request_id,"data":result}
        except Reject as exc:
            code="AUTH_DENIED" if exc.code=="UNTRUSTED_REFERENCE" else exc.code
            if code=="INVALID_CREDENTIAL":status=401
            elif code in {"AUTH_DENIED","API_SCOPE_DENIED","SCOPE_DENIED"}:status=403
            elif code in {"ENDPOINT_NOT_FOUND","OPERATION_NOT_FOUND","TASK_NOT_FOUND"}:status=404
            elif code=="UNSUPPORTED_GUARANTEE":status=422
            elif code.startswith("INVALID") or code in {"DUPLICATE_KEY","JSON_NUMBERS_UNSUPPORTED","REQUEST_TOO_LARGE","REQUEST_TOO_DEEP"}:status=400
            else:status=409
            return status,self.error(code,trace,request_id,"do_not_retry" if status in {401,403,404,422} else "correct_request" if status==400 else "inspect_state_before_retry")
        except (psycopg.Error,OSError):return 503,self.error("DEPENDENCY_UNAVAILABLE",trace,request_id,"query_before_retry")

    @staticmethod
    def error(code,trace,request_id=None,advice="do_not_retry"):
        return {"trace_id":trace,"error":{"code":code,"request_id":request_id,"operation_ref":None,"retry_advice":advice,"requirement_ids":["R01","R02","R16"],"detail_ref":None}}
