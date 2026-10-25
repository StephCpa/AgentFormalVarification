from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
import unittest
import test_postgres
from avcl.canonical import strict_loads
from avcl.contract import Clause,default_contract
from avcl.ir import compile_predicate,FieldType
from avcl.postgres import PostgresKernel
from avcl.storage_codec import loads
from avcl.tasks import TaskAuthority,PostgresTasks
from avcl.types import Request,Capability,Approval
from avcl.validation import ValidationAuthority
from avcl.workflow import RefType
from avcl.workflow_postgres import DurableWorkflowExecutor
from avcl.workflow_runtime import FaultPlan
from tests.test_contract_policy import amended,seal
from tests.test_encoding_ir import field
from tests.test_workflow import P,D,S,Q,branch
from tests.test_workflow_close import C
from tests.test_workflow_validate import V,flow,VALIDATE_REFS

class DurableValidationTests(unittest.TestCase):
    setUpClass=classmethod(test_postgres.PersistenceTests.setUpClass.__func__)
    tearDown=test_postgres.PersistenceTests.tearDown
    fresh=test_postgres.PersistenceTests.fresh
    query=test_postgres.PersistenceTests.query
    error=test_postgres.PersistenceTests.error
    crash=test_postgres.PersistenceTests.crash

    def setUp(self):
        test_postgres.PersistenceTests.setUp(self)
        predicate=compile_predicate(field("confirmed"),{"confirmed":FieldType("Bool")},"R10")
        clause=Clause("validated-refund-success","completion","success","refund",("close",),("R10",),(),False,predicate)
        self.contract=amended(default_contract(),extra=(clause,));self.k.trusted.activate_policy(seal(self.k,self.contract))
        self.k.trusted.register_capability(Capability("validated-cap","demo","a",frozenset({"a"}),100,1000,self.k.status()["epoch"]))
        self.request=Request("demo","a","task-a","case-a","a","v1","original-a","70","0")
        self.k.trusted.register_approval(Approval("approved",self.request.request_digest,1000))
        self.va=ValidationAuthority(self.k);order,fee=self.s.read_order("a")
        self.context=self.va.register_order_snapshot("a","a",order,fee,500)
        self.task=TaskAuthority(self.k).register("a","task-a",frozenset({"case-a"}))
        self.refs={"r":self.request,"c":"validated-cap","a":"approved","ctx":self.context,"task":self.task}
        self.ref_types={**VALIDATE_REFS,"task":RefType("Task","refund")}
        self.executor=DurableWorkflowExecutor(self.k,self.s)

    def program(self,body=None,types=None):return flow([V,P,D,S,C] if body is None else body,self.contract,self.ref_types if types is None else types)
    def run_flow(self,body=None,refs=None,faults=None):
        return self.executor.run(self.program(body),"a","run",self.refs if refs is None else refs,{"flag":True},faults)

    def test_full_chain_starts_without_host_generated_evidence(self):
        self.assertEqual(self.query("SELECT * FROM evidence"),[])
        result=self.run_flow();self.assertEqual(result.status,"Finished")
        self.assertEqual([dict(step)["command"] for step in result.steps],["Validate","Prepare","Dispatch","Send","Close"])
        self.assertEqual(dict(result.steps[-1])["result"],"Completed")
        self.assertEqual(len(self.query("SELECT * FROM candidate_validation")),1)
        self.assertEqual(len(self.query("SELECT * FROM evidence")),1)
        self.assertEqual(self.s.real_cost,70);self.assertEqual(self.k.accounts()["tenant:CNY"].pending,0)

    def test_validation_does_not_supply_missing_approval(self):
        result=self.run_flow(refs={**self.refs,"a":"forged"})
        self.assertEqual(result.status,"Halted");self.assertEqual(result.failed_node,"w0.1")
        self.assertEqual(len(self.query("SELECT * FROM evidence")),1)
        self.assertEqual(self.query("SELECT * FROM operation"),[])
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,0)

    def test_unqualified_candidate_stops_at_validate(self):
        result=self.run_flow(refs={**self.refs,"r":replace(self.request,destination="attacker")})
        self.assertEqual((result.status,result.error,result.failed_node),("Halted","INELIGIBLE","w0.0"))
        self.assertEqual(self.query("SELECT * FROM evidence"),[]);self.assertEqual(self.s.effects,())

    def test_foreign_context_and_candidate_identity_fail_at_start(self):
        other_order,fee=self.s.read_order("b");other=self.va.register_order_snapshot("b","b",other_order,fee,500)
        foreign=PostgresKernel(self.dsn,tenant="other",schema=self.schema,clock=self.clock);foreign.initialize()
        foreign_ref=ValidationAuthority(foreign).register_order_snapshot("a","b",other_order,fee,500)
        for ref in (other,foreign_ref,"forged"):
            self.error("AUTH_DENIED",lambda:self.executor.start(self.program(),"a","run",{**self.refs,"ctx":ref},{"flag":True}))
        self.error("INVALID_WORKFLOW_CANDIDATE",lambda:self.executor.start(self.program(),"a","run",{**self.refs,"r":replace(self.request,tenant="other")},{"flag":True}))
        self.assertEqual(self.query("SELECT * FROM workflow_run"),[])

    def test_refresh_after_validation_rejects_prepare_without_consuming_approval(self):
        self.executor.start(self.program(),"a","run",self.refs,{"flag":True});self.executor.advance("a","run","w")
        self.s.change_version("a","v2");order,fee=self.s.read_order("a");self.va.register_order_snapshot("a","a",order,fee,500)
        result=self.executor.resume("a","run")
        self.assertEqual(result.error,"STALE_EVIDENCE");self.assertEqual(result.failed_node,"w0.1")
        approval=loads(self.query("SELECT payload FROM approval WHERE id='approved'")[0]["payload"])
        self.assertIsNone(approval.operation_id);self.assertEqual(self.query("SELECT * FROM operation"),[])

    def test_refresh_after_prepare_keeps_budget_and_blocks_dispatch(self):
        self.executor.start(self.program(),"a","run",self.refs,{"flag":True})
        self.executor.advance("a","run","w");self.executor.advance("a","run","w")
        self.va.revoke_order_context("a");result=self.executor.resume("a","run")
        self.assertEqual((result.error,result.failed_node),("STALE_EVIDENCE","w0.2"))
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70);self.assertEqual(self.s.effects,())

    def test_expiry_between_start_and_validation_is_rechecked(self):
        self.executor.start(self.program(),"a","run",self.refs,{"flag":True});self.clock.now=500
        result=self.executor.resume("a","run")
        self.assertEqual(result.error,"STALE_EVIDENCE");self.assertEqual(self.query("SELECT * FROM evidence"),[])

    def test_policy_change_before_validation_does_not_issue_evidence(self):
        self.executor.start(self.program(),"a","run",self.refs,{"flag":True})
        new=replace(self.contract,policy_version=3);self.k.trusted.activate_policy(seal(self.k,new))
        result=self.executor.resume("a","run")
        self.assertEqual(result.error,"WORKFLOW_POLICY_CHANGED");self.assertEqual(self.query("SELECT * FROM evidence"),[])

    def test_validation_crash_before_commit_rolls_back_all_artifacts_and_pc(self):
        self.executor.start(self.program(),"a","run",self.refs,{"flag":True})
        self.crash("workflow.advance",("a","run","w"),"workflow-step.before_commit")
        for table in ("evidence","checked_request","candidate_validation","workflow_step"):
            self.assertEqual(self.query("SELECT * FROM "+table),[])
        self.assertEqual(self.query("SELECT pc FROM workflow_run")[0]["pc"],0)
        self.assertEqual(self.executor.resume("a","run").status,"Finished")

    def test_validation_crash_after_commit_reuses_evidence_and_checkpoint(self):
        self.executor.start(self.program(),"a","run",self.refs,{"flag":True})
        self.crash("workflow.advance",("a","run","w"),"workflow-node:w0.0.after_commit")
        evidence=self.query("SELECT id FROM evidence")[0]["id"]
        result=DurableWorkflowExecutor(self.fresh(),self.s).resume("a","run")
        self.assertEqual(result.status,"Finished");self.assertEqual(dict(result.steps[0])["evidence_ref"],evidence)
        self.assertEqual(sum(e.kind=="CandidateValidated" for e in self.k.events()),1)

    def test_concurrent_validation_step_commits_once(self):
        self.executor.start(self.program([V]),"a","run",self.refs,{"flag":True});barrier=Barrier(2)
        def advance(i):
            executor=DurableWorkflowExecutor(self.fresh());barrier.wait();return executor.advance("a","run","w"+str(i))
        with ThreadPoolExecutor(2) as pool:list(pool.map(advance,range(2)))
        self.assertEqual(len(self.query("SELECT * FROM evidence")),1);self.assertEqual(len(self.query("SELECT * FROM workflow_step")),1)

    def test_evidence_cannot_authorize_a_different_candidate_digest(self):
        other=replace(self.request,amount_minor="60")
        self.k.trusted.register_approval(Approval("other-approved",other.request_digest,1000))
        program=self.program([V,{**P,"candidate":"other","approval":"other_a"}],{**self.ref_types,"other":RefType("Candidate","refund"),"other_a":RefType("Approval","refund")})
        result=self.executor.run(program,"a","run",{**self.refs,"other":other,"other_a":"other-approved"},{"flag":True})
        self.assertEqual(result.error,"STALE_EVIDENCE");self.assertEqual(self.query("SELECT * FROM operation"),[])

    def test_branch_generated_evidence_and_unknown_reconciliation_complete(self):
        result=self.run_flow([branch([V],[V]),P,D,S,C,Q,{**C,"out":"after_query"}],faults=FaultPlan(drop_receipt_at=frozenset({"w0.3"})))
        self.assertEqual(result.status,"Finished")
        self.assertEqual([dict(step)["result"] for step in result.steps if dict(step)["command"]=="Close"],["Pending","Completed"])
        self.assertEqual(PostgresTasks(self.k).get("a",self.task)["status"],"Completed")
        self.assertEqual(len(self.query("SELECT * FROM candidate_validation")),1)

    def test_completed_run_replay_does_not_revalidate_or_refresh_evidence(self):
        program=self.program();result=self.run_flow();self.clock.now=600
        replay=DurableWorkflowExecutor(self.fresh(),self.s).run(program,"a","run",self.refs,{"flag":True})
        self.assertEqual(replay,result);self.assertEqual(len(self.query("SELECT * FROM evidence")),1)
        self.assertEqual(self.s.real_cost,70)

if __name__=="__main__":unittest.main()
