from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
import json
import psycopg
import test_postgres
import test_gateway
from avcl.canonical import Reject
from avcl.gateway import GatewayAuthority,SCOPES
from avcl.storage_codec import loads
from avcl.types import Settlement
from tests.test_delegation import provision,payload,authorization,prepare

class DurableDelegationTests(unittest.TestCase):
    setUpClass=classmethod(test_postgres.PersistenceTests.setUpClass.__func__)
    tearDown=test_postgres.PersistenceTests.tearDown
    fresh=test_postgres.PersistenceTests.fresh
    query=test_postgres.PersistenceTests.query
    error=test_postgres.PersistenceTests.error
    crash=test_postgres.PersistenceTests.crash

    def setUp(self):
        test_postgres.PersistenceTests.setUp(self);self.root_account=provision(self.k)

    def child(self,**changes):return self.k.delegate("parent",payload(**changes))["child_cap_ref"]

    def test_restart_preserves_grants_and_rechecks_parent_revocation(self):
        ref=self.child();auth=authorization(self.k,self.s,ref);op=prepare(self.fresh(),auth)
        self.k.trusted.revoke_capability("root")
        self.error("AUTH_DENIED",lambda:self.fresh().dispatch("child",op.id,op.permit_ref))
        self.assertEqual(self.fresh().accounts()[self.root_account].pending,60)
        self.fresh().abort_prepared("child",op.id)
        self.assertEqual(self.fresh().accounts()[self.root_account].pending,0)

    def test_concurrent_same_delegation_commits_one_child_and_account(self):
        barrier=Barrier(2)
        def run(_):
            k=self.fresh();barrier.wait();return k.delegate("parent",payload())
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(len(self.query("SELECT * FROM delegation_grant")),2)
        self.assertEqual(sum(e.kind=="CapabilityDelegated" for e in self.k.events()),1)

    def test_concurrent_siblings_compete_for_single_root(self):
        one=self.child();two=self.child(child="sibling",request_id="sibling")
        auths=[authorization(self.k,self.s,one),authorization(self.k,self.s,two,"sibling","b",60,case="sibling")]
        barrier=Barrier(2)
        def run(auth):
            k=self.fresh();barrier.wait()
            try:return prepare(k,auth)
            except Reject as exc:return exc.code
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,auths))
        self.assertEqual(results.count("BUDGET_EXCEEDED"),1)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,60);self.assertEqual(self.k.accounts()[self.root_account].pending,60)
        self.assertEqual(len(self.query("SELECT * FROM operation")),1);self.k.assert_invariants()

    def test_real_crash_before_commit_leaves_no_child_or_quota(self):
        self.crash("delegate",("parent",payload()),"delegate.before_commit")
        self.assertEqual(len(self.query("SELECT * FROM capability")),3)
        self.assertEqual(len(self.query("SELECT * FROM delegation_grant")),1)
        self.assertEqual(len(self.query("SELECT * FROM budget_account")),4)
        self.assertEqual(self.query("SELECT * FROM delegation_request"),[])
        self.assertTrue(self.fresh().delegate("parent",payload())["child_cap_ref"])

    def test_real_crash_after_commit_returns_original_grant(self):
        self.crash("delegate",("parent",payload()),"delegate.after_commit")
        ref=self.query("SELECT capability_ref FROM delegation_request")[0]["capability_ref"]
        self.assertEqual(self.fresh().delegate("parent",payload())["child_cap_ref"],ref)
        self.assertEqual(sum(e.kind=="CapabilityDelegated" for e in self.k.events()),1)

    def test_unknown_and_raw_receipt_recovery_settle_every_quota_after_revoke(self):
        ref=self.child();op=prepare(self.k,authorization(self.k,self.s,ref))
        op=self.k.dispatch("child",op.id,op.permit_ref);receipt=self.s.send(op);self.k.trusted.unknown(op.id)
        self.k.trusted.revoke_capability("root")
        self.crash("observe",(receipt,),"observe.after_raw_commit")
        self.assertEqual(self.fresh().accounts()[self.root_account].pending,60)
        self.fresh().recover_observations();result=self.fresh().get("child",op.id)
        self.assertEqual(result.settlement,Settlement.FINAL)
        self.assertTrue(all((r.charged,r.remaining)==(60,0) for r in result.reservations))
        self.assertEqual(self.s.real_cost,60);self.k.assert_invariants()

    def test_revocation_race_has_one_order_and_no_later_admission(self):
        ref=self.child();op=prepare(self.k,authorization(self.k,self.s,ref));barrier=Barrier(2)
        def run(action):
            k=self.fresh();barrier.wait()
            if action=="revoke":k.trusted.revoke_capability("root");return "Revoked"
            try:k.dispatch("child",op.id,op.permit_ref);return "Admitted"
            except Reject as exc:return exc.code
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,("dispatch","revoke")))
        self.assertIn(results[0],("Admitted","AUTH_DENIED"))
        events=self.k.events();revoked=next(e.sequence for e in events if e.kind=="CapabilityRevoked")
        self.assertTrue(all(e.sequence<revoked for e in events if e.kind=="DispatchAdmitted"))
        self.assertEqual(self.k.accounts()[self.root_account].pending,60)

    def test_clock_rollback_cannot_mint_child(self):
        self.clock.now=5;self.k.trusted.register_subject("clock-principal")
        self.clock.now=4
        self.error("CLOCK_ROLLBACK",lambda:self.child())
        self.assertEqual(self.query("SELECT * FROM delegation_request"),[])

    def test_policy_epoch_change_invalidates_full_tree(self):
        ref=self.child();auth=authorization(self.k,self.s,ref)
        self.k.trusted.revoke()
        self.error("AUTH_DENIED",lambda:prepare(self.k,auth))
        self.error("AUTH_DENIED",lambda:self.child(request_id="new"))

    def test_frozen_grant_cannot_be_expanded_or_unrevoked_in_storage(self):
        ref=self.child();row=self.query("SELECT payload FROM delegation_grant WHERE capability_ref=%s",(ref,))[0]
        payload_value=json.loads(row["payload"]);payload_value["fields"]["can_delegate"]=True
        with self.assertRaises(psycopg.errors.RaiseException):
            self.query("UPDATE delegation_grant SET payload=%s WHERE capability_ref=%s",(json.dumps(payload_value),ref))
        self.k.trusted.revoke_capability(ref)
        with self.assertRaises(psycopg.errors.RaiseException):
            self.query("UPDATE delegation_grant SET payload=%s WHERE capability_ref=%s",(row["payload"],ref))
        for value in (None,"false"):
            malformed=json.loads(row["payload"])
            if value is None:malformed["fields"].pop("revoked")
            else:malformed["fields"]["revoked"]=value
            with self.assertRaises(psycopg.errors.RaiseException):
                self.query("UPDATE delegation_grant SET payload=%s WHERE capability_ref=%s",(json.dumps(malformed),ref))
        self.assertTrue(loads(self.query("SELECT payload FROM delegation_grant WHERE capability_ref=%s",(ref,))[0]["payload"]).revoked)

class DelegationGatewayTests(unittest.TestCase):
    setUpClass=classmethod(test_gateway.GatewayTests.setUpClass.__func__)
    tearDown=test_gateway.GatewayTests.tearDown
    fresh=test_gateway.GatewayTests.fresh
    query=test_gateway.GatewayTests.query
    launch_server=test_gateway.GatewayTests.launch_server
    cleanup_server=test_gateway.GatewayTests.cleanup_server
    call=test_gateway.GatewayTests.call

    def setUp(self):
        test_gateway.GatewayTests.setUp(self);provision(self.k)
        self.credential=self.authority.issue("parent",SCOPES,900)

    def test_authenticated_delegate_and_child_use_through_http(self):
        status,result=self.call("POST","/v1/capabilities/delegate",raw=payload())
        self.assertEqual(status,200,result);ref=result["data"]["child_cap_ref"]
        retry=self.call("POST","/v1/capabilities/delegate",raw=payload())
        self.assertEqual(retry[1]["data"],result["data"])
        request,cap,ev,approval=authorization(self.k,self.s,ref)
        checked=self.authority.register_checked(request,ev,800)
        token=self.authority.issue("child",SCOPES,900).token
        body={"client_request_id":"child-prepare","checked_ref":checked,"capability_ref":cap,"approval_refs":[approval],"business_action_key":request.business_key}
        status,result=self.call("POST","/v1/operations/prepare",body,token)
        self.assertEqual(status,200,result);op=result["data"]
        status,result=self.call("POST","/v1/operations/"+op["operation_id"]+"/dispatch",{"client_request_id":"send","permit_ref":op["permit_ref"]},token)
        self.assertEqual(status,202,result)
        self.assertEqual(len(result["data"]["reservations"]),4)

    def test_scope_identity_and_revoked_credential_cannot_delegate(self):
        token=self.authority.issue("parent",frozenset({"get"}),900).token
        self.assertEqual(self.call("POST","/v1/capabilities/delegate",token=token,raw=payload())[0],403)
        token=self.authority.issue("child",SCOPES,900).token
        self.assertEqual(self.call("POST","/v1/capabilities/delegate",token=token,raw=payload())[0],403)
        self.assertEqual(self.call("POST","/v1/capabilities/delegate",raw=payload())[0],200)
        self.authority.revoke(self.credential.id)
        self.assertEqual(self.call("POST","/v1/capabilities/delegate",raw=payload())[0],401)

    def test_parent_revocation_blocks_cached_response_and_dispatch(self):
        status,result=self.call("POST","/v1/capabilities/delegate",raw=payload())
        self.assertEqual(status,200,result);ref=result["data"]["child_cap_ref"]
        op=prepare(self.k,authorization(self.k,self.s,ref));token=self.authority.issue("child",SCOPES,900).token
        self.k.trusted.revoke_capability("root")
        self.assertEqual(self.call("POST","/v1/capabilities/delegate",raw=payload())[0],403)
        self.assertEqual(self.call("POST","/v1/operations/"+op.id+"/dispatch",{"client_request_id":"send","permit_ref":op.permit_ref},token)[0],403)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,60)

if __name__=="__main__":unittest.main()
