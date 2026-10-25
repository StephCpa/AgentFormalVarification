import unittest
from avcl.canonical import Reject, amount, canonical_bytes, digest, strict_loads
from avcl.ir import FieldType, compile_predicate, refund_predicate


def const(v): return {"tag": "Const", "value": v}
def field(n): return {"tag": "Field", "name": n}
def call(tag, *args): return {"tag": tag, "args": list(args)}


class EncodingTests(unittest.TestCase):
    def test_canonical_binding_independent_of_dictionary_order(self):
        self.assertEqual(digest({"b": "退款", "a": "10"}), digest({"a": "10", "b": "退款"}))
        self.assertNotEqual(digest({"a": "10"}), digest({"a": "10"}, "avcl/evidence/0.1"))
        self.assertNotEqual(digest({"a": "10"}), digest({"a": "11"}))

    def test_decimal_boundary_is_unambiguous(self):
        self.assertEqual(amount("999999999999999999"), 10**18-1)
        for v in [True, 1, 1.0, "01", "-1", "+1", "1e2", "1.0", " 1", "1\n", "１", "1"*19]:
            with self.subTest(value=v), self.assertRaises(Reject): amount(v)

    def test_json_rejects_duplicate_keys_numbers_and_extensions(self):
        for text in ['{"a":"x","a":"y"}', '{"a":1}', '{"a":1.5}', '{"a":NaN}',
                     '{"a":Infinity}', '{"a":null,}', '[] true', '{"中文":"x"}']:
            with self.subTest(text=text), self.assertRaises(Reject): strict_loads(text)
        self.assertEqual(strict_loads('{"a":"1","b":true}'), {"a":"1","b":True})

    def test_noncanonical_objects_unicode_and_resource_limits(self):
        for value in [1, 1.5, ("a",), {1:"x"}, "\ud800", "x"*65537]:
            with self.subTest(type=type(value)), self.assertRaises(Reject): canonical_bytes(value)
        cyclic=[]; cyclic.append(cyclic)
        with self.assertRaises(Reject): canonical_bytes(cyclic)
        with self.assertRaises(Reject): strict_loads("["*1100 + "]"*1100)


class PredicateTests(unittest.TestCase):
    fields={"x":FieldType("Int",0,100),"flag":FieldType("Bool"),"s":FieldType("String")}

    def compile(self, root): return compile_predicate(root,self.fields)

    def test_T09_effectful_and_nonlinear_nodes_rejected(self):
        for root in [call("ToolCall",const("refund")),call("Eval",const("True")),
                     call("Le",call("MulConst",field("x"),field("x")),const(100))]:
            with self.subTest(root=root), self.assertRaises(Reject): self.compile(root)

    def test_invalid_dead_branch_is_checked_before_evaluation(self):
        for root in [call("Or",const(True),field("missing")),
                     call("And",const(False),call("ToolCall",const("refund")))]:
            with self.assertRaises(Reject): self.compile(root)

    def test_types_boolean_integer_and_input_range_are_distinct(self):
        with self.assertRaises(Reject): self.compile(call("Eq",field("x"),const(True)))
        p=self.compile(call("Le",field("x"),const(50)))
        for value in [True,-1,101,1.0,"1"]:
            with self.assertRaises(Reject): p.evaluate({"x":value,"flag":True,"s":"ok"})
        with self.assertRaises(Reject): p.evaluate({"x":1,"flag":True,"s":"ok","extra":0})

    def test_total_scalar_arithmetic_and_boolean_logic(self):
        p=self.compile(call("And",call("Not",field("flag")),call("Eq",
                            call("Sub",call("MulConst",const(2),field("x")),const(1)),const(9))))
        self.assertTrue(p.evaluate({"x":5,"flag":False,"s":"ok"}))
        self.assertFalse(p.evaluate({"x":4,"flag":False,"s":"ok"}))
        p=self.compile(call("Eq",call("Add",field("x"),const(2)),const(7)))
        self.assertTrue(p.evaluate({"x":5,"flag":True,"s":"ok"}))

    def test_ast_limits_and_strict_shape(self):
        root=const(True)
        for _ in range(66): root=call("Not",root)
        with self.assertRaises(Reject): self.compile(root)
        root=const(True)
        for _ in range(10): root=call("And",root,root)
        with self.assertRaises(Reject): self.compile(root)
        for root in [const(1),{"tag":"Const","value":True,"extra":1},call("And",const(True))]:
            with self.assertRaises(Reject): self.compile(root)

    def test_refund_predicate_truth_values(self):
        p=refund_predicate()
        self.assertTrue(p.evaluate({"amount":1,"currency":"CNY"}))
        self.assertFalse(p.evaluate({"amount":0,"currency":"CNY"}))
        self.assertFalse(p.evaluate({"amount":1,"currency":"USD"}))
