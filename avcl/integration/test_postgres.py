"""Real PostgreSQL tests; no mock database and no skip-on-missing dependency."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from uuid import uuid4
import json
import subprocess
import sys
import unittest
import psycopg
from psycopg import sql
from avcl.postgres import PostgresKernel
from avcl.supervision import PostgresSupervisor
from avcl.storage_codec import dumps
from avcl.canonical import Reject
from avcl.types import Capability,Execution,Settlement
from avcl.contract import default_contract,Health
from avcl.simulator import RefundService,Order
from tests.support import Clock,authorize,prepare

ROOT=Path(__file__).resolve().parents[1]

class PersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn=json.loads((ROOT/".runtime"/"local-postgres.json").read_text(encoding="utf-8"))["dsn"]
        with psycopg.connect(cls.dsn) as conn:
            assert conn.info.server_version==180006
            for setting in ("fsync","synchronous_commit","full_page_writes"):
                assert conn.execute("SHOW "+setting).fetchone()[0]=="on"

    def setUp(self):
        self.schema="avcl_it_"+uuid4().hex;self.clock=Clock();self.k=self.fresh();self.k.initialize()
        self.k.trusted.add_account("tenant:CNY",100)
        for name in ("a","b"):
            self.k.trusted.add_account("order:"+name+":CNY",100)
            self.k.trusted.register_capability(Capability("cap-"+name,"demo",name,frozenset({name}),10000,1000,1))
        self.s=RefundService({name:Order("v1","original-"+name,100) for name in ("a","b")})

    def tearDown(self):
        # Only the randomly named schema created by this individual test.
        with psycopg.connect(self.dsn) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def fresh(self,**kwargs): return PostgresKernel(self.dsn,schema=self.schema,clock=self.clock,**kwargs)
    def error(self,code,fn):
        with self.assertRaises(Reject) as caught:fn()
        self.assertEqual(caught.exception.code,code)

    def query(self,statement,args=()):
        with self.k._connect() as conn:
            conn.execute(sql.SQL("SET LOCAL search_path={},pg_catalog").format(sql.Identifier(self.schema)))
            cursor=conn.execute(statement,args)
            return cursor.fetchall() if cursor.description else []

    def crash(self,method,args,point):
        job=ROOT/".runtime"/("crash-job-"+uuid4().hex+".json")
        job.write_text(json.dumps({"schema":self.schema,"method":method,"args":dumps(args),"crash_at":point,"now":self.clock.now}),encoding="utf-8")
        try:
            result=subprocess.run([sys.executable,"-X","utf8",str(ROOT/"integration"/"crash_worker.py"),str(job)],
                capture_output=True,text=True,encoding="utf-8",timeout=30)
            self.assertEqual(result.returncode,97,result.stdout+result.stderr)
        finally:job.unlink(missing_ok=True)

    def prepared(self):return prepare(self.k,authorize(self.k,self.s))
    def dispatched(self):
        op=self.prepared();return self.k.dispatch("a",op.id,op.permit_ref)

    def test_restart_retains_bindings_and_once_consumption(self):
        auth=authorize(self.k,self.s);op=prepare(self.k,auth)
        other=self.fresh();self.assertEqual(other.get("a",op.id),op)
        self.assertEqual(prepare(other,auth),op)
        first=other.dispatch("a",op.id,op.permit_ref)
        self.assertEqual(self.fresh().dispatch("a",op.id,op.permit_ref),first)
        self.assertEqual(len(self.k.status()["outbox"]),1)
        self.assertEqual(sum(e.kind=="DispatchAdmitted" for e in self.k.events()),1)

    def test_concurrent_budget_uses_independent_connections(self):
        auths=[authorize(self.k,self.s,"a",70),authorize(self.k,self.s,"b",60)];barrier=Barrier(2)
        def run(auth):
            k=self.fresh();barrier.wait()
            try:return prepare(k,auth)
            except Reject as exc:return exc.code
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,auths))
        self.assertEqual(results.count("BUDGET_EXCEEDED"),1);self.k.assert_invariants()
        self.assertIn(self.k.accounts()["tenant:CNY"].pending,(60,70))
        self.assertEqual(len(self.query("SELECT * FROM operation")),1)

    def test_concurrent_same_request_returns_one_operation(self):
        auth=authorize(self.k,self.s);barrier=Barrier(2)
        def run(_):barrier.wait();return prepare(self.fresh(),auth)
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)

    def test_second_account_rejection_rolls_back_every_row(self):
        self.query("UPDATE budget_account SET limit_amount=10 WHERE id='order:a:CNY'")
        auth=authorize(self.k,self.s);before=self.k.events()
        self.error("BUDGET_EXCEEDED",lambda:prepare(self.k,auth))
        self.assertEqual(self.k.events(),before);self.assertEqual(self.k.accounts()["tenant:CNY"].pending,0)
        for table in ("operation","reservation","permit","business_action","request_dedup"):
            self.assertEqual(self.query("SELECT * FROM "+table),[])

    def test_other_instance_revoke_invalidates_prepared_permit(self):
        op=self.prepared();self.fresh().trusted.revoke()
        self.error("STALE_PERMIT",lambda:self.k.dispatch("a",op.id,op.permit_ref))
        self.assertEqual(self.k.status()["epoch"],2)

    def test_policy_snapshot_restores_after_expiry_but_admission_denies(self):
        op=self.dispatched();self.clock.now=1500
        self.assertEqual(self.fresh().get("a",op.id),op)
        self.k.trusted.observe(self.s.send(op))
        self.assertEqual(self.fresh().accounts()["tenant:CNY"].spent,70)

    def test_current_broken_health_is_not_reset_by_restore(self):
        op=self.dispatched()
        self.k.trusted.set_assumption_health("remote-idempotency",Health("simulator","refund",0,1000,"Broken"))
        self.error("ASSUMPTION_BROKEN",lambda:self.fresh().claim_outbox("worker"))
        self.assertEqual(self.k.status()["outbox"][0]["status"],"Pending")

    def test_policy_publication_and_historical_operations_survive_restart(self):
        op=self.prepared();contract=replace(default_contract(),policy_version=2)
        envelope=self.k.trusted.seal_policy(contract,900,contract.requirements(),
            frozenset(c.id for c in contract.clauses if c.kind=="safety"),{"refund":{"amount":1,"currency":"CNY"}})
        digest=self.fresh().trusted.activate_policy(envelope)
        self.assertEqual(digest,contract.semantic_digest)
        self.assertEqual(self.fresh().get("a",op.id).contract_digest,op.contract_digest)
        self.error("STALE_PERMIT",lambda:self.fresh().dispatch("a",op.id,op.permit_ref))

    def test_prepare_process_death_before_commit_rolls_back(self):
        auth=authorize(self.k,self.s);args=("a",*auth,"req")
        self.crash("prepare",args,"prepare.before_commit")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,0)
        self.assertEqual(self.query("SELECT * FROM operation"),[])
        op=prepare(self.fresh(),auth);self.assertEqual(op.state,Execution.PREPARED)

    def test_prepare_process_death_after_commit_retry_returns_same_operation(self):
        auth=authorize(self.k,self.s);self.crash("prepare",("a",*auth,"req"),"prepare.after_commit")
        rows=self.query("SELECT id FROM operation");self.assertEqual(len(rows),1)
        self.assertEqual(prepare(self.fresh(),auth).id,rows[0]["id"])
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)

    def test_dispatch_process_death_retains_outbox_and_once_admission(self):
        op=self.prepared();args=("a",op.id,op.permit_ref)
        self.crash("dispatch",args,"dispatch.after_commit")
        self.assertEqual(self.k.status()["outbox"][0]["status"],"Pending")
        self.k.dispatch(*args);claim=self.fresh().claim_outbox("worker")
        self.assertEqual(claim["operation"].id,op.id);self.assertIsNone(self.fresh().claim_outbox("other"))
        self.assertEqual(sum(e.kind=="DispatchAdmitted" for e in self.k.events()),1)

    def test_claim_process_death_lease_expiry_retains_exposure_and_deadline(self):
        op=self.dispatched();self.crash("claim_outbox",("worker",5),"claim.after_commit")
        self.clock.now=6;self.assertEqual(self.fresh().reap_expired_leases(),1)
        unknown=self.k.get("a",op.id);self.assertEqual(unknown.state,Execution.UNKNOWN)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        self.assertEqual(self.k.status()["outbox"][0]["status"],"NeedsReconcile")
        self.assertIsNone(self.k.claim_outbox("new-worker"));self.clock.now=40
        self.assertEqual(self.fresh().trusted.unknown(op.id).obligation_due,unknown.obligation_due)
        self.k.trusted.observe(self.s.send(op))
        self.assertEqual(self.k.status()["obligations"][0]["status"],"Satisfied")

    def test_raw_receipt_survives_process_death_before_projection(self):
        op=self.dispatched();receipt=self.s.send(op)
        self.crash("observe",(receipt,),"observe.after_raw_commit")
        self.assertEqual(self.k.status()["raw_observations"],1)
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,0)
        self.assertEqual(len(self.fresh().recover_observations()),1)
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,70)
        self.assertEqual(self.k.recover_observations(),[])
        self.k.trusted.observe(receipt);self.assertEqual(self.k.accounts()["tenant:CNY"].spent,70)

    def test_projection_process_death_rolls_back_then_recovers(self):
        op=self.dispatched();receipt=self.s.send(op)
        self.crash("observe",(receipt,),"observe.before_commit")
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,0)
        self.assertEqual(len(self.k.recover_observations()),1)
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,70)

    def test_projection_process_death_after_commit_is_idempotent(self):
        op=self.dispatched();receipt=self.s.send(op)
        self.crash("observe",(receipt,),"observe.after_commit")
        self.assertEqual(self.fresh().accounts()["tenant:CNY"].spent,70)
        self.assertEqual(self.k.recover_observations(),[])
        self.k.trusted.observe(receipt);self.assertEqual(self.k.accounts()["tenant:CNY"].spent,70)

    def test_real_cost_over_bound_is_durable_not_rolled_back(self):
        op=self.dispatched();receipt=replace(self.s.send(op),cumulative_costs=(("tenant:CNY",110),("order:a:CNY",70)))
        settled=self.k.trusted.observe(receipt)
        self.assertEqual(settled.settlement,Settlement.DISPUTED)
        account=self.fresh().accounts()["tenant:CNY"]
        self.assertEqual(account.spent,110);self.assertTrue(account.breached)
        self.assertTrue(self.k.status()["quarantined"]);self.assertEqual(self.k.status()["raw_observations"],1)
        self.k.assert_invariants()

    def test_raw_and_audit_and_policy_rows_are_immutable(self):
        op=self.dispatched();self.k.trusted.observe(self.s.send(op))
        for table,column in (("observation_receipt","payload"),("audit_event","payload"),("policy_snapshot","envelope")):
            with self.assertRaises(psycopg.errors.RaiseException):self.query(f"UPDATE {table} SET {column}={column}")
        self.k.assert_invariants()

    def test_invalid_raw_receipt_does_not_starve_recovery(self):
        op=self.dispatched();valid=self.s.send(op);bad=replace(valid,id="invalid",request_digest="wrong")
        self.crash("observe",(bad,),"observe.after_raw_commit")
        self.crash("observe",(valid,),"observe.after_raw_commit")
        results=self.fresh().recover_observations()
        self.assertEqual(len(results),2)
        self.assertIn({"status":"Rejected","code":"INVALID_OBSERVATION_BINDING"},results)
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,70)
        self.assertEqual(self.k.recover_observations(),[])
        rows=self.query("SELECT applied,error_code FROM observation_application WHERE NOT applied")
        self.assertEqual(rows,[{"applied":False,"error_code":"INVALID_OBSERVATION_BINDING"}])
        self.assertEqual(self.k.status()["raw_observations"],2)

    def test_tenant_isolation_and_permit_projection_corruption_denied(self):
        op=self.prepared();other=self.fresh(tenant="other");other.initialize()
        self.error("UNTRUSTED_REFERENCE",lambda:other.get("a",op.id))
        self.assertEqual(other.accounts(),{})
        self.query("UPDATE permit SET status='Consumed' WHERE tenant_id='demo'")
        self.error("CORRUPT_PERMIT_PROJECTION",lambda:self.k.get("a",op.id))

    def unknown(self):
        op=self.dispatched();return self.k.trusted.unknown(op.id)

    def test_supervisor_claim_crash_recovers_without_deadline_reset(self):
        op=self.unknown();self.crash("supervisor.claim",("dead-worker",5),"claim-obligation.after_commit")
        row=self.k.status()["obligations"][0];old_generation=row["generation"]
        self.clock.now=6;supervisor=PostgresSupervisor(self.fresh());claim=supervisor.claim("new-worker")
        self.assertGreater(claim["generation"],old_generation);self.assertEqual(claim["due"],op.obligation_due)
        self.error("STALE_OBLIGATION_CLAIM",lambda:supervisor.defer(op.id,"dead-worker",old_generation))
        self.assertEqual(self.k.status()["obligations"][0]["worker"],"new-worker")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)

    def test_supervisor_only_one_claim_across_connections(self):
        self.unknown();barrier=Barrier(2)
        def run(worker):barrier.wait();return PostgresSupervisor(self.fresh()).claim(worker)
        with ThreadPoolExecutor(2) as pool:claims=list(pool.map(run,["worker1","worker2"]))
        self.assertEqual(sum(c is not None for c in claims),1)

    def test_supervisor_free_query_after_revocation_settles_lost_receipt(self):
        op=self.unknown();self.s.send(op);self.k.trusted.revoke()
        result=PostgresSupervisor(self.fresh()).run_once("worker",self.s)
        self.assertEqual(result["status"],"Reconciled")
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,70)
        self.assertEqual(len(self.s.effects),1)
        self.assertEqual(self.k.status()["obligations"][0]["resolution"],"FinalEffect")

    def test_supervisor_absent_record_preserves_unknown_and_backoff(self):
        op=self.unknown();supervisor=PostgresSupervisor(self.k)
        self.assertEqual(supervisor.run_once("worker",self.s)["status"],"Deferred")
        self.assertEqual(supervisor.run_once("worker",self.s)["status"],"Idle")
        self.assertEqual(self.k.get("a",op.id).state,Execution.UNKNOWN)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        self.assertEqual(self.s.effects,())
        self.assertEqual(self.k.status()["obligations"][0]["due"],op.obligation_due)

    def test_late_settlement_keeps_breach_discovery_and_original_due(self):
        op=self.unknown();self.clock.now=op.obligation_due+10
        supervisor=PostgresSupervisor(self.fresh());supervisor.sweep()
        row=self.k.status()["obligations"][0]
        self.assertEqual(row["status"],"Breached");self.assertEqual(row["breached_at"],self.clock.now)
        self.k.trusted.observe(self.s.send(op));row=self.k.status()["obligations"][0]
        self.assertEqual(row["status"],"Breached");self.assertEqual(row["resolution"],"FinalEffect")
        self.assertEqual(row["due"],op.obligation_due)
        self.assertIsNone(supervisor.claim("late-worker"))
        self.assertEqual(sum(e.kind=="ObligationBreached" for e in self.k.events()),1)

    def test_handoff_requires_registered_ack_and_keeps_exposure(self):
        op=self.unknown();supervisor=PostgresSupervisor(self.k);claim=supervisor.claim("worker")
        args=(op.id,"worker",claim["generation"],"human-team","ticket-1")
        self.error("UNAUTHORIZED_HANDOFF_RECEIVER",lambda:supervisor.accept_handoff(*args))
        self.assertIsNone(self.k.status()["obligations"][0]["resolution"])
        supervisor.register_receiver("human-team",1000)
        self.crash("supervisor.accept_handoff",args,"accept-handoff.after_commit")
        self.assertEqual(PostgresSupervisor(self.fresh()).accept_handoff(*args),"Escalated")
        row=self.k.status()["obligations"][0]
        self.assertEqual((row["status"],row["owner"],row["resolution"]),("Satisfied","human-team","Escalated"))
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        self.assertEqual(self.k.get("a",op.id).state,Execution.UNKNOWN)
        self.assertIsNone(supervisor.claim("worker2"))
        with self.assertRaises(psycopg.errors.RaiseException):self.query("UPDATE handoff_receipt SET receipt_id='changed'")

    def test_late_handoff_does_not_erase_breach(self):
        op=self.unknown();self.clock.now=op.obligation_due+1;supervisor=PostgresSupervisor(self.k)
        supervisor.register_receiver("human-team",self.clock.now+1000);claim=supervisor.claim("worker")
        supervisor.accept_handoff(op.id,"worker",claim["generation"],"human-team","late-ticket")
        row=self.k.status()["obligations"][0]
        self.assertEqual(row["status"],"Breached");self.assertEqual(row["resolution"],"Escalated")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)

    def test_clock_rollback_cannot_extend_or_reclaim_active_lease(self):
        op=self.unknown();self.clock.now=20;supervisor=PostgresSupervisor(self.k);claim=supervisor.claim("worker",5)
        self.clock.now=2
        self.error("CLOCK_ROLLBACK",lambda:PostgresSupervisor(self.fresh()).claim("rollback-worker"))
        row=self.k.status()["obligations"][0]
        self.assertEqual(row["lease_until"],25);self.assertEqual(row["due"],op.obligation_due)
        self.assertEqual(row["worker"],"worker")
        self.clock.now=26;new=supervisor.claim("next-worker")
        self.assertGreater(new["generation"],claim["generation"])

    def test_unclaimed_dispatch_wait_bound_creates_durable_obligation(self):
        op=self.dispatched();self.clock.now=31;PostgresSupervisor(self.fresh()).sweep()
        op=self.k.get("a",op.id);self.assertEqual(op.state,Execution.UNKNOWN)
        self.assertEqual(op.obligation_due,86431)
        self.assertEqual(self.k.status()["outbox"][0]["status"],"NeedsReconcile")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)

    def test_partial_cost_observation_creates_supervision(self):
        op=self.dispatched();receipt=replace(self.s.send(op),final_cost=False)
        self.k.trusted.observe(receipt)
        row=self.k.status()["obligations"][0]
        self.assertEqual(row["status"],"Open");self.assertEqual(row["trigger_event"],next(e.sequence for e in self.k.events() if e.kind=="ReconcileRequired"))
        self.k.trusted.observe(replace(receipt,id="final-cost",final_cost=True))
        self.assertEqual(self.k.status()["obligations"][0]["status"],"Satisfied")

    def test_obligation_deadline_and_breach_cannot_be_rewritten(self):
        op=self.unknown()
        with self.assertRaises(psycopg.errors.RaiseException):self.query("UPDATE obligation SET due=due+1")
        self.clock.now=op.obligation_due+1;PostgresSupervisor(self.k).sweep()
        with self.assertRaises(psycopg.errors.RaiseException):self.query("UPDATE obligation SET breached_at=NULL,status='Open'")

    def test_wrong_query_receipt_is_not_applied_to_other_operation(self):
        op=self.unknown();receipt=replace(self.s.send(op),operation_id="another-operation")
        self.s.query=lambda operation_id:receipt
        self.assertEqual(PostgresSupervisor(self.k).run_once("worker",self.s)["status"],"RejectedReceipt")
        self.assertEqual(self.k.accounts()["tenant:CNY"].spent,0)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)

    def test_schema_v1_migration_preserves_pending_and_legacy_final_obligations(self):
        self.check_schema_migration(1)

    def test_schema_v2_migration_preserves_supervision_and_adds_workflows(self):
        self.check_schema_migration(2)

    def test_schema_v3_migration_preserves_operations_and_adds_capacity(self):
        self.check_schema_migration(3)

    def test_schema_v4_migration_preserves_operations_and_adds_gateway(self):
        self.check_schema_migration(4)

    def test_schema_v5_migration_preserves_operations_and_adds_validation(self):
        self.check_schema_migration(5)

    def test_schema_v6_migration_preserves_operations_and_adds_tasks(self):
        self.check_schema_migration(6)

    def test_schema_v7_migration_preserves_operations_and_adds_delegation(self):
        self.check_schema_migration(7)

    def check_schema_migration(self,version):
        pending=self.unknown()
        other=prepare(self.k,authorize(self.k,self.s,"b",20),"req-b")
        other=self.k.dispatch("b",other.id,other.permit_ref);self.k.trusted.unknown(other.id)
        self.k.trusted.observe(self.s.send(other))
        old_schema=self.schema+"_v"+str(version)
        tables=("authority_guard","policy_snapshot","capability","evidence","approval","health","budget_account",
                "operation","reservation","permit","business_action","request_dedup","observation","observation_receipt",
                "observation_application","outbox","send_attempt","obligation","audit_event")
        try:
            with self.k._connect() as c:
                c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(old_schema)))
                c.execute(sql.SQL("SET LOCAL search_path={},pg_catalog").format(sql.Identifier(old_schema)))
                c.execute((ROOT/"migrations"/"001_core.sql").read_text(encoding="utf-8"))
                if version>=2:c.execute((ROOT/"migrations"/"002_supervision.sql").read_text(encoding="utf-8"))
                if version>=3:c.execute((ROOT/"migrations"/"003_workflow.sql").read_text(encoding="utf-8"))
                if version>=4:c.execute((ROOT/"migrations"/"004_capacity.sql").read_text(encoding="utf-8"))
                if version>=5:c.execute((ROOT/"migrations"/"005_gateway.sql").read_text(encoding="utf-8"))
                if version>=6:c.execute((ROOT/"migrations"/"006_validation.sql").read_text(encoding="utf-8"))
                if version>=7:c.execute((ROOT/"migrations"/"007_task_close.sql").read_text(encoding="utf-8"))
                for table in tables:
                    columns=[r["column_name"] for r in c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",(old_schema,table))]
                    rows=c.execute(sql.SQL("SELECT * FROM {}.{}").format(sql.Identifier(self.schema),sql.Identifier(table))).fetchall()
                    for row in rows:
                        if version==1 and table=="obligation" and row["status"]=="Satisfied":row["status"]="Fulfilled"
                        c.execute(sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(sql.Identifier(table),sql.SQL(",").join(map(sql.Identifier,columns)),sql.SQL(",").join(sql.Placeholder() for _ in columns)),tuple(row[col] for col in columns))
                if version>=4:
                    c.execute(sql.SQL("INSERT INTO capacity_policy SELECT * FROM {}.capacity_policy").format(sql.Identifier(self.schema)))
            restored=PostgresKernel(self.dsn,schema=old_schema,clock=self.clock);restored.migrate();restored.migrate()
            self.assertEqual(restored.get("a",pending.id),pending)
            rows={r["operation_id"]:r for r in restored.status()["obligations"]}
            self.assertEqual(rows[pending.id]["due"],pending.obligation_due)
            self.assertEqual(rows[other.id]["resolution"],"LegacyFinal" if version==1 else "FinalEffect")
            if version==1:self.assertIsNone(rows[other.id]["resolved_at"])
            PostgresSupervisor(restored).sweep()
            self.assertEqual(restored.accounts(),self.k.accounts())
        finally:
            with self.k._connect() as c:c.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(old_schema)))

if __name__=="__main__":unittest.main()
