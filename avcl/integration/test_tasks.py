from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
import unittest
import psycopg
import test_gateway
import test_postgres
from avcl.canonical import strict_loads
from avcl.contract import Clause,default_contract
from avcl.ir import FieldType,compile_predicate
from avcl.postgres import PostgresKernel
from avcl.tasks import TaskAuthority,PostgresTasks
from avcl.supervision import PostgresSupervisor
from avcl.types import Capability,Execution,Observation
from avcl.workflow_postgres import DurableWorkflowExecutor
from tests.support import authorize,prepare
from tests.test_contract_policy import amended,seal
from tests.test_encoding_ir import field,const
from tests.test_workflow import make,P,D,S

G=test_gateway.GatewayTests

class TaskTests(unittest.TestCase):
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
        self.publish()
        self.cap="task-cap-a"
        self.k.trusted.register_capability(Capability(self.cap,"demo","a",frozenset({"a"}),100,1000,self.k.status()["epoch"]))
        self.auth=(self.auth[0],self.cap,self.auth[2],self.auth[3])
        self.payload={**self.payload,"capability_ref":self.cap}
        self.tasks=PostgresTasks(self.k);self.ta=TaskAuthority(self.k)

    def publish(self,predicate=None,version=2):
        predicate=predicate or compile_predicate(field("confirmed"),{"confirmed":FieldType("Bool")},"R10")
        clause=Clause("refund-success","completion","success","refund",("close",),("R10",),(),False,predicate)
        self.contract=amended(default_contract(),version=version,extra=(clause,))
        self.k.trusted.activate_policy(seal(self.k,self.contract))

    def register(self,keys=None):
        return self.ta.register("a","task-a",frozenset({"case-a"}) if keys is None else keys)

    def dispatched(self):
        op=prepare(self.k,self.auth)
        return self.k.dispatch("a",op.id,op.permit_ref)

    def completed(self):
        op=self.dispatched();receipt=self.s.send(op);self.k.trusted.observe(receipt)
        return op,receipt

    def close_http(self,ref,key="close1"):
        status,response=self.call("POST","/v1/tasks/"+ref+"/close",{"client_request_id":key})
        self.assertEqual(status,200,response);return response["data"]

    def test_explicit_close_after_success_is_durable_and_idempotent(self):
        ref=self.register();self.completed()
        self.assertEqual(self.tasks.get("a",ref)["status"],"Running")
        report=self.close_http(ref)
        self.assertEqual(report["status"],"Completed");self.assertTrue(report["sealed"])
        self.assertEqual(self.close_http(ref),report)
        self.assertEqual(PostgresTasks(self.fresh()).get("a",ref),report)
        self.assertEqual(len(self.query("SELECT * FROM task_close_request")),1)
        self.assertEqual(sum(e.kind=="TaskCloseRequested" for e in self.k.events()),1)

    def test_close_cannot_omit_missing_expected_business_action(self):
        ref=self.register(frozenset({"case-a","case-other"}));self.completed()
        report=self.close_http(ref)
        self.assertEqual(report["status"],"Pending");self.assertEqual(report["missing_business_keys"],["case-other"])
        self.assertFalse(report["sealed"])

    def test_host_requires_nonempty_scope_and_explicit_supported_completion(self):
        self.error("INVALID_TASK_SCOPE",lambda:self.register(frozenset()))
        foreign=PostgresKernel(self.dsn,tenant="without-completion",schema=self.schema,clock=self.clock);foreign.initialize()
        self.error("MISSING_OR_UNSUPPORTED_COMPLETION",lambda:TaskAuthority(foreign).register("a","task",frozenset({"case"})))
        self.publish(compile_predicate(field("client_says_done"),{"client_says_done":FieldType("Bool")},"R10"),3)
        self.error("UNSUPPORTED_COMPLETION_CONTEXT",self.register)

    def test_registered_scope_blocks_unlisted_operation_without_budget_change(self):
        self.register()
        auth=authorize(self.k,self.s,case="unlisted")
        auth=(auth[0],self.cap,*auth[2:])
        self.error("TASK_SCOPE_MISMATCH",lambda:prepare(self.k,auth))
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,0)
        self.assertEqual(self.query("SELECT * FROM operation"),[])

    def test_scope_cannot_be_defined_after_effects_or_rewritten(self):
        ref=self.register()
        self.assertEqual(self.register(),ref)
        self.error("TASK_SCOPE_CONFLICT",lambda:self.register(frozenset({"different"})))
        for statement in ("UPDATE task_run SET expected_keys='[]'","UPDATE task_run SET task_key='other'"):
            with self.assertRaises(psycopg.errors.RaiseException):self.query(statement)
        # A separate logical task already has an operation and cannot be retrofitted.
        auth=authorize(self.k,self.s,"b",10)
        self.k.trusted.register_capability(Capability("task-cap-b","demo","b",frozenset({"b"}),100,1000,self.k.status()["epoch"]))
        prepare(self.k,(auth[0],"task-cap-b",*auth[2:]))
        self.error("TASK_ALREADY_STARTED",lambda:self.ta.register("b","task-b",frozenset({"case-b"})))

    def test_unknown_close_keeps_money_and_obligation(self):
        ref=self.register();op=self.dispatched();self.k.trusted.unknown(op.id)
        report=self.close_http(ref)
        self.assertEqual(report["status"],"Pending")
        self.assertEqual(report["open_obligation_count"],"1")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        self.assertEqual(self.k.get("a",op.id).state,Execution.UNKNOWN)

    def test_confirmed_but_unsettled_fees_cannot_complete(self):
        auth=authorize(self.k,self.s,fee=5,case="fee-case")
        self.auth=(auth[0],self.cap,*auth[2:])
        ref=self.register(frozenset({"fee-case"}));op=self.dispatched();receipt=self.s.send(op)
        self.k.trusted.observe(replace(receipt,id="partial-fees",final_cost=False))
        report=self.close_http(ref);self.assertEqual(report["status"],"Pending")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,5)
        self.k.trusted.observe(receipt)
        self.assertEqual(self.tasks.get("a",ref)["status"],"Completed")
        original=strict_loads(self.query("SELECT report FROM task_close_request")[0]["report"])
        self.assertEqual(original["status"],"Pending")

    def test_terminal_rejection_and_local_abort_are_not_completed(self):
        ref=self.register();op=prepare(self.k,self.auth);self.k.abort_prepared("a",op.id)
        self.assertEqual(self.close_http(ref)["status"],"Rejected")

    def test_remote_rejection_is_not_completed(self):
        ref=self.register();op=self.dispatched();self.s.change_version("a","v2")
        self.k.trusted.observe(self.s.send(op))
        self.assertEqual(self.close_http(ref)["status"],"Rejected")

    def test_completion_clause_false_cannot_be_replaced_by_confirmed(self):
        self.publish(compile_predicate(const(False),{"confirmed":FieldType("Bool")},"R10"),3)
        self.cap="cap-v3"
        self.k.trusted.register_capability(Capability(self.cap,"demo","a",frozenset({"a"}),100,1000,self.k.status()["epoch"]))
        self.auth=(self.auth[0],self.cap,*self.auth[2:])
        ref=self.register();self.completed()
        report=self.close_http(ref)
        self.assertEqual(report["status"],"Failed");self.assertEqual(report["reason"],"completion_clause_violated")

    def test_partial_effect_is_failed_even_with_final_cost(self):
        ref=self.register();op=self.dispatched()
        self.k.trusted.observe(Observation("partial",op.id,op.request.request_digest,Execution.PARTIAL,
            (("tenant:CNY",20),("order:a:CNY",20)),True,True))
        self.assertEqual(self.close_http(ref)["status"],"Failed")
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,20)

    def test_verified_handoff_escalates_without_freeing_exposure(self):
        ref=self.register();op=self.dispatched();self.k.trusted.unknown(op.id)
        self.close_http(ref)
        supervisor=PostgresSupervisor(self.k);claim=supervisor.claim("worker")
        supervisor.register_receiver("human-desk",900)
        supervisor.accept_handoff(op.id,"worker",claim["generation"],"human-desk","ack")
        report=self.tasks.get("a",ref)
        self.assertEqual(report["status"],"Escalated");self.assertEqual(report["verified_handoff_count"],"1")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        self.k.trusted.observe(self.s.send(op))
        self.assertEqual(self.tasks.get("a",ref)["status"],"Completed")

    def test_breach_survives_late_success(self):
        ref=self.register();op=self.dispatched();unknown=self.k.trusted.unknown(op.id)
        self.close_http(ref)
        self.clock.now=unknown.obligation_due+1
        self.k.trusted.observe(self.s.send(op))
        report=self.tasks.get("a",ref)
        self.assertEqual(report["status"],"Failed");self.assertEqual(report["breached_obligation_count"],"1")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,0)

    def test_late_disputed_charge_changes_status_preserves_close_receipt(self):
        ref=self.register();op,receipt=self.completed()
        self.assertEqual(self.close_http(ref)["status"],"Completed")
        late=replace(receipt,id="late",cumulative_costs=(("tenant:CNY",80),("order:a:CNY",70)))
        self.k.trusted.observe(late)
        report=self.tasks.get("a",ref);self.assertEqual(report["status"],"Failed");self.assertTrue(report["sealed"])
        original=strict_loads(self.query("SELECT report FROM task_close_request")[0]["report"])
        self.assertEqual(original["status"],"Completed")
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,80)
        self.assertEqual(self.close_http(ref)["status"],"Failed")
        self.assertEqual(len(self.query("SELECT * FROM task_close_request")),1)

    def test_workflow_finished_with_unknown_is_only_pending_task(self):
        ref=self.register()
        flow=make([P,D,S],contract=self.contract)
        request,cap,evidence,approval=self.auth
        refs={"r":request,"c":cap,"e":evidence,"a":approval}
        from avcl.workflow_runtime import FaultPlan
        result=DurableWorkflowExecutor(self.k,self.s).run(flow,"a","run",refs,{"flag":True},FaultPlan(drop_receipt_at=frozenset({"w0.2"})))
        self.assertEqual(result.status,"Finished")
        self.assertEqual(self.close_http(ref)["status"],"Pending")

    def test_http_scope_ownership_and_client_outcome_injection(self):
        ref=self.register()
        other=self.authority.issue("b",frozenset({"get","close"}),900)
        no_close=self.authority.issue("a",frozenset({"get"}),900)
        route="/v1/tasks/"+ref
        for method,path,body in (("GET",route,None),("POST",route+"/close",{"client_request_id":"x"})):
            self.assertEqual(self.call(method,path,body,other.token)[0],404)
        foreign=PostgresKernel(self.dsn,tenant="other",schema=self.schema,clock=self.clock);foreign.initialize()
        foreign.trusted.activate_policy(seal(foreign,self.contract))
        foreign_ref=TaskAuthority(foreign).register("a","task-a",frozenset({"case-a"}))
        for inaccessible in (foreign_ref,"task-"+"0"*32):
            self.assertEqual(self.call("GET","/v1/tasks/"+inaccessible)[0],404)
        self.assertEqual(self.call("POST",route+"/close",{"client_request_id":"x"},no_close.token)[0],403)
        for key in ("status","operation_ids","success","expected_business_keys"):
            self.assertEqual(self.call("POST",route+"/close",{"client_request_id":"x",key:"Completed"})[0],400)
        self.assertEqual(self.tasks.get("a",ref)["status"],"Running")

    def test_concurrent_close_writes_one_receipt(self):
        ref=self.register();self.completed();barrier=Barrier(2)
        def run(_):
            tasks=PostgresTasks(self.fresh());barrier.wait();return tasks.close("a",ref,"same")
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(results[0]["status"],"Completed")
        self.assertEqual(len(self.query("SELECT * FROM task_close_request")),1)

    def test_close_process_death_before_commit_rolls_back(self):
        ref=self.register();self.completed()
        self.crash("tasks.close",("a",ref,"close1"),"task-close.before_commit")
        self.assertEqual(self.tasks.get("a",ref)["status"],"Running")
        self.assertEqual(self.query("SELECT * FROM task_close_request"),[])
        self.assertEqual(self.close_http(ref)["status"],"Completed")

    def test_close_process_death_after_commit_retries_same_result(self):
        ref=self.register();self.completed()
        self.crash("tasks.close",("a",ref,"close1"),"task-close.after_commit")
        self.assertEqual(self.close_http(ref)["status"],"Completed")
        self.assertEqual(len(self.query("SELECT * FROM task_close_request")),1)
        with self.assertRaises(psycopg.errors.RaiseException):self.query("DELETE FROM task_close_request")
        with self.assertRaises(psycopg.errors.RaiseException):self.query("UPDATE task_run SET sealed=false")

    def test_sealed_task_blocks_new_admission_but_retains_exact_retry(self):
        ref=self.register();op,_=self.completed();self.close_http(ref)
        self.assertEqual(prepare(self.k,self.auth).id,op.id)
        self.error("TASK_CLOSED",lambda:prepare(self.k,self.auth,"different-request"))
        self.assertEqual(len(self.query("SELECT * FROM operation")),1)

    def test_new_policy_can_authorize_work_but_cannot_rewrite_task_success(self):
        self.publish(compile_predicate(const(False),{"confirmed":FieldType("Bool")},"R10"),3)
        ref=self.register();original=self.contract.semantic_digest
        self.publish(version=4)
        self.cap="cap-v4"
        self.k.trusted.register_capability(Capability(self.cap,"demo","a",frozenset({"a"}),100,1000,self.k.status()["epoch"]))
        self.auth=(self.auth[0],self.cap,*self.auth[2:])
        self.completed()
        report=self.close_http(ref)
        self.assertEqual(report["contract_digest"],original)
        self.assertEqual(report["status"],"Failed")

    def test_api_restart_and_revoked_credentials_preserve_task_boundary(self):
        ref=self.register();self.completed();first=self.close_http(ref)
        self.process.terminate();self.process.wait(timeout=5);self.ready.unlink();self.launch_server()
        self.assertEqual(self.close_http(ref),first)
        self.authority.revoke(self.credential.id)
        self.assertEqual(self.call("POST","/v1/tasks/"+ref+"/close",{"client_request_id":"close1"})[0],401)

    def test_v6_migration_keeps_existing_operation_without_inventing_task_scope(self):
        op=self.dispatched()
        self.query("DROP TRIGGER audit_requires_allocation ON audit_event")
        self.query("DROP FUNCTION require_audit_allocation()")
        self.query("DROP TABLE audit_counter")
        self.query("DROP TABLE rejection_audit_subject, rejection_audit_policy")
        self.assertEqual(self.query("SELECT * FROM delegation_grant"),[])
        self.query("DROP TABLE delegation_request, delegation_grant, delegation_subject")
        self.query("DROP FUNCTION freeze_delegation_grant()")
        self.query("DROP TABLE task_close_request, task_run")
        self.query("DROP FUNCTION freeze_task_definition()")
        self.query("ALTER TABLE schema_version DROP CONSTRAINT schema_version_version_check")
        self.query("UPDATE schema_version SET version=6")
        self.query("ALTER TABLE schema_version ADD CHECK(version BETWEEN 1 AND 6)")
        self.k.migrate();self.k.migrate()
        self.assertEqual(self.k.get("a",op.id),op)
        self.assertEqual(self.query("SELECT * FROM task_run"),[])
        self.error("TASK_ALREADY_STARTED",self.register)

del G
if __name__=="__main__":unittest.main()
