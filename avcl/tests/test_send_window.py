import unittest
from unittest.mock import patch

from avcl.canonical import Reject
from avcl.sending import admit_attempt, MAX_ATTEMPTS
from avcl.simulator import RefundService, Order
from avcl.types import Execution, Settlement
from avcl.workflow_runtime import WorkflowExecutor, FaultPlan
from .support import setup, authorize, prepare
from .test_workflow import make, context, P, D, S, Q
from models.send_window_model import explore,successors


class SendWindowTests(unittest.TestCase):
    def error(self, code, action):
        with self.assertRaises(Reject) as caught:action()
        self.assertEqual(caught.exception.code, code)

    def environment(self, ttl=5, retention=5):
        k, _, clock=setup()
        service=RefundService({"a":Order("v1","original-a",100)},retention_seconds=retention,clock=clock)
        auth=authorize(k,service,value=20)
        op=prepare(k,auth,ttl=ttl)
        op=k.dispatch("a",op.id,op.permit_ref)
        return k,service,clock,op,auth

    def test_expired_remote_record_does_not_enable_second_effect(self):
        k,s,clock,op,auth=self.environment()
        receipt=s.send(op)
        k.trusted.unknown(op.id)
        clock.now=1
        self.assertEqual(s.send(op),receipt)
        clock.now=5
        self.assertIsNone(s.query(op.id))  # Actual record deletion, not just host expiry.
        self.error("REMOTE_ACCEPTANCE_CLOSED",lambda:s.send(op))
        self.assertEqual((len(s.effects),s.real_cost),(1,20))
        self.assertEqual(k.accounts()["tenant:CNY"].pending,20)
        # A new client request key still resolves the same unresolved business action.
        self.assertEqual(prepare(k,auth,request_id="new-client-key").id,op.id)
        k.trusted.observe(receipt)  # Independently retained authoritative evidence.
        self.assertEqual(k.get("a",op.id).settlement,Settlement.FINAL)

    def test_expired_cancellation_tombstone_rejects_late_packet(self):
        k,s,clock,op,_=self.environment()
        receipt=s.cancel_with_tombstone(op)
        self.assertEqual(receipt.kind,Execution.REJECTED)
        clock.now=5
        self.assertIsNone(s.query(op.id))
        self.error("REMOTE_ACCEPTANCE_CLOSED",lambda:s.send(op))
        self.error("REMOTE_ACCEPTANCE_CLOSED",lambda:s.cancel_with_tombstone(op))
        self.assertEqual(s.effects,())
        self.assertEqual(k.accounts()["tenant:CNY"].pending,20)

    def test_retention_cannot_end_before_acceptance_deadline(self):
        _,s,_,op,_=self.environment(ttl=6,retention=5)
        self.error("REMOTE_RETENTION_TOO_SHORT",lambda:s.send(op))
        self.assertIsNone(s.query(op.id))
        self.assertEqual(s.real_cost,0)

    def test_duplicate_packet_does_not_extend_retention(self):
        _,s,clock,op,_=self.environment()
        s.send(op)
        clock.now=4
        s.send(op)
        clock.now=5
        self.assertIsNone(s.query(op.id))

    def test_local_safety_margin_and_clock_rollback(self):
        k,_,clock,op,_=self.environment()
        clock.now=3
        with k._lock:self.assertTrue(admit_attempt(k,op))
        clock.now=4
        with k._lock:self.assertFalse(admit_attempt(k,op))
        self.assertEqual(k.get("a",op.id).state,Execution.UNKNOWN)
        clock.now=3
        self.error("CLOCK_ROLLBACK",lambda:admit_attempt(k,op))
        self.assertEqual(k.accounts()["tenant:CNY"].pending,20)

    def test_remote_clock_rollback_cannot_reopen_window(self):
        _,s,clock,op,_=self.environment()
        s.send(op)
        clock.now=5
        self.assertIsNone(s.query(op.id))
        clock.now=0
        self.error("REMOTE_CLOCK_ROLLBACK",lambda:s.send(op))
        self.assertEqual(len(s.effects),1)

    def test_workflow_caps_attempts_without_dropping_unknown_budget(self):
        k,s,_=setup()
        flow=make([P,D,S,S,S,S,Q])
        faults=FaultPlan(no_send_at=frozenset({"w0.2","w0.3","w0.4"}))
        with patch.object(s,"send",wraps=s.send) as send:
            result=WorkflowExecutor(k,s).run(flow,"a","attempts",context(k,s),{"flag":True},faults)
            send.assert_not_called()
        self.assertEqual(result.status,"Finished")
        self.assertEqual(sum(e.kind=="SendAttemptAdmitted" for e in k.events()),MAX_ATTEMPTS)
        self.assertEqual(k.accounts()["tenant:CNY"].pending,70)
        self.assertEqual(k.get("a",result.operation_ids[0]).state,Execution.UNKNOWN)
        self.assertTrue(any(dict(e.details).get("reason")=="SEND_ATTEMPTS_EXHAUSTED" for e in k.events()))

    def test_delayed_worker_is_fenced_at_remote_acceptance(self):
        k,_,clock=setup()
        class Delayed(RefundService):
            def send(self,op):
                clock.now=op.expires
                return super().send(op)
        s=Delayed({"a":Order("v1","original-a",100)},retention_seconds=60,clock=clock)
        result=WorkflowExecutor(k,s).run(make([P,D,S,Q]),"a","delayed",context(k,s),{"flag":True})
        self.assertEqual(result.status,"Finished")
        self.assertEqual(s.effects,())
        self.assertEqual(k.get("a",result.operation_ids[0]).state,Execution.UNKNOWN)
        self.assertEqual(k.accounts()["tenant:CNY"].pending,70)
        self.assertFalse(k._quarantined)

    def test_invalid_finite_retention_configuration(self):
        for value in (True,0,-1,"60"):
            with self.assertRaises(ValueError):RefundService({},retention_seconds=value,clock=lambda:0)
        with self.assertRaises(ValueError):RefundService({},retention_seconds=5)

    def test_bounded_window_model_includes_delayed_packets_and_record_expiry(self):
        report,paths=explore()
        self.assertEqual(report["violations"],{})
        self.assertTrue(any(s.now>=2 and s.packets>0 and s.effects==1 and s.record_until==0 for s in paths))
        self.assertTrue(any(s.effects==1 for s in paths))
        for s in paths:
            if s.now>=2:
                self.assertFalse(any(action=="AdmitPacket" for action,_ in successors(s)))

    def test_three_window_mutants_have_replayable_counterexamples(self):
        for mode,error in (("late_sender","late_sender_admission"),("unfenced_server","duplicate_effect"),("absence_releases","actual_exceeds_exposure")):
            report,paths=explore(mode)
            trace=report["violations"][error]["trace"]
            state=next(iter(paths))
            self.assertTrue(trace)
            for action in trace:state=dict(successors(state,mode))[action]
            if error=="duplicate_effect":self.assertGreater(state.effects,1)
            elif error=="actual_exceeds_exposure":self.assertGreater(state.effects,state.pending)
            else:self.assertGreaterEqual(state.now,2)
