"""PostgreSQL SERIALIZABLE storage for the executable reference transitions.

All mutators share the tenant authority row. State lives in relational rows;
the Python kernel is rehydrated inside each transaction, never a durable cache.
This initial adapter favors semantic consistency over large-tenant throughput.
"""
from dataclasses import replace
from contextlib import nullcontext
from pathlib import Path
import hashlib
import os
import re
import time
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from .canonical import Reject
from .runtime import ReferenceKernel
from .types import Account,Reservation,Execution,Settlement,Operation
from .contract import default_contract,assumptions_valid
from .policy import PolicyAuthority
from .storage_codec import dumps,loads
from .supervision import sync_obligations
from . import capacity
from . import validation
from . import tasks
from .sending import admit_attempt
from . import receipt_envelope
from .evaluation import WorkBudget, DEFAULT_WORK_LIMIT
from . import rejection_audit
from . import audit

DDL=Path(__file__).resolve().parents[1]/"migrations"/"001_core.sql"

class PostgresKernel:
    def __init__(self,dsn,tenant="demo",schema="avcl",clock=None,crash_at=None,*,evaluation_limit=DEFAULT_WORK_LIMIT):
        self.evaluation_limit=WorkBudget(evaluation_limit).limit
        if type(schema) is not str or re.fullmatch(r"avcl(?:_[a-z0-9_]{1,48})?",schema) is None: raise Reject("INVALID_DATABASE_SCHEMA")
        if type(tenant) is not str or not tenant or len(tenant)>256: raise Reject("INVALID_TENANT")
        self.dsn=dsn;self.tenant=tenant;self.schema=schema;self.clock=clock or (lambda:0);self.crash_at=crash_at

    def _connect(self):
        return psycopg.connect(self.dsn,row_factory=dict_row,connect_timeout=5)

    def migrate(self):
        with self._connect() as conn:
            if conn.info.server_version!=180006: raise Reject("UNSUPPORTED_DATABASE_VERSION")
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",(self.schema,))
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
            conn.execute(sql.SQL("SET LOCAL search_path = {}, pg_catalog").format(sql.Identifier(self.schema)))
            exists=conn.execute("SELECT to_regclass('schema_version') AS name").fetchone()["name"]
            if exists:
                versions=conn.execute("SELECT version FROM schema_version").fetchall()
                if versions not in ([{"version":v}] for v in range(1,11)): raise Reject("DATABASE_SCHEMA_MISMATCH")
            else: conn.execute(DDL.read_text(encoding="utf-8"))
            if conn.execute("SELECT version FROM schema_version").fetchone()["version"]==1:
                conn.execute((DDL.parent/"002_supervision.sql").read_text(encoding="utf-8"))
            if conn.execute("SELECT version FROM schema_version").fetchone()["version"]==2:
                conn.execute((DDL.parent/"003_workflow.sql").read_text(encoding="utf-8"))
            if conn.execute("SELECT version FROM schema_version").fetchone()["version"]==3:
                conn.execute((DDL.parent/"004_capacity.sql").read_text(encoding="utf-8"))
            if conn.execute("SELECT version FROM schema_version").fetchone()["version"]==4:
                conn.execute((DDL.parent/"005_gateway.sql").read_text(encoding="utf-8"))
            if conn.execute("SELECT version FROM schema_version").fetchone()["version"]==5:
                conn.execute((DDL.parent/"006_validation.sql").read_text(encoding="utf-8"))
            if conn.execute("SELECT version FROM schema_version").fetchone()["version"]==6:
                conn.execute((DDL.parent/"007_task_close.sql").read_text(encoding="utf-8"))
            if conn.execute("SELECT version FROM schema_version").fetchone()["version"]==7:
                conn.execute((DDL.parent/"008_delegation.sql").read_text(encoding="utf-8"))
            if conn.execute("SELECT version FROM schema_version").fetchone()["version"]==8:
                conn.execute((DDL.parent/"009_rejection_audit.sql").read_text(encoding="utf-8"))
            if conn.execute("SELECT version FROM schema_version").fetchone()["version"]==9:
                conn.execute((DDL.parent/"010_audit_counter.sql").read_text(encoding="utf-8"))

    def _crash(self,point):
        if self.crash_at==point: os._exit(97)

    def _rows(self,conn,table):
        return conn.execute(sql.SQL("SELECT * FROM {} WHERE tenant_id=%s").format(sql.Identifier(table)),(self.tenant,)).fetchall()

    def _upsert(self,conn,table,keys,values,immutable=False):
        names=tuple(values);updates=[n for n in names if n not in keys]
        action=sql.SQL("DO NOTHING") if immutable else sql.SQL("DO UPDATE SET ")+sql.SQL(",").join(
            sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(n),sql.Identifier(n)) for n in updates)
        query=sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) ").format(
            sql.Identifier(table),sql.SQL(",").join(map(sql.Identifier,names)),sql.SQL(",").join(sql.Placeholder() for _ in names),
            sql.SQL(",").join(map(sql.Identifier,keys)))+action
        conn.execute(query,tuple(values[n] for n in names))

    def _load(self,conn,guard):
        k=ReferenceKernel(self.tenant,guard["active_policy"],self.clock,evaluation_limit=self.evaluation_limit)
        k.epoch=guard["epoch"];k._quarantined=guard["quarantined"];k._next_id=guard["next_id"]
        base=default_contract();k._policy_authority=PolicyAuthority(base.tools,base.validators,key=bytes(guard["signing_key"]))
        k._policy_snapshots={r["digest"]:k._policy_authority.restore(r["envelope"]) for r in self._rows(conn,"policy_snapshot")}
        if any(d!=v.compiled.contract.semantic_digest for d,v in k._policy_snapshots.items()): raise Reject("CORRUPT_POLICY_SNAPSHOT")
        k._release=k._policy_snapshots[guard["contract_digest"]]
        for table,attribute in [("capability","_caps"),("evidence","_evidence"),("approval","_approvals"),("health","_health")]:
            setattr(k,attribute,{r["id"]:loads(r["payload"]) for r in self._rows(conn,table)})
        k._subjects={r["subject"]:r["active"] for r in self._rows(conn,"delegation_subject")}
        k._grants={r["capability_ref"]:loads(r["payload"]) for r in self._rows(conn,"delegation_grant")}
        k._delegation_requests={(r["subject"],r["request_id"]):(r["digest"],r["capability_ref"]) for r in self._rows(conn,"delegation_request")}
        k._accounts={r["id"]:Account(int(r["limit_amount"]),int(r["spent"]),int(r["pending"]),r["breached"]) for r in self._rows(conn,"budget_account")}
        reservations={}
        for r in self._rows(conn,"reservation"):
            reservations.setdefault(r["operation_id"],[]).append(Reservation(r["account_id"],int(r["upper_bound"]),int(r["charged"]),int(r["remaining"])))
        k._operations={}
        for row in self._rows(conn,"operation"):
            op=loads(row["payload"])
            if (op.id,op.request.subject,str(op.state),str(op.settlement),op.revision)!=(row["id"],row["subject"],row["state"],row["settlement"],row["revision"]):
                raise Reject("CORRUPT_OPERATION_PROJECTION")
            k._operations[op.id]=replace(op,reservations=tuple(sorted(reservations.get(op.id,()),key=lambda r:r.account)))
        permits=self._rows(conn,"permit")
        k._permits={r["id"]:r["operation_id"] for r in permits}
        if len(permits)!=len(k._operations): raise Reject("CORRUPT_PERMIT_PROJECTION")
        for row in permits:
            op=k._operations[row["operation_id"]]
            expected="Consumed" if op.admitted else "Aborted" if op.state==Execution.ABORTED else "Live"
            if row["id"]!=op.permit_ref or row["status"]!=expected: raise Reject("CORRUPT_PERMIT_PROJECTION")
        k._business={r["business_key"]:r["operation_id"] for r in self._rows(conn,"business_action")}
        k._dedup={(r["subject"],r["request_id"]):(r["digest"],r["operation_id"]) for r in self._rows(conn,"request_dedup")}
        k._observations={r["id"]:loads(r["payload"]) for r in self._rows(conn,"observation")}
        audit.hydrate(self,conn,k)
        owners=conn.execute("SELECT o.operation_id,o.subject,o.run_id,r.workflow_digest FROM workflow_operation o JOIN workflow_run r ON (r.tenant_id,r.subject,r.id)=(o.tenant_id,o.subject,o.run_id) WHERE o.tenant_id=%s",(self.tenant,)).fetchall()
        k._send_owners={r["operation_id"]:(r["subject"],r["run_id"],r["workflow_digest"]) for r in owners}
        k.assert_invariants()
        return k

    def _save(self,conn,k):
        tenant={"tenant_id":self.tenant}
        conn.execute("UPDATE authority_guard SET epoch=%s,active_policy=%s,revision=revision+1,quarantined=%s,next_id=%s,contract_digest=%s WHERE tenant_id=%s",
            (k.epoch,k.policy,k._quarantined,k._next_id,k._release.compiled.contract.semantic_digest,self.tenant))
        for key,release in k._policy_snapshots.items():
            self._upsert(conn,"policy_snapshot",("tenant_id","digest"),{**tenant,"digest":key,"envelope":release.envelope},True)
        for table,records in [("capability",k._caps),("evidence",k._evidence),("approval",k._approvals),("health",k._health)]:
            for key,value in records.items(): self._upsert(conn,table,("tenant_id","id"),{**tenant,"id":key,"payload":dumps(value)})
        for key,a in sorted(k._accounts.items()):
            self._upsert(conn,"budget_account",("tenant_id","id"),{**tenant,"id":key,"limit_amount":a.limit,"spent":a.spent,"pending":a.pending,"breached":a.breached})
        for subject,active in k._subjects.items():
            self._upsert(conn,"delegation_subject",("tenant_id","subject"),{**tenant,"subject":subject,"active":active})
        for ref,grant in k._grants.items():
            self._upsert(conn,"delegation_grant",("tenant_id","capability_ref"),{**tenant,"capability_ref":ref,"payload":dumps(grant)})
        for (subject,request_id),(value,ref) in k._delegation_requests.items():
            self._upsert(conn,"delegation_request",("tenant_id","subject","request_id"),{**tenant,"subject":subject,"request_id":request_id,"digest":value,"capability_ref":ref},True)
        for key,op in k._operations.items():
            if op.admitted and op.settlement in (Settlement.PARTIAL,Settlement.DISPUTED) and op.obligation_due is None:
                op=replace(op,obligation_due=k._clock()+86400,revision=op.revision+1)
                k._operations[key]=op;k._event("ReconcileRequired",key,due=op.obligation_due,requirement_id="R10")
            self._upsert(conn,"operation",("tenant_id","id"),{**tenant,"id":key,"subject":op.request.subject,
                "state":str(op.state),"settlement":str(op.settlement),"revision":op.revision,"payload":dumps(replace(op,reservations=()))})
            for r in op.reservations:
                self._upsert(conn,"reservation",("tenant_id","operation_id","account_id"),{**tenant,"operation_id":key,"account_id":r.account,"upper_bound":r.upper,"charged":r.charged,"remaining":r.remaining})
            status="Consumed" if op.admitted else "Aborted" if op.state==Execution.ABORTED else "Live"
            self._upsert(conn,"permit",("tenant_id","id"),{**tenant,"id":op.permit_ref,"operation_id":key,"status":status})
            if op.admitted:
                self._upsert(conn,"outbox",("tenant_id","operation_id","kind"),{**tenant,"operation_id":key,"kind":"Send","status":"Pending","wait_until":k._clock()+30},True)
                if op.settlement==Settlement.FINAL:
                    conn.execute("UPDATE outbox SET status='Done',worker=NULL,lease_until=NULL WHERE tenant_id=%s AND operation_id=%s",(self.tenant,key))
                elif op.state==Execution.UNKNOWN or op.settlement in (Settlement.PARTIAL,Settlement.DISPUTED):
                    conn.execute("UPDATE outbox SET status='NeedsReconcile',worker=NULL,lease_until=NULL WHERE tenant_id=%s AND operation_id=%s",(self.tenant,key))
        sync_obligations(self,conn,k)
        tasks.sync(self,conn,k)
        for key,op_id in k._business.items(): self._upsert(conn,"business_action",("tenant_id","business_key"),{**tenant,"business_key":key,"operation_id":op_id})
        for (subject,request_id),(value,op_id) in k._dedup.items():
            self._upsert(conn,"request_dedup",("tenant_id","subject","endpoint","request_id"),{**tenant,"subject":subject,"endpoint":"prepare","request_id":request_id,"digest":value,"operation_id":op_id})
        for key,observation in k._observations.items(): self._upsert(conn,"observation",("tenant_id","id"),{**tenant,"id":key,"operation_id":observation.operation_id,"payload":dumps(observation)},True)
        audit.persist(conn,k)

    def initialize(self):
        self.migrate()
        with self._connect() as conn:
            conn.execute(sql.SQL("SET LOCAL search_path = {}, pg_catalog").format(sql.Identifier(self.schema)))
            k=ReferenceKernel(self.tenant,clock=self.clock,evaluation_limit=self.evaluation_limit)
            row=conn.execute("INSERT INTO authority_guard(tenant_id,epoch,active_policy,quarantined,next_id,signing_key,contract_digest) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING tenant_id",
                (self.tenant,k.epoch,k.policy,k._quarantined,k._next_id,k._policy_authority._key,k._release.compiled.contract.semantic_digest)).fetchone()
            if row:
                conn.execute("INSERT INTO audit_counter(tenant_id) VALUES(%s)",(self.tenant,))
                audit.attach(conn,k)
                conn.execute("INSERT INTO rejection_audit_policy(tenant_id) VALUES(%s)",(self.tenant,))
                conn.execute("INSERT INTO capacity_policy VALUES(%s,%s,%s,%s)",(self.tenant,capacity.DEFAULT_LIMIT,capacity.DEFAULT_LIMIT,capacity.DEFAULT_LIMIT))
                self._save(conn,k)

    def _transaction(self,tag,fn,write=True,*,authenticate=None):
        for attempt in range(8):
            try:
                rejected=None;receipt=None
                with self._connect() as conn:
                    conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                    conn.execute(sql.SQL("SET LOCAL search_path = {}, pg_catalog").format(sql.Identifier(self.schema)))
                    guard=conn.execute("SELECT * FROM authority_guard WHERE tenant_id=%s FOR UPDATE",(self.tenant,)).fetchone()
                    if guard is None: raise Reject("UNKNOWN_TENANT")
                    now=self.clock()
                    if type(now) is not int or now<0: raise Reject("INVALID_CLOCK")
                    if now<guard["last_clock"] and tag in {"prepare","dispatch","claim","claim-obligation"}:
                        raise Reject("CLOCK_ROLLBACK")
                    effective=max(now,guard["last_clock"])
                    k=self._load(conn,guard);k._clock=lambda:effective
                    k._clock_rollback=now<guard["last_clock"]
                    k._before_prepare=lambda:capacity.check_new_operation(self,conn,k)
                    k._check_evidence=lambda ref:validation.check_evidence(k,conn,ref)
                    k._check_task=lambda request:tasks.admission(k,conn,request)
                    context=authenticate(k,conn) if authenticate is not None else None
                    if authenticate is not None:
                        if type(context) is not rejection_audit.AuthenticatedContext:raise Reject("INVALID_REJECTION_CONTEXT")
                        context.validate()
                    try:
                        with conn.transaction() if context is not None else nullcontext():
                            result=fn(k,conn)
                            if write:conn.execute("UPDATE authority_guard SET last_clock=%s WHERE tenant_id=%s",(effective,self.tenant))
                            if write:k.assert_invariants();self._save(conn,k)
                            else:audit.check_readonly(conn,k)
                            if type(result) is Operation:result=k._operations[result.id]
                    except Reject as exc:
                        if context is None:raise
                        # SQL writes and memory changes in the failed action are
                        # both discarded. Keep the same authority guard/snapshot.
                        k=self._load(conn,guard);k._clock=lambda:effective
                        receipt=rejection_audit.record(self,conn,k,context,exc)
                        conn.execute("UPDATE authority_guard SET last_clock=%s WHERE tenant_id=%s",(effective,self.tenant))
                        rejected=exc
                        self._crash(tag+".rejected.before_commit")
                    self._crash(tag+".before_commit")
                self._crash(tag+".after_commit")
                if rejected is not None:
                    self._crash(tag+".rejected.after_commit")
                    rejected.audit_event_id,rejected.audit_sequence=receipt
                    raise rejected
                return result
            except (psycopg.errors.SerializationFailure,psycopg.errors.DeadlockDetected):
                if attempt==7: raise
                time.sleep(0.005*(attempt+1))

    @property
    def trusted(self): return PostgresTrusted(self)
    def prepare(self,*args,**kwargs): return self._transaction("prepare",lambda k,c:k.prepare(*args,**kwargs))
    def delegate(self,*args):return self._transaction("delegate",lambda k,c:k.delegate(*args))
    def dispatch(self,*args,**kwargs): return self._transaction("dispatch",lambda k,c:k.dispatch(*args,**kwargs))
    def abort_prepared(self,*args,**kwargs): return self._transaction("abort",lambda k,c:k.abort_prepared(*args,**kwargs))
    def get(self,*args): return self._transaction("get",lambda k,c:k.get(*args),False)
    def accounts(self): return self._transaction("accounts",lambda k,c:k.accounts(),False)
    def capacity(self):return self._transaction("capacity",lambda k,c:capacity.snapshot(self,c,k),False)
    def events(self): return self._transaction("events",lambda k,c:k.events(),False)
    def assert_invariants(self): return self._transaction("invariants",lambda k,c:k.assert_invariants(),False)
    def status(self):
        return self._transaction("status",lambda k,c:{"epoch":k.epoch,"policy":k.policy,"quarantined":k._quarantined,
            "outbox":self._rows(c,"outbox"),"obligations":self._rows(c,"obligation"),"raw_observations":len(self._rows(c,"observation_receipt"))},False)

    def claim_outbox(self,worker,lease_seconds=30,authenticated_receipts=False):
        if type(worker) is not str or not worker or type(lease_seconds) is not int or not 1<=lease_seconds<=3600: raise Reject("INVALID_LEASE")
        if type(authenticated_receipts) is not bool:raise Reject("INVALID_RECEIPT_MODE")
        def claim(k,conn):
            if k._quarantined: raise Reject("ASSUMPTION_BROKEN")
            rows=conn.execute("SELECT b.* FROM outbox b WHERE b.tenant_id=%s AND b.status='Pending' AND NOT EXISTS (SELECT 1 FROM workflow_operation w WHERE w.tenant_id=b.tenant_id AND w.operation_id=b.operation_id) ORDER BY b.operation_id FOR UPDATE OF b",(self.tenant,)).fetchall()
            row=None
            for candidate in rows:
                current=k._operations[candidate["operation_id"]]
                if candidate["wait_until"]<=k._clock():
                    if current.state==Execution.DISPATCHED:k.trusted.unknown(current.id)
                    k._event("SendBlocked",current.id,reason="OUTBOX_WAIT_EXPIRED",requirement_id="R05")
                    continue
                row=candidate
                break
            if row is None:return None
            op=k._operations[row["operation_id"]];original=k._policy_snapshots[op.contract_digest].compiled.contract
            ids=tuple({a for c in original.clauses if c.kind=="safety" for a in c.assumptions})
            if not assumptions_valid(original,ids,k._health,k._clock()): raise Reject("ASSUMPTION_BROKEN")
            generation=row["generation"]+1
            if authenticated_receipts:receipt_envelope.require(k,op.id)
            envelope=admit_attempt(k,op,generation=generation,worker=worker)
            if not envelope:return None
            conn.execute("UPDATE outbox SET status='InFlight',worker=%s,lease_until=%s,generation=%s,attempts=attempts+1 WHERE tenant_id=%s AND operation_id=%s",
                (worker,k._clock()+lease_seconds,generation,self.tenant,op.id))
            conn.execute("INSERT INTO send_attempt VALUES(%s,%s,%s,%s,%s,%s,%s)",(self.tenant,op.id,generation,worker,op.request.request_digest,op.binding,k._clock()))
            return {"operation":op,"generation":generation,"worker":worker,"envelope":envelope,
                    "authenticated_receipts":receipt_envelope.required(k,op.id)}
        return self._transaction("claim",claim)

    def reap_expired_leases(self):
        def reap(k,conn):
            rows=conn.execute("SELECT operation_id FROM outbox WHERE tenant_id=%s AND ((status='InFlight' AND lease_until<=%s) OR (status='Pending' AND wait_until<=%s)) FOR UPDATE",(self.tenant,k._clock(),k._clock())).fetchall()
            for row in rows:
                op=k._operations[row["operation_id"]]
                if op.state==Execution.DISPATCHED: k.trusted.unknown(op.id)
                conn.execute("UPDATE outbox SET status='NeedsReconcile',worker=NULL,lease_until=NULL,generation=generation+1 WHERE tenant_id=%s AND operation_id=%s",(self.tenant,op.id))
            return len(rows)
        return self._transaction("reap",reap)

    def recover_observations(self):
        pending=self._transaction("pending-observations",lambda k,c:c.execute("SELECT r.payload FROM observation_receipt r LEFT JOIN observation_application a USING(tenant_id,digest) WHERE r.tenant_id=%s AND a.digest IS NULL ORDER BY r.digest",(self.tenant,)).fetchall(),False)
        results=[]
        for row in pending:
            try:
                value=loads(row["payload"])
                if type(value) is tuple and len(value)==3 and value[0]==receipt_envelope.RAW_RECORD:
                    results.append(self.trusted.observe(value[2],expected_operation_id=value[1]))
                else:results.append(self.trusted.observe(value))
            except Reject as exc:results.append({"status":"Rejected","code":exc.code})
        return results

class PostgresTrusted:
    def export_audit(self,*,after=0,limit=100,expected_previous=None):
        from .audit_export import build_bundle
        return self.k._transaction("export-audit",lambda k,c:build_bundle(k.tenant,k._persisted_event_payloads,
                    after=after,limit=limit,expected_previous=expected_previous),False)

    def configure_rejection_audit(self,total_limit=10000,subject_limit=100,window_seconds=60):
        return self.k._transaction("audit-configure",lambda k,c:rejection_audit.configure(self.k,c,k,total_limit,subject_limit,window_seconds))

    METHODS={"add_account","register_capability","register_evidence","register_approval","revoke","unknown","seal_policy","activate_policy","set_assumption_health", "register_subject","disable_subject","enable_delegation","revoke_capability","require_authenticated_receipts"}
    def __init__(self,kernel): self.k=kernel
    def send_verifier(self):
        return self.k._transaction("send-verifier",lambda k,c:k.trusted.send_verifier(),False)
    def set_capacity(self,operations,obligations,workflows):
        return self.k._transaction("capacity-policy",lambda k,c:capacity.configure(self.k,c,k,operations,obligations,workflows))
    def __getattr__(self,name):
        if name not in self.METHODS: raise AttributeError(name)
        return lambda *a,**kw:self.k._transaction(name,lambda k,c:getattr(k.trusted,name)(*a,**kw))
    def observe(self,observation,expected_operation_id=None):
        if expected_operation_id is not None and (type(expected_operation_id) is not str or not 0<len(expected_operation_id)<=256):raise Reject("INVALID_OBSERVATION_BINDING")
        value=(receipt_envelope.RAW_RECORD,expected_operation_id,observation) if type(observation) is str or expected_operation_id is not None else observation
        raw=dumps(value);key="sha256:"+hashlib.sha256(raw.encode()).hexdigest()
        self.k._transaction("raw-observation",lambda k,c:self.k._upsert(c,"observation_receipt",("tenant_id","digest"),
            {"tenant_id":self.k.tenant,"digest":key,"payload":raw},True))
        self.k._crash("observe.after_raw_commit")
        def apply(k,conn):
            result=k.trusted.observe(observation,expected_operation_id)
            self.k._upsert(conn,"observation_application",("tenant_id","digest"),{"tenant_id":self.k.tenant,"digest":key,"applied":True,"error_code":None})
            return result
        try:return self.k._transaction("observe",apply)
        except Reject as exc:
            # The failed projection transaction has rolled back. Preserve a
            # separate rejection decision so one bad receipt cannot starve recovery.
            def rejected(k,conn):
                row=conn.execute("SELECT applied FROM observation_application WHERE tenant_id=%s AND digest=%s",(self.k.tenant,key)).fetchone()
                if row is None:
                    self.k._upsert(conn,"observation_application",("tenant_id","digest"),
                        {"tenant_id":self.k.tenant,"digest":key,"applied":False,"error_code":exc.code})
                    k._event("ObservationRejected",digest=key,reason=exc.code)
                    if type(observation) is str or exc.code=="AUTHENTICATED_RECEIPT_REQUIRED":
                        receipt_envelope.rejected(k,expected_operation_id,exc.code,key)
            self.k._transaction("rejected-observation",rejected)
            raise
