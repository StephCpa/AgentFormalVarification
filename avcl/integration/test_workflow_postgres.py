from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from unittest.mock import patch
import unittest
import psycopg
import test_postgres
from avcl.workflow_postgres import DurableWorkflowExecutor
from avcl.workflow_runtime import FaultPlan
from avcl.contract import default_contract
from avcl.types import Execution
from tests.test_workflow import make,P,D,S,Q,branch,context

class DurableWorkflowTests(unittest.TestCase):
    setUpClass=classmethod(test_postgres.PersistenceTests.setUpClass.__func__)
    setUp=test_postgres.PersistenceTests.setUp
    tearDown=test_postgres.PersistenceTests.tearDown
    fresh=test_postgres.PersistenceTests.fresh
    error=test_postgres.PersistenceTests.error
    query=test_postgres.PersistenceTests.query
    crash=test_postgres.PersistenceTests.crash

    def start(self,body=None,flag=True,run_id="run1",faults=None):
        self.flow=make(body if body is not None else [P,D,S,Q]);self.refs=context(self.k,self.s)
        executor=DurableWorkflowExecutor(self.k,self.s)
        executor.start(self.flow,"a",run_id,self.refs,{"flag":flag},faults)
        return executor

    def test_normal_run_and_reopen_are_idempotent(self):
        executor=self.start();result=executor.resume("a","run1")
        self.assertEqual(result.status,"Finished");self.assertEqual(self.s.real_cost,70)
        with patch.object(self.s,"send",wraps=self.s.send) as sender:
            again=DurableWorkflowExecutor(self.fresh(),self.s).run(self.flow,"a","run1",self.refs,{"flag":True})
            self.assertEqual(again,result);sender.assert_not_called()
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,70)
        self.assertEqual(len(self.query("SELECT * FROM workflow_operation")),1)

    def test_committed_prepare_process_death_resumes_without_second_reservation(self):
        self.start();self.crash("workflow.resume",("a","run1"),"workflow-node:w0.0.after_commit")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        result=DurableWorkflowExecutor(self.fresh(),self.s).resume("a","run1")
        self.assertEqual(result.status,"Finished");self.assertEqual(len(result.operation_ids),1)
        self.assertEqual(sum(e.kind=="Prepared" for e in self.k.events()),1)

    def test_prepare_checkpoint_process_death_before_commit_is_atomic(self):
        self.start();self.crash("workflow.resume",("a","run1"),"workflow-step.before_commit")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,0)
        self.assertEqual(self.query("SELECT * FROM workflow_step"),[])
        self.assertEqual(self.query("SELECT * FROM workflow_operation"),[])
        result=DurableWorkflowExecutor(self.fresh(),self.s).resume("a","run1")
        self.assertEqual(result.status,"Finished");self.assertEqual(self.s.real_cost,70)

    def test_dispatch_checkpoint_process_death_keeps_consumed_permit(self):
        self.start();self.crash("workflow.resume",("a","run1"),"workflow-node:w0.1.after_commit")
        self.assertEqual(self.k.status()["outbox"][0]["status"],"Pending")
        result=DurableWorkflowExecutor(self.fresh(),self.s).resume("a","run1")
        self.assertEqual(result.status,"Finished")
        self.assertEqual(sum(e.kind=="DispatchAdmitted" for e in self.k.events()),1)

    def test_send_admission_crash_never_blindly_repeats_write(self):
        self.start();self.crash("workflow.resume",("a","run1"),"workflow-node:w0.2.after_commit")
        executor=DurableWorkflowExecutor(self.fresh(),self.s)
        self.assertEqual(executor.resume("a","run1").status,"WaitingIO")
        self.clock.now=31
        with patch.object(self.s,"send",wraps=self.s.send) as sender:
            result=executor.resume("a","run1");sender.assert_not_called()
        self.assertEqual(result.status,"Finished")
        self.assertEqual(self.k.get("a",result.operation_ids[0]).state,Execution.UNKNOWN)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        self.assertEqual(self.k.status()["obligations"][0]["status"],"Open")

    def test_effect_then_raw_receipt_crash_recovers_without_sending_again(self):
        executor=self.start();executor.advance("a","run1","worker");executor.advance("a","run1","worker")
        job=executor.advance("a","run1","worker");receipt=self.s.send(job["operation"])
        self.crash("workflow.finish_io",("a","run1",job["node_id"],"worker",job["generation"],receipt),"observe.after_raw_commit")
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,0)
        self.assertEqual(len(self.fresh().recover_observations()),1)
        with patch.object(self.s,"send",wraps=self.s.send) as sender:
            result=DurableWorkflowExecutor(self.fresh(),self.s).resume("a","run1");sender.assert_not_called()
        self.assertEqual(result.status,"Finished");self.assertEqual(self.s.real_cost,70)
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,70)

    def test_receipt_applied_before_workflow_checkpoint_crash_is_reused(self):
        executor=self.start();executor.advance("a","run1","worker");executor.advance("a","run1","worker")
        job=executor.advance("a","run1","worker");receipt=self.s.send(job["operation"])
        self.crash("workflow.finish_io",("a","run1",job["node_id"],"worker",1,receipt),"workflow-io-finish.before_commit")
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,70)
        result=DurableWorkflowExecutor(self.fresh(),self.s).resume("a","run1")
        self.assertEqual(result.status,"Finished");self.assertEqual(len(self.s.effects),1)

    def test_late_worker_cannot_advance_a_new_io_step(self):
        executor=self.start();executor.advance("a","run1","old");executor.advance("a","run1","old")
        old=executor.advance("a","run1","old");self.clock.now=31
        executor.advance("a","run1","new")  # Expired Send is now uncertain.
        new=executor.advance("a","run1","new")  # Independent read-only reconciliation.
        self.assertEqual(new["kind"],"Reconcile")
        self.assertEqual(executor.finish_io("a","run1",old["node_id"],"old",1)["status"],"StaleIO")
        row=self.query("SELECT pc,status FROM workflow_run")[0]
        self.assertEqual(row,{"pc":3,"status":"WaitingIO"})
        executor.finish_io("a","run1",new["node_id"],"new",1)
        self.assertEqual(executor.snapshot("a","run1").status,"Finished")

    def test_branch_choice_and_affine_join_survive_restart(self):
        self.start([P,branch([D,S],[{"tag":"Drop","ref":"p"}])],False)
        self.crash("workflow.resume",("a","run1"),"workflow-node:w0.1.after_commit")
        executor=DurableWorkflowExecutor(self.fresh(),self.s)
        self.error("WORKFLOW_RUN_CONFLICT",lambda:executor.start(self.flow,"a","run1",self.refs,{"flag":True}))
        result=executor.resume("a","run1")
        self.assertEqual(result.status,"Finished");self.assertEqual(self.s.effects,())
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        self.assertIn("Join",[dict(s)["command"] for s in result.steps])

    def test_move_alias_and_abort_persist_resource_state(self):
        executor=self.start([{"tag":"Alias","ref":"c","out":"copy"},{**P,"capability":"copy"},
            {"tag":"Move","ref":"p","out":"moved"},{"tag":"Abort","permit":"moved"}])
        result=executor.resume("a","run1")
        self.assertEqual(result.status,"Finished");self.assertEqual(self.k.accounts()["tenant:CNY"].pending,0)
        self.assertEqual(self.k.get("a",result.operation_ids[0]).state,Execution.ABORTED)

    def test_separate_run_cannot_allocate_an_already_owned_permit(self):
        executor=self.start([P]);self.assertEqual(executor.resume("a","run1").status,"Finished")
        other=DurableWorkflowExecutor(self.fresh(),self.s)
        result=other.run(self.flow,"a","run2",self.refs,{"flag":True})
        self.assertEqual(result.error,"WORKFLOW_PERMIT_NOT_FRESH")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)

    def test_policy_change_halts_unprepared_workflow_atomically(self):
        executor=self.start();contract=replace(default_contract(),policy_version=2)
        envelope=self.k.trusted.seal_policy(contract,900,contract.requirements(),
            frozenset(c.id for c in contract.clauses if c.kind=="safety"),{"refund":{"amount":1,"currency":"CNY"}})
        self.k.trusted.activate_policy(envelope)
        result=executor.resume("a","run1")
        self.assertEqual(result.error,"WORKFLOW_POLICY_CHANGED");self.assertEqual(self.k.accounts()["tenant:CNY"].pending,0)
        self.assertEqual(result,DurableWorkflowExecutor(self.fresh()).resume("a","run1"))

    def test_two_executors_share_one_checkpoint_and_send_intent(self):
        self.start();barrier=Barrier(2)
        def run(_):barrier.wait();return DurableWorkflowExecutor(self.fresh(),self.s).resume("a","run1")
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,range(2)))
        result=DurableWorkflowExecutor(self.fresh(),self.s).resume("a","run1")
        self.assertEqual(result.status,"Finished");self.assertEqual(len(self.s.effects),1)
        self.assertEqual(len(self.query("SELECT * FROM send_attempt")),1)
        self.assertTrue(all(r.status in {"Finished","WaitingIO"} for r in results))

    def test_mismatched_receipt_halts_and_keeps_pending(self):
        executor=self.start();executor.advance("a","run1","worker");executor.advance("a","run1","worker")
        job=executor.advance("a","run1","worker")
        receipt=replace(self.s.send(job["operation"]),operation_id="wrong-operation")
        executor.finish_io("a","run1",job["node_id"],"worker",1,receipt)
        result=executor.snapshot("a","run1")
        self.assertEqual(result.error,"UNEXPECTED_ADAPTER_OBSERVATION")
        self.assertTrue(self.k.status()["quarantined"]);self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)

    def test_lost_receipt_fault_continues_to_read_only_reconciliation(self):
        executor=self.start(faults=FaultPlan(drop_receipt_at=frozenset({"w0.2"})))
        result=executor.resume("a","run1")
        self.assertEqual(result.status,"Finished");self.assertEqual(self.k.accounts()["tenant:CNY"].spent,70)
        self.assertEqual(len(self.s.effects),1)

    def test_subject_cannot_read_another_subjects_checkpoint(self):
        self.start()
        self.error("UNKNOWN_WORKFLOW_RUN",lambda:DurableWorkflowExecutor(self.fresh()).snapshot("b","run1"))

    def test_persisted_definition_branch_inputs_and_trace_cannot_be_rewritten(self):
        executor=self.start([P,branch([D,S],[{"tag":"Drop","ref":"p"}])])
        with self.assertRaises(psycopg.errors.RaiseException):self.query("UPDATE workflow_run SET values_wire='{}',revision=revision+1")
        executor.advance("a","run1","worker")
        with self.assertRaises(psycopg.errors.RaiseException):self.query("UPDATE workflow_run SET pc=0,revision=revision+1")
        with self.assertRaises(psycopg.errors.RaiseException):self.query("UPDATE workflow_step SET payload='{}'")
        self.assertEqual(executor.resume("a","run1").status,"Finished")

if __name__=="__main__":unittest.main()
