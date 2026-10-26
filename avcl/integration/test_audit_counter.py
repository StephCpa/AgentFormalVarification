"""Actual database allocation, rollback, migration and corruption boundaries."""
from dataclasses import replace
import unittest
import psycopg
from psycopg import sql
import test_postgres
from avcl import audit
from avcl.canonical import Reject,digest
from avcl.rejection_audit import AuthenticatedContext
from avcl.storage_codec import dumps
from avcl.types import Event
from tests.support import authorize,prepare


class AuditCounterTests(unittest.TestCase):
    setUpClass=classmethod(test_postgres.PersistenceTests.setUpClass.__func__)
    setUp=test_postgres.PersistenceTests.setUp
    tearDown=test_postgres.PersistenceTests.tearDown
    fresh=test_postgres.PersistenceTests.fresh
    query=test_postgres.PersistenceTests.query
    crash=test_postgres.PersistenceTests.crash
    error=test_postgres.PersistenceTests.error

    def counter(self):return self.query("SELECT last_sequence FROM audit_counter WHERE tenant_id='demo'")[0]["last_sequence"]

    def downgrade(self):
        self.query("DROP TRIGGER audit_requires_allocation ON audit_event")
        self.query("DROP FUNCTION require_audit_allocation()")
        self.query("DROP TABLE audit_counter")
        self.query("UPDATE schema_version SET version=9")
        self.query("ALTER TABLE schema_version DROP CONSTRAINT schema_version_version_check")
        self.query("ALTER TABLE schema_version ADD CHECK(version BETWEEN 1 AND 9)")

    def test_allocation_precedes_flush_and_repeat_flush_is_idempotent(self):
        before=self.counter();events=self.k.events()
        def generate(k,c):
            for i in range(3):k._event("CounterProbe",index=i)
            self.assertEqual(c.execute("SELECT last_sequence FROM audit_counter WHERE tenant_id='demo'").fetchone()["last_sequence"],before+3)
            self.assertEqual(c.execute("SELECT count(*) AS n FROM audit_event WHERE tenant_id='demo'").fetchone()["n"],len(events))
            self.k._save(c,k)
        self.k._transaction("counter-probe",generate)
        self.assertEqual(self.counter(),before+3)
        self.assertEqual(self.fresh().events()[:len(events)],events)
        self.assertEqual([e.sequence for e in self.k.events()],list(range(1,self.counter()+1)))

    def test_savepoint_reuses_discarded_slots_for_one_durable_rejection(self):
        before=self.counter();events=self.k.events()
        context=AuthenticatedContext("a","test",digest({"case":"rollback"}),"trace-counter")
        def fail(k,c):
            for i in range(3):k._event("DiscardedProbe",index=i)
            self.k._save(c,k)
            raise Reject("BUDGET_EXCEEDED")
        self.error("BUDGET_EXCEEDED",lambda:self.k._transaction("counter-fail",fail,authenticate=lambda k,c:context))
        self.assertEqual(self.counter(),before+1)
        self.assertEqual(self.k.events()[:-1],events)
        self.assertEqual((self.k.events()[-1].sequence,self.k.events()[-1].kind),(before+1,"ActionRejected"))

    def test_prepare_crash_and_retry_keep_counter_and_events_atomic(self):
        auth=authorize(self.k,self.s);before=self.counter();events=self.k.events()
        args=("a",*auth,"counter-crash")
        self.crash("prepare",args,"prepare.before_commit")
        self.assertEqual((self.counter(),self.k.events()),(before,events))
        self.crash("prepare",args,"prepare.after_commit")
        after=self.k.events();self.assertGreater(self.counter(),before)
        self.assertEqual(self.counter(),len(after))
        prepare(self.fresh(),auth,"counter-crash")
        self.assertEqual((self.counter(),self.k.events()),(len(after),after))

    def test_mutated_history_or_unallocated_append_is_not_silently_ignored(self):
        self.k._transaction("seed",lambda k,c:k._event("Original"))
        before=self.k.events();counter=self.counter()
        def rewrite(k,c):k._events[0]=replace(k._events[0],kind="Rewrite")
        def remove(k,c):k._events.pop()
        def forge(k,c):k._events.append(Event(len(k._events)+1,"NoAllocation",None,()))
        for code,fn in (("AUDIT_HISTORY_MUTATION",rewrite),("AUDIT_HISTORY_MUTATION",remove),("CORRUPT_AUDIT_COUNTER",forge)):
            self.error(code,lambda:self.k._transaction("invalid",fn))
            self.assertEqual((self.counter(),self.k.events()),(counter,before))

    def test_counter_tampering_or_loss_is_detected_without_automatic_repair(self):
        before=self.counter()
        self.query("UPDATE audit_counter SET last_sequence=last_sequence+1 WHERE tenant_id='demo'")
        self.error("CORRUPT_AUDIT_COUNTER",self.k.events)
        self.assertEqual(self.counter(),before+1)
        self.query("DELETE FROM audit_counter WHERE tenant_id='demo'")
        self.error("CORRUPT_AUDIT_COUNTER",self.k.events)
        self.assertEqual(self.query("SELECT * FROM audit_counter"),[])

    def test_payload_sequence_projection_is_checked_even_when_row_count_matches(self):
        self.k._transaction("seed",lambda k,c:k._event("Original"))
        event=self.k.events()[0]
        self.query("ALTER TABLE audit_event DISABLE TRIGGER USER")
        self.query("UPDATE audit_event SET payload=%s WHERE tenant_id='demo' AND sequence=%s",(dumps(replace(event,sequence=True)),event.sequence))
        self.error("CORRUPT_AUDIT_SEQUENCE",self.k.events)

    def test_v9_upgrade_preserves_bytes_and_next_sequence(self):
        self.k._transaction("seed",lambda k,c:k._event("Legacy"))
        before=self.query("SELECT * FROM audit_event ORDER BY tenant_id,sequence")
        self.downgrade();self.k.migrate();self.k.migrate()
        self.assertEqual(self.query("SELECT * FROM audit_event ORDER BY tenant_id,sequence"),before)
        self.assertEqual(self.counter(),len(before))
        self.k._transaction("next",lambda k,c:k._event("AfterUpgrade"))
        self.assertEqual((self.counter(),self.k.events()[-1].sequence),(len(before)+1,len(before)+1))

    def test_v9_upgrade_refuses_holes_atomically(self):
        self.k._transaction("seed",lambda k,c:[k._event("Legacy") for _ in range(3)])
        self.downgrade()
        self.query("ALTER TABLE audit_event DISABLE TRIGGER USER")
        self.query("DELETE FROM audit_event WHERE tenant_id='demo' AND sequence=1")
        before=self.query("SELECT * FROM audit_event ORDER BY sequence")
        with self.assertRaises(psycopg.errors.RaiseException):self.k.migrate()
        self.assertEqual(self.query("SELECT version FROM schema_version"),[{"version":9}])
        self.assertIsNone(self.query("SELECT to_regclass('audit_counter') AS name")[0]["name"])
        self.assertEqual(self.query("SELECT * FROM audit_event ORDER BY sequence"),before)

    def test_v9_upgrade_refuses_payload_projection_mismatch(self):
        self.k._transaction("seed",lambda k,c:k._event("Legacy"))
        event=self.k.events()[0];self.downgrade()
        self.query("ALTER TABLE audit_event DISABLE TRIGGER USER")
        self.query("UPDATE audit_event SET payload=%s WHERE tenant_id='demo' AND sequence=%s",(dumps(replace(event,sequence=True)),event.sequence))
        with self.assertRaises(psycopg.errors.RaiseException):self.k.migrate()
        self.assertEqual(self.query("SELECT version FROM schema_version"),[{"version":9}])

    def test_allocator_exhaustion_and_sql_failure_do_not_leave_holes(self):
        before=self.counter()
        with self.k._connect() as c:
            c.execute(sql.SQL("SET LOCAL search_path={},pg_catalog").format(sql.Identifier(self.schema)))
            c.execute("UPDATE audit_counter SET last_sequence=%s WHERE tenant_id='demo'",(audit.MAX_SEQUENCE,))
            self.error("AUDIT_SEQUENCE_UNAVAILABLE",lambda:audit.allocate(c,"demo"))
            c.rollback()
        self.assertEqual(self.counter(),before)
        self.query("CREATE FUNCTION fail_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected'; END; $$")
        self.query("CREATE TRIGGER fail_audit BEFORE INSERT ON audit_event FOR EACH ROW EXECUTE FUNCTION fail_audit()")
        with self.assertRaises(psycopg.errors.RaiseException):self.k._transaction("failed-insert",lambda k,c:k._event("Probe"))
        self.assertEqual(self.counter(),before)
        self.assertEqual(len(self.k.events()),before)

    def test_new_tenant_starts_its_own_counter(self):
        before=self.counter();other=self.fresh(tenant="second");other.initialize()
        self.assertEqual(other.events(),())
        other._transaction("seed",lambda k,c:k._event("First"))
        self.assertEqual(other.events()[0].sequence,1)
        self.assertEqual(self.counter(),before)

    def test_legacy_writer_cannot_insert_an_unallocated_event(self):
        before=self.counter();payload=dumps(Event(before+1,"LegacyWrite",None,()))
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.query("INSERT INTO audit_event VALUES('demo',%s,%s)",(before+1,payload))
        self.assertEqual(self.counter(),before)
        self.assertEqual(len(self.k.events()),before)

    def test_readonly_callback_cannot_commit_allocations_without_events(self):
        before=self.counter();events=self.k.events()
        self.error("AUDIT_HISTORY_MUTATION",lambda:self.k._transaction("read-probe",lambda k,c:k._event("Unexpected"),False))
        self.error("CORRUPT_AUDIT_COUNTER",lambda:self.k._transaction("read-probe",lambda k,c:audit.allocate(c,k.tenant),False))
        self.assertEqual((self.counter(),self.k.events()),(before,events))

    def test_corrupt_counter_is_http_unavailable_without_new_event(self):
        from avcl.gateway import GatewayAuthority,RuntimeGateway
        token=GatewayAuthority(self.k).issue("a",frozenset({"get"}),900).token
        before=self.counter()
        self.query("UPDATE audit_counter SET last_sequence=last_sequence+1 WHERE tenant_id='demo'")
        status,response=RuntimeGateway(self.dsn,self.schema,self.clock).handle("GET","/v1/operations/op-"+"0"*32,"Bearer "+token)
        self.assertEqual((status,response["error"]["code"]),(503,"CORRUPT_AUDIT_COUNTER"))
        self.assertEqual(self.query("SELECT count(*) AS n FROM audit_event WHERE tenant_id='demo'"),[{"n":before}])


if __name__=="__main__":unittest.main()
