from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
from contextlib import closing
import http.client
import json
import os
import subprocess
import sys
import sysconfig
import time
import unittest
import test_postgres
from avcl.gateway import GatewayAuthority,RuntimeGateway,SCOPES,token_hash
from avcl.http_gateway import LocalRuntimeServer
from tests.support import authorize,prepare

ROOT=Path(__file__).resolve().parents[1]

class GatewayTests(unittest.TestCase):
    setUpClass=classmethod(test_postgres.PersistenceTests.setUpClass.__func__)
    fresh=test_postgres.PersistenceTests.fresh
    error=test_postgres.PersistenceTests.error
    query=test_postgres.PersistenceTests.query

    def setUp(self):
        test_postgres.PersistenceTests.setUp(self)
        self.authority=GatewayAuthority(self.k);self.credential=self.authority.issue("a",SCOPES,900)
        self.auth=authorize(self.k,self.s);self.checked=self.authority.register_checked(self.auth[0],self.auth[2],800)
        self.payload={"client_request_id":"prepare1","checked_ref":self.checked,"capability_ref":self.auth[1],"approval_refs":[self.auth[3]],"business_action_key":self.auth[0].business_key}
        stem=ROOT/".runtime"/("http-test-"+uuid4().hex)
        self.config=stem.with_suffix(".config.json");self.ready=stem.with_suffix(".ready.json")
        self.clock_file=stem.with_suffix(".clock.json");self.log=stem.with_suffix(".log")
        self.clock_file.write_text('{"now":0}',encoding="utf-8")
        self.config.write_text(json.dumps({"dsn":self.dsn,"schema":self.schema,"clock_file":str(self.clock_file)}),encoding="utf-8")
        self.log_handle=self.log.open("wb")
        self.launch_server()
        self.addCleanup(self.cleanup_server)

    def launch_server(self):
        environment=os.environ.copy();environment["PYTHONPATH"]=sysconfig.get_paths()["purelib"]
        self.process=subprocess.Popen([sys._base_executable,"-X","utf8",str(ROOT/"scripts"/"runtime_api.py"),"--config",str(self.config),"--ready-file",str(self.ready)],
            env=environment,stdout=self.log_handle,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        deadline=time.monotonic()+15
        while not self.ready.exists():
            if self.process.poll() is not None:raise AssertionError("Local API process exited before readiness")
            if time.monotonic()>deadline:raise AssertionError("Local API readiness timed out")
            time.sleep(0.05)
        ready=json.loads(self.ready.read_text(encoding="utf-8"));self.port=ready["port"]
        self.assertEqual(ready["pid"],self.process.pid)

    def cleanup_server(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
        self.log_handle.close()
        for path in (self.config,self.ready,self.clock_file,self.log):path.unlink(missing_ok=True)

    def tearDown(self):
        try:self.cleanup_server()
        finally:test_postgres.PersistenceTests.tearDown(self)

    def call(self,method,path,body=None,token="default",headers=None,raw=None):
        if token=="default":token=self.credential.token
        request_headers={"Content-Type":"application/json"}
        if token is not None:request_headers["Authorization"]="Bearer "+token
        request_headers.update(headers or {})
        data=raw if raw is not None else json.dumps(body).encode() if body is not None else None
        with closing(http.client.HTTPConnection("127.0.0.1",self.port,timeout=10)) as conn:
            conn.request(method,path,body=data,headers=request_headers);response=conn.getresponse();payload=response.read()
            self.assertNotIn(self.credential.token.encode(),payload)
            self.assertNotIn(self.dsn.encode(),payload)
            return response.status,json.loads(payload)

    def prepared(self):
        status,result=self.call("POST","/v1/operations/prepare",self.payload)
        self.assertEqual(status,200,result);return result["data"]

    def test_authenticated_prepare_dispatch_and_query_use_durable_core(self):
        op=self.prepared();route="/v1/operations/"+op["operation_id"]
        status,result=self.call("POST",route+"/dispatch",{"client_request_id":"dispatch1","permit_ref":op["permit_ref"]})
        self.assertEqual(status,202,result);self.assertEqual(result["data"]["execution_state"],"Dispatched")
        status,result=self.call("GET",route)
        self.assertEqual(status,200);self.assertNotIn("permit_ref",result["data"])
        self.assertEqual(self.k.status()["outbox"][0]["status"],"Pending")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        rows=self.query("SELECT token_hash FROM api_credential")
        self.assertEqual(rows[0]["token_hash"],token_hash(self.credential.token))
        self.assertNotIn(self.credential.token,str(rows))

    def test_missing_forged_revoked_and_expired_credentials_fail(self):
        for token in (None,"avcl_"+"x"*43,"not-a-token"):
            status,result=self.call("POST","/v1/operations/prepare",self.payload,token)
            self.assertEqual(status,401);self.assertEqual(result["error"]["code"],"INVALID_CREDENTIAL")
        expired=self.authority.issue("a",SCOPES,1)
        self.clock_file.write_text('{"now":2}',encoding="utf-8")
        self.assertEqual(self.call("POST","/v1/operations/prepare",self.payload,expired.token)[0],401)
        self.authority.revoke(self.credential.id)
        self.assertEqual(self.call("POST","/v1/operations/prepare",self.payload)[0],401)
        self.assertEqual(self.query("SELECT * FROM operation"),[])

    def test_authenticated_body_cannot_supply_tenant_subject_or_amount(self):
        for key,value in (("tenant_id","other"),("subject","b"),("amount_minor","1"),("destination","attacker")):
            status,result=self.call("POST","/v1/operations/prepare",{**self.payload,key:value})
            self.assertEqual(status,400,result)
        changed={**self.payload,"business_action_key":"other-business"}
        status,result=self.call("POST","/v1/operations/prepare",changed)
        self.assertEqual(status,409);self.assertEqual(result["error"]["code"],"BINDING_MISMATCH")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,0)

    def test_scopes_and_cross_subject_checked_refs_are_enforced(self):
        readonly=self.authority.issue("a",frozenset({"get"}),900)
        self.assertEqual(self.call("POST","/v1/operations/prepare",self.payload,readonly.token)[0],403)
        other=self.authority.issue("b",SCOPES,900)
        self.assertEqual(self.call("POST","/v1/operations/prepare",self.payload,other.token)[0],403)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,0)

    def test_cross_subject_and_cross_tenant_reads_match_missing_operation(self):
        op=self.prepared();route="/v1/operations/"+op["operation_id"]
        other_subject=self.authority.issue("b",SCOPES,900)
        other_kernel=self.fresh(tenant="other");other_kernel.initialize()
        other_tenant=GatewayAuthority(other_kernel).issue("a",SCOPES,900)
        for token in (other_subject.token,other_tenant.token):
            status,result=self.call("GET",route,token=token)
            missing,missing_result=self.call("GET","/v1/operations/op-"+"0"*32,token=token)
            self.assertEqual((status,result["error"]["code"]),(missing,missing_result["error"]["code"]))
            self.assertEqual(status,404)

    def test_idempotency_and_dispatch_override_rejection(self):
        op=self.prepared();self.assertEqual(self.prepared(),op)
        status,result=self.call("POST","/v1/operations/prepare",{**self.payload,"business_action_key":"different"})
        self.assertEqual((status,result["error"]["code"]),(409,"IDEMPOTENCY_CONFLICT"))
        route="/v1/operations/"+op["operation_id"]+"/dispatch"
        payload={"client_request_id":"dispatch1","permit_ref":op["permit_ref"]}
        self.assertEqual(self.call("POST",route,{**payload,"amount":"1"})[0],400)
        self.assertEqual(self.call("POST",route,payload)[0],202)
        self.assertEqual(self.call("POST",route,payload)[0],202)
        self.assertEqual(sum(e.kind=="DispatchAdmitted" for e in self.k.events()),1)

    def test_reconciliation_is_queued_and_cannot_cancel_admitted_effect(self):
        op=self.prepared();route="/v1/operations/"+op["operation_id"]
        self.call("POST",route+"/dispatch",{"client_request_id":"dispatch1","permit_ref":op["permit_ref"]})
        status,result=self.call("POST",route+"/reconcile",{"client_request_id":"reconcile1","reason":"lost-receipt"})
        self.assertEqual(status,202);self.assertEqual(result["data"]["execution_state"],"Unknown")
        self.assertTrue(result["data"]["obligation_refs"])
        status,result=self.call("POST",route+"/cancel",{"client_request_id":"cancel1","cancel_kind":"before_dispatch"})
        self.assertEqual((status,result["error"]["code"]),(409,"NO_EFFECT_NOT_ESTABLISHED"))
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)

    def test_prepared_cancel_and_unmounted_control_plane(self):
        op=self.prepared();route="/v1/operations/"+op["operation_id"]
        self.assertEqual(self.call("POST",route+"/cancel",{"client_request_id":"cancel1","cancel_kind":"before_dispatch"})[0],200)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,0)
        for path in ("/v1/admin/authorities/revoke","/v1/admin/policies/activate","/v1/observations","/v1/send","/v1/credentials"):
            self.assertEqual(self.call("POST",path,{})[0],404)
        self.assertEqual(self.k.status()["epoch"],1)

    def test_revocation_between_lookup_and_transaction_is_rechecked(self):
        gateway=RuntimeGateway(self.dsn,self.schema,self.clock);locate=gateway._locate
        def race(hashed):
            tenant=locate(hashed);self.authority.revoke(self.credential.id);return tenant
        with patch.object(gateway,"_locate",side_effect=race):
            status,result=gateway.handle("POST","/v1/operations/prepare","Bearer "+self.credential.token,json.dumps(self.payload))
        self.assertEqual((status,result["error"]["code"]),(401,"INVALID_CREDENTIAL"))
        self.assertEqual(self.query("SELECT * FROM operation"),[])

    def test_http_boundary_rejects_duplicate_keys_headers_origins_and_large_body(self):
        path="/v1/operations/prepare"
        self.assertEqual(self.call("POST",path,raw=b'{"x":"one","x":"two"}')[1]["error"]["code"],"DUPLICATE_KEY")
        self.assertEqual(self.call("POST",path,raw=b'{"x":1}')[1]["error"]["code"],"JSON_NUMBERS_UNSUPPORTED")
        self.assertEqual(self.call("POST",path,self.payload,headers={"Origin":"https://untrusted.example"})[0],403)
        self.assertEqual(self.call("POST",path,self.payload,headers={"Host":"untrusted.example"})[0],403)
        self.assertEqual(self.call("POST",path,raw=b"x"*65537)[0],413)
        with closing(http.client.HTTPConnection("127.0.0.1",self.port,timeout=10)) as conn:
            conn.putrequest("POST",path);conn.putheader("Content-Type","application/json");conn.putheader("Content-Length","2")
            conn.putheader("Authorization","Bearer "+self.credential.token);conn.putheader("Authorization","Bearer "+self.credential.token)
            conn.endheaders(b"{}");response=conn.getresponse();self.assertEqual(response.status,400);response.read()
        with self.assertRaises(ValueError):LocalRuntimeServer(("0.0.0.0",0),None)

    def test_capacity_gate_cannot_be_bypassed_through_http(self):
        self.k.trusted.set_capacity(0,0,0)
        status,result=self.call("POST","/v1/operations/prepare",self.payload)
        self.assertEqual((status,result["error"]["code"]),(409,"OPERATION_CAPACITY_EXCEEDED"))
        self.assertEqual(self.query("SELECT * FROM api_request"),[])

    def test_checked_reference_expiry_and_wrong_permit_do_not_mutate(self):
        checked=self.authority.register_checked(self.auth[0],self.auth[2],1)
        self.clock_file.write_text('{"now":2}',encoding="utf-8")
        status,result=self.call("POST","/v1/operations/prepare",{**self.payload,"checked_ref":checked})
        self.assertEqual((status,result["error"]["code"]),(409,"STALE_EVIDENCE"))
        op=self.prepared()
        status,result=self.call("POST","/v1/operations/"+op["operation_id"]+"/dispatch",{"client_request_id":"wrong","permit_ref":"forged"})
        self.assertEqual(status,403)
        self.assertEqual(self.k.get("a",op["operation_id"]).state,"Prepared")

    def test_server_process_restart_reuses_persisted_request_and_permit(self):
        op=self.prepared();old_pid=self.process.pid
        self.process.terminate();self.process.wait(timeout=5);self.ready.unlink()
        self.launch_server();self.assertNotEqual(self.process.pid,old_pid)
        self.assertEqual(self.prepared(),op)
        status,_=self.call("POST","/v1/operations/"+op["operation_id"]+"/dispatch",{"client_request_id":"dispatch1","permit_ref":op["permit_ref"]})
        self.assertEqual(status,202);self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        self.assertEqual(sum(e.kind=="Prepared" for e in self.k.events()),1)

    def test_revocation_blocks_cached_retry_without_releasing_pending(self):
        op=self.prepared();self.authority.revoke(self.credential.id)
        self.assertEqual(self.call("POST","/v1/operations/prepare",self.payload)[0],401)
        self.assertEqual(self.call("POST","/v1/operations/"+op["operation_id"]+"/dispatch",{"client_request_id":"dispatch1","permit_ref":op["permit_ref"]})[0],401)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        self.assertEqual(self.k.get("a",op["operation_id"]).state,"Prepared")

if __name__=="__main__":unittest.main()
