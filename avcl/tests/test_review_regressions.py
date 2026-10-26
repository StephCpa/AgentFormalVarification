"""Cross-module regressions from the review of 3e5c63f."""
from dataclasses import replace
import unittest
from avcl.canonical import Reject, amount, cumulative_cost, canonical_bytes, strict_loads
import hmac
import hashlib
from avcl.receipt_envelope import MAC_DOMAIN
from avcl.types import Observation, Execution, Settlement, Capability
from avcl.simulator import RefundService, Order
from avcl.sending import admit_attempt
from avcl.workflow_runtime import WorkflowExecutor, FaultPlan
from avcl.contract import default_contract
from .support import setup, authorize, prepare
from .test_workflow import make, context, P, D, S, Q, branch
from .test_contract_policy import amended, seal


class ReviewRegressions(unittest.TestCase):
    def assertCode(self, code, fn):
        with self.assertRaises(Reject) as caught: fn()
        self.assertEqual(caught.exception.code, code)

    def test_final_knowledge_survives_late_weaker_authenticated_facts(self):
        for fee in (0, 10):
            for final_first in (True, False):
                with self.subTest(fee=fee, final_first=final_first):
                    k, _, clock = setup()
                    verifier = k.trusted.send_verifier()
                    s = RefundService({'a': Order('v1', 'original-a', 100)},
                                      retention_seconds=120, clock=clock, verifier=verifier)
                    op = prepare(k, authorize(k, s, fee=fee))
                    op = k.dispatch('a', op.id, op.permit_ref)
                    with k._lock: packet = admit_attempt(k, op)
                    s.send(packet)
                    older = Observation('old', op.id, op.request.request_digest, Execution.CONFIRMED,
                                        (('tenant:CNY', 70), ('order:a:CNY', 70)), False, True)
                    newer = replace(older, id='new', final_cost=True)
                    clock.now = 2
                    old_raw = verifier._receipt_signer.seal(older, op.binding, 1)
                    new_raw = verifier._receipt_signer.seal(newer, op.binding, 2)
                    for raw in ([new_raw, old_raw] if final_first else [old_raw, new_raw]):
                        k.trusted.observe(raw, op.id)
                    k.trusted.observe(old_raw, op.id)
                    self.assertEqual(k.get('a', op.id).settlement, Settlement.FINAL)
                    self.assertEqual((k.accounts()['tenant:CNY'].spent, k.accounts()['tenant:CNY'].pending), (70, 0))
                    self.assertFalse(k._quarantined)
                    self.assertEqual((s.real_cost, len(s.effects)), (70, 1))
                    self.assertEqual(len(k._observations), 2)
                    k.assert_invariants()

    def test_final_does_not_hide_new_cost_or_contradictory_effect(self):
        for change in ('cost', 'effect', 'same-id'):
            k, s, _ = setup()
            op = prepare(k, authorize(k, s, fee=10)); op = k.dispatch('a', op.id, op.permit_ref)
            receipt = s.send(op); k.trusted.observe(receipt)
            bad = replace(receipt, id='late', final_cost=False)
            if change == 'effect': bad = replace(bad, kind=Execution.REJECTED)
            else:
                bad = replace(bad, id=receipt.id if change == 'same-id' else 'late',
                              cumulative_costs=(('tenant:CNY', 71), ('order:a:CNY', 70)))
            self.assertEqual(k.trusted.observe(bad).settlement, Settlement.DISPUTED)
            self.assertTrue(k._quarantined)

    def test_workflow_owner_blocks_generic_sender_after_unselected_send(self):
        k, s, _ = setup(); executor = WorkflowExecutor(k, s)
        result = executor.run(make([P, D, branch([S], [])]), 'a', 'owned', context(k, s), {'flag': False})
        op = k.get('a', result.operation_ids[0])
        self.assertCode('WORKFLOW_SEND_NOT_AUTHORIZED', lambda: admit_attempt(k, op))
        self.assertCode('WORKFLOW_SEND_NOT_AUTHORIZED', lambda: admit_attempt(
            k, op, run_id='owned', node_id='w0.2.then.0', workflow_digest=result.workflow_digest))
        self.assertEqual(s.effects, ())

    def test_legal_aggregate_receipts_cover_full_request_domain(self):
        maximum = 10**18 - 1
        for principal, fee in ((1, maximum-1), (1, maximum), (maximum, maximum)):
            k, _, clock = setup(limit=2*maximum, order_limit=maximum)
            k.trusted.register_capability(Capability('large', 'demo', 'a', frozenset({'a'}), maximum, 1000, k.epoch))
            verifier = k.trusted.send_verifier()
            s = RefundService({'a': Order('v1', 'original-a', maximum)}, fee=fee,
                              retention_seconds=120, clock=clock, verifier=verifier)
            auth = authorize(k, s, value=principal, fee=fee)
            op = prepare(k, (auth[0], 'large', *auth[2:])); op = k.dispatch('a', op.id, op.permit_ref)
            with k._lock: packet = admit_attempt(k, op)
            raw = s.send(packet)
            self.assertEqual(k.trusted.observe(raw, op.id).settlement, Settlement.FINAL)
            self.assertEqual(k.accounts()['tenant:CNY'].spent, principal+fee)
            self.assertEqual(k.accounts()['tenant:CNY'].pending, 0)
            self.assertEqual(s.real_cost, principal+fee)
            k.assert_invariants()
        self.assertCode('INVALID_AMOUNT', lambda: amount(str(10**18)))

    def test_cumulative_domain_is_bounded_canonical_and_independent_of_time(self):
        for raw in ('0','999999999999999999','1000000000000000000','9999999999999999999'):
            self.assertEqual(cumulative_cost(raw),int(raw))
        for raw in ('10000000000000000000','01','-1','+1','1.0','1e18','',True,1):
            self.assertCode('INVALID_CUMULATIVE_COST',lambda:cumulative_cost(raw))
        k,_,clock=setup();verifier=k.trusted.send_verifier()
        s=RefundService({'a':Order('v1','original-a',100)},retention_seconds=120,clock=clock,verifier=verifier)
        op=prepare(k,authorize(k,s));op=k.dispatch('a',op.id,op.permit_ref)
        with k._lock:packet=admit_attempt(k,op)
        body=strict_loads(s.send(packet));body.pop('signature')
        # A correctly authenticated out-of-domain value still cannot decode.
        body['cumulative_costs']['tenant:CNY']=str(10**19)
        signature=hmac.new(verifier._receipt_signer._key,MAC_DOMAIN+canonical_bytes(body),hashlib.sha256).hexdigest()
        self.assertCode('INVALID_CUMULATIVE_COST',lambda:k.trusted.observe(canonical_bytes({**body,'signature':signature}).decode(),op.id))
        self.assertEqual(k.accounts()['tenant:CNY'].pending,70)
        self.assertTrue(k._quarantined)
        self.assertCode('INVALID_AMOUNT',lambda:amount(str(10**18)))

    def test_memory_replay_uses_history_but_preserves_conflict_checks(self):
        k, s, _ = setup(); flow = make([P, D, S, Q]); refs = context(k, s)
        executor = WorkflowExecutor(k, s)
        first = executor.run(flow, 'a', 'history', refs, {'flag': True})
        k.trusted.activate_policy(seal(k, amended(default_contract())))
        self.assertEqual(executor.run(flow, 'a', 'history', refs, {'flag': True}), first)
        self.assertCode('WORKFLOW_RUN_CONFLICT', lambda: executor.run(flow, 'a', 'history', refs, {'flag': False}))
        self.assertCode('WORKFLOW_RUN_CONFLICT', lambda: executor.run(flow, 'a', 'history', {**refs, 'c': 'other'}, {'flag': True}))
        self.assertCode('WORKFLOW_RUN_CONFLICT', lambda: executor.run(flow, 'a', 'history', refs, {'flag': True},
                        FaultPlan(drop_receipt_at=frozenset({'w0.2'}))))
        self.assertCode('WORKFLOW_RUN_CONFLICT', lambda: executor.run(make([P, D, S]), 'a', 'history', refs, {'flag': True}))
        self.assertCode('WORKFLOW_CONTRACT_MISMATCH', lambda: executor.run(flow, 'a', 'new-run', refs, {'flag': True}))
        self.assertEqual((s.real_cost, len(s.effects)), (70, 1))

    def test_missing_history_fails_explicitly_and_new_run_uses_current_contract(self):
        k,s,_=setup();refs=context(k,s);old=make([]);ex=WorkflowExecutor(k,s)
        ex.run(old,'a','old',refs,{'flag':True})
        new=amended(default_contract());k.trusted.activate_policy(seal(k,new))
        self.assertEqual(ex.run(make([],contract=new),'a','new',refs,{'flag':True}).status,'Finished')
        k._policy_snapshots.pop(old.contract_digest)
        self.assertCode('WORKFLOW_HISTORY_UNAVAILABLE',lambda:ex.run(old,'a','old',refs,{'flag':True}))
        self.assertEqual(s.effects,())
