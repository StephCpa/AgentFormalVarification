"""M1 authority/binding probes with independently valid replacement evidence."""
from dataclasses import replace
import unittest

from avcl.canonical import Reject
from avcl.simulator import RefundService, Order
from avcl.types import Capability, Evidence, Approval
from tests.support import setup, authorize, prepare


class M1BoundaryTests(unittest.TestCase):
    def test_T01_objects_and_unregistered_refs_cannot_create_authority(self):
        k, service, _ = setup()
        auth = authorize(k, service)
        baseline = (k.accounts(), k.events(), service.effects)
        self.acceptance_evidence = {"requirements": ["R01"], "attempts": []}
        objects = (Capability("made-cap", "demo", "a", frozenset({"a"}), 100, 1000, 1),
                   Evidence("made-evidence", auth[0].request_digest, "order-service", "RefundEligible", 1000),
                   Approval("made-approval", auth[0].request_digest, 1000))
        for slot, role in enumerate(("capability", "evidence", "approval"), 1):
            for kind, supplied in (("unknown-ref", "invented"), ("object", objects[slot - 1]),
                                   ("candidate-json", {"ref": auth[slot]})):
                changed = list(auth); changed[slot] = supplied
                with self.assertRaises(Reject) as caught:
                    prepare(k, tuple(changed), role + "-" + kind)
                self.acceptance_evidence["attempts"].append({"role": role, "input_kind": kind, "rejection": caught.exception.code})
                self.assertEqual((k.accounts(), k.events(), service.effects), baseline)
        op = prepare(k, auth)
        op = k.dispatch("a", op.id, op.permit_ref)
        k.trusted.observe(service.send(op))
        self.assertEqual(service.real_cost, 70)
        self.assertEqual(k.accounts()["tenant:CNY"].pending, 0)
        self.acceptance_evidence["positive_control"] = {"actual_cost": service.real_cost, "effects": len(service.effects)}

    def test_T02_fresh_evidence_does_not_rebind_an_old_approval(self):
        k, service, _ = setup(limit=20000, order_limit=20000)
        auth = authorize(k, service, value=5000)
        original = prepare(k, auth)
        self.acceptance_evidence = {"requirements": ["R02"], "approved_amount": 5000, "attempts": []}
        for field, value in (("amount_minor", "5001"), ("max_fee_minor", "1"),
                             ("destination", "new-destination"), ("order_version", "v2"),
                             ("task", "other-task"), ("business_key", "other-business-action")):
            changed = replace(auth[0], **{field: value})
            validator = RefundService({"a": Order(changed.order_version, changed.destination, 20000)})
            fresh = validator.validate(changed, "fresh-" + field, 1000)
            k.trusted.register_evidence(fresh)
            before = (k.accounts(), k.events(), k.get("a", original.id), service.effects)
            with self.assertRaises(Reject) as caught:
                k.prepare("a", changed, auth[1], fresh.ref, auth[3], "changed-" + field)
            self.acceptance_evidence["attempts"].append({"field": field, "fresh_evidence_matches": fresh.request_digest == changed.request_digest,
                                                        "rejection": caught.exception.code})
            self.assertEqual(caught.exception.code, "APPROVAL_MISMATCH")
            self.assertEqual((k.accounts(), k.events(), k.get("a", original.id), service.effects), before)
        self.assertEqual(k.accounts()["tenant:CNY"].pending, 5000)
        self.assertEqual(k.get("a", original.id).request, auth[0])
        op = k.dispatch("a", original.id, original.permit_ref)
        k.trusted.observe(service.send(op))
        self.assertEqual(service.real_cost, 5000)
        self.assertEqual(len(service.effects), 1)
        self.acceptance_evidence["positive_control"] = {"actual_cost": service.real_cost, "effects": len(service.effects)}
