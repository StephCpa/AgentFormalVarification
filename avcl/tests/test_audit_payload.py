import ast
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from avcl.audit_payload import CATALOG, PROFILE, validate_payload
from avcl.audit_export import build_bundle, verify_bundle, encode_bundle
from avcl.canonical import Reject, canonical_bytes
from avcl.storage_codec import dumps
from avcl.types import Event
from avcl.sending import admit_attempt, bind_workflow, workflow_send_context
from tests.support import setup, authorize, prepare

ROOT = Path(__file__).resolve().parents[1]


def event(kind, operation=None, **details):
    return Event(1, kind, operation, tuple(sorted(details.items())))


class AuditPayloadTests(unittest.TestCase):
    def reject(self, value, code="INVALID_AUDIT_PAYLOAD"):
        with self.assertRaises(Reject) as caught: validate_payload(value)
        self.assertEqual(caught.exception.code, code)

    def test_actual_refund_events_and_send_ownership_variants(self):
        for workflow in (False, True):
            for durable in (False, True):
                k, service, _ = setup()
                op = prepare(k, authorize(k, service))
                if workflow: bind_workflow(k, op, "a", "run", "sha256:" + "1"*64)
                op = k.dispatch("a", op.id, op.permit_ref)
                details = {"worker":"worker", "generation":1} if durable else {}
                if workflow:
                    with workflow_send_context(k, op, "a", "run", "sha256:" + "1"*64, "send"):
                        admit_attempt(k, op, **details)
                else: admit_attempt(k, op, **details)
                k.trusted.observe(service.send(op))
                for actual in k.events(): validate_payload(actual)
                bundle = build_bundle("demo", tuple(dumps(e) for e in k.events()))
                report = verify_bundle(bundle, expected_tenant="demo", check_payloads=True)
                self.assertEqual(report["payload_schema"], {"profile":PROFILE, "status":"Conforms", "checked_events":str(len(k.events())),
                    "structured_observation_events":"1", "opaque_observation_events":"0"})
                self.assertEqual(report["status"], "InternallyConsistent")
                self.assertEqual(report["semantic_replay"], "not_performed")

    def test_rehashed_malformed_payload_is_rejected_even_with_matching_checkpoint(self):
        k, service, _ = setup(); prepare(k, authorize(k, service))
        original = k.events()[0]
        for mutate in (lambda d: d.pop("binding"), lambda d: d.update(extra="ignored"),
                       lambda d: d.update(binding="sha256:bad"), lambda d: d.update(requirement_ids='["R09","R01"]')):
            details = dict(original.details); mutate(details)
            bad = replace(original, details=tuple(sorted(details.items())))
            bundle = build_bundle("demo", (dumps(bad),))
            self.assertEqual(verify_bundle(bundle, expected_tenant="demo", expected_checkpoint=bundle["checkpoint"])["status"], "VerifiedAgainstCheckpoint")
            with self.assertRaises(Reject):
                verify_bundle(bundle, expected_tenant="demo", expected_checkpoint=bundle["checkpoint"], check_payloads=True)

    def test_operation_scope_and_unknown_kind_are_explicit(self):
        validate_payload(event("AuthorityRevoked", epoch="1"))
        self.reject(event("AuthorityRevoked", "op", epoch="1"))
        self.reject(event("Settled", observation="obs"))
        self.reject(event("ExtensionEvent"), "UNSUPPORTED_AUDIT_EVENT_KIND")
        bundle = build_bundle("demo", (dumps(event("ExtensionEvent")),))
        self.assertNotIn("payload_schema", verify_bundle(bundle, expected_tenant="demo"))
        with self.assertRaises(Reject): verify_bundle(bundle, expected_tenant="demo", check_payloads="true")

    def test_canonical_numbers_requirements_and_evaluation_work(self):
        for value in ("01", "-1", "1.0", "1e1", "True", "9223372036854775808", "9"*5000):
            self.reject(event("AuthorityRevoked", epoch=value))
        k, service, _ = setup(); prepare(k, authorize(k, service)); original = k.events()[0]
        work = json.loads(dict(original.details)["evaluation_work"])
        bad_work = [dict(work, used=str(int(work["limit"])+1)), dict(work, exhausted=True),
                    dict(work, limit=True), dict(work, extra="x"), dict(work, model="future")]
        for value in bad_work:
            details = dict(original.details); details["evaluation_work"] = canonical_bytes(value).decode()
            self.reject(replace(original, details=tuple(sorted(details.items()))))
        for value in ('["R01","R01"]', '["R01", "R09"]', '["bad id"]', '[true]', '{"R01":true}'):
            details = dict(original.details); details["requirement_ids"] = value
            self.reject(replace(original, details=tuple(sorted(details.items()))))

    def test_sender_alternatives_cannot_mix_owners_or_partial_lease_fields(self):
        k, service, _ = setup(); op = prepare(k, authorize(k, service)); op = k.dispatch("a", op.id, op.permit_ref)
        admit_attempt(k, op); original = k.events()[-1]
        for fields in ({"send_owner":"workflow"}, {"worker":"worker"}, {"attempt":"0"}, {"attempt":"4"}, {"profile":"other"}):
            details = {**dict(original.details), **fields}
            self.reject(replace(original, details=tuple(sorted(details.items()))))
        validate_payload(event("SendBlocked", "op", reason="OUTBOX_WAIT_EXPIRED", requirement_id="R05"))
        self.reject(event("SendBlocked", "op", reason="OUTBOX_WAIT_EXPIRED", requirement_id="R05", at="0"))

    def test_rejection_shape_enforces_nullable_operation_and_no_budget_mutation_claim(self):
        details = dict(event_id="event-"+"1"*32, actor="a", action="prepare", request_digest="sha256:"+"1"*64,
                       trace_id="trace", operation_revision="null", policy_digest="sha256:"+"2"*64,
                       epoch="1", code="BUDGET_EXCEEDED", requirement_ids='["R08"]', time="0",
                       budget_deltas="{}", causal_parents="[]", evidence_refs="[]")
        validate_payload(event("ActionRejected", **details))
        validate_payload(event("ActionRejected", "op", **{**details, "operation_revision":"2"}))
        self.reject(event("ActionRejected", "op", **details))
        for fields in ({"operation_revision":"0"}, {"budget_deltas":'{"account":"-70"}'}, {"causal_parents":'["made-up"]'}, {"event_id":"event-invalid"}):
            self.reject(event("ActionRejected", **{**details, **fields}))

    def test_opaque_observation_repr_is_never_parsed_or_executed(self):
        validate_payload(event("ObservationRecorded", "op", observation="obs", content="__import__('os').remove('never-execute')"))
        # Shape conformance deliberately makes no assertion about that text.
        validate_payload(event("ConflictingObservation", "op", observation="arbitrary opaque text"))

    def test_catalog_tracks_literal_runtime_emitters(self):
        kinds = set()
        for path in (ROOT/"avcl").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_event"): continue
                arg = node.args[0]
                alternatives = (arg.body, arg.orelse) if isinstance(arg, ast.IfExp) else (arg,)
                for item in alternatives:
                    self.assertIsInstance(item, ast.Constant, f"Review dynamic event kind at {path}:{node.lineno}")
                    kinds.add(item.value)
        self.assertEqual(kinds, set(CATALOG))

    def test_cli_strict_mode_preserves_integrity_exit_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"page.json"; pin = Path(directory)/"pin.json"
            bundle = build_bundle("demo", (dumps(event("AuthorityRevoked", epoch="1")),))
            path.write_text(encode_bundle(bundle), encoding="utf-8")
            pin.write_text(json.dumps(bundle["checkpoint"]), encoding="utf-8")
            command = [sys.executable, str(ROOT/"scripts/audit_snapshot.py"), "verify", str(path), "--tenant", "demo", "--check-payloads"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
            self.assertEqual(json.loads(result.stdout)["payload_schema"]["status"], "Conforms")
            self.assertEqual(subprocess.run(command+["--expected-checkpoint", str(pin)], capture_output=True, timeout=30).returncode, 0)
            path.write_text(encode_bundle(build_bundle("demo", (dumps(event("ExtensionEvent")),))), encoding="utf-8")
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)["code"], "UNSUPPORTED_AUDIT_EVENT_KIND")


if __name__ == "__main__": unittest.main()
