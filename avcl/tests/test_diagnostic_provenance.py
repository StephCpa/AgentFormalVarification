"""Error provenance survives loader/compiler boundaries and malformed metadata."""
import json
import unittest
from dataclasses import replace

from avcl.canonical import Reject
from avcl.checks import compile_checks,load_checks
from avcl.contract import default_contract,build_contract,load_contract
from avcl.diagnostics import annotate,describe
from avcl.ir import compile_predicate,load_predicate,FieldType
from avcl.workflow import load_workflow
from .test_encoding_ir import const,field,call
from .test_workflow import make,branch,P,D


class DiagnosticProvenanceTests(unittest.TestCase):
    def capture(self,fn):
        with self.assertRaises(Reject) as caught:fn()
        return describe(caught.exception)

    def predicate(self):
        return compile_predicate(call("Eq",field("x"),const(1)),
                                 {"x":FieldType("Int")},"LOCAL_RULE",source="policy.avcl:7")

    def test_loaded_constant_and_node_id_keep_expected_location(self):
        for mutation in ("constant","node"):
            obj=json.loads(self.predicate().to_json())
            child=obj["root"]["args"][1]
            if mutation=="constant":child["value"]="bad-int"
            else:child["node_id"]="n0.999"
            d=self.capture(lambda:load_predicate(json.dumps(obj)))
            self.assertEqual(d["node_id"],"n0.1")
            self.assertEqual(d["location"],"/root/args/1")
            self.assertEqual(d["source_ref"],"policy.avcl:7")
            self.assertEqual(d["component"],"predicate")
            self.assertIn("LOCAL_RULE",d["requirement_ids"])

    def test_invalid_json_and_field_have_honest_coarse_locations(self):
        d=self.capture(lambda:load_predicate("{",source="input.json"))
        self.assertEqual((d["source_ref"],d["location"],d["node_id"]),("input.json","/",None))
        obj=json.loads(self.predicate().to_json());obj["fields"]["x"]={"kind":"NotAType"}
        d=self.capture(lambda:load_predicate(json.dumps(obj),source="input.json"))
        self.assertEqual(d["location"],"/fields/x")
        self.assertIn("LOCAL_RULE",d["requirement_ids"])

    def test_checks_tampering_keeps_artifact_and_source_requirements(self):
        p=self.predicate();obj=json.loads(compile_checks(p).to_json());obj["instructions"]=[]
        d=self.capture(lambda:load_checks(json.dumps(obj),p))
        self.assertEqual(d["code"],"CHECK_ARTIFACT_MISMATCH")
        self.assertTrue({"R13","LOCAL_RULE"}<=set(d["requirement_ids"]))
        self.assertEqual((d["component"],d["source_ref"]),("checks","policy.avcl:7"))
        self.assertEqual(self.capture(lambda:load_checks("{",p))["location"],"/")

    def test_workflow_code_and_tag_requirements_are_merged(self):
        d=self.capture(lambda:make([P,{**P,"out":"second"}]))
        self.assertEqual(d["code"],"E-AFFINE-201")
        self.assertTrue({"R08","R01","R02","R04"}<=set(d["requirement_ids"]))
        obj=json.loads(make([P,D]).to_json());obj["body"][1]["permit"]="missing"
        obj["source_map"]["commands"]["w0.1"]="flow.avcl:20"
        d=self.capture(lambda:load_workflow(json.dumps(obj),default_contract()))
        self.assertEqual(d["code"],"UNKNOWN_WORKFLOW_VARIABLE")
        self.assertEqual((d["node_id"],d["location"],d["source_ref"]),("w0.1","/body/1","flow.avcl:20"))

    def test_nested_predicate_is_not_relabelled_as_workflow(self):
        obj=json.loads(make([branch([],[])]).to_json())
        obj["body"][0]["condition"]["root"]["name"]="missing"
        obj["source_map"]["conditions"]["w0.0"]["n0"]="condition.avcl:9"
        d=self.capture(lambda:load_workflow(json.dumps(obj),default_contract()))
        self.assertEqual((d["component"],d["source_ref"],d["location"]),
                         ("predicate","condition.avcl:9","/root"))

    def test_contract_clause_context_and_malformed_requirement_container(self):
        base=default_contract();c=replace(base.clauses[0],kind="unsupported",requirement_ids=("CUSTOM",))
        def build(clause):
            return build_contract(base.module_id,base.policy_version,base.tools,base.validators,
                                  base.assumptions,(clause,)+base.clauses[1:])
        d=self.capture(lambda:build(c))
        self.assertEqual((d["clause_id"],d["location"],d["component"]),(c.id,"/clauses/0","contract"))
        self.assertIn("CUSTOM",d["requirement_ids"])
        d=self.capture(lambda:build(replace(c,requirement_ids="CUSTOM")))
        self.assertNotIn("C",d["requirement_ids"])
        obj=json.loads(base.to_json());obj["clauses"][0]["events"]=None
        d=self.capture(lambda:load_contract(json.dumps(obj),source="contract.json"))
        self.assertEqual((d["clause_id"],d["source_ref"]),(base.clauses[0].id,"contract.json"))

    def test_contract_preserves_inner_predicate_location(self):
        base=default_contract();obj=json.loads(base.to_json());c=obj["clauses"][0]
        c["predicate"]["root"]["node_id"]="bad"
        obj["source_map"][c["id"]]["n0"]="clause.avcl:3"
        d=self.capture(lambda:load_contract(json.dumps(obj)))
        self.assertEqual((d["clause_id"],d["component"],d["source_ref"]),(c["id"],"predicate","clause.avcl:3"))

    def test_untrusted_metadata_is_bounded_and_detail_not_exported(self):
        exc=Reject("INVALID_AST");exc.detail="secret-token"
        annotate(exc,source_ref="x"*257,node_id="\ud800",requirement_ids=({},None,"RULE"))
        d=describe(exc)
        self.assertIsNone(d["source_ref"]);self.assertIsNone(d["node_id"])
        self.assertNotIn("secret-token",json.dumps(d))
        obj=json.loads(self.predicate().to_json());obj["requirement_id"]={"unexpected":True}
        d=self.capture(lambda:load_predicate(json.dumps(obj)))
        self.assertIn("R09",d["requirement_ids"])


if __name__=="__main__":unittest.main()
