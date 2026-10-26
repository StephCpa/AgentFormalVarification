import copy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch
from avcl.canonical import Reject, canonical_bytes
from avcl.observation_fact import encode_fact, decode_fact, fact_digest, decode_effective_costs
from avcl.audit_payload import validate_payload
from avcl.audit_export import build_bundle, verify_bundle
from avcl.storage_codec import dumps
from avcl.types import Event, Execution, Settlement
from tests.support import setup, authorize, prepare
from tests.test_delegation import provision, payload, authorization


class ObservationFactTests(unittest.TestCase):
    def dispatched(self, fee=0):
        k, service, _ = setup(fee=fee)
        op = prepare(k, authorize(k, service, fee=fee))
        op = k.dispatch("a", op.id, op.permit_ref)
        return k, service, op

    def recorded(self, k):
        return [e for e in k.events() if e.kind == "ObservationRecorded"]

    def test_recorded_fact_preserves_raw_order_and_separates_effective_costs(self):
        k, service, op = self.dispatched()
        receipt = service.send(op)
        receipt = replace(receipt, cumulative_costs=tuple(reversed(sorted(receipt.cumulative_costs))))
        k.trusted.observe(receipt)
        entry = self.recorded(k)[0]; details = dict(entry.details)
        self.assertNotIn("content", details)
        fact = decode_fact(details["fact"])
        self.assertEqual(fact["cumulative_costs"], [{"account":a,"cumulative_cost":str(c)} for a,c in receipt.cumulative_costs])
        self.assertEqual(fact["request_digest"], op.request.request_digest)
        self.assertEqual(decode_effective_costs(details["effective_costs"]), sorted(fact["cumulative_costs"], key=lambda r:r["account"]))
        validate_payload(entry)
        before = k.events()
        k.trusted.observe(replace(receipt, cumulative_costs=tuple(reversed(receipt.cumulative_costs))))
        self.assertEqual(k.events(), before)

    def test_conflicting_id_preserves_invalid_candidate_and_links_prior_fact(self):
        k, service, op = self.dispatched(); original = service.send(op)
        k.trusted.observe(original)
        candidate = replace(original, request_digest="wrong-binding", kind=Execution.PREPARED,
                            cumulative_costs=(("tenant:CNY",70),("tenant:CNY",71)))
        result = k.trusted.observe(candidate)
        self.assertEqual(result.settlement, Settlement.DISPUTED)
        self.assertEqual(k._observations[original.id], original)
        entry = next(e for e in k.events() if e.kind == "ConflictingObservation")
        details = dict(entry.details); fact = decode_fact(details["fact"])
        self.assertEqual(details["observation"], original.id)
        self.assertEqual(fact["kind"], "Prepared")
        self.assertEqual(fact["request_digest"], "wrong-binding")
        self.assertEqual(len(fact["cumulative_costs"]), 2)
        self.assertEqual(details["prior_fact_digest"], fact_digest(encode_fact(original)))
        validate_payload(entry)
        self.assertEqual(k.accounts()["tenant:CNY"].spent, 70)

    def test_partial_final_subsumed_and_above_bound_incident_all_retain_facts(self):
        k, service, op = self.dispatched(fee=10); final = service.send(op)
        partial = replace(final, id="partial", final_cost=False)
        k.trusted.observe(partial); k.trusted.observe(final)
        settled = k.get("a",op.id)
        k.trusted.observe(replace(partial,id="late"))
        self.assertEqual(k.get("a",op.id), settled)
        # Do not force an observed incident back into the signed receipt's
        # 19-digit domain: trusted host costs already supported larger values.
        large = 10**100
        k.trusted.observe(replace(final,id="over-bound",cumulative_costs=(("tenant:CNY",large),("order:a:CNY",70))))
        self.assertEqual(k.accounts()["tenant:CNY"].spent, large)
        self.assertEqual(k.get("a",op.id).settlement, Settlement.DISPUTED)
        entries = self.recorded(k)
        self.assertEqual(len(entries),4)
        for entry in entries: validate_payload(entry)
        raw = decode_fact(dict(entries[-1].details)["fact"])
        self.assertEqual(raw["cumulative_costs"][0]["cumulative_cost"],str(large))

    def test_delegated_costs_keep_local_quotas_out_of_remote_fact(self):
        k, service, _ = setup(); root_account = provision(k)
        child = k.delegate("parent",payload())["child_cap_ref"]
        request,cap,evidence,approval = authorization(k,service,child)
        op = k.prepare("child",request,cap,evidence,approval,"request")
        op = k.dispatch("child",op.id,op.permit_ref)
        k.trusted.observe(service.send(op))
        entry = self.recorded(k)[0]; details = dict(entry.details)
        raw = {row["account"]:row["cumulative_cost"] for row in decode_fact(details["fact"])["cumulative_costs"]}
        effective = {row["account"]:row["cumulative_cost"] for row in decode_effective_costs(details["effective_costs"])}
        self.assertEqual(set(raw), {"tenant:CNY","order:a:CNY"})
        self.assertNotIn(root_account, raw)
        self.assertEqual(effective[root_account],raw["tenant:CNY"])
        self.assertEqual(set(effective), {r.account for r in k.get("child",op.id).reservations})
        validate_payload(entry)

    def test_codec_rejects_noncanonical_unknown_or_ill_typed_values(self):
        k, service, op = self.dispatched(); base = decode_fact(encode_fact(service.send(op)))
        cases = [dict(base, extra="x"), dict(base, format="future"), dict(base, final_cost="True"),
                 dict(base, kind="UnknownExtension"), dict(base, cumulative_costs={}),dict(base, observation_id="")]
        for cost in ("01","-1","1.0","1e1",True):
            obj=copy.deepcopy(base);obj["cumulative_costs"][0]["cumulative_cost"]=cost;cases.append(obj)
        for obj in cases:
            with self.assertRaises(Reject): decode_fact(canonical_bytes(obj).decode())
        raw = canonical_bytes(base).decode()
        for bad in (json.dumps(base),raw.replace('"final_cost":true','"final_cost":true,"final_cost":true'),raw.replace('"final_cost":true','"final_cost":1')):
            with self.assertRaises(Reject): decode_fact(bad)
        for invalid in (replace(service.send(op), final_cost=1), replace(service.send(op), cumulative_costs=(("tenant:CNY",True),))):
            with self.assertRaises(Reject): encode_fact(invalid)

    def test_schema_rejects_fact_outer_mismatch_and_altered_effective_costs(self):
        k, service, op = self.dispatched(); k.trusted.observe(service.send(op))
        entry = self.recorded(k)[0]; original = dict(entry.details)
        for key,value in (("observation","other"),("prior_fact_digest","sha256:"+"1"*64)):
            details = {**original,key:value}
            with self.assertRaises(Reject):validate_payload(replace(entry,details=tuple(sorted(details.items()))))
        for mutate in (lambda rows:rows.pop(),lambda rows:rows[0].update(cumulative_cost="999"),lambda rows:rows.append(rows[0]),lambda rows:rows.reverse()):
            rows=decode_effective_costs(original["effective_costs"]);mutate(rows)
            details={**original,"effective_costs":canonical_bytes(rows).decode()}
            with self.assertRaises(Reject):validate_payload(replace(entry,details=tuple(sorted(details.items()))))
        for mutate in (lambda f:f.update(operation_id="other"),lambda f:f.update(request_digest="bad"),lambda f:f.update(kind="Prepared"),lambda f:f.update(terminal_effect=False)):
            fact=decode_fact(original["fact"]);mutate(fact)
            details={**original,"fact":canonical_bytes(fact).decode()}
            with self.assertRaises(Reject):validate_payload(replace(entry,details=tuple(sorted(details.items()))))

    def test_legacy_opaque_facts_remain_visible_in_coverage_not_reconstructed(self):
        k,service,op=self.dispatched();k.trusted.observe(service.send(op))
        entry=self.recorded(k)[0]
        legacy=Event(1,"ObservationRecorded",op.id,(("content","arbitrary legacy repr"),("observation","legacy")))
        current=replace(entry,sequence=2)
        payloads=(dumps(legacy),dumps(current));page=build_bundle("demo",payloads)
        report=verify_bundle(page,expected_tenant="demo",check_payloads=True)
        self.assertEqual(report["payload_schema"]["opaque_observation_events"],"1")
        self.assertEqual(report["payload_schema"]["structured_observation_events"],"1")
        self.assertEqual(tuple(r["payload"] for r in page["records"]),payloads)
        self.assertEqual(report["semantic_replay"],"not_performed")
        empty=build_bundle("demo",())
        self.assertEqual(verify_bundle(empty,expected_tenant="demo",check_payloads=True)["payload_schema"]["structured_observation_events"],"0")

    def test_fact_encoding_failure_precedes_reference_state_mutation(self):
        k,service,op=self.dispatched();receipt=service.send(op)
        before=(k.get("a",op.id),k.accounts(),k.events(),dict(k._observations))
        with patch("avcl.observation_fact.encode_fact",side_effect=Reject("REQUEST_TOO_LARGE")):
            with self.assertRaises(Reject):k.trusted.observe(receipt)
        self.assertEqual((k.get("a",op.id),k.accounts(),k.events(),dict(k._observations)),before)
        k.trusted.observe(receipt)
        self.assertEqual(k.get("a",op.id).settlement,Settlement.FINAL)


if __name__ == "__main__": unittest.main()
