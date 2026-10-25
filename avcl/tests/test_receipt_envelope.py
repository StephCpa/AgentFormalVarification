from dataclasses import replace
import hashlib
import hmac
import unittest
from unittest.mock import patch

from avcl.canonical import Reject,canonical_bytes,strict_loads
from avcl.receipt_envelope import MAC_DOMAIN
from avcl.sending import admit_attempt
from avcl.simulator import RefundService,Order
from avcl.types import Observation,Execution,Settlement
from avcl.workflow_runtime import WorkflowExecutor,FaultPlan
from .support import setup,authorize,prepare
from .test_workflow import make,context,P,D,S,Q


class ReceiptEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.k,_,self.clock=setup(limit=3,order_limit=2)
        self.verifier=self.k.trusted.send_verifier()
        self.s=RefundService({"a":Order("v1","original-a",2)},fee=1,retention_seconds=60,
                             clock=self.clock,verifier=self.verifier)
        op=prepare(self.k,authorize(self.k,self.s,value=2,fee=1))
        self.op=self.k.dispatch("a",op.id,op.permit_ref)
        self.k.trusted.require_authenticated_receipts(op.id)
        with self.k._lock:self.packet=admit_attempt(self.k,self.op)

    def error(self,code,fn):
        with self.assertRaises(Reject) as caught:fn()
        self.assertEqual(caught.exception.code,code)

    def authenticated(self,observation,at=0):
        return self.verifier._receipt_signer.seal(observation,self.op.binding,at)

    def observation(self,total=3,final=True):
        return Observation("receipt",self.op.id,self.op.request.request_digest,Execution.CONFIRMED,
                           (("tenant:CNY",total),("order:a:CNY",2)),final,True)

    def test_signed_send_query_and_duplicate_are_stable_and_charge_once(self):
        receipt=self.s.send(self.packet)
        self.clock.now=1
        self.assertEqual(self.s.query(self.op.id),receipt)
        self.k.trusted.observe(receipt,self.op.id)
        self.k.trusted.observe(receipt,self.op.id)
        self.assertEqual((self.k.accounts()["tenant:CNY"].spent,self.k.accounts()["tenant:CNY"].pending),(3,0))

    def test_tampered_cost_finality_scope_or_binding_never_releases_budget(self):
        receipt=strict_loads(self.s.send(self.packet))
        mutations={"cumulative_costs":{"tenant:CNY":"0","order:a:CNY":"0"},
                   "final_cost":False,"terminal_effect":False,"issuer":"other","tenant":"other",
                   "binding":"sha256:"+"0"*64,"operation_id":"op-"+"0"*32,"kind":"RejectedNoEffect",
                   "observed_at":"1","observation_id":"other","request_digest":"sha256:"+"0"*64}
        for field,value in mutations.items():
            changed=canonical_bytes({**receipt,field:value}).decode()
            self.error("INVALID_RECEIPT_AUTHENTICATION",lambda:self.k.trusted.observe(changed,self.op.id))
        self.assertEqual((self.k.accounts()["tenant:CNY"].spent,self.k.accounts()["tenant:CNY"].pending),(0,3))
        self.assertTrue(self.k._quarantined)

    def test_unsigned_downgrade_is_rejected_after_mode_binding(self):
        self.error("AUTHENTICATED_RECEIPT_REQUIRED",lambda:self.k.trusted.observe(self.observation(),self.op.id))
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,3)

    def test_cross_operation_context_rejects_valid_receipt(self):
        raw=self.s.send(self.packet)
        self.error("UNEXPECTED_ADAPTER_OBSERVATION",lambda:self.k.trusted.observe(raw,"op-"+"1"*32))
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,0)

    def test_late_authentic_receipt_survives_send_expiry_and_record_deletion(self):
        raw=self.s.send(self.packet)
        self.k.trusted.unknown(self.op.id)
        self.clock.now=2000
        self.assertIsNone(self.s.query(self.op.id))
        self.assertEqual(self.k.trusted.observe(raw,self.op.id).settlement,Settlement.FINAL)
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,3)

    def test_authenticated_partial_then_final_preserves_unsettled_fee(self):
        partial=replace(self.observation(total=2,final=False),id="partial")
        self.assertEqual(self.k.trusted.observe(self.authenticated(partial),self.op.id).settlement,Settlement.PARTIAL)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,1)
        self.k.trusted.observe(self.authenticated(self.observation()),self.op.id)
        self.assertEqual((self.k.accounts()["tenant:CNY"].spent,self.k.accounts()["tenant:CNY"].pending),(3,0))

    def test_authentication_does_not_hide_cost_bound_violation(self):
        result=self.k.trusted.observe(self.authenticated(self.observation(total=4)),self.op.id)
        self.assertEqual(result.settlement,Settlement.DISPUTED)
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,4)
        self.assertTrue(self.k._quarantined)

    def test_same_observation_id_with_changed_authenticated_cost_is_disputed(self):
        self.k.trusted.observe(self.authenticated(self.observation()),self.op.id)
        result=self.k.trusted.observe(self.authenticated(self.observation(total=4)),self.op.id)
        self.assertEqual(result.settlement,Settlement.DISPUTED)
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,3)

    def test_wrong_key_and_send_key_cannot_authenticate_receipt(self):
        obj=strict_loads(self.s.send(self.packet));body={k:v for k,v in obj.items() if k!="signature"}
        for key in (b"x"*32,self.verifier._key,self.k._policy_authority._key):
            raw=canonical_bytes({**body,"signature":hmac.new(key,MAC_DOMAIN+canonical_bytes(body),hashlib.sha256).hexdigest()}).decode()
            self.error("INVALID_RECEIPT_AUTHENTICATION",lambda:self.k.trusted.observe(raw,self.op.id))

    def test_future_fact_waits_for_logical_time_and_can_be_resubmitted(self):
        raw=self.authenticated(self.observation(),at=10)
        self.error("RECEIPT_FROM_FUTURE",lambda:self.k.trusted.observe(raw,self.op.id))
        self.clock.now=10
        self.assertEqual(self.k.trusted.observe(raw,self.op.id).settlement,Settlement.FINAL)
        self.assertTrue(self.k._quarantined)  # Acceptance does not erase the incident.

    def test_authenticated_but_invalid_terminal_claim_does_not_settle(self):
        invalid=replace(self.observation(),terminal_effect=False)
        self.error("TERMINAL_EFFECT_NOT_ESTABLISHED",lambda:self.k.trusted.observe(self.authenticated(invalid),self.op.id))
        self.assertTrue(self.k._quarantined)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,3)

    def test_workflow_response_tampering_halts_without_budget_release(self):
        k,_,clock=setup();service=RefundService({"a":Order("v1","original-a",100)},
            retention_seconds=60,clock=clock,verifier=k.trusted.send_verifier())
        original=service.send
        def tampered(raw):
            reply=strict_loads(original(raw));reply["cumulative_costs"]["tenant:CNY"]="0"
            return canonical_bytes(reply).decode()
        with patch.object(service,"send",side_effect=tampered):
            result=WorkflowExecutor(k,service).run(make([P,D,S,Q]),"a","tampered",context(k,service),{"flag":True})
        self.assertEqual((result.status,result.error),("Halted","INVALID_RECEIPT_AUTHENTICATION"))
        self.assertEqual((service.real_cost,k.accounts()["tenant:CNY"].pending),(70,70))

    def test_signed_query_recovers_dropped_response(self):
        k,_,clock=setup();service=RefundService({"a":Order("v1","original-a",100)},
            retention_seconds=60,clock=clock,verifier=k.trusted.send_verifier())
        result=WorkflowExecutor(k,service).run(make([P,D,S,Q]),"a","lost",context(k,service),{"flag":True},FaultPlan(drop_receipt_at=frozenset({"w0.2"})))
        self.assertEqual(result.status,"Finished")
        self.assertEqual((service.real_cost,k.accounts()["tenant:CNY"].spent),(70,70))

    def test_invalid_expected_operation_context_fails_closed(self):
        for context in ([],False,"",{}):
            self.error("INVALID_OBSERVATION_BINDING",lambda:self.k.trusted.observe("{}",context))

    def test_signed_malformed_flags_and_cost_dimensions_are_rejected(self):
        obj=strict_loads(self.s.send(self.packet))
        for field,value,code in (("final_cost","true","INVALID_RECEIPT_ENVELOPE"),
            ("terminal_effect",1,"JSON_NUMBERS_UNSUPPORTED"),
            ("cumulative_costs",{"tenant:CNY":"3"},"INVALID_COST_VECTOR")):
            body={**obj,field:value};body.pop("signature")
            if type(value) is int:
                import json
                raw=json.dumps({**obj,field:value})
            else:
                raw=canonical_bytes({**body,"signature":hmac.new(self.verifier._receipt_signer._key,
                    MAC_DOMAIN+canonical_bytes(body),hashlib.sha256).hexdigest()}).decode()
            self.error(code,lambda:self.k.trusted.observe(raw,self.op.id))
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,3)

    def test_duplicate_receipt_json_key_is_not_accepted(self):
        raw=self.s.send(self.packet)
        self.error("DUPLICATE_KEY",lambda:self.k.trusted.observe(raw[:-1]+',"final_cost":true}',self.op.id))

    def test_legacy_fact_reauthenticated_with_reordered_costs_is_idempotent(self):
        k,service,_=setup();op=prepare(k,authorize(k,service,value=20));op=k.dispatch("a",op.id,op.permit_ref)
        observation=service.send(op);k.trusted.observe(observation)
        saved=k._observations[observation.id]
        raw=k.trusted.send_verifier()._receipt_signer.seal(observation,op.binding,0)
        self.assertEqual(k.trusted.observe(raw,op.id).settlement,Settlement.FINAL)
        self.assertEqual(k._observations[observation.id],saved)
        self.assertEqual(k.accounts()["tenant:CNY"].spent,20)
        self.assertFalse(k._quarantined)
