import unittest
from dataclasses import replace, FrozenInstanceError
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from avcl.canonical import Reject
from avcl.types import Execution, Settlement, Observation, Capability
from .support import setup, authorize, prepare


class ProtocolTests(unittest.TestCase):
    def assertCode(self, code, fn):
        with self.assertRaises(Reject) as cm: fn()
        self.assertEqual(cm.exception.code, code)

    def test_T01_unregistered_evidence_cannot_authorize(self):
        k,s,_ = setup(); auth=authorize(k,s)
        self.assertCode("UNTRUSTED_REFERENCE", lambda:k.prepare("a",auth[0],auth[1],"invented",auth[3],"req"))
        self.assertEqual(k.accounts()["tenant:CNY"].pending,0)

    def test_T02_parameter_mutation_rejected(self):
        k,s,_=setup(); auth=authorize(k,s)
        for req in [replace(auth[0],amount_minor="71"),replace(auth[0],destination="attacker")]:
            self.assertCode("STALE_EVIDENCE",lambda:k.prepare("a",req,*auth[1:],"req"))

    def test_request_identity_conflict(self):
        k,s,_=setup(); auth=authorize(k,s); prepare(k,auth)
        changed=(replace(auth[0],amount_minor="71"),*auth[1:])
        self.assertCode("IDEMPOTENCY_CONFLICT",lambda:prepare(k,changed))

    def test_repeated_prepare_does_not_reserve_twice(self):
        k,s,_=setup(); auth=authorize(k,s); op=prepare(k,auth)
        self.assertEqual(op,prepare(k,auth)); self.assertEqual(k.accounts()["tenant:CNY"].pending,70)
        k.assert_invariants()

    def test_new_request_key_same_business_action_returns_same_operation(self):
        k,s,_=setup(); auth=authorize(k,s); op=prepare(k,auth)
        self.assertEqual(op.id,prepare(k,auth,"new-key").id)
        self.assertEqual(len(k.events()),1)

    def test_T03_remote_version_change_before_send(self):
        k,s,_=setup(); op=prepare(k,authorize(k,s)); op=k.dispatch("a",op.id,op.permit_ref)
        s.change_version("a","v2"); receipt=s.send(op)
        self.assertEqual(receipt.kind,Execution.REJECTED)
        k.trusted.observe(receipt); self.assertEqual(s.effects,()); k.assert_invariants()

    def test_T04_concurrent_reservation(self):
        k,s,_=setup(); a=authorize(k,s,"a",70); b=authorize(k,s,"b",60); barrier=Barrier(2)
        def run(auth):
            barrier.wait()
            try:return prepare(k,auth)
            except Reject as e:return e.code
        with ThreadPoolExecutor(2) as pool: results=list(pool.map(run,[a,b]))
        self.assertEqual(results.count("BUDGET_EXCEEDED"),1);k.assert_invariants()

    def test_T05_lost_receipt_preserves_exposure_and_replay_is_idempotent(self):
        k,s,_=setup(); op=prepare(k,authorize(k,s)); op=k.dispatch("a",op.id,op.permit_ref)
        s.send(op)  # Deliberately drop receipt.
        k.trusted.unknown(op.id)
        self.assertEqual(k.accounts()["tenant:CNY"].pending,70)
        self.assertEqual(s.real_cost,70)
        self.assertCode("BUDGET_EXCEEDED",lambda:prepare(k,authorize(k,s,"b",60)))
        k.trusted.observe(s.send(op)); k.trusted.observe(s.query(op.id))
        self.assertEqual(len(s.effects),1);self.assertEqual(k.accounts()["tenant:CNY"].spent,70)
        self.assertEqual(k.accounts()["tenant:CNY"].pending,0);k.assert_invariants()

    def test_timeout_before_effect_retains_bound(self):
        k,s,_=setup(); op=prepare(k,authorize(k,s));op=k.dispatch("a",op.id,op.permit_ref)
        k.trusted.unknown(op.id);self.assertIsNone(s.query(op.id))
        self.assertEqual(k.accounts()["tenant:CNY"].pending,70)
        k.trusted.observe(s.send(op));k.assert_invariants()

    def test_T06_revoke_before_and_after_admission(self):
        k,s,_=setup();op=prepare(k,authorize(k,s));k.trusted.revoke()
        self.assertCode("STALE_PERMIT",lambda:k.dispatch("a",op.id,op.permit_ref))
        k,s,_=setup();op=prepare(k,authorize(k,s));op=k.dispatch("a",op.id,op.permit_ref)
        k.trusted.revoke();k.trusted.observe(s.send(op))
        self.assertEqual(s.real_cost,70);k.assert_invariants()

    def test_T07_local_cancel_cannot_release_admitted_operation(self):
        k,s,_=setup();op=prepare(k,authorize(k,s));op=k.dispatch("a",op.id,op.permit_ref)
        k.trusted.unknown(op.id)
        self.assertCode("NO_EFFECT_NOT_ESTABLISHED",lambda:k.abort_prepared("a",op.id))
        receipt=s.cancel_with_tombstone(op);k.trusted.observe(receipt)
        self.assertEqual(s.send(op),receipt);self.assertEqual(s.effects,());k.assert_invariants()

    def test_prepared_abort_is_idempotent(self):
        k,s,_=setup();op=prepare(k,authorize(k,s));k.abort_prepared("a",op.id);k.abort_prepared("a",op.id)
        self.assertEqual(k.accounts()["tenant:CNY"].pending,0)
        self.assertCode("INVALID_TRANSITION",lambda:k.dispatch("a",op.id,op.permit_ref));k.assert_invariants()

    def test_T08_duplicate_concurrent_dispatch(self):
        k,s,_=setup();op=prepare(k,authorize(k,s));barrier=Barrier(2)
        def run(_):barrier.wait();return k.dispatch("a",op.id,op.permit_ref)
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,range(2)))
        self.assertEqual(results[0],results[1])
        self.assertEqual(sum(e.kind=="DispatchAdmitted" for e in k.events()),1);k.assert_invariants()

    def test_wrong_permit_or_subject_rejected(self):
        k,s,_=setup(limit=200);a=prepare(k,authorize(k,s));b=prepare(k,authorize(k,s,"b",60))
        self.assertCode("BINDING_MISMATCH",lambda:k.dispatch("a",a.id,b.permit_ref))
        self.assertCode("AUTH_DENIED",lambda:k.dispatch("b",a.id,a.permit_ref))

    def test_expiry_checked_again_at_dispatch(self):
        k,s,clock=setup();op=prepare(k,authorize(k,s));clock.now=op.expires
        self.assertCode("STALE_PERMIT",lambda:k.dispatch("a",op.id,op.permit_ref))
        self.assertEqual(k.accounts()["tenant:CNY"].pending,70)

    def test_unknown_deadline_does_not_reset(self):
        k,s,clock=setup();op=prepare(k,authorize(k,s));k.dispatch("a",op.id,op.permit_ref)
        first=k.trusted.unknown(op.id);clock.now=123
        self.assertEqual(k.trusted.unknown(op.id).obligation_due,first.obligation_due)

    def test_business_confirmation_retains_unsettled_fee(self):
        k,s,_=setup(fee=10);op=prepare(k,authorize(k,s,value=70,fee=10));op=k.dispatch("a",op.id,op.permit_ref)
        receipt=s.send(op)
        partial=replace(receipt,id="partial",final_cost=False,cumulative_costs=(("tenant:CNY",70),("order:a:CNY",70)))
        result=k.trusted.observe(partial)
        self.assertEqual(result.state,Execution.CONFIRMED);self.assertEqual(result.settlement,Settlement.PARTIAL)
        self.assertEqual(k.accounts()["tenant:CNY"].pending,10)
        k.trusted.observe(receipt);self.assertEqual(k.accounts()["tenant:CNY"].spent,80);k.assert_invariants()

    def test_no_effect_can_have_fee(self):
        k,s,_=setup();op=prepare(k,authorize(k,s,value=70,fee=10));k.dispatch("a",op.id,op.permit_ref)
        receipt=Observation("fee-only",op.id,op.request.request_digest,Execution.REJECTED,
                            (("tenant:CNY",3),("order:a:CNY",0)),True,True)
        k.trusted.observe(receipt);self.assertEqual(k.accounts()["tenant:CNY"].spent,3);k.assert_invariants()

    def test_T14_actual_over_bound_cost_is_recorded_and_quarantined(self):
        k,s,_=setup();op=prepare(k,authorize(k,s));op=k.dispatch("a",op.id,op.permit_ref)
        receipt=s.send(op); receipt=replace(receipt,cumulative_costs=(("tenant:CNY",110),("order:a:CNY",70)))
        result=k.trusted.observe(receipt)
        self.assertEqual(result.settlement,Settlement.DISPUTED)
        self.assertEqual(k.accounts()["tenant:CNY"].spent,110)
        self.assertTrue(k.accounts()["tenant:CNY"].breached)
        self.assertCode("ASSUMPTION_BROKEN",lambda:prepare(k,authorize(k,s,"b",1)))
        self.assertTrue(any(e.kind=="ObservationRecorded" for e in k.events()));k.assert_invariants()

    def test_contradictory_terminal_receipt_not_overwritten(self):
        k,s,_=setup();op=prepare(k,authorize(k,s));op=k.dispatch("a",op.id,op.permit_ref)
        receipt=s.send(op);k.trusted.observe(receipt)
        result=k.trusted.observe(replace(receipt,id="conflict",kind=Execution.REJECTED))
        self.assertEqual(result.state,Execution.CONFIRMED);self.assertEqual(result.settlement,Settlement.DISPUTED)
        self.assertEqual(k.accounts()["tenant:CNY"].spent,70)

    def test_T19_second_account_failure_is_atomic(self):
        k,s,_=setup(order_limit=100);auth=authorize(k,s,value=70)
        # Trusted setup fault: local account tighter than remote eligibility.
        k._accounts["order:a:CNY"]=replace(k._accounts["order:a:CNY"],limit=60)
        before=k.accounts()
        self.assertCode("BUDGET_EXCEEDED",lambda:prepare(k,auth))
        self.assertEqual(k.accounts(),before);self.assertEqual(k.events(),());k.assert_invariants()

    def test_dispute_is_not_cleared_by_an_ordinary_receipt(self):
        k,s,_=setup();op=prepare(k,authorize(k,s));op=k.dispatch("a",op.id,op.permit_ref)
        receipt=s.send(op);k.trusted.observe(receipt)
        k.trusted.observe(replace(receipt,id="conflict",kind=Execution.REJECTED))
        result=k.trusted.observe(replace(receipt,id="later"))
        self.assertEqual(result.settlement,Settlement.DISPUTED)
        self.assertCode("ASSUMPTION_BROKEN",lambda:prepare(k,authorize(k,s,"b",1)))
        k.assert_invariants()

    def test_returned_snapshots_cannot_mutate_internal_state(self):
        k,s,_=setup();op=prepare(k,authorize(k,s))
        with self.assertRaises(FrozenInstanceError):op.state=Execution.CONFIRMED
        snapshot=k.accounts();snapshot.clear()
        self.assertEqual(k.accounts()["tenant:CNY"].pending,70);k.assert_invariants()

    def test_trusted_ingress_rejects_mutable_capability_scope(self):
        k,_,_=setup()
        bad=Capability("bad","demo","a",{"a"},100,1000,1)
        self.assertCode("INVALID_CAPABILITY",lambda:k.trusted.register_capability(bad))

    def test_malformed_adapter_receipt_cannot_free_reservation(self):
        k,s,_=setup();op=prepare(k,authorize(k,s));op=k.dispatch("a",op.id,op.permit_ref)
        receipt=s.send(op)
        for bad in [replace(receipt,final_cost="false"),replace(receipt,terminal_effect=1),
                    replace(receipt,cumulative_costs=[("tenant:CNY",70)]),replace(receipt,id=[])]:
            self.assertCode("UNTRUSTED_OBSERVATION",lambda:k.trusted.observe(bad))
        self.assertEqual(k.accounts()["tenant:CNY"].pending,70);k.assert_invariants()

    def test_final_fee_without_terminal_effect_keeps_exposure(self):
        k,s,_=setup();op=prepare(k,authorize(k,s));op=k.dispatch("a",op.id,op.permit_ref)
        obs=Observation("partial",op.id,op.request.request_digest,Execution.PARTIAL,
                        (("tenant:CNY",20),("order:a:CNY",20)),True,False)
        result=k.trusted.observe(obs)
        self.assertEqual(result.settlement,Settlement.PARTIAL)
        self.assertEqual((k.accounts()["tenant:CNY"].spent,k.accounts()["tenant:CNY"].pending),(20,50))
        bad=replace(obs,id="erase",kind=Execution.REJECTED,terminal_effect=True)
        result=k.trusted.observe(bad)
        self.assertEqual(result.state,Execution.PARTIAL)
        self.assertEqual(result.settlement,Settlement.DISPUTED)
        self.assertEqual(k.accounts()["tenant:CNY"].pending,50);k.assert_invariants()

    def test_additional_cost_after_final_is_recorded_as_incident(self):
        k,s,_=setup();op=prepare(k,authorize(k,s));op=k.dispatch("a",op.id,op.permit_ref)
        receipt=s.send(op);k.trusted.observe(receipt)
        result=k.trusted.observe(replace(receipt,id="extra-fee",cumulative_costs=(("tenant:CNY",71),("order:a:CNY",70))))
        self.assertEqual(result.settlement,Settlement.DISPUTED)
        self.assertEqual(k.accounts()["tenant:CNY"].spent,71)
        self.assertTrue(k.accounts()["tenant:CNY"].breached);k.assert_invariants()
