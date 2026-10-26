"""Resource failure is Unknown; it neither proves a predicate nor spends a permit."""
import json
from dataclasses import replace
import unittest
from unittest.mock import patch
from avcl.canonical import Reject,canonical_bytes
from avcl.checks import compile_checks,Instruction
from avcl.contract import compile_contract,default_contract,Clause
from avcl.evaluation import WorkBudget,EvaluationLimit,MAX_WORK_LIMIT
from avcl.ir import compile_predicate,FieldType
from avcl.ir_types import Record,Some,Tagged
from avcl.runtime import PolicyDenied
from avcl.verification import verify_finite,runtime_report
from .support import setup,authorize,prepare
from .test_encoding_ir import const,field,call
from .test_contract_policy import amended


def refund_check_work(k):
    budget=WorkBudget()
    assert k._checks.evaluate({"amount":70,"currency":"CNY"},budget=budget)
    return budget.used


class EvaluationBudgetTests(unittest.TestCase):
    def test_exact_threshold_and_short_circuit_agree_across_backends(self):
        schema={"x":FieldType("Int",0,10),"flag":FieldType("Bool")}
        expressions=(call("If",field("flag"),call("Lt",field("x"),const(5)),const(True)),
                     call("And",field("flag"),call("Eq",call("MulConst",const(3),field("x")),const(6))),
                     call("Or",field("flag"),call("Le",call("Sub",call("Add",field("x"),const(1)),const(2)),const(4))))
        for expr in expressions:
            p=compile_predicate(expr,schema);checks=compile_checks(p)
            for flag in (False,True):
                values={"x":2,"flag":flag};full=WorkBudget();answer=p.evaluate(values,budget=full)
                vm=WorkBudget();self.assertEqual(checks.evaluate(values,budget=vm),answer)
                self.assertEqual(full.used,vm.used)
                for limit in range(full.used+1):
                    for backend in (p,checks):
                        with self.subTest(expr=expr,flag=flag,limit=limit,backend=type(backend).__name__):
                            if limit<full.used:
                                with self.assertRaises(EvaluationLimit):backend.evaluate(values,budget=WorkBudget(limit))
                            else:self.assertEqual(backend.evaluate(values,budget=WorkBudget(limit)),answer)

    def test_structural_comparison_and_membership_are_not_single_unit_shortcuts(self):
        record=FieldType("Record",fields=tuple(("f"+str(i),FieldType("String")) for i in range(8)))
        collection=FieldType("Set",element=record)
        items=frozenset(Record(tuple(("f"+str(j),str(i).zfill(2)+"x"*30) for j in range(8))) for i in range(64))
        p=compile_predicate(call("Eq",field("a"),field("b")),{"a":collection,"b":collection})
        costs=[]
        for backend in (p,compile_checks(p)):
            budget=WorkBudget();self.assertTrue(backend.evaluate({"a":items,"b":items},budget=budget));costs.append(budget.used)
            with self.assertRaises(EvaluationLimit):backend.evaluate({"a":items,"b":items},budget=WorkBudget(10000))
        self.assertEqual(costs[0],costs[1]);self.assertGreater(costs[0],10000)
        member=next(iter(items))
        p=compile_predicate(call("InSet",field("a"),field("b")),{"a":record,"b":collection})
        for backend in (p,compile_checks(p)):
            self.assertTrue(backend.evaluate({"a":member,"b":items}))
            with self.assertRaises(EvaluationLimit):backend.evaluate({"a":member,"b":items},budget=WorkBudget(10000))

    def test_legal_nested_input_exceeds_default_budget_and_is_not_a_violation(self):
        record=FieldType("Record",fields=(("f0",FieldType("String")),("f1",FieldType("String"))))
        inner=FieldType("Set",element=record,max_size=4)
        outer=FieldType("Set",element=inner,max_size=64)
        def values():
            return frozenset(frozenset(Record(tuple(("f"+str(k),f"{i:02}{j:02}"+"x"*28) for k in range(2)))
                                      for j in range(4)) for i in range(64))
        env={"a":values(),"b":values()}
        self.assertLess(len(canonical_bytes({k:outer.value_wire(v) for k,v in env.items()})),65536)
        p=compile_predicate(call("Eq",field("a"),field("b")),{"a":outer,"b":outer})
        work=[]
        for backend in (p,compile_checks(p)):
            with self.assertRaises(EvaluationLimit):backend.evaluate(env)
            budget=WorkBudget(MAX_WORK_LIMIT)
            self.assertTrue(backend.evaluate(env,budget=budget));work.append(budget.used)
        self.assertEqual(work[0],work[1])
        self.assertEqual(runtime_report(p,env)["status"],"Unknown")

    def test_noncanonical_record_set_normalization_charges_rehashing(self):
        record=FieldType("Record",fields=(("a",FieldType("Int")),("b",FieldType("Int"))))
        t=FieldType("Set",element=record)
        values=frozenset(Record((("b",i),("a",i))) for i in range(32))
        budget=WorkBudget();normalized=t.normalize(values,budget=budget)
        self.assertEqual(normalized,frozenset(Record((("a",i),("b",i))) for i in range(32)))
        with self.assertRaises(EvaluationLimit):t.normalize(values,budget=WorkBudget(budget.used-1))

    def test_option_record_get_and_hash_collisions_preserve_results(self):
        integer=FieldType("Int",-10,10)
        option=FieldType("Option",element=integer)
        record=FieldType("Record",fields=(("x",option),))
        get={"tag":"Get","field":"x","args":[field("r")]}
        p=compile_predicate(call("If",call("IsSome",get),call("Eq",call("Unwrap",get),const(-1)),const(False)),{"r":record})
        for value in (None,Some(-1),Some(2)):
            budgets=[WorkBudget(),WorkBudget()]
            results=[b.evaluate({"r":Record((("x",value),))},budget=w) for b,w in zip((p,compile_checks(p)),budgets)]
            self.assertEqual(results,[value==Some(-1)]*2);self.assertEqual(budgets[0].used,budgets[1].used)
        self.assertEqual(hash(-1),hash(-2))
        t=FieldType("Set",element=integer)
        p=compile_predicate(call("Eq",field("a"),field("b")),{"a":t,"b":t})
        self.assertTrue(p.evaluate({"a":frozenset({-1,-2}),"b":frozenset({-2,-1})}))

    def test_forced_set_hash_collisions_are_covered_by_comparison_charge(self):
        record=FieldType("Record",fields=(("x",FieldType("Int",0,100)),))
        collection=FieldType("Set",element=record)
        p=compile_predicate(call("Eq",field("a"),field("b")),{"a":collection,"b":collection})
        original=Record.__eq__;calls=[]
        def counted(a,b):
            calls.append(1)
            return original(a,b)
        with patch.object(Record,"__hash__",return_value=0),patch.object(Record,"__eq__",counted):
            a=frozenset(Record((("x",i),)) for i in range(32))
            b=frozenset(Record((("x",i),)) for i in range(32))
            calls.clear();budget=WorkBudget()
            self.assertTrue(p.evaluate({"a":a,"b":b},budget=budget))
            self.assertGreater(len(calls),32)
            self.assertGreater(budget.used,len(calls))

    def test_invalid_large_shapes_are_rejected_before_traversal_and_custom_equality(self):
        t=FieldType("Record",fields=(("x",FieldType("Bool")),))
        with self.assertRaises(Reject) as caught:t.normalize(Record((("x",True),)*100000),budget=WorkBudget(1))
        self.assertEqual(caught.exception.code,"EVALUATION_TYPE_OR_RANGE")
        with self.assertRaises(EvaluationLimit):FieldType("Int").normalize(1<<100000,budget=WorkBudget(10))
        class Poison:
            def __eq__(self,other):raise AssertionError("untrusted comparison executed")
        with self.assertRaises(Reject):FieldType("Id",name="Order").normalize(Tagged(Poison(),"a"))

    def test_shared_clause_budget_and_forbid_never_allow_unknown(self):
        base=default_contract()
        extra=Clause("zzz-forbid","safety","forbid","refund",("prepare","dispatch"),("R09",),(),False,
                     compile_predicate(const(False),{"amount":FieldType("Int")}))
        cc=compile_contract(amended(base,extra=(extra,)))
        k,_,_=setup();values={"amount":70,"currency":"CNY"}
        first=WorkBudget();baseline=compile_contract(base).evaluate("prepare","refund",values,k._health,0,budget=first)
        self.assertTrue(baseline["allowed"])
        result=cc.evaluate("prepare","refund",values,k._health,0,budget=WorkBudget(first.used))
        self.assertFalse(result["allowed"])
        self.assertEqual([(r["status"],r["reason"]) for r in result["reports"]],[("RuntimeSatisfied",None),("Unknown","UNKNOWN_EVALUATION")])
        self.assertTrue(result["evaluation_work"]["exhausted"])

    def test_prepare_and_dispatch_fail_atomically_after_first_check_consumes_budget(self):
        k,s,_=setup();auth=authorize(k,s);limit=refund_check_work(k)
        k._evaluation_limit=limit
        before=k.accounts(),k.events(),dict(k._approvals),dict(k._dedup)
        with self.assertRaises(PolicyDenied) as caught:prepare(k,auth)
        self.assertEqual(caught.exception.code,"UNKNOWN_EVALUATION")
        self.assertEqual(caught.exception.report["reports"][0]["status"],"Unknown")
        self.assertEqual((k.accounts(),k.events(),k._approvals,k._dedup),before)
        k._evaluation_limit=WorkBudget().limit;op=prepare(k,auth)
        stats=json.loads(dict(k.events()[-1].details)["evaluation_work"])
        self.assertGreater(int(stats["used"]),limit)
        k._evaluation_limit=limit;before=k.accounts(),k.events(),k.get("a",op.id),dict(k._permits)
        with self.assertRaises(Reject) as caught:k.dispatch("a",op.id,op.permit_ref)
        self.assertEqual(caught.exception.code,"UNKNOWN_EVALUATION")
        self.assertEqual((k.accounts(),k.events(),k.get("a",op.id),k._permits),before)
        k._evaluation_limit=WorkBudget().limit
        self.assertTrue(k.dispatch("a",op.id,op.permit_ref).admitted)

    def test_finite_and_runtime_reports_classify_budget_exhaustion_as_unknown(self):
        p=compile_predicate(field("flag"),{"flag":FieldType("Bool")})
        reports=verify_finite(p,evaluation_limit=0)
        self.assertEqual([r["status"] for r in reports],["Unknown","Unknown"])
        self.assertTrue(all(r["reason"]=="UNKNOWN_EVALUATION" for r in reports))
        report=runtime_report(p,{"flag":True},budget=WorkBudget(0))
        self.assertEqual((report["status"],report["allow"],report["reason"]),("Unknown",False,"UNKNOWN_EVALUATION"))

    def test_budget_parameters_are_host_bounded_and_exhaustion_is_sticky(self):
        for invalid in (True,-1,MAX_WORK_LIMIT+1,"10"):
            with self.assertRaises(Reject):WorkBudget(invalid)
        b=WorkBudget(1)
        with self.assertRaises(EvaluationLimit):b.charge(2)
        with self.assertRaises(EvaluationLimit):b.charge(0)

    def test_invalid_vm_jump_cycle_cannot_bypass_work_accounting(self):
        p=compile_predicate(field("flag"),{"flag":FieldType("Bool")})
        # Public loading regenerates and rejects this code. Also bound this
        # internal corruption seam so a compiler defect cannot spin forever.
        checks=replace(compile_checks(p),instructions=(Instruction("JUMP","n0",0),))
        with self.assertRaises(EvaluationLimit):checks.evaluate({"flag":True},budget=WorkBudget(100))
