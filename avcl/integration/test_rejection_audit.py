"""Real transaction, restart, crash and concurrency checks for rejection audit."""
import json
import unittest
from unittest.mock import patch
import psycopg
import test_postgres
import test_m1_processes
from avcl.canonical import Reject,digest
from avcl.gateway import RuntimeGateway,GatewayAuthority,SCOPES
from avcl.rejection_audit import AuthenticatedContext
from tests.support import authorize,prepare


class RejectionAuditTests(unittest.TestCase):
    setUpClass=classmethod(test_postgres.PersistenceTests.setUpClass.__func__)
    tearDown=test_postgres.PersistenceTests.tearDown
    fresh=test_postgres.PersistenceTests.fresh
    query=test_postgres.PersistenceTests.query
    crash=test_postgres.PersistenceTests.crash
    overlap=test_m1_processes.M1ProcessTests.overlap

    def setUp(self):
        test_postgres.PersistenceTests.setUp(self)
        self.authority=GatewayAuthority(self.k)
        self.credential=self.authority.issue("a",SCOPES,900)
        self.auth=authorize(self.k,self.s)
        self.checked=self.authority.register_checked(self.auth[0],self.auth[2],800)
        self.body={"client_request_id":"audit1","checked_ref":self.checked,"capability_ref":self.auth[1],
                   "approval_refs":[self.auth[3]],"business_action_key":self.auth[0].business_key}
        self.gateway=RuntimeGateway(self.dsn,self.schema,self.clock)

    def call(self,body=None,token=None,method="POST",path="/v1/operations/prepare"):
        return self.gateway.handle(method,path,"Bearer "+(token or self.credential.token),
                                   "" if method=="GET" else json.dumps(self.body if body is None else body))

    def denied(self):return [e for e in self.fresh().events() if e.kind=="ActionRejected"]

    def snapshot(self):
        tables=("budget_account","operation","reservation","permit","approval","request_dedup","business_action",
                "outbox","api_request","checked_request","obligation","workflow_run","capacity_policy")
        return {table:sorted(self.query("SELECT * FROM "+table),key=repr) for table in tables}

    def context(self):return AuthenticatedContext("a","prepare",digest({"case":"test"}),"trace-test")

    def test_budget_rejection_is_durable_and_same_request_can_later_succeed(self):
        other=prepare(self.k,authorize(self.k,self.s,"b",60),"other")
        before=self.snapshot();events=self.k.events()
        status,response=self.call()
        self.assertEqual((status,response["error"]["code"]),(409,"BUDGET_EXCEEDED"))
        self.assertEqual(self.snapshot(),before)
        event=self.denied()[0];data=dict(event.details)
        self.assertEqual(event.sequence,len(events)+1)
        self.assertEqual((data["actor"],data["code"],json.loads(data["requirement_ids"])),("a","BUDGET_EXCEEDED",["R04"]))
        self.assertEqual(data["budget_deltas"],"{}")
        self.assertNotIn(self.credential.token,repr(event));self.assertNotIn(self.body["approval_refs"][0],repr(event))
        self.k.abort_prepared("b",other.id)
        self.assertEqual(self.call()[0],200)
        self.assertEqual(len(self.denied()),1)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)

    def test_savepoint_discards_partial_sql_memory_events_and_guard_revision(self):
        before=self.snapshot();events=self.k.events();guard=self.query("SELECT * FROM authority_guard")[0]
        def fail(k,c):
            k.prepare("a",*self.auth,"partial")
            self.k._save(c,k)
            raise Reject("BUDGET_EXCEEDED")
        with self.assertRaises(Reject) as caught:
            self.k._transaction("partial",fail,authenticate=lambda k,c:self.context())
        self.assertEqual(caught.exception.code,"BUDGET_EXCEEDED")
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.query("SELECT * FROM authority_guard")[0],guard)
        self.assertEqual(self.k.events()[:-1],events)
        self.assertEqual(caught.exception.audit_sequence,len(events)+1)
        self.assertEqual(dict(self.denied()[0].details)["event_id"],caught.exception.audit_event_id)

    def test_subject_and_tenant_quotas_bound_growth_without_poisoning_business(self):
        self.k.trusted.configure_rejection_audit(2,1,10)
        bad={**self.body,"business_action_key":"wrong"}
        self.assertEqual(self.call(bad)[0],409)
        status,response=self.call(bad)
        self.assertEqual((status,response["error"]["code"]),(503,"AUDIT_CAPACITY_EXCEEDED"))
        self.clock.now=10;self.assertEqual(self.call(bad)[0],409)
        self.clock.now=20;self.assertEqual(self.call(bad)[0],503)
        self.assertEqual(len(self.denied()),2)
        self.assertEqual(self.query("SELECT recorded FROM rejection_audit_policy"),[{"recorded":2}])
        self.assertEqual(self.query("SELECT last_clock FROM authority_guard"),[{"last_clock":10}])
        self.assertEqual(self.call()[0],200)
        self.assertEqual(len(self.query("SELECT * FROM rejection_audit_subject")),1)

    def test_authenticated_scope_is_logged_but_revoked_credentials_are_not(self):
        readonly=self.authority.issue("a",frozenset({"get"}),900)
        self.assertEqual(self.call(token=readonly.token)[0],403)
        self.assertEqual(dict(self.denied()[0].details)["code"],"API_SCOPE_DENIED")
        self.authority.revoke(readonly.id)
        self.assertEqual(self.call(token=readonly.token)[0],401)
        self.assertEqual(self.call(token="avcl_"+"x"*43)[0],401)
        self.assertEqual(self.call({"invalid":"shape"})[0],400)
        self.assertEqual(len(self.denied()),1)

    def test_foreign_and_missing_objects_keep_masking_and_no_foreign_revision(self):
        op=prepare(self.k,authorize(self.k,self.s,"b",20),"other")
        for ref in (op.id,"op-"+"0"*32):
            status,response=self.call(method="GET",path="/v1/operations/"+ref)
            self.assertEqual((status,response["error"]["code"]),(404,"OPERATION_NOT_FOUND"))
            self.assertIsNone(response["error"]["detail_ref"])
        for e in self.denied():
            self.assertIsNone(e.operation_id)
            self.assertEqual(dict(e.details)["operation_revision"],"null")

    def test_operation_revision_policy_epoch_and_clock_are_observed_under_guard(self):
        op=self.call()[1]["data"];before=self.snapshot()
        self.clock.now=10
        status,_=self.call({"client_request_id":"wrong-permit","permit_ref":"forged"},
                           path="/v1/operations/"+op["operation_id"]+"/dispatch")
        self.assertEqual(status,403);self.assertEqual(self.snapshot(),before)
        e=self.denied()[0];d=dict(e.details)
        self.assertEqual((e.operation_id,d["operation_revision"],d["policy_digest"]),
                         (op["operation_id"],op["revision"],op["contract_digest"]))
        self.assertEqual((d["epoch"],d["time"]),("1","10"))
        self.assertEqual(self.query("SELECT last_clock FROM authority_guard"),[{"last_clock":10}])

    def test_audit_write_failure_returns_unavailable_and_rolls_back_everything(self):
        before=self.snapshot();events=self.k.events()
        self.query("CREATE FUNCTION fail_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected audit unavailable'; END; $$")
        self.query("CREATE TRIGGER fail_audit BEFORE INSERT ON audit_event FOR EACH ROW EXECUTE FUNCTION fail_audit()")
        status,response=self.call({**self.body,"business_action_key":"wrong"})
        self.assertEqual((status,response["error"]["code"]),(503,"DEPENDENCY_UNAVAILABLE"))
        self.assertEqual(self.snapshot(),before);self.assertEqual(self.k.events(),events)
        self.assertEqual(self.query("SELECT recorded FROM rejection_audit_policy"),[{"recorded":0}])
        self.assertEqual(self.query("SELECT * FROM rejection_audit_subject"),[])

    def test_crash_before_and_after_rejection_commit(self):
        before=self.snapshot();events=self.k.events()
        for suffix,count in (("before_commit",0),("after_commit",1)):
            self.crash("audit-rejection",("a",*self.auth,"crash"),"audit-rejection.rejected."+suffix)
            self.assertEqual(self.snapshot(),before)
            self.assertEqual(len(self.denied()),count)
            self.assertEqual(self.k.events()[:len(events)],events)
        # Retrying the rejected business key is still legal; no API/dedup tombstone.
        self.assertEqual(self.call()[0],200)

    def test_two_processes_success_and_rejection_have_contiguous_events(self):
        auth_b=authorize(self.k,self.s,"b",60)
        results=self.overlap("audited-prepare",[("a",*self.auth,"parallel"),("b",*auth_b,"parallel")])
        self.assertEqual(sum(r["status"]=="accepted" for r in results),1)
        self.assertEqual([r.get("code") for r in results if r["status"]=="rejected"],["BUDGET_EXCEEDED"])
        self.assertEqual(len(self.denied()),1)
        events=self.fresh().events();self.assertEqual([e.sequence for e in events],list(range(1,len(events)+1)))
        self.assertEqual(self.query("SELECT recorded FROM rejection_audit_policy"),[{"recorded":1}])
        self.assertEqual(len(self.query("SELECT * FROM operation")),1)
        self.k.assert_invariants()

    def test_serialization_retry_does_not_duplicate_denial(self):
        from avcl import rejection_audit
        original=rejection_audit.record;calls=[]
        def conflict(*args):
            receipt=original(*args);calls.append(receipt)
            if len(calls)==1:raise psycopg.errors.SerializationFailure("injected after audit insert")
            return receipt
        with patch.object(rejection_audit,"record",side_effect=conflict):
            self.assertEqual(self.call({**self.body,"business_action_key":"wrong"})[0],409)
        self.assertEqual(len(calls),2);self.assertEqual(len(self.denied()),1)
        self.assertEqual(self.query("SELECT recorded FROM rejection_audit_policy"),[{"recorded":1}])

    def test_two_processes_cannot_overrun_last_audit_slot(self):
        self.k.trusted.configure_rejection_audit(1,100,60)
        auth_b=authorize(self.k,self.s,"b",60)
        args=[(a[0].subject,a[0],"forged",a[2],a[3],"quota") for a in (self.auth,auth_b)]
        results=self.overlap("audited-prepare",args)
        self.assertEqual(sorted(r["code"] for r in results),["AUDIT_CAPACITY_EXCEEDED","UNTRUSTED_REFERENCE"])
        self.assertEqual(len(self.denied()),1)
        self.assertEqual(self.query("SELECT recorded FROM rejection_audit_policy"),[{"recorded":1}])
        self.assertEqual(self.query("SELECT * FROM operation"),[])

    def test_invalid_context_unknown_tenant_and_large_event_never_claim_recording(self):
        before=self.k.events()
        def fail(k,c):raise Reject("BUDGET_EXCEEDED")
        contexts=[None,AuthenticatedContext("a","prepare","bad","trace"),
                  AuthenticatedContext("\ud800","prepare",digest({}),"trace")]
        for context in contexts:
            with self.assertRaises(Reject) as caught:
                self.k._transaction("invalid",fail,authenticate=lambda k,c:context)
            self.assertEqual(caught.exception.code,"INVALID_REJECTION_CONTEXT")
            self.assertFalse(hasattr(caught.exception,"audit_event_id"))
        with self.assertRaises(Reject) as caught:
            self.fresh(tenant="unknown")._transaction("unknown",fail,authenticate=lambda k,c:self.context())
        self.assertEqual(caught.exception.code,"UNKNOWN_TENANT")
        def too_large(k,c):raise Reject("BUDGET_EXCEEDED",requirement_ids=tuple("RULE_"+str(i)+"x"*100 for i in range(200)))
        with self.assertRaises(Reject) as caught:
            self.k._transaction("large",too_large,authenticate=lambda k,c:self.context())
        self.assertEqual(caught.exception.code,"AUDIT_RECORD_TOO_LARGE")
        self.assertFalse(hasattr(caught.exception,"audit_event_id"))
        self.assertEqual(self.k.events(),before)
        self.assertEqual(self.query("SELECT recorded FROM rejection_audit_policy"),[{"recorded":0}])

    def test_policy_change_is_not_relabelled_and_clock_rollback_cannot_reset_quota(self):
        bad={**self.body,"business_action_key":"wrong"}
        self.k.trusted.configure_rejection_audit(10,1,60)
        self.clock.now=30;self.assertEqual(self.call(bad)[0],409)
        self.k.trusted.revoke()
        self.clock.now=0
        self.assertEqual(self.call(bad)[0],503)
        self.clock.now=90;self.assertEqual(self.call(bad)[0],409)
        first,last=(dict(e.details) for e in self.denied())
        self.assertEqual((first["epoch"],last["epoch"]),("1","2"))
        self.assertEqual((first["time"],last["time"]),("30","90"))


if __name__=="__main__":unittest.main()
