"""Structured observation evidence across real commits, recovery and legacy logs."""
from dataclasses import replace
import unittest
import test_postgres
from avcl.audit_export import verify_bundle
from avcl.observation_fact import decode_fact, fact_digest, encode_fact
from avcl.storage_codec import dumps
from avcl.types import Execution, Settlement
from tests.support import authorize, prepare


class DurableObservationAuditTests(unittest.TestCase):
    setUpClass=classmethod(test_postgres.PersistenceTests.setUpClass.__func__)
    setUp=test_postgres.PersistenceTests.setUp
    tearDown=test_postgres.PersistenceTests.tearDown
    fresh=test_postgres.PersistenceTests.fresh
    query=test_postgres.PersistenceTests.query
    crash=test_postgres.PersistenceTests.crash

    def dispatched(self):
        op=prepare(self.k,authorize(self.k,self.s))
        return self.k.dispatch("a",op.id,op.permit_ref)

    def test_raw_receipt_crash_recovery_records_exactly_one_structured_fact(self):
        op=self.dispatched();receipt=self.s.send(op)
        self.crash("observe",(receipt,),"observe.before_commit")
        self.assertFalse(any(e.kind=="ObservationRecorded" for e in self.fresh().events()))
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,0)
        self.assertEqual(len(self.fresh().recover_observations()),1)
        self.assertEqual(self.fresh().recover_observations(),[])
        entries=[e for e in self.k.events() if e.kind=="ObservationRecorded"]
        self.assertEqual(len(entries),1)
        self.assertEqual(dict(entries[0].details)["fact"],encode_fact(receipt))
        self.assertEqual(self.fresh().get("a",op.id).settlement,Settlement.FINAL)
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,70)
        report=verify_bundle(self.fresh().trusted.export_audit(),expected_tenant="demo",check_payloads=True)
        self.assertEqual(report["payload_schema"]["structured_observation_events"],"1")

    def test_conflicting_id_is_durable_without_replacing_original_or_recharging(self):
        op=self.dispatched();receipt=self.s.send(op);self.k.trusted.observe(receipt)
        conflict=replace(receipt,request_digest="wrong-binding",kind=Execution.PREPARED,
                         cumulative_costs=(("tenant:CNY",71),("tenant:CNY",72)))
        self.k.trusted.observe(conflict)
        fresh=self.fresh();entry=next(e for e in fresh.events() if e.kind=="ConflictingObservation")
        details=dict(entry.details)
        self.assertEqual(details["fact"],encode_fact(conflict))
        self.assertEqual(details["prior_fact_digest"],fact_digest(encode_fact(receipt)))
        self.assertEqual(fresh.get("a",op.id).settlement,Settlement.DISPUTED)
        self.assertEqual(fresh.accounts()["tenant:CNY"].spent,70)
        report=verify_bundle(fresh.trusted.export_audit(),expected_tenant="demo",check_payloads=True)
        self.assertEqual(report["payload_schema"]["structured_observation_events"],"2")
        # The stored accepted observation remains the idempotency authority.
        before=fresh.events();fresh.trusted.observe(receipt)
        self.assertEqual(fresh.events(),before)

    def test_legacy_payload_bytes_survive_new_structured_conflict_and_late_fact(self):
        op=self.dispatched();receipt=self.s.send(op);self.k.trusted.observe(receipt)
        recorded=next(e for e in self.k.events() if e.kind=="ObservationRecorded")
        legacy=replace(recorded,details=(("content",repr(receipt)),("observation",receipt.id)))
        raw=dumps(legacy)
        # Reconstruct only this test's old storage shape; never edit real history.
        self.query("ALTER TABLE audit_event DISABLE TRIGGER USER")
        try:self.query("UPDATE audit_event SET payload=%s WHERE tenant_id='demo' AND sequence=%s",(raw,recorded.sequence))
        finally:self.query("ALTER TABLE audit_event ENABLE TRIGGER USER")
        self.fresh().trusted.observe(replace(receipt,id="late",final_cost=False))
        self.fresh().trusted.observe(replace(receipt,cumulative_costs=(("tenant:CNY",71),("order:a:CNY",70))))
        page=self.fresh().trusted.export_audit()
        self.assertEqual(page["records"][recorded.sequence-1]["payload"],raw)
        self.assertEqual(self.query("SELECT payload FROM audit_event WHERE tenant_id='demo' AND sequence=%s",(recorded.sequence,))[0]["payload"],raw)
        report=verify_bundle(page,expected_tenant="demo",check_payloads=True)
        self.assertEqual(report["payload_schema"]["opaque_observation_events"],"1")
        self.assertEqual(report["payload_schema"]["structured_observation_events"],"2")
        conflict=next(e for e in self.k.events() if e.kind=="ConflictingObservation")
        self.assertEqual(dict(conflict.details)["prior_fact_digest"],fact_digest(encode_fact(receipt)))
        self.assertEqual(decode_fact(dict(conflict.details)["fact"])["observation_id"],receipt.id)


if __name__=="__main__":unittest.main()
