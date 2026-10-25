import unittest
from avcl.types import Execution
from models.budget_model import explore, transitions, COSTS
from .support import setup, authorize, prepare


class BoundedConformanceTests(unittest.TestCase):
    def replay(self,path):
        k,service,_=setup();auth=[authorize(k,service,n,c) for n,c in zip(("a","b"),COSTS)]
        ops={}
        for action in path:
            if action=="revoke": k.trusted.revoke();continue
            verb,index=action.split(":");i=int(index);name=("a","b")[i]
            if verb=="prepare": ops[i]=prepare(k,auth[i])
            elif verb in ("dispatch","repeat_dispatch"):
                ops[i]=k.dispatch(name,ops[i].id,ops[i].permit_ref)
            elif verb=="abort": ops[i]=k.abort_prepared(name,ops[i].id)
            elif verb=="timeout": ops[i]=k.trusted.unknown(ops[i].id)
            elif verb in ("remote_apply","remote_retry"): service.send(ops[i])
            elif verb=="remote_cancel": service.cancel_with_tombstone(ops[i])
            elif verb=="receipt": ops[i]=k.trusted.observe(service.query(ops[i].id))
            else: self.fail(action)
            k.assert_invariants()
        return k,service,ops

    def test_every_correct_model_edge_replays_against_reference(self):
        report,paths=explore()
        self.assertIsNone(report["counterexample"])
        self.assertTrue(report["exploration_complete"])
        checked=0
        for state,path in paths.items():
            for action,expected in transitions(state):
                k,service,ops=self.replay(path+(action,));checked+=1
                account=k.accounts()["tenant:CNY"]
                self.assertEqual((account.spent,account.pending),(sum(expected.spent),sum(expected.pending)))
                self.assertEqual(service.real_cost,expected.actual)
                self.assertEqual(sum(e.kind=="DispatchAdmitted" for e in k.events()),sum(expected.admissions))
                mapping={Execution.PREPARED:"P",Execution.DISPATCHED:"D",Execution.UNKNOWN:"U",
                         Execution.CONFIRMED:"F",Execution.REJECTED:"F",Execution.ABORTED:"X"}
                self.assertEqual(tuple(mapping[ops[i].state] if i in ops else "N" for i in range(2)),expected.local)
        self.assertEqual(checked,report["edges"])

    def test_oracle_finds_both_deliberate_protocol_mutations(self):
        for mode,error in [("split_check","ledger_exceeds_budget"),("early_release","actual_exceeds_exposure")]:
            report,_=explore(mode)
            self.assertIn(error,report["counterexample"]["violations"])
            self.assertGreater(len(report["counterexample"]["trace"]),0)
