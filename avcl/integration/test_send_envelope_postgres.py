import unittest
from unittest.mock import patch
import test_postgres

from avcl.canonical import canonical_bytes,strict_loads
from avcl.send_envelope import envelope_digest
from avcl.simulator import RefundService,Order
from avcl.types import Execution,Settlement
from avcl.workflow_postgres import DurableWorkflowExecutor
from tests.support import authorize,prepare
from tests.test_workflow import make,context,P,D,S,Q


class DurableEnvelopeTests(unittest.TestCase):
    setUpClass=classmethod(test_postgres.PersistenceTests.setUpClass.__func__)
    setUp=test_postgres.PersistenceTests.setUp
    tearDown=test_postgres.PersistenceTests.tearDown
    fresh=test_postgres.PersistenceTests.fresh
    error=test_postgres.PersistenceTests.error
    query=test_postgres.PersistenceTests.query
    crash=test_postgres.PersistenceTests.crash

    def service(self):
        return RefundService({"a":Order("v1","original-a",100)},retention_seconds=60,
                             clock=self.clock,verifier=self.k.trusted.send_verifier())

    def dispatch(self,service):
        op=prepare(self.k,authorize(self.k,service,value=20))
        return self.k.dispatch("a",op.id,op.permit_ref)

    def test_claim_packet_and_receiver_key_survive_database_reopen(self):
        service=self.service();op=self.dispatch(service)
        claim=self.fresh().claim_outbox("sender")
        raw=claim["envelope"]
        self.assertEqual(self.fresh().trusted.send_verifier().verify(raw,0).id,op.id)
        receipt=service.send(raw)
        self.fresh().trusted.observe(receipt)
        self.assertEqual(self.k.get("a",op.id).settlement,Settlement.FINAL)
        event=next(e for e in self.k.events() if e.kind=="SendAttemptAdmitted")
        self.assertEqual(dict(event.details)["envelope_digest"],envelope_digest(raw))
        self.assertIsNone(self.fresh().claim_outbox("again"))

    def test_new_claim_after_reopen_uses_original_receiver_key(self):
        service=self.service();op=self.dispatch(service)
        claim=self.fresh().claim_outbox("reopened-sender")
        self.assertEqual(strict_loads(service.send(claim["envelope"]))["operation_id"],op.id)
        self.assertEqual(service.real_cost,20)

    def test_claim_process_death_records_authorization_without_reissuing(self):
        service=self.service();op=self.dispatch(service)
        self.crash("claim_outbox",("worker",2),"claim.after_commit")
        event=next(e for e in self.fresh().events() if e.kind=="SendAttemptAdmitted")
        self.assertTrue(dict(event.details)["envelope_digest"].startswith("sha256:"))
        self.clock.now=3
        self.fresh().reap_expired_leases()
        self.assertIsNone(self.fresh().claim_outbox("replacement"))
        self.assertEqual(self.k.get("a",op.id).state,Execution.UNKNOWN)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,20)
        self.assertEqual(len(self.query("SELECT * FROM send_attempt")),1)

    def test_claim_before_commit_death_does_not_publish_an_attempt(self):
        service=self.service();self.dispatch(service)
        self.crash("claim_outbox",("worker",2),"claim.before_commit")
        self.assertEqual(self.query("SELECT * FROM send_attempt"),[])
        self.assertFalse(any(e.kind=="SendAttemptAdmitted" for e in self.k.events()))
        raw=self.fresh().claim_outbox("recovery")["envelope"]
        self.assertEqual(strict_loads(raw)["attempt"],"1")
        self.assertEqual(strict_loads(service.send(raw))["cumulative_costs"]["tenant:CNY"],"20")

    def test_strict_workflow_passes_authenticated_json_to_receiver(self):
        service=self.service();refs=context(self.k,service)
        with patch.object(service,"send",wraps=service.send) as send:
            result=DurableWorkflowExecutor(self.k,service).run(make([P,D,S,Q]),"a","signed",refs,{"flag":True})
        self.assertEqual(result.status,"Finished")
        self.assertIs(type(send.call_args.args[0]),str)
        self.assertEqual((len(service.effects),self.k.accounts()["tenant:CNY"].spent),(1,70))

    def test_transport_tampering_halts_without_effect_or_budget_release(self):
        service=self.service();refs=context(self.k,service);original=service.send
        def tampered(raw):
            obj=strict_loads(raw);obj["request"]["destination"]="attacker"
            return original(canonical_bytes(obj).decode())
        with patch.object(service,"send",side_effect=tampered):
            result=DurableWorkflowExecutor(self.k,service).run(make([P,D,S,Q]),"a","tampered",refs,{"flag":True})
        self.assertEqual((result.status,result.error),("Halted","INVALID_SEND_AUTHENTICATION"))
        self.assertEqual(service.effects,())
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        self.assertTrue(self.k.status()["quarantined"])

    def test_expired_authenticated_packet_and_modified_deadline_are_rejected(self):
        service=self.service();op=self.dispatch(service)
        raw=self.k.claim_outbox("sender")["envelope"]
        receipt=service.send(raw)
        self.clock.now=60
        self.assertIsNone(service.query(op.id))
        self.error("REMOTE_ACCEPTANCE_CLOSED",lambda:service.send(raw))
        changed={**strict_loads(raw),"not_after":"120"}
        self.error("INVALID_SEND_AUTHENTICATION",lambda:service.send(canonical_bytes(changed).decode()))
        self.fresh().reap_expired_leases()
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,20)
        self.fresh().trusted.observe(receipt)
        self.assertEqual((service.real_cost,self.k.accounts()["tenant:CNY"].spent),(20,20))
