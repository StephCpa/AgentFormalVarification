"""Durable sender deadlines and real record expiry; uses the owned test cluster."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
import unittest

import test_postgres
from avcl.simulator import RefundService,Order
from avcl.types import Execution,Settlement
from avcl.workflow_postgres import DurableWorkflowExecutor
from tests.support import authorize,prepare
from tests.test_workflow import make,context,P,D,S,Q


class DurableSendWindowTests(unittest.TestCase):
    setUpClass=classmethod(test_postgres.PersistenceTests.setUpClass.__func__)
    setUp=test_postgres.PersistenceTests.setUp
    tearDown=test_postgres.PersistenceTests.tearDown
    fresh=test_postgres.PersistenceTests.fresh
    error=test_postgres.PersistenceTests.error
    query=test_postgres.PersistenceTests.query
    crash=test_postgres.PersistenceTests.crash

    def dispatch(self,ttl=60):
        self.auth=authorize(self.k,self.s,value=20)
        op=prepare(self.k,self.auth,ttl=ttl)
        return self.k.dispatch("a",op.id,op.permit_ref)

    def test_claim_checks_outbox_deadline_without_waiting_for_reaper(self):
        op=self.dispatch()
        self.clock.now=30
        self.assertIsNone(self.fresh().claim_outbox("late-worker"))
        self.assertEqual(self.fresh().get("a",op.id).state,Execution.UNKNOWN)
        self.assertEqual(self.k.status()["outbox"][0]["status"],"NeedsReconcile")
        self.assertEqual(self.query("SELECT * FROM send_attempt"),[])
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,20)
        self.assertEqual(self.k.status()["obligations"][0]["status"],"Open")

    def test_claim_checks_packet_margin_before_outbox_expiry(self):
        op=self.dispatch(ttl=5)
        self.clock.now=4
        self.assertIsNone(self.fresh().claim_outbox("near-cutoff"))
        self.assertEqual(self.fresh().get("a",op.id).state,Execution.UNKNOWN)
        self.assertTrue(any(dict(e.details).get("reason")=="SEND_WINDOW_CLOSED" for e in self.k.events()))
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,20)

    def test_claim_crash_then_retention_expiry_never_reissues_write(self):
        self.s=RefundService({"a":Order("v1","original-a",100)},retention_seconds=5,clock=self.clock)
        op=self.dispatch(ttl=5)
        self.crash("claim_outbox",("worker",2),"claim.after_commit")
        lost_receipt=self.s.send(op)
        self.clock.now=5
        self.assertIsNone(self.s.query(op.id))
        self.fresh().reap_expired_leases()
        self.assertIsNone(self.fresh().claim_outbox("retry"))
        self.error("REMOTE_ACCEPTANCE_CLOSED",lambda:self.s.send(op))
        self.assertEqual(len(self.query("SELECT * FROM send_attempt")),1)
        self.assertEqual((self.s.real_cost,self.k.accounts()["tenant:CNY"].pending),(20,20))
        self.assertEqual(prepare(self.fresh(),self.auth,request_id="different-client-key").id,op.id)
        self.fresh().trusted.observe(lost_receipt)
        self.assertEqual(self.k.get("a",op.id).settlement,Settlement.FINAL)

    def test_two_workers_cannot_claim_one_attempt(self):
        op=self.dispatch();barrier=Barrier(2)
        def claim(worker):
            barrier.wait()
            return self.fresh().claim_outbox(worker)
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(claim,("one","two")))
        self.assertEqual(sum(r is not None for r in results),1)
        self.assertEqual(len(self.query("SELECT * FROM send_attempt")),1)
        event=next(e for e in self.k.events() if e.kind=="SendAttemptAdmitted")
        self.assertEqual(dict(event.details)["not_after"],str(op.expires))
        self.assertEqual(dict(event.details)["attempt"],"1")

    def test_workflow_deadline_blocks_transport_and_continues_query(self):
        refs=context(self.k,self.s);flow=make([P,D,S,Q])
        executor=DurableWorkflowExecutor(self.k,self.s)
        executor.start(flow,"a","cutoff",refs,{"flag":True})
        executor.advance("a","cutoff","worker")
        executor.advance("a","cutoff","worker")
        self.clock.now=59
        with patch.object(self.s,"send",wraps=self.s.send) as send:
            result=DurableWorkflowExecutor(self.fresh(),self.s).resume("a","cutoff")
            send.assert_not_called()
        self.assertEqual(result.status,"Finished")
        self.assertEqual(self.k.get("a",result.operation_ids[0]).state,Execution.UNKNOWN)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        self.assertEqual(self.query("SELECT * FROM send_attempt"),[])

    def test_delayed_workflow_packet_rejected_without_false_quarantine(self):
        clock=self.clock
        service=RefundService({"a":Order("v1","original-a",100)},retention_seconds=60,clock=clock)
        original=service.send
        def delayed(op):
            clock.now=op.expires
            return original(op)
        refs=context(self.k,service)
        with patch.object(service,"send",side_effect=delayed):
            result=DurableWorkflowExecutor(self.k,service).run(make([P,D,S,Q]),"a","delay",refs,{"flag":True})
        self.assertEqual(result.status,"Finished")
        self.assertEqual(service.effects,())
        self.assertFalse(self.k.status()["quarantined"])
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)

    def test_clock_rollback_after_refusal_does_not_reopen_claim(self):
        self.dispatch(ttl=5)
        self.clock.now=4
        self.assertIsNone(self.k.claim_outbox("cutoff"))
        self.clock.now=0
        self.error("CLOCK_ROLLBACK",lambda:self.fresh().claim_outbox("backwards"))
