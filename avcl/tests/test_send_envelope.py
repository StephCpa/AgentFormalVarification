from dataclasses import replace
import hashlib
import hmac
import json
import unittest

from avcl.canonical import Reject,canonical_bytes,strict_loads
from avcl.send_envelope import SendVerifier,SIGN_DOMAIN,envelope_digest
from avcl.sending import admit_attempt
from avcl.simulator import RefundService,Order
from avcl.workflow_runtime import WorkflowExecutor
from .support import setup,authorize,prepare
from .test_workflow import make,context,P,D,S,Q


class SendEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.k,_,self.clock=setup()
        self.verifier=self.k.trusted.send_verifier()
        self.service=RefundService({"a":Order("v1","original-a",100)},
            retention_seconds=60,clock=self.clock,verifier=self.verifier)
        self.op=prepare(self.k,authorize(self.k,self.service,value=20))

    def error(self,code,fn):
        with self.assertRaises(Reject) as caught:fn()
        self.assertEqual(caught.exception.code,code)

    def packet(self):
        self.op=self.k.dispatch("a",self.op.id,self.op.permit_ref)
        with self.k._lock:return admit_attempt(self.k,self.op)

    def resign(self,obj):
        # Deliberately authorized-but-malformed issuer output tests parser checks.
        body={k:v for k,v in obj.items() if k!="signature"}
        return canonical_bytes({**body,"signature":hmac.new(self.verifier._key,
            SIGN_DOMAIN+canonical_bytes(body),hashlib.sha256).hexdigest()}).decode()

    def test_prepared_or_forged_admitted_operation_cannot_mint_packet(self):
        self.error("NOT_ADMITTED",lambda:admit_attempt(self.k,self.op))
        self.error("UNTRUSTED_OPERATION",lambda:admit_attempt(self.k,replace(self.op,admitted=True)))
        self.assertFalse(any(e.kind=="SendAttemptAdmitted" for e in self.k.events()))

    def test_signed_packet_executes_but_raw_operation_cannot_bypass(self):
        raw=self.packet()
        self.error("INVALID_JSON",lambda:self.service.send(self.op))
        receipt=self.service.send(raw)
        self.k.trusted.observe(receipt)
        self.assertEqual((self.service.real_cost,self.k.accounts()["tenant:CNY"].spent),(20,20))
        event=next(e for e in self.k.events() if e.kind=="SendAttemptAdmitted")
        self.assertEqual(dict(event.details)["envelope_digest"],envelope_digest(raw))
        self.assertNotIn("signature",dict(event.details))

    def test_every_authenticated_binding_field_rejects_tampering(self):
        raw=self.packet();obj=strict_loads(raw)
        mutations={"operation_id":"op-"+"a"*32,"tenant":"other","receiver":"other",
            "not_after":"999","issued_at":"1","attempt":"2","epoch":"2",
            "binding":"sha256:"+"0"*64,"contract_digest":"sha256:"+"0"*64,
            "request_digest":"sha256:"+"0"*64,"action":"cancel","profile":"other",
            "key_id":"other","format":"other"}
        for name,value in mutations.items():
            with self.subTest(field=name):
                changed={**obj,name:value}
                self.error("INVALID_SEND_AUTHENTICATION",lambda:self.service.send(canonical_bytes(changed).decode()))
        for name,value in (("destination","attacker"),("amount_minor","40"),("order","b"),("business_key","other")):
            changed={**obj,"request":{**obj["request"],name:value}}
            self.error("INVALID_SEND_AUTHENTICATION",lambda:self.service.send(canonical_bytes(changed).decode()))
        self.assertEqual(self.service.effects,())

    def test_wrong_authority_key_cannot_authenticate(self):
        raw=self.packet();other,_,_=setup()
        self.error("INVALID_SEND_AUTHENTICATION",lambda:other.trusted.send_verifier().verify(raw,0))

    def test_same_key_still_requires_exact_tenant_and_receiver(self):
        raw=self.packet()
        for tenant,receiver in (("other","refund-simulator"),("demo","other-receiver")):
            verifier=SendVerifier(self.verifier._key,tenant,receiver)
            self.error("SEND_AUTHORITY_SCOPE_MISMATCH",lambda:verifier.verify(raw,0))

    def test_sender_key_is_separated_from_policy_key_and_mac_domain(self):
        obj=strict_loads(self.packet());body={k:v for k,v in obj.items() if k!="signature"}
        for key,domain in ((self.k._policy_authority._key,SIGN_DOMAIN),(self.verifier._key,b"avcl/policy-signature/0.3\x00")):
            forged={**body,"signature":hmac.new(key,domain+canonical_bytes(body),hashlib.sha256).hexdigest()}
            self.error("INVALID_SEND_AUTHENTICATION",lambda:self.service.send(canonical_bytes(forged).decode()))

    def test_authenticated_claims_still_need_valid_semantics(self):
        obj=strict_loads(self.packet())
        for changes,code in (({"attempt":"0"},"INVALID_SEND_ENVELOPE"),
            ({"attempt":"4"},"INVALID_SEND_ENVELOPE"),({"epoch":"0"},"INVALID_SEND_ENVELOPE"),
            ({"issued_at":"60"},"INVALID_SEND_ENVELOPE"),
            ({"action":"cancel"},"SEND_AUTHORITY_SCOPE_MISMATCH"),
            ({"request_digest":"sha256:"+"0"*64},"SEND_REQUEST_BINDING_MISMATCH"),
            ({"operation_id":"bad"},"INVALID_SEND_ENVELOPE"),
            ({"binding":"bad"},"INVALID_SEND_ENVELOPE")):
            self.error(code,lambda:self.service.send(self.resign({**obj,**changes})))
        self.assertEqual(self.service.real_cost,0)

    def test_strict_shape_duplicates_numbers_and_size_are_rejected(self):
        raw=self.packet();obj=strict_loads(raw)
        for changed in ({k:v for k,v in obj.items() if k!="epoch"},{**obj,"extra":"value"}):
            self.error("INVALID_SEND_ENVELOPE",lambda:self.service.send(canonical_bytes(changed).decode()))
        self.error("DUPLICATE_KEY",lambda:self.service.send(raw[:-1]+',"attempt":"1"}'))
        self.error("JSON_NUMBERS_UNSUPPORTED",lambda:self.service.send(json.dumps({**obj,"attempt":1})))
        self.error("REQUEST_TOO_LARGE",lambda:self.service.send(" "*65537))

    def test_replays_deduplicate_and_expired_packet_cannot_be_extended(self):
        raw=self.packet();receipt=self.service.send(raw)
        self.clock.now=1
        with self.k._lock:second=admit_attempt(self.k,self.op)
        self.assertNotEqual(raw,second)
        self.assertEqual(self.service.send(second),receipt)
        self.clock.now=60
        self.assertIsNone(self.service.query(self.op.id))
        self.error("REMOTE_ACCEPTANCE_CLOSED",lambda:self.service.send(raw))
        changed={**strict_loads(raw),"not_after":"120"}
        self.error("INVALID_SEND_AUTHENTICATION",lambda:self.service.send(canonical_bytes(changed).decode()))
        self.assertEqual((len(self.service.effects),self.service.real_cost),(1,20))

    def test_future_packet_is_not_yet_valid(self):
        self.clock.now=2
        raw=self.packet()
        self.error("SEND_ENVELOPE_NOT_YET_VALID",lambda:self.verifier.verify(raw,1))

    def test_send_authority_cannot_be_reused_as_cancel_authority(self):
        raw=self.packet()
        self.error("SIGNED_CANCEL_UNSUPPORTED",lambda:self.service.cancel_with_tombstone(raw))
        self.assertIsNone(self.service.query(self.op.id))

    def test_strict_workflow_sends_authenticated_bytes_and_retries_once_effect(self):
        k,_,clock=setup()
        service=RefundService({"a":Order("v1","original-a",100)},retention_seconds=60,
                              clock=clock,verifier=k.trusted.send_verifier())
        result=WorkflowExecutor(k,service).run(make([P,D,S,S,Q]),"a","signed",context(k,service),{"flag":True})
        self.assertEqual(result.status,"Finished")
        self.assertEqual((len(service.effects),k.accounts()["tenant:CNY"].spent),(1,70))

    def test_invalid_verifier_configuration_fails_closed(self):
        for key in (b"short",True,"secret"):
            self.error("INVALID_SEND_VERIFIER",lambda:SendVerifier(key,"demo"))
        with self.assertRaises(ValueError):RefundService({},verifier=self.verifier)

    def test_workflow_authentication_failure_quarantines_without_downgrade(self):
        k,_,clock=setup();other,_,_=setup()
        service=RefundService({"a":Order("v1","original-a",100)},retention_seconds=60,
                              clock=clock,verifier=other.trusted.send_verifier())
        result=WorkflowExecutor(k,service).run(make([P,D,S,Q]),"a","wrong-key",context(k,service),{"flag":True})
        self.assertEqual((result.status,result.error),("Halted","INVALID_SEND_AUTHENTICATION"))
        self.assertEqual(service.effects,())
        self.assertEqual(k.accounts()["tenant:CNY"].pending,70)
        self.assertTrue(k._quarantined)
