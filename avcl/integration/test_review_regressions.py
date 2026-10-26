"""Review regressions on real PostgreSQL, including restart/crash composition."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
import test_postgres
from avcl.canonical import Reject
from avcl.contract import default_contract, Clause
from avcl.ir import compile_predicate, FieldType
from avcl.types import Observation, Execution, Settlement, Capability
from avcl.simulator import RefundService, Order
from avcl.tasks import TaskAuthority, PostgresTasks
from avcl.workflow_postgres import DurableWorkflowExecutor
from avcl.sending import admit_attempt
from tests.support import authorize, prepare
from tests.test_workflow import make, context, P, D, S, branch
from tests.test_contract_policy import amended, seal
from tests.test_encoding_ir import field


class DurableReviewRegressions(unittest.TestCase):
    setUpClass=classmethod(test_postgres.PersistenceTests.setUpClass.__func__)
    setUp=test_postgres.PersistenceTests.setUp
    tearDown=test_postgres.PersistenceTests.tearDown
    fresh=test_postgres.PersistenceTests.fresh
    query=test_postgres.PersistenceTests.query
    error=test_postgres.PersistenceTests.error
    crash=test_postgres.PersistenceTests.crash

    def service(self, fee=0):
        return RefundService({'a':Order('v1','original-a',100)},fee=fee,retention_seconds=120,
                             clock=self.clock,verifier=self.k.trusted.send_verifier())

    def final_task(self, fee):
        clause=Clause('refund-success','completion','success','refund',('close',),('R10',),(),False,
                      compile_predicate(field('confirmed'),{'confirmed':FieldType('Bool')},'R10'))
        self.k.trusted.activate_policy(seal(self.k,amended(default_contract(),extra=(clause,))))
        self.k.trusted.register_capability(Capability('review-cap','demo','a',frozenset({'a'}),100,1000,self.k.status()['epoch']))
        task=TaskAuthority(self.k).register('a','task-a',frozenset({'case-a'}))
        self.k.trusted.set_capacity(1,1,1)
        s=self.service();auth=authorize(self.k,s,fee=fee)
        op=prepare(self.k,(auth[0],'review-cap',*auth[2:]));op=self.k.dispatch('a',op.id,op.permit_ref)
        s.send(self.k.claim_outbox('sender',authenticated_receipts=True)['envelope'])
        old=Observation('old',op.id,op.request.request_digest,Execution.CONFIRMED,
                        (('tenant:CNY',70),('order:a:CNY',70)),False,True)
        self.clock.now=2
        signer=s._verifier._receipt_signer
        self.k.trusted.observe(signer.seal(replace(old,id='final',final_cost=True),op.binding,2),op.id)
        self.assertEqual(PostgresTasks(self.k).close('a',task,'close')['status'],'Completed')
        return s,op,task,signer.seal(old,op.binding,1)

    def assert_final_task(self, op, task):
        k=self.fresh();status=k.status()
        self.assertEqual(k.get('a',op.id).settlement,Settlement.FINAL)
        self.assertEqual((k.accounts()['tenant:CNY'].spent,k.accounts()['tenant:CNY'].pending),(70,0))
        self.assertFalse(status['quarantined'])
        self.assertEqual(status['outbox'][0]['status'],'Done')
        self.assertEqual(PostgresTasks(k).get('a',task)['status'],'Completed')
        self.assertEqual(k.capacity()['usage'],{'operations':0,'obligation_slots':0,'open_obligations':0,'workflows':0})
        self.assertEqual(k.capacity()['operation_headroom'],1)
        self.assertEqual(len(self.query('SELECT * FROM task_close_request')),1)
        k.assert_invariants()

    def test_late_weaker_fact_preserves_task_outbox_obligations_and_capacity(self):
        s,op,task,old=self.final_task(0)
        historical=self.query('SELECT * FROM task_close_request')
        self.k.trusted.observe(old,op.id);self.fresh().trusted.observe(old,op.id)
        self.assert_final_task(op,task)
        self.assertEqual(self.query('SELECT * FROM task_close_request'),historical)
        self.assertEqual((s.real_cost,len(s.effects)),(70,1))

    def test_unused_fee_is_not_restored_after_raw_receipt_commit_crash(self):
        s,op,task,old=self.final_task(10)
        self.crash('observe',(old,op.id),'observe.after_raw_commit')
        self.assertEqual(len(self.fresh().recover_observations()),1)
        self.assertEqual(self.fresh().recover_observations(),[])
        self.assert_final_task(op,task)
        self.assertEqual((s.real_cost,len(s.effects)),(70,1))

    def test_actual_cost_after_final_still_records_dispute(self):
        s,op,task,_=self.final_task(10)
        bad=Observation('extra-fee',op.id,op.request.request_digest,Execution.CONFIRMED,
                        (('tenant:CNY',71),('order:a:CNY',70)),True,True)
        raw=s._verifier._receipt_signer.seal(bad,op.binding,2)
        self.k.trusted.observe(raw,op.id)
        self.assertEqual(self.fresh().get('a',op.id).settlement,Settlement.DISPUTED)
        self.assertEqual(self.k.accounts()['tenant:CNY'].spent,71)
        self.assertEqual(PostgresTasks(self.fresh()).get('a',task)['status'],'Failed')

    def workflow(self, body, effects=None, flag=False):
        s=self.service();flow=make(body,**({'effects':effects} if effects else {}))
        ex=DurableWorkflowExecutor(self.k,s)
        ex.start(flow,'a','review',context(self.k,s),{'flag':flag})
        return s,flow,ex

    def test_dispatch_only_cannot_be_claimed_by_generic_sender(self):
        s,flow,ex=self.workflow([P,D],('ResourceConsume',))
        result=ex.resume('a','review')
        self.assertEqual(result.status,'Finished')
        self.assertIsNone(self.fresh().claim_outbox('generic',authenticated_receipts=True))
        self.assertEqual(s.effects,())
        self.assertEqual(sum(e.kind=='SendAttemptAdmitted' for e in self.k.events()),0)
        op=self.k.get('a',result.operation_ids[0])
        self.error('WORKFLOW_SEND_NOT_AUTHORIZED',lambda:self.k._transaction('guard-check',
            lambda k,c:admit_attempt(k,k.get('a',op.id),run_id='review',node_id='fake',workflow_digest=flow.semantic_digest)))

    def test_false_branch_cannot_be_sent_after_dispatch_crash(self):
        s,flow,ex=self.workflow([P,D,branch([S],[])])
        self.crash('workflow.resume',('a','review'),'workflow-node:w0.1.after_commit')
        self.assertIsNone(self.fresh().claim_outbox('generic'))
        result=DurableWorkflowExecutor(self.fresh(),s).resume('a','review')
        self.assertEqual(result.status,'Finished')
        self.assertIsNone(self.fresh().claim_outbox('generic'))
        self.clock.now=31;self.k.reap_expired_leases()
        self.assertIsNone(self.fresh().claim_outbox('generic'))
        self.assertEqual(s.effects,())
        self.assertEqual(self.k.accounts()['tenant:CNY'].pending,70)

    def test_workflow_true_branch_and_generic_sender_compete_without_escape(self):
        s,flow,ex=self.workflow([P,D,branch([S],[])],flag=True)
        ex.advance('a','review','workflow');ex.advance('a','review','workflow')
        barrier=Barrier(2)
        def generic():barrier.wait();return self.fresh().claim_outbox('generic')
        def workflow():barrier.wait();return DurableWorkflowExecutor(self.fresh(),s).resume('a','review')
        with ThreadPoolExecutor(2) as pool:
            a=pool.submit(generic);b=pool.submit(workflow)
            self.assertIsNone(a.result());self.assertEqual(b.result().status,'Finished')
        self.assertEqual((s.real_cost,len(s.effects)),(70,1))
        event=[e for e in self.k.events() if e.kind=='SendAttemptAdmitted'][0]
        details=dict(event.details)
        self.assertEqual((details['run_id'],details['node_id'],details['workflow_digest']),('review','w0.2.then.0',flow.semantic_digest))
        self.assertEqual(details['send_owner'],'workflow')
        self.assertEqual(len(self.query("SELECT * FROM workflow_io WHERE kind='Send'")),1)
        self.assertEqual(len(self.query('SELECT * FROM send_attempt')),1)

    def test_send_intent_commit_crash_never_hands_operation_to_generic_sender(self):
        s,_,ex=self.workflow([P,D,S],flag=True)
        self.crash('workflow.resume',('a','review'),'workflow-node:w0.2.after_commit')
        self.assertIsNone(self.fresh().claim_outbox('generic'))
        self.clock.now=31
        result=DurableWorkflowExecutor(self.fresh(),s).resume('a','review')
        self.assertEqual(result.status,'Finished')
        self.assertIsNone(self.fresh().claim_outbox('generic'))
        self.assertEqual(s.effects,())
        self.assertEqual(len(self.query('SELECT * FROM send_attempt')),1)

    def test_false_branch_and_generic_sender_concurrently_produce_no_effect(self):
        s,_,ex=self.workflow([P,D,branch([S],[])],flag=False)
        ex.advance('a','review','workflow');ex.advance('a','review','workflow')
        barrier=Barrier(2)
        def sender():barrier.wait();return self.fresh().claim_outbox('generic')
        def workflow():barrier.wait();return DurableWorkflowExecutor(self.fresh(),s).resume('a','review')
        with ThreadPoolExecutor(2) as pool:
            a=pool.submit(sender);b=pool.submit(workflow)
            self.assertIsNone(a.result());self.assertEqual(b.result().status,'Finished')
        self.assertEqual(s.effects,())
        self.assertEqual(self.query('SELECT * FROM send_attempt'),[])

    def test_send_intent_rollback_leaves_no_grant_or_attempt(self):
        s,_,ex=self.workflow([P,D,S],flag=True)
        ex.advance('a','review','workflow');ex.advance('a','review','workflow')
        self.crash('workflow.resume',('a','review'),'workflow-step.before_commit')
        self.assertEqual(self.query('SELECT * FROM send_attempt'),[])
        self.assertEqual(self.query('SELECT * FROM workflow_io'),[])
        self.assertIsNone(self.fresh().claim_outbox('generic'))
        self.assertEqual(DurableWorkflowExecutor(self.fresh(),s).resume('a','review').status,'Finished')
        self.assertEqual(len(s.effects),1)

    def test_aggregate_cost_survives_receipt_commit_crash_and_reopen(self):
        maximum=10**18-1
        # Host setup of the independent test tenant's limits; no operation exists yet.
        self.query("UPDATE budget_account SET limit_amount=%s WHERE id='tenant:CNY'",(2*maximum,))
        s=self.service(fee=maximum)
        op=prepare(self.k,authorize(self.k,s,value=1,fee=maximum));op=self.k.dispatch('a',op.id,op.permit_ref)
        raw=s.send(self.k.claim_outbox('direct',authenticated_receipts=True)['envelope'])
        self.crash('observe',(raw,op.id),'observe.after_raw_commit')
        self.assertEqual(len(self.fresh().recover_observations()),1)
        self.assertEqual(self.fresh().get('a',op.id).settlement,Settlement.FINAL)
        self.assertEqual((self.k.accounts()['tenant:CNY'].spent,self.k.accounts()['tenant:CNY'].pending),(10**18,0))
        self.assertEqual(s.real_cost,10**18)

    def test_durable_history_still_matches_memory_replay_contract(self):
        s=self.service();flow=make([P,D,S]);refs=context(self.k,s)
        ex=DurableWorkflowExecutor(self.k,s);first=ex.run(flow,'a','history',refs,{'flag':True})
        self.k.trusted.activate_policy(seal(self.k,amended(default_contract())))
        reopened=DurableWorkflowExecutor(self.fresh(),s)
        self.assertEqual(reopened.run(flow,'a','history',refs,{'flag':True}),first)
        self.error('WORKFLOW_RUN_CONFLICT',lambda:reopened.run(flow,'a','history',refs,{'flag':False}))
        self.error('WORKFLOW_CONTRACT_MISMATCH',lambda:reopened.run(flow,'a','new',refs,{'flag':True}))
        self.assertEqual(len(s.effects),1)
