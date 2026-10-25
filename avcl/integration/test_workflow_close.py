from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
import test_postgres
from avcl.contract import Clause,default_contract
from avcl.ir import compile_predicate,FieldType
from avcl.tasks import TaskAuthority,PostgresTasks
from avcl.types import Capability
from avcl.workflow_postgres import DurableWorkflowExecutor
from avcl.workflow_runtime import FaultPlan
from avcl.storage_codec import loads
from avcl.canonical import strict_loads
from avcl.supervision import PostgresSupervisor
from avcl.workflow import LEGACY_FORMAT
from tests.support import authorize
from tests.test_contract_policy import amended,seal
from tests.test_encoding_ir import field
from tests.test_workflow import P,D,S,Q,branch,REFS
from tests.test_workflow_close import flow,C

class DurableCloseTests(unittest.TestCase):
    setUpClass=classmethod(test_postgres.PersistenceTests.setUpClass.__func__)
    tearDown=test_postgres.PersistenceTests.tearDown
    fresh=test_postgres.PersistenceTests.fresh
    query=test_postgres.PersistenceTests.query
    error=test_postgres.PersistenceTests.error
    crash=test_postgres.PersistenceTests.crash

    def setUp(self):
        test_postgres.PersistenceTests.setUp(self)
        predicate=compile_predicate(field("confirmed"),{"confirmed":FieldType("Bool")},"R10")
        clause=Clause("close-success","completion","success","refund",("close",),("R10",),(),False,predicate)
        self.contract=amended(default_contract(),extra=(clause,));self.k.trusted.activate_policy(seal(self.k,self.contract))
        self.k.trusted.register_capability(Capability("close-cap","demo","a",frozenset({"a"}),100,1000,self.k.status()["epoch"]))
        self.task=TaskAuthority(self.k).register("a","task-a",frozenset({"case-a"}))
        request,_,evidence,approval=authorize(self.k,self.s)
        self.refs={"r":request,"c":"close-cap","e":evidence,"a":approval,"task":self.task}
        self.executor=DurableWorkflowExecutor(self.k,self.s)

    def run_flow(self,body=None,faults=None,flag=True):
        program=flow([P,D,S,C] if body is None else body,self.contract)
        return self.executor.run(program,"a","run",self.refs,{"flag":flag},faults)

    def test_successful_close_records_task_status_and_data_only_result(self):
        result=self.run_flow()
        self.assertEqual(result.status,"Finished")
        step=dict(result.steps[-1]);self.assertEqual(step["result"],"Completed");self.assertEqual(step["task_ref"],self.task)
        self.assertEqual(PostgresTasks(self.k).get("a",self.task)["status"],"Completed")
        env=dict(loads(self.query("SELECT frame FROM workflow_run")[0]["frame"])[0])
        self.assertEqual(strict_loads(env["closed"])["status"],"Completed")
        self.assertEqual(len(self.query("SELECT * FROM task_close_request")),1)

    def test_finished_program_may_have_pending_close(self):
        result=self.run_flow(faults=FaultPlan(drop_receipt_at=frozenset({"w0.2"})))
        self.assertEqual(result.status,"Finished");self.assertEqual(dict(result.steps[-1])["result"],"Pending")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        self.assertEqual(self.s.real_cost,70)

    def test_reconcile_then_second_close_keeps_first_receipt(self):
        result=self.run_flow([P,D,S,C,Q,{**C,"out":"closed_after_query"}],FaultPlan(drop_receipt_at=frozenset({"w0.2"})))
        closes=[dict(step) for step in result.steps if dict(step)["command"]=="Close"]
        self.assertEqual([s["result"] for s in closes],["Pending","Completed"])
        self.assertEqual(len(self.query("SELECT * FROM task_close_request")),2)
        self.assertEqual(self.s.real_cost,70)

    def test_invalid_task_input_rejected_before_preparing(self):
        program=flow([P,D,S,C],self.contract)
        for ref in ("forged",TaskAuthority(self.k).register("b","task-b",frozenset({"case-b"}))):
            self.error("TASK_NOT_FOUND",lambda:self.executor.start(program,"a","run",{**self.refs,"task":ref},{"flag":True}))
        self.assertEqual(self.query("SELECT * FROM workflow_run"),[])
        self.assertEqual(self.query("SELECT * FROM operation"),[])

    def test_close_commit_crash_rolls_back_receipt_and_checkpoint_together(self):
        program=flow([C],self.contract)
        self.executor.start(program,"a","run",self.refs,{"flag":True})
        self.crash("workflow.advance",("a","run","worker"),"workflow-step.before_commit")
        self.assertEqual(self.query("SELECT * FROM task_close_request"),[])
        self.assertEqual(self.query("SELECT pc FROM workflow_run")[0]["pc"],0)
        self.assertFalse(PostgresTasks(self.k).get("a",self.task)["close_requested"])
        result=self.executor.resume("a","run")
        self.assertEqual(result.status,"Finished");self.assertEqual(dict(result.steps[0])["result"],"Pending")

    def test_crash_after_close_commit_does_not_repeat_close(self):
        program=flow([C],self.contract)
        self.executor.start(program,"a","run",self.refs,{"flag":True})
        self.crash("workflow.advance",("a","run","worker"),"workflow-node:w0.0.after_commit")
        result=DurableWorkflowExecutor(self.fresh(),self.s).resume("a","run")
        self.assertEqual(result.status,"Finished")
        self.assertEqual(len(self.query("SELECT * FROM task_close_request")),1)
        self.assertEqual(len(result.steps),1)

    def test_concurrent_workers_commit_one_close_step(self):
        self.executor.start(flow([C],self.contract),"a","run",self.refs,{"flag":True});barrier=Barrier(2)
        def advance(i):
            executor=DurableWorkflowExecutor(self.fresh());barrier.wait();return executor.advance("a","run","w"+str(i))
        with ThreadPoolExecutor(2) as pool:list(pool.map(advance,range(2)))
        self.assertEqual(len(self.query("SELECT * FROM task_close_request")),1)
        self.assertEqual(len(self.query("SELECT * FROM workflow_step")),1)

    def test_branch_report_survives_join_and_restart(self):
        result=self.run_flow([P,D,S,branch([C],[C]),{"tag":"Alias","ref":"closed","out":"copy"}],flag=False)
        self.assertEqual(result.status,"Finished")
        self.assertEqual(DurableWorkflowExecutor(self.fresh()).resume("a","run"),result)
        self.assertEqual(len(self.query("SELECT * FROM task_close_request")),1)

    def test_snapshot_does_not_rewrite_pending_step_after_later_resolution(self):
        result=self.run_flow(faults=FaultPlan(drop_receipt_at=frozenset({"w0.2"})))
        op=self.k.get("a",result.operation_ids[0]);self.k.trusted.observe(self.s.query(op.id))
        self.assertEqual(PostgresTasks(self.k).get("a",self.task)["status"],"Completed")
        self.assertEqual(self.executor.snapshot("a","run"),result)
        self.assertEqual(dict(result.steps[-1])["result"],"Pending")

    def test_handoff_result_is_escalated_in_close_step(self):
        program=flow([P,D,S,C],self.contract)
        self.executor.start(program,"a","run",self.refs,{"flag":True})
        self.executor.advance("a","run","w");self.executor.advance("a","run","w")
        job=self.executor.advance("a","run","w")
        self.executor.finish_io("a","run",job["node_id"],"w",job["generation"])
        supervisor=PostgresSupervisor(self.k);claim=supervisor.claim("desk-worker")
        supervisor.register_receiver("desk",900)
        supervisor.accept_handoff(claim["operation"].id,"desk-worker",claim["generation"],"desk","ack")
        result=self.executor.resume("a","run")
        self.assertEqual(dict(result.steps[-1])["result"],"Escalated")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)

    def test_legacy_checkpoint_keeps_version_and_digest_on_resume(self):
        legacy=flow([P,D,S],self.contract,refs=REFS,version=LEGACY_FORMAT)
        refs={k:v for k,v in self.refs.items() if k!="task"}
        self.executor.start(legacy,"a","old-run",refs,{"flag":True})
        self.executor.advance("a","old-run","w")
        result=DurableWorkflowExecutor(self.fresh(),self.s).resume("a","old-run")
        self.assertEqual(result.workflow_digest,legacy.semantic_digest)
        self.assertEqual(result.status,"Finished")
        self.assertEqual(strict_loads(self.query("SELECT program FROM workflow_run")[0]["program"])["format"],LEGACY_FORMAT)

if __name__=="__main__":unittest.main()
