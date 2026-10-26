import unittest
import test_postgres
from avcl.canonical import Reject
from avcl.runtime import ReferenceKernel
from tests.support import authorize,prepare
from tests.test_evaluation_budget import refund_check_work


class DurableEvaluationBudgetTests(unittest.TestCase):
    setUpClass=classmethod(test_postgres.PersistenceTests.setUpClass.__func__)
    setUp=test_postgres.PersistenceTests.setUp
    tearDown=test_postgres.PersistenceTests.tearDown
    fresh=test_postgres.PersistenceTests.fresh
    query=test_postgres.PersistenceTests.query

    def snapshot(self):
        return {table:self.query("SELECT * FROM "+table) for table in
                ("operation","permit","approval","request_dedup","business_action","outbox","audit_event")}

    def test_prepare_work_exhaustion_rolls_back_every_durable_projection(self):
        auth=authorize(self.k,self.s)
        limited=self.fresh(evaluation_limit=refund_check_work(ReferenceKernel()))
        before=self.snapshot(),self.k.accounts()
        with self.assertRaises(Reject) as caught:prepare(limited,auth)
        self.assertEqual(caught.exception.code,"UNKNOWN_EVALUATION")
        self.assertEqual((self.snapshot(),self.fresh().accounts()),before)
        self.acceptance_evidence={"gate":"prepare","work_limit":limited.evaluation_limit,
                                  "error":caught.exception.code,"durable_snapshot_unchanged":True}
        self.assertFalse(prepare(self.k,auth).admitted)

    def test_dispatch_work_exhaustion_keeps_permit_reservations_and_no_outbox(self):
        op=prepare(self.k,authorize(self.k,self.s))
        limited=self.fresh(evaluation_limit=refund_check_work(ReferenceKernel()))
        before=self.snapshot(),self.k.accounts()
        with self.assertRaises(Reject) as caught:limited.dispatch("a",op.id,op.permit_ref)
        self.assertEqual(caught.exception.code,"UNKNOWN_EVALUATION")
        self.assertEqual((self.snapshot(),self.fresh().accounts()),before)
        self.assertEqual(self.fresh().get("a",op.id).state,"Prepared")
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,70)
        self.acceptance_evidence={"gate":"dispatch","work_limit":limited.evaluation_limit,
                                  "error":caught.exception.code,"durable_snapshot_unchanged":True,"pending":70,"outbox_count":0}
        self.assertTrue(self.k.dispatch("a",op.id,op.permit_ref).admitted)
