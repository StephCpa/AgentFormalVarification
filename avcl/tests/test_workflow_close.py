from pathlib import Path
import json
import unittest
from avcl.canonical import Reject
from avcl.contract import default_contract
from avcl.workflow import RefType,compile_workflow,load_workflow,LEGACY_FORMAT
from avcl.workflow_runtime import WorkflowExecutor
from tests.support import setup,authorize
from tests.test_workflow import REFS,VALUES,EFFECTS,P,D,S,branch

C={"tag":"Close","task":"task","out":"closed"}

def flow(body,contract=None,refs=None,effects=EFFECTS,version="avcl/workflow/0.5"):
    return compile_workflow("close-flow",{**REFS,"task":RefType("Task","refund")} if refs is None else refs,
                            VALUES,effects,body,contract or default_contract(),format_version=version)

class WorkflowCloseTests(unittest.TestCase):
    def error(self,code,fn):
        with self.assertRaises(Reject) as caught:fn()
        self.assertEqual(caught.exception.code,code)

    def test_task_and_report_have_distinct_reusable_types(self):
        program=flow([C,{"tag":"Alias","ref":"closed","out":"copy"},{"tag":"Drop","ref":"copy"}])
        self.assertEqual(dict(program.remaining)["closed"],RefType("TaskReport","refund"))
        self.assertEqual(program.report()["backend_requirements"],["durable-task-close"])
        self.assertEqual(set(program.inferred_effects),{"Read","ResourceConsume"})
        self.assertEqual(load_workflow(program.to_json(),default_contract()),program)

    def test_task_report_cannot_be_permission_evidence_or_task(self):
        for tail in ({**D,"permit":"closed"},{**P,"evidence":"closed"},{**C,"task":"closed","out":"other"}):
            self.error("E-TYPE-101",lambda:flow([C,tail]))
        self.error("E-TYPE-101",lambda:flow([{**C,"task":"r"}]))
        self.error("INVALID_WORKFLOW_REFERENCE_TYPE",lambda:flow([],refs={"forged":RefType("TaskReport","refund")}))

    def test_effects_include_close_in_unselected_branch(self):
        self.error("E-EFFECT-301",lambda:flow([branch([C],[])],effects=("Read",)))
        self.error("WORKFLOW_VARIABLE_REBOUND",lambda:flow([C,C]))

    def test_report_branch_join_requires_both_paths(self):
        program=flow([branch([C],[C]),{"tag":"Alias","ref":"closed","out":"copy"}])
        self.assertIn("closed",program.body[0].survivors)
        self.error("E-AFFINE-201",lambda:flow([branch([C],[]),{"tag":"Alias","ref":"closed","out":"copy"}]))

    def test_legacy_frozen_artifact_preserves_bytes_and_digest(self):
        raw=(Path(__file__).parent/"fixtures"/"workflow-v04.ir.json").read_text(encoding="utf-8")
        program=load_workflow(raw,default_contract())
        self.assertEqual(program.format_version,LEGACY_FORMAT)
        self.assertEqual(program.to_json(),raw)
        self.assertEqual(program.semantic_digest,"sha256:0495bb352eef65d36232a7afee28a35b294dd4f336b9abd53996c41b410425da")

    def test_legacy_documents_cannot_smuggle_new_commands_or_types(self):
        self.error("INVALID_WORKFLOW_REFERENCE_TYPE",lambda:flow([C],version=LEGACY_FORMAT))
        self.error("UNSUPPORTED_WORKFLOW_COMMAND",lambda:flow([C],refs=REFS,version=LEGACY_FORMAT))
        obj=json.loads(flow([C]).to_json());obj["format"]={}
        self.error("UNSUPPORTED_WORKFLOW_VERSION",lambda:load_workflow(json.dumps(obj),default_contract()))

    def test_memory_backend_rejects_required_task_backend_before_any_effect(self):
        kernel,service,_=setup();request,cap,evidence,approval=authorize(kernel,service)
        self.error("WORKFLOW_REQUIRES_DURABLE_TASK_BACKEND",lambda:WorkflowExecutor(kernel,service).run(
            flow([P,D,S,C]),"a","run",{"r":request,"c":cap,"e":evidence,"a":approval,"task":"forged"},{"flag":True}))
        self.assertEqual(kernel.accounts()["tenant:CNY"].pending,0)
        self.assertEqual(service.effects,())

if __name__=="__main__":unittest.main()
