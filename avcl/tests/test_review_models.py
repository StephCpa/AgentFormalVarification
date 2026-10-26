from dataclasses import replace
import unittest
from models.review_composition_model import explore,receipt_steps
from avcl.types import Observation,Execution,Settlement
from .support import setup,authorize,prepare


class ReviewCompositionModels(unittest.TestCase):
    def test_bounded_compositions_and_both_mutation_oracles(self):
        for kind in ('receipt','send'):
            self.assertFalse(explore(kind)[0]['violations'])
            self.assertTrue(explore(kind,True)[0]['violations'])

    def test_each_receipt_model_edge_replayed_after_representative_prefix(self):
        report,paths=explore('receipt');edges=0
        for state,prefix in paths.items():
            for action,nxt in receipt_steps(state):
                k,s,_=setup(limit=3,order_limit=2)
                op=prepare(k,authorize(k,s,value=2,fee=1));op=k.dispatch('a',op.id,op.permit_ref)
                old=Observation('older',op.id,op.request.request_digest,Execution.CONFIRMED,
                                (('tenant:CNY',2),('order:a:CNY',2)),False,True)
                final=replace(old,id='final',final_cost=True)
                for step in prefix+(action,):
                    if step=='RemoteEffect':s.send(op)
                    elif step=='DeliverOlder':k.trusted.observe(old)
                    elif step=='DeliverFinal':k.trusted.observe(final)
                a=k.accounts()['tenant:CNY'];current=k.get('a',op.id)
                self.assertEqual((s.real_cost,a.spent,a.pending,current.settlement==Settlement.FINAL),
                                 (nxt.actual,nxt.spent,nxt.pending,nxt.final),(prefix,action))
                self.assertFalse(k._quarantined);k.assert_invariants();edges+=1
        self.assertEqual(edges,report['edges'])
