import json
import unittest
from dataclasses import replace
from itertools import product
from avcl.canonical import Reject,canonical_bytes,strict_loads
from avcl.ir import FieldType,Some,Tagged,Record,Diagnostic,compile_predicate,load_predicate,refund_predicate,nodes
from avcl.ir_types import type_from_wire
from avcl.checks import compile_checks,load_checks
from avcl.verification import verify_finite,runtime_report,PropertyStatus
from .test_encoding_ir import const,field,call
from .support import setup,authorize,prepare

def get(expr,name): return {"tag":"Get","args":[expr],"field":name}
def typed(t,value): return {"tag":"Const","type":t,"value":value}

class IRPipelineTests(unittest.TestCase):
    def assertReject(self,code,fn):
        with self.assertRaises(Reject) as cm: fn()
        self.assertEqual(cm.exception.code,code)

    def agree(self,p,inputs):
        loaded=load_predicate(p.to_json());checks=load_checks(compile_checks(p).to_json(),loaded)
        for values,expected in inputs:
            self.assertEqual(p.evaluate(values),expected)
            self.assertEqual(loaded.evaluate(values),expected)
            self.assertEqual(checks.evaluate(values),expected)

    def test_roundtrip_separates_semantics_and_source_digest(self):
        p=refund_predicate();restored=load_predicate(p.to_json())
        self.assertEqual(p.to_json(),restored.to_json())
        self.assertEqual(p.semantic_digest,restored.semantic_digest)
        moved=replace(p,source_map=tuple((n.node_id,"changed.avcl:12:3") for n in nodes(p.root)))
        self.assertEqual(p.semantic_digest,moved.semantic_digest)
        self.assertNotEqual(p.source_digest,moved.source_digest)
        self.assertNotEqual(p.artifact_digest,moved.artifact_digest)
        self.assertEqual(compile_checks(p).to_json(),compile_checks(moved).to_json())

    def test_loader_rejects_unknown_version_ids_extra_keys_and_numbers(self):
        original=strict_loads(refund_predicate().to_json())
        for mutate,code in [(lambda o:o.update(ir_version="future"),"UNSUPPORTED_IR_VERSION"),
                            (lambda o:o["root"].update(node_id="forged"),"INVALID_NODE_ID"),
                            (lambda o:o.update(extra=True),"INVALID_IR_DOCUMENT"),
                            (lambda o:o.update(source_map={}),"INVALID_SOURCE_MAP")]:
            obj=json.loads(json.dumps(original));mutate(obj)
            self.assertReject(code,lambda:load_predicate(json.dumps(obj)))
        self.assertReject("JSON_NUMBERS_UNSUPPORTED",lambda:load_predicate('{"x":1}'))

    def test_enum_and_id_are_nominal_not_untyped_strings(self):
        order=FieldType("Id",name="OrderId");user=FieldType("Id",name="UserId")
        self.assertReject("PREDICATE_TYPE_ERROR",lambda:compile_predicate(call("Eq",field("a"),field("b")),{"a":order,"b":user}))
        p=compile_predicate(call("Eq",field("a"),typed(order,Tagged("OrderId","o1"))),{"a":order})
        self.agree(p,[({"a":Tagged("OrderId","o1")},True),({"a":Tagged("OrderId","o2")},False)])
        for value in ["o1",Tagged("UserId","o1")]:
            self.assertReject("EVALUATION_TYPE_OR_RANGE",lambda:p.evaluate({"a":value}))
        currency=FieldType("Enum",name="Currency",members=("CNY","USD"))
        p=compile_predicate(call("Eq",field("c"),typed(currency,Tagged("Currency","CNY"))),{"c":currency})
        self.assertReject("EVALUATION_TYPE_OR_RANGE",lambda:p.evaluate({"c":Tagged("Currency","EUR")}))

    def test_conflicting_nominal_definitions_rejected(self):
        a=FieldType("Enum",name="Currency",members=("CNY",))
        b=FieldType("Enum",name="Currency",members=("USD",))
        self.assertReject("CONFLICTING_NOMINAL_TYPE",lambda:compile_predicate(const(True),{"a":a,"b":b}))

    def test_records_sets_and_input_shape(self):
        currency=FieldType("Enum",name="Currency",members=("CNY","USD"))
        row=FieldType("Record",fields=(("amount",FieldType("Int",0,3)),("currency",currency)))
        allowed=FieldType("Set",element=currency,max_size=1)
        p=compile_predicate(call("And",call("Lt",const(0),get(field("r"),"amount")),
                                 call("InSet",get(field("r"),"currency"),field("allowed"))),{"r":row,"allowed":allowed})
        cny=Tagged("Currency","CNY");usd=Tagged("Currency","USD")
        self.agree(p,[({"r":{"amount":1,"currency":cny},"allowed":frozenset([cny])},True),
                      ({"r":{"amount":0,"currency":cny},"allowed":frozenset([cny])},False),
                      ({"r":{"amount":1,"currency":usd},"allowed":frozenset([cny])},False)])
        for values in [{"r":{"amount":1,"currency":cny,"extra":True},"allowed":frozenset()},
                       {"r":{"amount":True,"currency":cny},"allowed":frozenset()},
                       {"r":{"amount":1,"currency":cny},"allowed":frozenset([cny,usd])}]:
            self.assertReject("EVALUATION_TYPE_OR_RANGE",lambda:p.evaluate(values))

    def test_option_guards_and_short_circuit_on_both_backends(self):
        schema={"x":FieldType("Option",element=FieldType("Int",0,3))}
        present=call("IsSome",field("x"));positive=call("Lt",const(0),call("Unwrap",field("x")))
        and_p=compile_predicate(call("And",present,positive),schema)
        self.agree(and_p,[({"x":None},False),({"x":Some(0)},False),({"x":Some(1)},True)])
        or_p=compile_predicate(call("Or",call("Not",present),positive),schema)
        self.agree(or_p,[({"x":None},True),({"x":Some(0)},False),({"x":Some(1)},True)])
        if_p=compile_predicate(call("If",present,positive,const(False)),schema)
        self.agree(if_p,[({"x":None},False),({"x":Some(1)},True)])
        self.assertReject("UNGUARDED_OPTION",lambda:compile_predicate(positive,schema))
        self.assertReject("UNGUARDED_OPTION",lambda:compile_predicate(call("Or",present,positive),schema))

    def test_option_guard_does_not_authorize_different_field(self):
        schema={k:FieldType("Option",element=FieldType("Int",0,3)) for k in ("x","y")}
        bad=call("And",call("IsSome",field("x")),call("Eq",call("Unwrap",field("y")),const(1)))
        self.assertReject("UNGUARDED_OPTION",lambda:compile_predicate(bad,schema))

    def test_nested_options_require_both_presence_guards(self):
        schema={"x":FieldType("Option",element=FieldType("Option",element=FieldType("Int",0,3)))}
        inner=call("Unwrap",field("x"));value=call("Unwrap",inner)
        expr=call("And",call("IsSome",field("x")),call("And",call("IsSome",inner),call("Eq",value,const(2))))
        p=compile_predicate(expr,schema)
        self.agree(p,[({"x":None},False),({"x":Some(None)},False),({"x":Some(Some(2))},True)])
        bad=call("And",call("IsSome",field("x")),call("Eq",value,const(2)))
        self.assertReject("UNGUARDED_OPTION",lambda:compile_predicate(bad,schema))

    def test_nested_option_guard_and_immutable_constant_snapshot(self):
        row=FieldType("Record",fields=(("o",FieldType("Option",element=FieldType("Int",0,3))),))
        expr=get(field("r"),"o")
        p=compile_predicate(call("And",call("IsSome",expr),call("Eq",call("Unwrap",expr),const(2))),{"r":row})
        self.agree(p,[({"r":{"o":None}},False),({"r":{"o":Some(2)}},True)])
        value={"o":Some(2)}
        p=compile_predicate(call("Eq",field("r"),typed(row,value)),{"r":row})
        before=p.semantic_digest;value["o"]=None
        self.assertEqual(p.semantic_digest,before);self.assertTrue(p.evaluate({"r":{"o":Some(2)}}))

    def test_set_serialization_is_order_independent_and_duplicate_free(self):
        t=FieldType("Set",element=FieldType("Int",0,3),max_size=2)
        p=compile_predicate(call("Eq",field("s"),typed(t,frozenset([2,1]))),{"s":t})
        self.assertEqual(load_predicate(p.to_json()).to_json(),p.to_json())
        self.assertReject("DUPLICATE_SET_ELEMENT",lambda:t.value_from_wire(["1","1"]))
        self.assertReject("INVALID_IR_INTEGER",lambda:t.value_from_wire(["01"]))

    def test_full_small_domain_nonvacuity_and_bytecode_equivalence(self):
        schema={"x":FieldType("Option",element=FieldType("Int",0,3))}
        p=compile_predicate(call("And",call("IsSome",field("x")),call("Lt",const(1),call("Unwrap",field("x")))),schema)
        reports=verify_finite(p)
        self.assertEqual([r["status"] for r in reports],["BoundedChecked","BoundedChecked"])
        self.assertEqual(reports[0]["checked_cases"],"5")
        self.assertIsNotNone(reports[1]["witness"]);self.assertIsNotNone(reports[1]["rejected_witness"])
        deny=compile_predicate(const(False),{"b":FieldType("Bool")})
        reports=verify_finite(deny)
        self.assertEqual(reports[1]["status"],"Violated")
        self.assertIsNone(reports[1]["witness"])

    def test_unenumerable_domain_is_unknown_without_sampling_promotion(self):
        reports=verify_finite(refund_predicate())
        self.assertTrue(all(r["status"]=="Unknown" and r["checked_cases"]=="0" for r in reports))
        self.assertTrue(all(r["solver_status"] is None for r in reports))
        small=compile_predicate(const(True),{"b":FieldType("Bool")})
        self.assertEqual(verify_finite(small,max_cases=1)[0]["status"],"Unknown")

    def test_structured_domain_exhaustive_cardinality(self):
        t=FieldType("Record",fields=(("s",FieldType("Set",element=FieldType("Bool"),max_size=1)),
                                     ("o",FieldType("Option",element=FieldType("Bool")))))
        p=compile_predicate(call("Eq",field("r"),field("r")),{"r":t})
        self.assertEqual(verify_finite(p)[0]["checked_cases"],"9")

    def test_runtime_results_do_not_imply_static_proof(self):
        p=refund_predicate()
        self.assertEqual(runtime_report(p,{"amount":1,"currency":"CNY"})["status"],"RuntimeSatisfied")
        self.assertEqual(runtime_report(p,{"amount":0,"currency":"CNY"})["status"],"Violated")
        result=runtime_report(p,{"amount":True,"currency":"CNY"})
        self.assertEqual(result["status"],"Unknown");self.assertFalse(result["allow"])

    def test_generated_checks_reject_tampering_and_wrong_ir(self):
        p=refund_predicate();original=compile_checks(p).to_json()
        obj=strict_loads(original);obj["instructions"][0]["value"]="-1"
        self.assertReject("CHECK_ARTIFACT_MISMATCH",lambda:load_checks(json.dumps(obj),p))
        other=compile_predicate(const(True),{"amount":FieldType("Int")})
        self.assertReject("CHECK_ARTIFACT_MISMATCH",lambda:load_checks(original,other))

    def test_diagnostics_retain_requirement_and_node(self):
        try: compile_predicate(call("And",const(True),field("missing")),{"x":FieldType("Bool")},"R13")
        except Diagnostic as exc:
            self.assertEqual((exc.code,exc.requirement_id,exc.node_id),("UNKNOWN_FIELD","R13","n0.1"))
        else: self.fail("missing field accepted")

    def test_permit_and_audit_bind_executed_ir(self):
        k,s,_=setup();op=prepare(k,authorize(k,s))
        self.assertEqual(op.ir_digest,k._checks.predicate.semantic_digest)
        self.assertEqual(dict(k.events()[0].details)["ir_digest"],op.ir_digest)
        k._predicate=compile_predicate(const(True),{"amount":FieldType("Int"),"currency":FieldType("String")})
        self.assertReject("STALE_PERMIT",lambda:k.dispatch("a",op.id,op.permit_ref))

    def test_schema_limits_and_invalid_shapes_fail_closed(self):
        invalid=[FieldType([]),FieldType("Enum",name="E",members=["x"]),FieldType("Set",element=FieldType("Bool"),max_size=65)]
        for t in invalid:
            self.assertReject("INVALID_FIELD_SCHEMA",lambda:compile_predicate(const(True),{"x":t}))
        t=FieldType("Bool")
        for _ in range(11): t=FieldType("Record",fields=(("a",t),("b",t)))
        self.assertReject("INVALID_FIELD_SCHEMA",lambda:compile_predicate(const(True),{"x":t}))

    def test_generated_arithmetic_truth_table_matches_reference(self):
        for operator in ("Add","Sub","MulConst"):
            left=const(2) if operator=="MulConst" else field("x")
            p=compile_predicate(call("Lt",call(operator,left,field("y")),const(2)),
                                {"x":FieldType("Int",-3,3),"y":FieldType("Int",-3,3)})
            report=verify_finite(p)
            self.assertEqual(report[0]["status"],"BoundedChecked")
            self.assertEqual(report[0]["checked_cases"],"49")

    def test_integer_interval_inference_includes_branch_and_large_results(self):
        big=10**18-1
        expr=call("If",field("b"),call("Add",field("x"),const(1)),call("MulConst",const(-2),field("x")))
        p=compile_predicate(call("Le",expr,const(big)),{"x":FieldType("Int",0,big),"b":FieldType("Bool")})
        derived=p.root.children[0].type
        self.assertEqual((derived.lower,derived.upper),(-2*big,big+1))
        self.agree(p,[({"x":big,"b":True},False),({"x":big,"b":False},True)])
