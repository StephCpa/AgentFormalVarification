from dataclasses import replace
from pathlib import Path
import json
import unittest
from avcl.canonical import Reject
from avcl.contract import default_contract,Validator
from avcl.workflow import RefType,compile_workflow,load_workflow,LEGACY_FORMAT,TASK_FORMAT
from avcl.workflow_runtime import WorkflowExecutor
from avcl.types import Request
from tests.support import setup
from tests.test_workflow import P,D,S,branch,VALUES,EFFECTS

V={"tag":"Validate","candidate":"r","context":"ctx","validator":"order-service","out":"e"}
VALIDATE_REFS={"r":RefType("Candidate","refund"),"c":RefType("Capability","refund"),
               "a":RefType("Approval","refund"),"ctx":RefType("Context","refund")}

def flow(body,contract=None,refs=None,effects=EFFECTS,version="avcl/workflow/0.6"):
    return compile_workflow("validate-flow",VALIDATE_REFS if refs is None else refs,VALUES,effects,body,
                            contract or default_contract(),format_version=version)

class WorkflowValidateTests(unittest.TestCase):
    def error(self,code,fn):
        with self.assertRaises(Reject) as caught:fn()
        self.assertEqual(caught.exception.code,code)

    def test_validate_produces_evidence_used_by_prepare(self):
        program=flow([V,P,D,S]);self.assertEqual(load_workflow(program.to_json(),default_contract()),program)
        self.assertEqual(dict(program.remaining)["e"],RefType("Evidence","refund"))
        self.assertEqual(program.report()["backend_requirements"],["durable-validation"])

    def test_evidence_does_not_become_capability_approval_or_context(self):
        for command in ({**P,"capability":"e"},{**P,"approval":"e"},{**V,"context":"e","out":"other"}):
            self.error("E-TYPE-101",lambda:flow([V,command]))
        self.error("E-TYPE-101",lambda:flow([{**V,"context":"c"}]))
        self.error("E-TYPE-101",lambda:flow([{**V,"candidate":"ctx"}]))

    def test_validator_must_be_registered_for_exact_predicate_and_version(self):
        for validator in ("arbitrary-code","structural-only",True,{}):
            self.error("UNSUPPORTED_WORKFLOW_VALIDATOR",lambda:flow([{**V,"validator":validator}]))
        for declaration in (Validator("order-service","StructureOK","sim/0.1"),Validator("order-service","RefundEligible","other")):
            self.error("UNSUPPORTED_WORKFLOW_VALIDATOR",lambda:flow([V],replace(default_contract(),validators=(declaration,))))

    def test_validation_effects_and_branch_evidence_are_conservative(self):
        self.error("E-EFFECT-301",lambda:flow([branch([V],[])],effects=("Read",)))
        self.error("E-AFFINE-201",lambda:flow([branch([V],[]),P]))
        program=flow([branch([V],[V]),P]);self.assertIn("e",program.body[0].survivors)

    def test_old_formats_cannot_accept_context_or_validate(self):
        for version in (LEGACY_FORMAT,TASK_FORMAT):
            self.error("INVALID_WORKFLOW_REFERENCE_TYPE",lambda:flow([V],version=version))
            self.error("UNSUPPORTED_WORKFLOW_COMMAND",lambda:flow([V],refs={"r":VALIDATE_REFS["r"]},version=version))
        obj=json.loads(flow([V]).to_json());obj["body"][0]["allow"]=True
        self.error("INVALID_WORKFLOW_COMMAND",lambda:load_workflow(json.dumps(obj),default_contract()))

    def test_frozen_v05_artifact_keeps_bytes_and_digest(self):
        raw=(Path(__file__).parent/"fixtures"/"workflow-v05.ir.json").read_text(encoding="utf-8")
        program=load_workflow(raw,default_contract())
        self.assertEqual(program.to_json(),raw);self.assertEqual(program.format_version,TASK_FORMAT)
        self.assertEqual(program.semantic_digest,"sha256:ee0b0af2404b4ffeba60d0e319503805fffaf0f7163c0ee48a5135e0ccb4e5ca")

    def test_memory_executor_rejects_context_backend_before_any_effect(self):
        kernel,service,_=setup();request=Request("demo","a","task-a","case-a","a","v1","original-a","70","0")
        self.error("WORKFLOW_REQUIRES_DURABLE_VALIDATION_BACKEND",lambda:WorkflowExecutor(kernel,service).run(
            flow([V,P,D,S]),"a","run",{"r":request,"c":"cap-a","a":"approval","ctx":"context"},{"flag":True}))
        self.assertEqual(kernel.accounts()["tenant:CNY"].pending,0);self.assertEqual(service.effects,())

if __name__=="__main__":unittest.main()
