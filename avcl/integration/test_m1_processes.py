"""Deterministic two-process overlap, observed through PostgreSQL lock waits."""
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import test_postgres
from avcl.storage_codec import dumps, loads
from tests.support import authorize, prepare

ROOT = Path(__file__).resolve().parents[1]


class M1ProcessTests(unittest.TestCase):
    setUpClass = classmethod(test_postgres.PersistenceTests.setUpClass.__func__)
    setUp = test_postgres.PersistenceTests.setUp
    tearDown = test_postgres.PersistenceTests.tearDown
    fresh = test_postgres.PersistenceTests.fresh
    query = test_postgres.PersistenceTests.query

    def overlap(self, method, args):
        self.acceptance_evidence = {"requirements": ["R04" if method == "prepare" else "R08"],
                                    "method": method, "schedule": "parent holds authority_guard; both children wait; parent commits",
                                    "children": [], "observed_waiters": [], "results": []}
        children = []
        runtime = (ROOT / ".runtime").resolve()
        runtime.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="m1-process-", dir=runtime) as folder:
            self.assertTrue(Path(folder).resolve().is_relative_to(runtime))
            try:
                with self.k._connect() as gate:
                    gate.execute(sql.SQL("SET LOCAL search_path={},pg_catalog").format(sql.Identifier(self.schema)))
                    gate.execute("SELECT * FROM authority_guard WHERE tenant_id=%s FOR UPDATE", ("demo",)).fetchone()
                    gate_pid = gate.info.backend_pid
                    self.acceptance_evidence["gate_backend_pid"] = gate_pid
                    applications = []
                    for index, call_args in enumerate(args):
                        application = "avcl_m1_" + uuid4().hex
                        applications.append(application)
                        path = Path(folder) / (str(index) + ".json")
                        path.write_text(json.dumps({"schema": self.schema, "application_name": application,
                            "now": self.clock.now, "method": method, "args": dumps(call_args)}), encoding="utf-8")
                        # Windows venv python.exe is a launcher with a different PID.
                        # Own the real interpreter so lifecycle and PID evidence agree.
                        environment = os.environ.copy()
                        environment["PYTHONPATH"] = sysconfig.get_paths()["purelib"]
                        process = subprocess.Popen([sys._base_executable, "-X", "utf8", str(ROOT / "integration/concurrent_worker.py"), str(path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            env=environment,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        children.append(process)
                        self.acceptance_evidence["children"].append({"pid": process.pid, "application_name": application})
                    deadline = time.monotonic() + 20
                    with psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row) as observer:
                        while True:
                            waiters = observer.execute("SELECT pid,application_name,state,wait_event_type,wait_event,"
                                "pg_blocking_pids(pid) AS blocking_pids,query FROM pg_stat_activity WHERE application_name=ANY(%s)",
                                (applications,)).fetchall()
                            self.acceptance_evidence["observed_waiters"] = waiters
                            blocked = [w for w in waiters if w["state"] == "active" and w["wait_event_type"] == "Lock"
                                       and "authority_guard" in w["query"] and w["blocking_pids"]]
                            if len(blocked) == 2: break
                            if any(p.poll() is not None for p in children): self.fail("child exited before observed transaction overlap")
                            if time.monotonic() >= deadline: self.fail("two lock-waiting backend sessions were not observed")
                            time.sleep(0.02)
                    self.assertEqual(len({w["pid"] for w in blocked}), 2)
                    self.assertTrue(all(p.poll() is None for p in children))
                    # Check the lock graph really reaches the row held by this test.
                    by_pid = {w["pid"]: w["blocking_pids"] for w in blocked}
                    def reaches_gate(pid, seen):
                        if pid == gate_pid: return True
                        if pid in seen: return False
                        return any(reaches_gate(p, seen | {pid}) for p in by_pid.get(pid, []))
                    self.assertTrue(all(reaches_gate(w["pid"], set()) for w in blocked))
                    self.acceptance_evidence["overlap_confirmed"] = True
                    gate.commit()
                results = []
                for process in children:
                    stdout, stderr = process.communicate(timeout=30)
                    self.assertEqual(process.returncode, 0, stderr)
                    value = json.loads(stdout)
                    self.assertEqual(value["pid"], process.pid)
                    results.append(value)
                self.assertEqual(len({r["pid"] for r in results}), 2)
                self.assertNotIn(os.getpid(), {r["pid"] for r in results})
                self.acceptance_evidence["results"] = results
                counter=self.query("SELECT last_sequence FROM audit_counter WHERE tenant_id='demo'")[0]["last_sequence"]
                history=self.query("SELECT count(*) AS total,coalesce(max(sequence),0) AS last FROM audit_event WHERE tenant_id='demo'")[0]
                self.assertEqual((counter,history["last"]),(history["total"],history["total"]))
                self.acceptance_evidence["audit_counter"]={"last_sequence":counter,**history}
                return results
            finally:
                for process in children:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=10)

    def test_T08_two_processes_consume_one_permit_once(self):
        op = prepare(self.k, authorize(self.k, self.s))
        args = ("a", op.id, op.permit_ref)
        results = self.overlap("dispatch", [args, args])
        self.assertTrue(all(r["status"] == "accepted" for r in results))
        operations = [loads(r["operation"]) for r in results]
        self.assertEqual(operations[0], operations[1])
        self.assertEqual(operations[0].id, op.id)
        admissions = [e for e in self.k.events() if e.kind == "DispatchAdmitted"]
        outbox = self.k.status()["outbox"]
        self.acceptance_evidence["postconditions"] = {"admissions": len(admissions), "outbox": len(outbox),
                                                       "pending": self.k.accounts()["tenant:CNY"].pending}
        self.assertEqual(len(admissions), 1)
        self.assertEqual(len(outbox), 1)
        self.assertEqual(self.query("SELECT status FROM permit"), [{"status": "Consumed"}])
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending, 70)
        for dispatched in operations: self.s.send(dispatched)
        self.k.trusted.observe(self.s.query(op.id))
        self.assertEqual(len(self.s.effects), 1)
        self.assertEqual(self.s.real_cost, 70)
        self.assertEqual(self.fresh().accounts()["tenant:CNY"].spent, 70)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending, 0)
        self.acceptance_evidence["oracle"] = {"effects": len(self.s.effects), "actual_cost": self.s.real_cost,
                                               "spent": 70, "pending": 0}
        self.k.assert_invariants()

    def test_T04_two_processes_contend_for_70_and_60_of_100(self):
        auths = [authorize(self.k, self.s, name, amount) for name, amount in (("a", 70), ("b", 60))]
        results = self.overlap("prepare", [(auth[0].subject, *auth, "request") for auth in auths])
        self.assertEqual(sum(r["status"] == "accepted" for r in results), 1)
        self.assertEqual([r.get("code") for r in results if r["status"] == "rejected"], ["BUDGET_EXCEEDED"])
        pending = self.k.accounts()["tenant:CNY"].pending
        count = len(self.query("SELECT id FROM operation"))
        self.acceptance_evidence["postconditions"] = {"operations": count, "pending": pending, "limit": 100}
        self.assertEqual(count, 1)
        self.assertIn(pending, (60, 70))
        self.assertEqual(self.s.effects, ())
        self.k.assert_invariants()

    def test_two_processes_retry_same_prepare_without_duplicate_reservation(self):
        auth = authorize(self.k, self.s)
        args = ("a", *auth, "same-request")
        results = self.overlap("prepare", [args, args])
        self.acceptance_evidence["requirements"] = ["R02", "R04", "R08"]
        self.assertTrue(all(r["status"] == "accepted" for r in results))
        self.assertEqual(results[0]["operation"], results[1]["operation"])
        counts = {name: len(self.query("SELECT * FROM " + name)) for name in ("operation", "permit", "business_action", "request_dedup")}
        pending = self.fresh().accounts()["tenant:CNY"].pending
        self.acceptance_evidence["postconditions"] = {"rows": counts, "pending": pending}
        self.assertTrue(all(count == 1 for count in counts.values()))
        self.assertEqual(pending, 70)
        self.k.assert_invariants()
