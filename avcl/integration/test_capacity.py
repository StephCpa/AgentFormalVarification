from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
import unittest
import test_postgres
from avcl.canonical import Reject
from avcl.storage_codec import loads
from avcl.supervision import PostgresSupervisor
from avcl.workflow_postgres import DurableWorkflowExecutor
from avcl.types import Execution,Settlement
from tests.support import authorize,prepare
from tests.test_workflow import make,P,D,S,Q,context

class CapacityTests(unittest.TestCase):
    setUpClass=classmethod(test_postgres.PersistenceTests.setUpClass.__func__)
    setUp=test_postgres.PersistenceTests.setUp
    tearDown=test_postgres.PersistenceTests.tearDown
    fresh=test_postgres.PersistenceTests.fresh
    error=test_postgres.PersistenceTests.error
    query=test_postgres.PersistenceTests.query

    def test_full_capacity_rejects_new_but_returns_existing_operation(self):
        self.k.trusted.set_capacity(1,10,10)
        first=authorize(self.k,self.s);second=authorize(self.k,self.s,"b",10)
        op=prepare(self.k,first)
        self.error("OPERATION_CAPACITY_EXCEEDED",lambda:prepare(self.fresh(),second,"second"))
        self.assertEqual(prepare(self.fresh(),first),op)
        self.assertEqual(prepare(self.fresh(),first,"same-business-new-request"),op)
        self.assertEqual(self.k.capacity()["usage"]["operations"],1)
        self.assertEqual(self.k.accounts()["order:b:CNY"].pending,0)
        approval=loads(self.query("SELECT payload FROM approval WHERE id=%s",(second[3],))[0]["payload"])
        self.assertIsNone(approval.operation_id)

    def test_future_obligation_slot_is_reserved_before_unknown(self):
        self.k.trusted.set_capacity(10,1,10)
        op=prepare(self.k,authorize(self.k,self.s));report=self.k.capacity()
        self.assertEqual(report["usage"]["open_obligations"],0)
        self.assertEqual(report["usage"]["obligation_slots"],1)
        second=authorize(self.k,self.s,"b",10)
        self.error("OPERATION_CAPACITY_EXCEEDED",lambda:prepare(self.k,second,"second"))
        op=self.k.dispatch("a",op.id,op.permit_ref);self.k.trusted.unknown(op.id)
        self.assertEqual(self.k.capacity()["usage"]["open_obligations"],1)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)

    def test_concurrent_last_slot_has_one_winner(self):
        self.k.trusted.set_capacity(1,1,10)
        auths=[authorize(self.k,self.s,"a",10),authorize(self.k,self.s,"b",10)];barrier=Barrier(2)
        def run(auth):
            barrier.wait()
            try:return prepare(self.fresh(),auth)
            except Reject as exc:return exc.code
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,auths))
        self.assertEqual(results.count("OPERATION_CAPACITY_EXCEEDED"),1)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,10)
        self.assertEqual(self.k.capacity()["operation_headroom"],0)

    def test_abort_releases_capacity_without_deleting_history(self):
        self.k.trusted.set_capacity(1,1,10)
        op=prepare(self.k,authorize(self.k,self.s));self.k.abort_prepared("a",op.id)
        self.assertEqual(self.k.capacity()["operation_headroom"],1)
        other=prepare(self.k,authorize(self.k,self.s,"b",10),"second")
        self.assertEqual(other.state,Execution.PREPARED)
        self.assertEqual(len(self.query("SELECT * FROM operation")),2)
        self.assertEqual(self.k.get("a",op.id).state,Execution.ABORTED)

    def test_full_capacity_keeps_supervision_and_final_settlement_available(self):
        self.k.trusted.set_capacity(1,1,0)
        op=prepare(self.k,authorize(self.k,self.s));op=self.k.dispatch("a",op.id,op.permit_ref)
        self.s.send(op);self.k.trusted.unknown(op.id);self.k.trusted.revoke()
        self.assertEqual(self.k.capacity()["operation_headroom"],0)
        result=PostgresSupervisor(self.fresh()).run_once("recovery",self.s)
        self.assertEqual(result["status"],"Reconciled")
        self.assertEqual(self.k.capacity()["operation_headroom"],1)
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,70)

    def test_handoff_does_not_free_unknown_operation_slot(self):
        self.k.trusted.set_capacity(1,1,10)
        op=prepare(self.k,authorize(self.k,self.s));op=self.k.dispatch("a",op.id,op.permit_ref);self.k.trusted.unknown(op.id)
        supervisor=PostgresSupervisor(self.k);supervisor.register_receiver("team",1000);claim=supervisor.claim("worker")
        supervisor.accept_handoff(op.id,"worker",claim["generation"],"team","ticket")
        report=self.k.capacity()
        self.assertEqual(report["usage"]["open_obligations"],0)
        self.assertEqual(report["usage"]["obligation_slots"],1)
        self.assertEqual(report["operation_headroom"],0)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)

    def test_capacity_reduction_cannot_erase_existing_commitment(self):
        self.k.trusted.set_capacity(2,2,2);prepare(self.k,authorize(self.k,self.s));before=self.k.capacity()
        self.error("CAPACITY_BELOW_COMMITMENT",lambda:self.k.trusted.set_capacity(0,2,2))
        self.error("CAPACITY_BELOW_COMMITMENT",lambda:self.k.trusted.set_capacity(2,0,2))
        self.assertEqual(self.k.capacity(),before)
        self.k.trusted.set_capacity(3,3,3)
        self.assertEqual(self.fresh().capacity()["limits"]["max_operations"],3)

    def test_workflow_count_backpressure_and_duplicate_start(self):
        self.k.trusted.set_capacity(10,10,1)
        executor=DurableWorkflowExecutor(self.k,self.s);flow=make([P]);refs=context(self.k,self.s)
        first=executor.start(flow,"a","run1",refs,{"flag":True})
        self.assertEqual(executor.start(flow,"a","run1",refs,{"flag":True}),first)
        self.error("WORKFLOW_CAPACITY_EXCEEDED",lambda:executor.start(flow,"a","run2",refs,{"flag":True}))
        self.assertEqual(len(self.query("SELECT * FROM workflow_run")),1)
        executor.resume("a","run1")
        self.assertEqual(self.k.capacity()["usage"]["workflows"],0)
        self.assertEqual(self.k.capacity()["usage"]["operations"],1)

    def test_workflow_waits_without_consuming_approval_then_resumes(self):
        self.k.trusted.set_capacity(1,1,1)
        first=prepare(self.k,authorize(self.k,self.s));auth=authorize(self.k,self.s,"b",10)
        refs=dict(zip(("r","c","e","a"),auth));flow=make([P,D,S,Q])
        executor=DurableWorkflowExecutor(self.k,self.s)
        waiting=executor.run(flow,"b","run",refs,{"flag":True})
        self.assertEqual(waiting.status,"WaitingCapacity");self.assertEqual(waiting.operation_ids,())
        revision=self.query("SELECT revision FROM workflow_run")[0]["revision"]
        self.assertEqual(executor.resume("b","run").status,"WaitingCapacity")
        self.assertEqual(self.query("SELECT revision FROM workflow_run")[0]["revision"],revision)
        approval=loads(self.query("SELECT payload FROM approval WHERE id=%s",(auth[3],))[0]["payload"])
        self.assertIsNone(approval.operation_id)
        self.assertEqual(sum(e.kind=="WorkflowBackpressured" for e in self.k.events()),1)
        self.k.abort_prepared("a",first.id)
        result=DurableWorkflowExecutor(self.fresh(),self.s).resume("b","run")
        self.assertEqual(result.status,"Finished");self.assertIsNone(result.error);self.assertIsNone(result.failed_node)
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,10)
        self.assertEqual(self.k.capacity()["usage"]["workflows"],0)

    def test_late_real_incident_is_recorded_even_when_capacity_is_full(self):
        self.k.trusted.set_capacity(1,1,10)
        op=prepare(self.k,authorize(self.k,self.s));op=self.k.dispatch("a",op.id,op.permit_ref)
        receipt=self.s.send(op);self.k.trusted.observe(receipt)
        prepare(self.k,authorize(self.k,self.s,"b",10),"second")
        late=replace(receipt,id="late-fee",cumulative_costs=(("tenant:CNY",80),("order:a:CNY",70)))
        result=self.k.trusted.observe(late)
        self.assertEqual(result.settlement,Settlement.DISPUTED)
        report=self.k.capacity();self.assertTrue(report["over_capacity"])
        self.assertEqual(report["usage"]["operations"],2)
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,80)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,10)
        self.assertTrue(self.k.status()["quarantined"])

    def test_invalid_capacity_and_falsey_fault_config_are_rejected(self):
        before=self.k.capacity()
        for limits in ((True,1,1),(-1,1,1),(1,1,1000001)):
            self.error("INVALID_CAPACITY_POLICY",lambda:self.k.trusted.set_capacity(*limits))
        self.assertEqual(self.k.capacity(),before)
        flow=make([P]);refs=context(self.k,self.s);executor=DurableWorkflowExecutor(self.k,self.s)
        for faults in (False,0,{},[]):
            self.error("INVALID_WORKFLOW_EXECUTION",lambda:executor.start(flow,"a","run",refs,{"flag":True},faults))
        self.assertEqual(self.query("SELECT * FROM workflow_run"),[])

    def test_v3_workflow_checkpoint_survives_capacity_migration(self):
        flow=make([P,D,S,Q]);executor=DurableWorkflowExecutor(self.k,self.s);refs=context(self.k,self.s)
        executor.start(flow,"a","run",refs,{"flag":True});executor.advance("a","run","worker")
        before=executor.snapshot("a","run")
        # Reconstruct the v3 schema shape in this test-owned schema only.
        self.assertEqual(self.query("SELECT * FROM delegation_grant"),[])
        self.query("DROP TABLE delegation_request, delegation_grant, delegation_subject")
        self.query("DROP FUNCTION freeze_delegation_grant()")
        self.query("DROP TABLE task_close_request, task_run")
        self.query("DROP FUNCTION freeze_task_definition()")
        self.query("DROP TABLE candidate_validation, order_context_head, order_snapshot")
        self.query("DROP TABLE api_request, checked_request, api_credential")
        self.query("DROP FUNCTION freeze_credential_identity()")
        self.query("DROP TABLE capacity_policy")
        self.query("UPDATE schema_version SET version=3")
        self.query("ALTER TABLE schema_version DROP CONSTRAINT schema_version_version_check")
        self.query("ALTER TABLE schema_version ADD CHECK(version BETWEEN 1 AND 3)")
        self.query("ALTER TABLE workflow_run DROP CONSTRAINT workflow_run_status_check")
        self.query("ALTER TABLE workflow_run ADD CHECK(status IN ('Running','WaitingIO','Finished','Halted'))")
        self.k.migrate();self.k.migrate()
        self.assertEqual(executor.snapshot("a","run"),before)
        self.assertEqual(self.k.capacity()["usage"]["operations"],1)
        self.assertEqual(self.k.capacity()["usage"]["workflows"],1)
        self.assertEqual(executor.resume("a","run").status,"Finished")

if __name__=="__main__":unittest.main()
