"""Independent remote costs, including fees that arise after partial observation."""
import unittest

from avcl.canonical import Reject
from avcl.types import Execution, Observation, Settlement
from models.cost_bridge_model import MODES, explore, successors
from .support import authorize, prepare, setup


class RemoteMeter:
    """Test adapter oracle. Never reads kernel accounting or local result state."""
    def __init__(self, operation):
        self.operation_id = operation.id
        self.request_digest = operation.request.request_digest
        self.principal = 0
        self.fee = 0
        self.closed = False
        self.effects = 0
        self.sequence = 0

    @property
    def actual(self):
        return self.principal + self.fee

    def apply_principal(self):
        if not self.closed and self.principal == 0:
            self.principal = 2
            self.effects += 1

    def accrue_fee(self, amount=1):
        if self.closed:
            raise ValueError("remote cost sources are closed")
        self.fee += amount

    def receipt(self):
        self.sequence += 1
        if not self.principal and not self.closed:
            raise ValueError("no authoritative business result yet")
        return Observation(f"meter:{self.sequence}", self.operation_id, self.request_digest,
            Execution.CONFIRMED if self.principal else Execution.REJECTED,
            (("tenant:CNY", self.actual), ("order:a:CNY", self.principal)), self.closed, True)


class CostBridgeTests(unittest.TestCase):
    def admitted(self):
        k, service, _ = setup(limit=3, order_limit=2)
        op = prepare(k, authorize(k, service, value=2, fee=1))
        op = k.dispatch("a", op.id, op.permit_ref)
        return k, op, RemoteMeter(op)

    def assertCovered(self, k, meter):
        for name, actual in (("tenant:CNY", meter.actual), ("order:a:CNY", meter.principal)):
            account = k.accounts()[name]
            self.assertLessEqual(actual, account.spent+account.pending)
            self.assertLessEqual(account.spent+account.pending, account.limit)
        k.assert_invariants()

    def test_lost_receipt_partial_then_new_fee_and_final(self):
        k, op, meter = self.admitted()
        self.assertCovered(k, meter)
        meter.apply_principal()
        meter.receipt()  # Deliberately dropped, after the actual effect.
        self.assertCovered(k, meter)
        k.trusted.unknown(op.id)
        meter.apply_principal()  # Same logical operation, no second effect.
        self.assertEqual(meter.effects, 1)
        self.assertCovered(k, meter)
        partial = meter.receipt()
        result = k.trusted.observe(partial)
        self.assertEqual(result.settlement, Settlement.PARTIAL)
        self.assertEqual(k.accounts()["tenant:CNY"].pending, 1)
        self.assertCovered(k, meter)
        meter.accrue_fee()  # New cost AFTER the partial observation.
        self.assertCovered(k, meter)
        k.trusted.observe(partial)  # An identical stale replay cannot charge twice.
        self.assertEqual(k.accounts()["tenant:CNY"].spent, 2)
        self.assertCovered(k, meter)
        meter.closed = True
        final = meter.receipt()
        self.assertEqual(k.trusted.observe(final).settlement, Settlement.FINAL)
        k.trusted.observe(final)
        self.assertEqual((k.accounts()["tenant:CNY"].spent, k.accounts()["tenant:CNY"].pending), (3, 0))
        self.assertCovered(k, meter)

    def test_fee_only_rejection_releases_only_after_remote_closure(self):
        k, op, meter = self.admitted()
        meter.accrue_fee()
        k.trusted.unknown(op.id)
        self.assertCovered(k, meter)
        meter.closed = True
        result = k.trusted.observe(meter.receipt())
        self.assertEqual((result.state, result.settlement), (Execution.REJECTED, Settlement.FINAL))
        self.assertEqual((meter.effects, k.accounts()["tenant:CNY"].spent), (0, 1))
        self.assertEqual(k.accounts()["order:a:CNY"].pending, 0)
        self.assertCovered(k, meter)

    def test_adapter_bound_violation_is_not_claimed_safe_and_cost_is_recorded(self):
        k, op, meter = self.admitted()
        meter.apply_principal()
        k.trusted.observe(meter.receipt())
        meter.accrue_fee(2)  # Adapter violates its declared fee upper bound of one.
        account = k.accounts()["tenant:CNY"]
        self.assertGreater(meter.actual, account.spent+account.pending)
        meter.closed = True
        result = k.trusted.observe(meter.receipt())
        self.assertEqual(result.settlement, Settlement.DISPUTED)
        self.assertEqual(k.accounts()["tenant:CNY"].spent, meter.actual)
        self.assertTrue(k.accounts()["tenant:CNY"].breached)
        _, service, _ = setup(order_limit=2)
        fresh = authorize(k, service, "b", 1)
        with self.assertRaises(Reject) as caught:
            prepare(k, fresh)
        self.assertEqual(caught.exception.code, "ASSUMPTION_BROKEN")

    def test_sound_model_has_nonempty_effect_fee_and_rejection_paths(self):
        report, paths = explore()
        self.assertTrue(report["exploration_complete"])
        self.assertEqual(report["violations"], {})
        self.assertTrue(any(s.phase == "Final" and s.principal == 2 and s.fee == 1 for s in paths))
        self.assertTrue(any(s.phase == "Final" and s.principal == 0 and s.fee == 1 for s in paths))
        # Once the remote sources close, no sound transition can add real cost.
        for s in paths:
            if s.closed:
                for _, nxt in successors(s):
                    self.assertEqual(nxt.actual, s.actual)

    def test_all_four_unsound_protocols_have_replayable_counterexamples(self):
        for mode in MODES[1:]:
            with self.subTest(mode=mode):
                report, paths = explore(mode)
                counterexample = report["violations"]["actual_exceeds_exposure"]
                state = next(iter(paths))
                for action in counterexample["trace"]:
                    state = dict(successors(state, mode))[action]
                self.assertGreater(state.actual, state.exposure)
                self.assertEqual(counterexample["actual"], state.actual)
