from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import test_postgres
from avcl.canonical import Reject
from avcl.storage_codec import dumps
from avcl.audit_export import verify_bundle,encode_bundle,load_bundle

ROOT=Path(__file__).resolve().parents[1]


class DurableAuditExportTests(unittest.TestCase):
    setUpClass=classmethod(test_postgres.PersistenceTests.setUpClass.__func__)
    setUp=test_postgres.PersistenceTests.setUp
    tearDown=test_postgres.PersistenceTests.tearDown
    fresh=test_postgres.PersistenceTests.fresh
    query=test_postgres.PersistenceTests.query
    error=test_postgres.PersistenceTests.error

    def seed(self):self.k._transaction("seed",lambda k,c:[k._event("ExportProbe",index=i) for i in range(4)])

    def snapshot(self):
        return {table:self.query("SELECT * FROM "+table+" ORDER BY tenant_id") for table in
                ("authority_guard","audit_counter","rejection_audit_policy","audit_event","operation","budget_account")}

    def test_export_is_readonly_preserves_payload_bytes_and_paginates(self):
        self.seed();before=self.snapshot()
        first=self.k.trusted.export_audit(limit=2)
        second=self.fresh().trusted.export_audit(after=2,expected_previous=first["checkpoint"])
        full=self.k.trusted.export_audit()
        self.assertEqual(first["records"]+second["records"],full["records"])
        self.assertEqual(second["checkpoint"],full["checkpoint"])
        stored=self.query("SELECT payload FROM audit_event WHERE tenant_id='demo' ORDER BY sequence")
        self.assertEqual([r["payload"] for r in full["records"]],[r["payload"] for r in stored])
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(verify_bundle(full,expected_tenant="demo",expected_checkpoint=full["checkpoint"])["status"],"VerifiedAgainstCheckpoint")

    def test_new_events_extend_old_page_without_changing_old_anchor(self):
        self.seed();first=self.k.trusted.export_audit()
        self.k._transaction("append",lambda k,c:k._event("Later"))
        page=self.k.trusted.export_audit(after=4,expected_previous=first["checkpoint"])
        self.assertEqual([r["sequence"] for r in page["records"]],["5"])
        self.assertEqual(verify_bundle(page,expected_tenant="demo",expected_after=4,expected_previous=first["checkpoint"])["status"],"ContinuationConsistent")

    def test_tenant_isolation_and_checkpoint_scope(self):
        self.seed();first=self.k.trusted.export_audit()
        other=self.fresh(tenant="other");other.initialize()
        other._transaction("seed",lambda k,c:[k._event("OtherTenant") for _ in range(4)])
        page=other.trusted.export_audit()
        self.assertNotIn("ExportProbe",encode_bundle(page))
        self.error("AUDIT_SCOPE_MISMATCH",lambda:verify_bundle(page,expected_tenant="demo"))
        self.error("AUDIT_CHECKPOINT_MISMATCH",lambda:other.trusted.export_audit(after=4,expected_previous=first["checkpoint"]))

    def test_external_checkpoint_detects_validly_shaped_database_rewrite(self):
        self.seed();original=self.k.trusted.export_audit();event=self.k.events()[0]
        self.query("ALTER TABLE audit_event DISABLE TRIGGER USER")
        self.query("UPDATE audit_event SET payload=%s WHERE tenant_id='demo' AND sequence=1",(dumps(replace(event,kind="Rewritten")),))
        rewritten=self.k.trusted.export_audit()
        self.assertEqual(verify_bundle(rewritten,expected_tenant="demo")["status"],"InternallyConsistent")
        self.error("AUDIT_CHECKPOINT_MISMATCH",lambda:verify_bundle(rewritten,expected_tenant="demo",expected_checkpoint=original["checkpoint"]))
        self.error("AUDIT_CHECKPOINT_MISMATCH",lambda:self.k.trusted.export_audit(after=4,expected_previous=original["checkpoint"]))

    def test_type_equivalent_prefix_mutation_is_rejected_for_writes_and_reads(self):
        self.seed();before=self.snapshot()
        def mutate(k,c):k._events[0]=replace(k._events[0],sequence=True)
        for write in (True,False):
            self.error("AUDIT_HISTORY_MUTATION",lambda:self.k._transaction("mutation",mutate,write))
            self.assertEqual(self.snapshot(),before)

    def test_cli_export_verify_and_no_overwrite(self):
        self.seed()
        with tempfile.TemporaryDirectory(prefix="audit-export-",dir=ROOT/".runtime") as folder:
            output=Path(folder)/"page.json";pin=Path(folder)/"checkpoint.json"
            def run(*args):return subprocess.run([sys.executable,str(ROOT/"scripts/audit_snapshot.py"),*map(str,args)],capture_output=True,text=True,encoding="utf-8",timeout=30)
            result=run("export","--schema",self.schema,"--tenant","demo","--output",output)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            raw=output.read_bytes();page=load_bundle(raw.decode("utf-8"));pin.write_text(json.dumps(page["checkpoint"]),encoding="utf-8")
            self.assertEqual(run("verify",output,"--tenant","demo").returncode,2)
            self.assertEqual(run("verify",output,"--tenant","demo","--expected-checkpoint",pin).returncode,0)
            self.assertEqual(run("export","--schema",self.schema,"--tenant","demo","--output",output).returncode,1)
            self.assertEqual(output.read_bytes(),raw)
            page["records"].pop()
            output.write_text(encode_bundle(page),encoding="utf-8")
            self.assertEqual(run("verify",output,"--tenant","demo","--expected-checkpoint",pin).returncode,1)

    def test_cli_dependency_errors_do_not_print_credentials(self):
        with tempfile.TemporaryDirectory(prefix="audit-export-",dir=ROOT/".runtime") as folder:
            profile=Path(folder)/"profile.json"
            profile.write_text(json.dumps({"dsn":"password=DO_NOT_ECHO invalid_parameter=yes","port":123}),encoding="utf-8")
            result=subprocess.run([sys.executable,str(ROOT/"scripts/audit_snapshot.py"),"export","--profile",str(profile),
                "--tenant","demo","--output",str(Path(folder)/"out.json")],capture_output=True,text=True,encoding="utf-8",timeout=30)
            self.assertEqual(result.returncode,1)
            self.assertNotIn("DO_NOT_ECHO",result.stdout+result.stderr)
            self.assertFalse((Path(folder)/"out.json").exists())

    def test_strict_payload_check_on_real_rejection_and_configuration(self):
        from avcl.rejection_audit import AuthenticatedContext
        from avcl.canonical import digest
        self.k.trusted.configure_rejection_audit(total_limit=10,subject_limit=5,window_seconds=60)
        context=AuthenticatedContext("a","prepare",digest({"request":"test"},"test"),"trace")
        def denied(k,c):raise Reject("BUDGET_EXCEEDED")
        self.error("BUDGET_EXCEEDED",lambda:self.k._transaction("denied",denied,authenticate=lambda k,c:context))
        before=self.snapshot();page=self.fresh().trusted.export_audit()
        report=verify_bundle(page,expected_tenant="demo",check_payloads=True)
        self.assertEqual(report["payload_schema"]["status"],"Conforms")
        self.assertEqual(report["event_count"],"2")
        self.assertEqual(before,self.snapshot())

    def test_strict_payload_check_on_real_workflow_and_receipts(self):
        from avcl.workflow_postgres import DurableWorkflowExecutor
        from tests.test_workflow import make,context,P,D,S
        from avcl.simulator import RefundService,Order
        service=RefundService({'a':Order('v1','original-a',100)},retention_seconds=120,
                              clock=self.clock,verifier=self.k.trusted.send_verifier())
        executor=DurableWorkflowExecutor(self.k,service)
        result=executor.run(make([P,D,S]),'a','audit-schema',context(self.k,service),{'flag':True})
        self.assertEqual(result.status,'Finished')
        before=self.snapshot();page=self.fresh().trusted.export_audit()
        report=verify_bundle(page,expected_tenant="demo",check_payloads=True)
        self.assertEqual(report["payload_schema"]["checked_events"],str(len(self.k.events())))
        self.assertIn('ReceiptAuthenticated',{e.kind for e in self.k.events()})
        self.assertEqual(before,self.snapshot())

    def test_strict_mode_does_not_rewrite_unsupported_legacy_events(self):
        self.seed();before=self.snapshot();page=self.k.trusted.export_audit()
        self.error("UNSUPPORTED_AUDIT_EVENT_KIND",lambda:verify_bundle(page,expected_tenant="demo",check_payloads=True))
        self.assertEqual(verify_bundle(page,expected_tenant="demo")["status"],"InternallyConsistent")
        self.assertEqual(before,self.snapshot())


if __name__=="__main__":unittest.main()
