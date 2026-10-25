"""Durable reconciliation obligations and a host-only free-query simulator runner.

Scheduling never implies a guaranteed SLA. Breach history survives late
settlement or acknowledged handoff, and handoff never releases money.
"""
from .canonical import Reject
from .types import Settlement,Observation

def sync_obligations(db,conn,k):
    now=k._clock()
    for op in k._operations.values():
        if op.obligation_due is None:continue
        triggers=[e.sequence for e in k._events if e.operation_id==op.id and e.kind in ("Unknown","ReconcileRequired")]
        db._upsert(conn,"obligation",("tenant_id","operation_id","kind"),
            {"tenant_id":db.tenant,"operation_id":op.id,"kind":"Reconcile","due":op.obligation_due,
             "owner":"reconciler","status":"Open","trigger_event":min(triggers) if triggers else None},True)
        row=conn.execute("SELECT * FROM obligation WHERE tenant_id=%s AND operation_id=%s AND kind='Reconcile'",(db.tenant,op.id)).fetchone()
        if row["resolution"] is not None:continue
        if now>row["due"] and row["breached_at"] is None:
            conn.execute("UPDATE obligation SET status='Breached',breached_at=%s WHERE tenant_id=%s AND operation_id=%s AND kind='Reconcile'",(now,db.tenant,op.id))
            k._event("ObligationBreached",op.id,due=row["due"],discovered_at=now,requirement_id="R10")
        if op.settlement==Settlement.FINAL:
            conn.execute("UPDATE obligation SET status=CASE WHEN breached_at IS NULL THEN 'Satisfied' ELSE 'Breached' END,resolved_at=%s,resolution='FinalEffect',evidence_ref=%s,worker=NULL,lease_until=NULL,generation=generation+1 WHERE tenant_id=%s AND operation_id=%s AND kind='Reconcile'",
                (now,"operation:"+op.id+":"+str(op.revision),db.tenant,op.id))
            k._event("ObligationResolved",op.id,resolution="FinalEffect",at=now,requirement_id="R10")

class PostgresSupervisor:
    def __init__(self,kernel):self.db=kernel

    @staticmethod
    def _text(value):return type(value) is str and 0<len(value)<=256

    def sweep(self):
        expired=self.db.reap_expired_leases()
        def recover(k,c):
            rows=c.execute("SELECT operation_id FROM obligation WHERE tenant_id=%s AND resolution IS NULL AND worker IS NOT NULL AND lease_until<=%s FOR UPDATE",(self.db.tenant,k._clock())).fetchall()
            for row in rows:
                c.execute("UPDATE obligation SET status=CASE WHEN breached_at IS NULL THEN 'Open' ELSE 'Breached' END,worker=NULL,lease_until=NULL,generation=generation+1 WHERE tenant_id=%s AND operation_id=%s AND kind='Reconcile'",(self.db.tenant,row["operation_id"]))
                k._event("ObligationLeaseExpired",row["operation_id"],at=k._clock(),requirement_id="R10")
            return {"expired_sends":expired,"expired_claims":len(rows)}
        return self.db._transaction("supervisor-sweep",recover)

    def claim(self,worker,lease_seconds=30):
        if not self._text(worker) or type(lease_seconds) is not int or not 1<=lease_seconds<=3600:raise Reject("INVALID_LEASE")
        self.sweep()
        def claim(k,c):
            row=c.execute("SELECT * FROM obligation WHERE tenant_id=%s AND resolution IS NULL AND worker IS NULL AND next_attempt<=%s ORDER BY due,operation_id LIMIT 1 FOR UPDATE",(self.db.tenant,k._clock())).fetchone()
            if row is None:return None
            generation=row["generation"]+1
            c.execute("UPDATE obligation SET status=CASE WHEN breached_at IS NULL THEN 'Claimed' ELSE 'Breached' END,worker=%s,lease_until=%s,generation=%s WHERE tenant_id=%s AND operation_id=%s AND kind='Reconcile'",
                (worker,k._clock()+lease_seconds,generation,self.db.tenant,row["operation_id"]))
            k._event("ObligationClaimed",row["operation_id"],worker=worker,generation=generation,due=row["due"],requirement_id="R10")
            return {"operation":k._operations[row["operation_id"]],"worker":worker,"generation":generation,"due":row["due"]}
        return self.db._transaction("claim-obligation",claim)

    def _fence(self,k,c,operation_id,worker,generation):
        if not self._text(operation_id) or not self._text(worker) or type(generation) is not int:raise Reject("STALE_OBLIGATION_CLAIM")
        row=c.execute("SELECT * FROM obligation WHERE tenant_id=%s AND operation_id=%s AND kind='Reconcile' FOR UPDATE",(self.db.tenant,operation_id)).fetchone()
        if row is None or row["resolution"] is not None or row["worker"]!=worker or row["generation"]!=generation or row["lease_until"] is None or row["lease_until"]<=k._clock():raise Reject("STALE_OBLIGATION_CLAIM")
        return row

    def defer(self,operation_id,worker,generation,reason="NoTerminalEvidence",retry_after=30):
        if not self._text(reason) or type(retry_after) is not int or not 1<=retry_after<=3600:raise Reject("INVALID_RETRY")
        def defer(k,c):
            self._fence(k,c,operation_id,worker,generation)
            c.execute("UPDATE obligation SET status=CASE WHEN breached_at IS NULL THEN 'Open' ELSE 'Breached' END,worker=NULL,lease_until=NULL,next_attempt=%s,generation=generation+1 WHERE tenant_id=%s AND operation_id=%s AND kind='Reconcile'",(k._clock()+retry_after,self.db.tenant,operation_id))
            k._event("ReconciliationDeferred",operation_id,reason=reason,requirement_id="R10")
        return self.db._transaction("defer-obligation",defer)

    def register_receiver(self,receiver,expires):
        """Trusted control-plane registration, not a client self-assertion."""
        if not self._text(receiver) or type(expires) is not int:raise Reject("INVALID_HANDOFF_RECEIVER")
        def register(k,c):
            if expires<=k._clock():raise Reject("INVALID_HANDOFF_RECEIVER")
            self.db._upsert(c,"handoff_receiver",("tenant_id","id"),{"tenant_id":self.db.tenant,"id":receiver,"expires":expires})
            k._event("HandoffReceiverRegistered",receiver=receiver,expires=expires)
        return self.db._transaction("register-receiver",register)

    def accept_handoff(self,operation_id,worker,generation,receiver,receipt_id):
        """Host-verified acceptance callback; never interpret a sent notice as ACK."""
        if not self._text(receiver) or not self._text(receipt_id):raise Reject("INVALID_HANDOFF_ACK")
        def accept(k,c):
            prior=c.execute("SELECT operation_id FROM handoff_receipt WHERE tenant_id=%s AND receiver=%s AND receipt_id=%s",(self.db.tenant,receiver,receipt_id)).fetchone()
            if prior:
                if prior["operation_id"]!=operation_id:raise Reject("HANDOFF_RECEIPT_CONFLICT")
                return "Escalated"
            self._fence(k,c,operation_id,worker,generation)
            target=c.execute("SELECT expires FROM handoff_receiver WHERE tenant_id=%s AND id=%s",(self.db.tenant,receiver)).fetchone()
            if target is None or target["expires"]<=k._clock():raise Reject("UNAUTHORIZED_HANDOFF_RECEIVER")
            # Record a breach before resolving if the acceptance is already late.
            sync_obligations(self.db,c,k)
            c.execute("INSERT INTO handoff_receipt VALUES(%s,%s,%s,%s,%s)",(self.db.tenant,receiver,receipt_id,operation_id,k._clock()))
            c.execute("UPDATE obligation SET owner=%s,status=CASE WHEN breached_at IS NULL THEN 'Satisfied' ELSE 'Breached' END,resolved_at=%s,resolution='Escalated',evidence_ref=%s,worker=NULL,lease_until=NULL,generation=generation+1 WHERE tenant_id=%s AND operation_id=%s AND kind='Reconcile'",
                (receiver,k._clock(),receipt_id,self.db.tenant,operation_id))
            k._event("HandoffAccepted",operation_id,receiver=receiver,receipt=receipt_id,at=k._clock(),requirement_id="R10")
            return "Escalated"
        return self.db._transaction("accept-handoff",accept)

    def run_once(self,worker,service):
        from .simulator import RefundService
        if type(service) is not RefundService:raise Reject("UNSUPPORTED_RECONCILIATION_ADAPTER")
        claim=self.claim(worker)
        if claim is None:return {"status":"Idle"}
        op=claim["operation"]
        if service.requires_envelope:self.db.trusted.require_authenticated_receipts(op.id)
        try:receipt=service.query(op.id)  # This simulator's registered query is free and read-only.
        except Exception:
            self.defer(op.id,worker,claim["generation"],"QueryFailed")
            return {"status":"Deferred","operation_id":op.id}
        if receipt is None:
            self.defer(op.id,worker,claim["generation"])
            return {"status":"Deferred","operation_id":op.id}
        if type(receipt) not in (Observation,str) or (type(receipt) is Observation and (receipt.operation_id!=op.id or receipt.request_digest!=op.request.request_digest)):
            self.defer(op.id,worker,claim["generation"],"MismatchedReceipt")
            return {"status":"RejectedReceipt","operation_id":op.id}
        try:result=self.db.trusted.observe(receipt,expected_operation_id=op.id)
        except Reject as exc:
            self.defer(op.id,worker,claim["generation"],exc.code)
            return {"status":"RejectedReceipt","operation_id":op.id,"reason":exc.code}
        if result.settlement!=Settlement.FINAL:self.defer(op.id,worker,claim["generation"],"NonFinalObservation")
        return {"status":"Reconciled" if result.settlement==Settlement.FINAL else "Deferred","operation_id":op.id}
