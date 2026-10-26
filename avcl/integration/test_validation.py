from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
import json
import unittest
import psycopg
import test_gateway
from avcl.gateway import GatewayAuthority,RuntimeGateway,SCOPES
from avcl.postgres import PostgresKernel
from avcl.validation import ValidationAuthority
from avcl.types import Approval
import test_postgres

G=test_gateway.GatewayTests
ROUTE="/v1/candidates/validate"

class ValidationTests(unittest.TestCase):
    setUpClass=classmethod(G.setUpClass.__func__)
    fresh=G.fresh
    query=G.query
    error=G.error
    call=G.call
    launch_server=G.launch_server
    cleanup_server=G.cleanup_server
    tearDown=G.tearDown
    crash=test_postgres.PersistenceTests.crash

    def setUp(self):
        test_gateway.GatewayTests.setUp(self)
        self.va=ValidationAuthority(self.k)
        order,fee=self.s.read_order("a")
        self.context=self.va.register_order_snapshot("a","a",order,fee,500)
        self.candidate={key:value for key,value in self.auth[0].payload().items() if key not in {"tenant","subject"}}
        self.body={"client_request_id":"validate1","candidate":self.candidate,"validator_id":"order-service","context_refs":[self.context]}

    def validated(self,body=None):
        status,response=self.call("POST",ROUTE,self.body if body is None else body)
        self.assertEqual(status,200,response);return response["data"]

    def prepare_validated(self,result):
        status,response=self.call("POST","/v1/operations/prepare",{**self.payload,"checked_ref":result["checked_ref"]})
        self.assertEqual(status,200,response);return response["data"]

    def test_validation_has_no_authority_then_prepares_and_dispatches(self):
        result=self.validated()
        self.assertEqual(result["status"],"RuntimeSatisfied")
        self.assertEqual(result["predicate_id"],"RefundEligible")
        audit=next(e for e in self.k.events() if e.kind=="CandidateValidated")
        self.assertTrue(dict(audit.details)["trace_id"].startswith("trace-"))
        self.assertEqual(dict(audit.details)["request_id"],self.body["client_request_id"])
        self.assertEqual(self.query("SELECT * FROM operation"),[])
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,0)
        self.assertEqual(len(self.query("SELECT * FROM capability")),2)
        self.assertEqual(len(self.query("SELECT * FROM approval")),1)
        prepared={**self.payload,"checked_ref":result["checked_ref"]}
        self.assertEqual(self.call("POST","/v1/operations/prepare",{**prepared,"capability_ref":"forged"})[0],403)
        self.assertEqual(self.call("POST","/v1/operations/prepare",{**prepared,"approval_refs":["forged"]})[0],403)
        op=self.prepare_validated(result)
        status,_=self.call("POST","/v1/operations/"+op["operation_id"]+"/dispatch",{"client_request_id":"dispatch1","permit_ref":op["permit_ref"]})
        self.assertEqual(status,202)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        claim=self.k.claim_outbox("host-sender")
        self.k.trusted.observe(self.s.send(claim["operation"]))
        status,response=self.call("GET","/v1/operations/"+op["operation_id"])
        self.assertEqual(status,200)
        self.assertEqual(response["data"]["execution_state"],"Confirmed")
        self.assertEqual(response["data"]["settlement_state"],"Final")
        self.assertEqual(self.s.real_cost,70)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,0)

    def test_different_business_request_still_requires_matching_approval(self):
        result=self.validated({**self.body,"candidate":{**self.candidate,"business_key":"new-case"}})
        status,response=self.call("POST","/v1/operations/prepare",{**self.payload,"checked_ref":result["checked_ref"],"business_action_key":"new-case"})
        self.assertEqual((status,response["error"]["code"]),(409,"APPROVAL_MISMATCH"))
        self.assertEqual(self.query("SELECT * FROM operation"),[])

    def test_client_cannot_assert_identity_facts_or_predicate(self):
        for key in ("tenant","subject","predicate_id","remaining","issuer","expires"):
            body={**self.body,"candidate":{**self.candidate,key:"attacker"}}
            self.assertEqual(self.call("POST",ROUTE,body)[0],400)
        for key in ("evidence_refs","allow","snapshot","checked_ref"):
            self.assertEqual(self.call("POST",ROUTE,{**self.body,key:"forged"})[0],400)
        self.assertEqual(self.call("POST",ROUTE,{**self.body,"candidate":{**self.candidate,"amount_minor":1}})[0],400)
        self.assertEqual(self.query("SELECT * FROM candidate_validation"),[])
        self.assertEqual(len(self.query("SELECT * FROM evidence")),1)

    def test_eligibility_is_evaluated_against_host_snapshot(self):
        for key,value in (("order","b"),("order_version","v2"),("destination","attacker"),("amount_minor","101")):
            status,response=self.call("POST",ROUTE,{**self.body,"candidate":{**self.candidate,key:value}})
            self.assertEqual((status,response["error"]["code"]),(409,"INELIGIBLE"))
        order,_=self.s.read_order("a")
        ref=self.va.register_order_snapshot("a","a",order,2,500)
        status,response=self.call("POST",ROUTE,{**self.body,"context_refs":[ref]})
        self.assertEqual((status,response["error"]["code"]),(409,"INELIGIBLE"))
        self.assertEqual(self.query("SELECT * FROM candidate_validation"),[])

    def test_only_registered_fixed_validator_and_owned_context_are_accepted(self):
        for identifier in ("structural-checker","attacker","RefundEligible"):
            status,response=self.call("POST",ROUTE,{**self.body,"validator_id":identifier})
            self.assertEqual((status,response["error"]["code"]),(409,"UNREGISTERED_VALIDATOR"))
        order,fee=self.s.read_order("b")
        other=self.va.register_order_snapshot("b","b",order,fee,500)
        foreign=PostgresKernel(self.dsn,tenant="other",schema=self.schema,clock=self.clock);foreign.initialize()
        foreign_ref=ValidationAuthority(foreign).register_order_snapshot("a","b",order,fee,500)
        for ref in (other,foreign_ref,"forged"):
            status,response=self.call("POST",ROUTE,{**self.body,"context_refs":[ref]})
            self.assertEqual((status,response["error"]["code"]),(403,"AUTH_DENIED"))

    def test_expiry_rejects_cached_validation_and_prepare(self):
        result=self.validated()
        self.assertEqual(result["not_after_logical"],"300")
        self.clock_file.write_text('{"now":300}',encoding="utf-8")
        self.assertEqual(self.call("POST",ROUTE,self.body)[1]["error"]["code"],"STALE_EVIDENCE")
        status,response=self.call("POST","/v1/operations/prepare",{**self.payload,"checked_ref":result["checked_ref"]})
        self.assertEqual((status,response["error"]["code"]),(409,"STALE_EVIDENCE"))
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,0)

    def test_snapshot_refresh_invalidates_prepare_and_dispatch_in_kernel(self):
        result=self.validated();op=self.prepare_validated(result)
        new_request=replace(self.auth[0],business_key="second")
        self.k.trusted.register_approval(Approval("ap-second",new_request.request_digest,1000))
        second=self.validated({**self.body,"client_request_id":"second","candidate":{**self.candidate,"business_key":"second"}})
        self.s.change_version("a","v2");order,fee=self.s.read_order("a")
        self.va.register_order_snapshot("a","a",order,fee,500)
        self.error("STALE_EVIDENCE",lambda:self.fresh().dispatch("a",op["operation_id"],op["permit_ref"]))
        status,response=self.call("POST",ROUTE,self.body)
        self.assertEqual((status,response["error"]["code"]),(409,"STALE_EVIDENCE"))
        self.assertEqual(self.k.get("a",op["operation_id"]).state,"Prepared")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        self.k.abort_prepared("a",op["operation_id"])
        # A second checked request was never prepared; reject it at the kernel boundary too.
        self.error("STALE_EVIDENCE",lambda:self.fresh().prepare("a",new_request,self.auth[1],second["evidence_refs"][0],"ap-second","new-request"))

    def test_context_revocation_retains_prepared_budget_and_allows_abort(self):
        result=self.validated();op=self.prepare_validated(result)
        self.va.revoke_order_context("a")
        status,response=self.call("POST","/v1/operations/"+op["operation_id"]+"/dispatch",{"client_request_id":"d","permit_ref":op["permit_ref"]})
        self.assertEqual((status,response["error"]["code"]),(409,"STALE_EVIDENCE"))
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        self.k.abort_prepared("a",op["operation_id"])
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,0)

    def test_idempotence_and_api_process_restart_keep_same_references(self):
        result=self.validated();self.assertEqual(self.validated(),result)
        status,response=self.call("POST",ROUTE,{**self.body,"candidate":{**self.candidate,"amount_minor":"60"}})
        self.assertEqual((status,response["error"]["code"]),(409,"IDEMPOTENCY_CONFLICT"))
        self.process.terminate();self.process.wait(timeout=5);self.ready.unlink();self.launch_server()
        self.assertEqual(self.validated(),result)
        self.assertEqual(len(self.query("SELECT * FROM candidate_validation")),1)
        self.assertEqual(sum(e.kind=="CandidateValidated" for e in self.k.events()),1)

    def test_scope_and_revoked_credential_do_not_return_cached_evidence(self):
        other=self.authority.issue("a",frozenset({"get","prepare"}),900)
        self.assertEqual(self.call("POST",ROUTE,self.body,other.token)[0],403)
        self.validated();self.authority.revoke(self.credential.id)
        self.assertEqual(self.call("POST",ROUTE,self.body)[0],401)

    def test_concurrent_validation_requests_share_one_result(self):
        barrier=Barrier(2)
        def run(_):
            gateway=RuntimeGateway(self.dsn,self.schema,self.clock);barrier.wait()
            return gateway.handle("POST",ROUTE,"Bearer "+self.credential.token,json.dumps(self.body))
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,range(2)))
        self.assertTrue(all(status==200 for status,_ in results),results)
        self.assertEqual(results[0][1]["data"],results[1][1]["data"])
        self.assertEqual(len(self.query("SELECT * FROM candidate_validation")),1)

    def test_snapshot_and_validation_records_are_immutable(self):
        self.validated()
        for statement in ("UPDATE order_snapshot SET subject='attacker'","DELETE FROM order_snapshot",
                          "UPDATE candidate_validation SET validator_id='attacker'","DELETE FROM candidate_validation"):
            with self.assertRaises(psycopg.errors.RaiseException):self.query(statement)

    def test_v5_migration_retains_existing_gateway_credentials_and_checked_refs(self):
        self.query("DROP TRIGGER audit_requires_allocation ON audit_event")
        self.query("DROP FUNCTION require_audit_allocation()")
        self.query("DROP TABLE audit_counter")
        self.query("DROP TABLE rejection_audit_subject, rejection_audit_policy")
        self.assertEqual(self.query("SELECT * FROM delegation_grant"),[])
        self.query("DROP TABLE delegation_request, delegation_grant, delegation_subject")
        self.query("DROP FUNCTION freeze_delegation_grant()")
        self.query("DROP TABLE task_close_request, task_run")
        self.query("DROP FUNCTION freeze_task_definition()")
        self.query("DROP TABLE candidate_validation, order_context_head, order_snapshot")
        self.query("ALTER TABLE schema_version DROP CONSTRAINT schema_version_version_check")
        self.query("UPDATE schema_version SET version=5")
        self.query("ALTER TABLE schema_version ADD CHECK(version BETWEEN 1 AND 5)")
        self.k.migrate();self.k.migrate()
        status,response=self.call("POST","/v1/operations/prepare",self.payload)
        self.assertEqual(status,200,response)
        self.assertEqual(response["data"]["execution_state"],"Prepared")

    def test_validation_process_death_before_commit_leaves_no_partial_artifacts(self):
        self.crash("validation",("a",json.dumps(self.body)),"api-validate.before_commit")
        self.assertEqual(self.query("SELECT * FROM candidate_validation"),[])
        self.assertEqual(len(self.query("SELECT * FROM evidence")),1)
        self.assertEqual(len(self.query("SELECT * FROM checked_request")),1)
        self.validated()
        self.assertEqual(len(self.query("SELECT * FROM candidate_validation")),1)

    def test_validation_process_death_after_commit_reuses_all_artifacts(self):
        self.crash("validation",("a",json.dumps(self.body)),"api-validate.after_commit")
        row=self.query("SELECT * FROM candidate_validation")[0]
        result=self.validated()
        self.assertEqual(result["checked_ref"],row["checked_ref"])
        self.assertEqual(result["evidence_refs"],[row["evidence_ref"]])
        self.assertEqual(sum(e.kind=="CandidateValidated" for e in self.k.events()),1)

    def test_clock_rollback_and_expired_context_cannot_create_evidence(self):
        self.clock.now=10
        order,fee=self.s.read_order("a")
        ref=self.va.register_order_snapshot("a","a",order,fee,11)
        body={**self.body,"context_refs":[ref]}
        status,response=self.call("POST",ROUTE,body)
        self.assertEqual((status,response["error"]["code"]),(409,"CLOCK_ROLLBACK"))
        self.clock_file.write_text('{"now":11}',encoding="utf-8")
        status,response=self.call("POST",ROUTE,body)
        self.assertEqual((status,response["error"]["code"]),(409,"STALE_EVIDENCE"))
        self.assertEqual(self.query("SELECT * FROM candidate_validation"),[])

del G  # Avoid discovering the imported GatewayTests a second time.
if __name__=="__main__":unittest.main()
