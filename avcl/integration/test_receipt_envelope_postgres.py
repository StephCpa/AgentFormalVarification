from unittest.mock import patch
import unittest
import test_postgres
from avcl.canonical import canonical_bytes,strict_loads
from avcl.simulator import RefundService,Order
from avcl.supervision import PostgresSupervisor
from avcl.types import Observation,Execution,Settlement
from avcl.workflow_postgres import DurableWorkflowExecutor
from tests.support import authorize,prepare
from tests.test_workflow import make,context,P,D,S,Q


class DurableReceiptEnvelopeTests(unittest.TestCase):
    setUpClass=classmethod(test_postgres.PersistenceTests.setUpClass.__func__)
    setUp=test_postgres.PersistenceTests.setUp
    tearDown=test_postgres.PersistenceTests.tearDown
    fresh=test_postgres.PersistenceTests.fresh
    error=test_postgres.PersistenceTests.error
    query=test_postgres.PersistenceTests.query
    crash=test_postgres.PersistenceTests.crash

    def service(self):
        return RefundService({"a":Order("v1","original-a",100)},retention_seconds=120,
                             clock=self.clock,verifier=self.k.trusted.send_verifier())

    def sent(self):
        service=self.service();op=prepare(self.k,authorize(self.k,service,value=20))
        op=self.k.dispatch("a",op.id,op.permit_ref)
        job=self.k.claim_outbox("sender",authenticated_receipts=True)
        return service,op,service.send(job["envelope"])

    def unsigned(self,op):
        return Observation("unsigned",op.id,op.request.request_digest,Execution.CONFIRMED,
                           (("tenant:CNY",20),("order:a:CNY",20)),True,True)

    def test_authenticated_raw_receipt_commit_crash_recovers_once(self):
        service,op,raw=self.sent()
        self.crash("observe",(raw,op.id),"observe.after_raw_commit")
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,0)
        self.assertEqual(len(self.fresh().recover_observations()),1)
        self.assertEqual(self.fresh().recover_observations(),[])
        self.assertEqual((service.real_cost,self.k.accounts()["tenant:CNY"].spent),(20,20))

    def test_expected_operation_survives_raw_commit_crash(self):
        _,op,raw=self.sent()
        self.crash("observe",(raw,"op-"+"1"*32),"observe.after_raw_commit")
        result=self.fresh().recover_observations()
        self.assertEqual(result,[{"status":"Rejected","code":"UNEXPECTED_ADAPTER_OBSERVATION"}])
        self.assertEqual((self.k.accounts()["tenant:CNY"].spent,self.k.accounts()["tenant:CNY"].pending),(0,20))
        self.assertTrue(self.k.status()["quarantined"])
        self.assertEqual(self.fresh().recover_observations(),[])

    def test_bad_mac_retained_without_starving_later_valid_evidence(self):
        _,op,raw=self.sent()
        bad={**strict_loads(raw),"final_cost":False}
        self.error("INVALID_RECEIPT_AUTHENTICATION",lambda:self.fresh().trusted.observe(canonical_bytes(bad).decode(),op.id))
        self.assertEqual(self.query("SELECT applied,error_code FROM observation_application")[0],
                         {"applied":False,"error_code":"INVALID_RECEIPT_AUTHENTICATION"})
        self.assertEqual(self.fresh().recover_observations(),[])
        self.assertEqual(self.fresh().trusted.observe(raw,op.id).settlement,Settlement.FINAL)
        self.assertEqual(len(self.query("SELECT * FROM observation_receipt")),2)
        self.assertTrue(self.k.status()["quarantined"])

    def test_receipt_requirement_survives_reopen_and_blocks_unsigned(self):
        _,op,_=self.sent()
        self.error("AUTHENTICATED_RECEIPT_REQUIRED",lambda:self.fresh().trusted.observe(self.unsigned(op),op.id))
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,20)

    def test_workflow_mode_survives_prepare_crash_without_service_object(self):
        service=self.service();executor=DurableWorkflowExecutor(self.k,service)
        executor.start(make([P,D,S,Q]),"a","auth",context(self.k,service),{"flag":True})
        self.crash("workflow.resume",("a","auth"),"workflow-node:w0.0.after_commit")
        remote=DurableWorkflowExecutor(self.fresh())
        remote.advance("a","auth","worker")
        io=remote.advance("a","auth","worker")
        op=io["operation"]
        result=remote.finish_io("a","auth",io["node_id"],"worker",io["generation"],self.unsigned(op))
        self.assertEqual(result["status"],"Halted")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        self.assertEqual(self.k.get("a",op.id).state,Execution.UNKNOWN)

    def test_reopened_workflow_cannot_switch_to_unauthenticated_service(self):
        service=self.service();executor=DurableWorkflowExecutor(self.k,service)
        executor.start(make([P,D,S,Q]),"a","auth",context(self.k,service),{"flag":True})
        self.error("WORKFLOW_ADAPTER_MODE_CHANGED",lambda:DurableWorkflowExecutor(self.fresh(),self.s).resume("a","auth"))
        self.assertEqual(service.effects,())

    def test_supervisor_accepts_signed_query_after_send_deadline(self):
        service,op,_=self.sent();self.k.trusted.unknown(op.id)
        self.clock.now=70
        result=PostgresSupervisor(self.fresh()).run_once("reconciler",service)
        self.assertEqual(result["status"],"Reconciled")
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,20)

    def test_supervisor_rejects_forged_query_and_keeps_obligation(self):
        service,op,raw=self.sent();self.k.trusted.unknown(op.id)
        bad={**strict_loads(raw),"kind":"RejectedNoEffect"}
        with patch.object(service,"query",return_value=canonical_bytes(bad).decode()):
            result=PostgresSupervisor(self.fresh()).run_once("reconciler",service)
        self.assertEqual(result["status"],"RejectedReceipt")
        self.assertEqual(result["reason"],"INVALID_RECEIPT_AUTHENTICATION")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,20)
        self.assertIsNone(self.k.status()["obligations"][0]["resolution"])

    def test_authentic_over_bound_receipt_still_records_incident(self):
        service,op,_=self.sent()
        observation=Observation("over-bound",op.id,op.request.request_digest,Execution.CONFIRMED,
                                (("tenant:CNY",30),("order:a:CNY",20)),True,True)
        raw=service._verifier._receipt_signer.seal(observation,op.binding,0)
        result=self.fresh().trusted.observe(raw,op.id)
        self.assertEqual(result.settlement,Settlement.DISPUTED)
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,30)

    def test_legacy_observation_reauthentication_preserves_frozen_record(self):
        op=prepare(self.k,authorize(self.k,self.s,value=20));op=self.k.dispatch("a",op.id,op.permit_ref)
        observation=self.s.send(op);self.k.trusted.observe(observation)
        original=self.query("SELECT payload FROM observation WHERE id=%s",(observation.id,))[0]["payload"]
        raw=self.k.trusted.send_verifier()._receipt_signer.seal(observation,op.binding,0)
        self.assertEqual(self.fresh().trusted.observe(raw,op.id).settlement,Settlement.FINAL)
        self.assertEqual(self.query("SELECT payload FROM observation WHERE id=%s",(observation.id,))[0]["payload"],original)
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,20)
        self.assertFalse(self.k.status()["quarantined"])
