from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from dataclasses import replace
import unittest
import test_postgres
from avcl.contract import default_contract
from avcl.storage_codec import loads
from avcl.workflow_postgres import DurableWorkflowExecutor
from tests.test_delegation import provision,authorization,prepare
from tests.test_contract_policy import seal
from tests.test_workflow import make,P,D,S,Q,branch
from tests.test_workflow_delegate import command,flow

class DurableWorkflowDelegationTests(unittest.TestCase):
    setUpClass=classmethod(test_postgres.PersistenceTests.setUpClass.__func__)
    tearDown=test_postgres.PersistenceTests.tearDown
    fresh=test_postgres.PersistenceTests.fresh
    query=test_postgres.PersistenceTests.query
    error=test_postgres.PersistenceTests.error
    crash=test_postgres.PersistenceTests.crash

    def setUp(self):
        test_postgres.PersistenceTests.setUp(self);self.root_account=provision(self.k)
        self.executor=DurableWorkflowExecutor(self.k,self.s)

    def start(self,program=None):return self.executor.start(flow() if program is None else program,"parent","run",{"c":"root"},{"flag":True})
    def run_flow(self,program=None):return self.executor.run(flow() if program is None else program,"parent","run",{"c":"root"},{"flag":True})
    def issued(self,result):return next(dict(step)["child_cap_ref"] for step in result.steps if dict(step)["command"]=="Delegate")

    def test_commits_grant_frame_step_and_one_event_together(self):
        result=self.run_flow();self.assertEqual(result.status,"Finished");ref=self.issued(result)
        frame=loads(self.query("SELECT frame FROM workflow_run")[0]["frame"])
        self.assertEqual(dict(frame[0])["child_cap"],ref)
        self.assertEqual(len(self.query("SELECT * FROM delegation_request")),1)
        self.assertEqual(self.query("SELECT pc FROM workflow_run")[0]["pc"],1)
        self.assertEqual(sum(e.kind=="CapabilityDelegated" for e in self.k.events()),1)
        self.assertEqual(self.query("SELECT * FROM operation"),[])

    def test_real_crash_before_commit_rolls_back_grant_and_checkpoint(self):
        self.start();self.crash("workflow.advance",("parent","run","worker"),"workflow-step.before_commit")
        self.assertEqual(len(self.query("SELECT * FROM delegation_grant")),1)
        self.assertEqual(len(self.query("SELECT * FROM budget_account")),4)
        self.assertEqual(self.query("SELECT * FROM delegation_request"),[])
        self.assertEqual(self.query("SELECT * FROM workflow_step"),[])
        self.assertEqual(self.query("SELECT pc FROM workflow_run")[0]["pc"],0)
        self.assertEqual(DurableWorkflowExecutor(self.fresh()).resume("parent","run").status,"Finished")

    def test_real_crash_after_commit_reuses_capability_and_advances(self):
        program=flow([command(),{"tag":"Alias","ref":"child_cap","out":"copy"}]);self.start(program)
        self.crash("workflow.advance",("parent","run","worker"),"workflow-node:w0.0.after_commit")
        ref=self.query("SELECT capability_ref FROM delegation_request")[0]["capability_ref"]
        result=DurableWorkflowExecutor(self.fresh()).resume("parent","run")
        self.assertEqual(result.status,"Finished");self.assertEqual(self.issued(result),ref)
        self.assertEqual(len(self.query("SELECT * FROM delegation_request")),1)
        self.assertEqual(len(self.query("SELECT * FROM workflow_step")),2)

    def test_concurrent_workers_issue_only_one_child(self):
        self.start();barrier=Barrier(2)
        def advance(worker):
            executor=DurableWorkflowExecutor(self.fresh());barrier.wait();return executor.advance("parent","run",worker)
        with ThreadPoolExecutor(2) as pool:list(pool.map(advance,("one","two")))
        self.assertEqual(len(self.query("SELECT * FROM delegation_request")),1)
        self.assertEqual(len(self.query("SELECT * FROM workflow_step")),1)
        self.assertEqual(sum(e.kind=="CapabilityDelegated" for e in self.k.events()),1)

    def test_expansion_fails_without_partial_grant_or_checkpoint(self):
        result=self.run_flow(flow([command(limit_minor="101")]))
        self.assertEqual((result.status,result.error,result.failed_node),("Halted","BUDGET_EXCEEDED","w0.0"))
        self.assertEqual(len(self.query("SELECT * FROM delegation_grant")),1)
        self.assertEqual(self.query("SELECT * FROM delegation_request"),[])

    def test_parent_revocation_between_start_and_node_is_rechecked(self):
        self.start();self.k.trusted.revoke_capability("root")
        result=self.executor.resume("parent","run")
        self.assertEqual(result.error,"AUTH_DENIED");self.assertEqual(self.query("SELECT * FROM delegation_request"),[])

    def test_policy_change_before_delegate_halts_bound_workflow(self):
        self.start();policy=replace(default_contract(),policy_version=2)
        self.k.trusted.activate_policy(seal(self.k,policy))
        result=self.executor.resume("parent","run")
        self.assertEqual(result.error,"WORKFLOW_POLICY_CHANGED");self.assertEqual(self.query("SELECT * FROM delegation_request"),[])

    def test_clock_rollback_before_delegate_halts_without_issuance(self):
        self.clock.now=5;self.start();self.clock.now=4
        result=self.executor.resume("parent","run")
        self.assertEqual(result.error,"CLOCK_ROLLBACK");self.assertEqual(self.query("SELECT * FROM delegation_request"),[])

    def test_branch_and_distinct_nodes_preserve_each_issuance_identity(self):
        program=flow([branch([command()],[command(child="sibling")]),command(child="sibling",out="other")])
        result=self.run_flow(program);self.assertEqual(result.status,"Finished")
        refs=[dict(step)["child_cap_ref"] for step in result.steps if dict(step)["command"]=="Delegate"]
        self.assertEqual(len(set(refs)),2)
        self.assertEqual(DurableWorkflowExecutor(self.fresh()).run(program,"parent","run",{"c":"root"},{"flag":True}),result)

    def test_completed_run_replay_is_history_and_cannot_refresh_expired_capability(self):
        result=self.run_flow();ref=self.issued(result);auth=authorization(self.k,self.s,ref)
        self.clock.now=900
        self.assertEqual(self.run_flow(),result)
        self.assertEqual(len(self.query("SELECT * FROM delegation_request")),1)
        self.error("AUTH_DENIED",lambda:prepare(self.fresh(),auth))

    def test_child_runs_own_workflow_but_parent_cannot_impersonate(self):
        ref=self.issued(self.run_flow())
        invalid=authorization(self.k,self.s,ref,"parent",case="impersonate")
        self.error("AUTH_DENIED",lambda:prepare(self.k,invalid))
        request,cap,evidence,approval=authorization(self.k,self.s,ref)
        result=DurableWorkflowExecutor(self.fresh(),self.s).run(make([P,D,S]),"child","child-run",
            {"r":request,"c":cap,"e":evidence,"a":approval},{"flag":True})
        self.assertEqual(result.status,"Finished");self.assertEqual(self.s.real_cost,60)
        self.assertEqual(self.k.accounts()[self.root_account].spent,60)

    def test_child_can_redelegate_only_in_its_own_subject_context(self):
        ref=self.issued(self.run_flow(flow([command(can_delegate=True)])))
        child_program=flow([command(child="grandchild")])
        result=DurableWorkflowExecutor(self.fresh()).run(child_program,"child","child-delegate",{"c":ref},{"flag":True})
        self.assertEqual(result.status,"Finished")
        grand=self.issued(result)
        grants={row["capability_ref"]:loads(row["payload"]) for row in self.query("SELECT * FROM delegation_grant")}
        self.assertEqual(grants[grand].parent_ref,ref)
        self.k.trusted.revoke_capability(ref)
        auth=authorization(self.k,self.s,grand,"grandchild")
        self.error("AUTH_DENIED",lambda:prepare(self.k,auth))

    def test_revoked_parent_does_not_erase_child_unknown_and_reconciliation(self):
        ref=self.issued(self.run_flow());request,cap,evidence,approval=authorization(self.k,self.s,ref)
        executor=DurableWorkflowExecutor(self.fresh(),self.s)
        executor.start(make([P,D,S,Q]),"child","child-run",{"r":request,"c":cap,"e":evidence,"a":approval},{"flag":True})
        executor.advance("child","child-run","w");executor.advance("child","child-run","w")
        io=executor.advance("child","child-run","w");self.s.send(io["operation"])
        executor.finish_io("child","child-run",io["node_id"],"w",io["generation"],None)
        self.k.trusted.revoke_capability("root")
        self.assertEqual(self.k.accounts()[self.root_account].pending,60)
        result=DurableWorkflowExecutor(self.fresh(),self.s).resume("child","child-run")
        self.assertEqual(result.status,"Finished");self.assertEqual(self.k.accounts()[self.root_account].pending,0)
        self.assertEqual(self.k.accounts()[self.root_account].spent,60);self.assertEqual(self.s.real_cost,60)

if __name__=="__main__":unittest.main()
