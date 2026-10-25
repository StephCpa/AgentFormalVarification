from pathlib import Path
import json
import unittest
from avcl.canonical import Reject
from avcl.contract import default_contract
from avcl.workflow import RefType,compile_workflow,load_workflow,LEGACY_FORMAT,TASK_FORMAT,VALIDATION_FORMAT
from avcl.workflow_runtime import WorkflowExecutor
from tests.support import setup
from tests.test_delegation import provision,payload,authorization,prepare
from tests.test_workflow import REFS,VALUES,EFFECTS,P,D,branch

def command(child="child",out="child_cap",**changes):
    spec=json.loads(payload(**changes))["attenuations"]
    return {"tag":"Delegate","capability":"c","child_subject":child,"attenuations":spec,"out":out}

def flow(body=None,refs=None,effects=EFFECTS,contract=None,version="avcl/workflow/0.7"):
    return compile_workflow("delegate-flow",{"c":RefType("Capability","refund")} if refs is None else refs,
                            VALUES,effects,[command()] if body is None else body,contract or default_contract(),format_version=version)

class WorkflowDelegateTests(unittest.TestCase):
    def error(self,code,fn):
        with self.assertRaises(Reject) as caught:fn()
        self.assertEqual(caught.exception.code,code)

    def test_child_capability_cannot_be_used_as_parent_authority(self):
        program=flow();self.assertEqual(dict(program.remaining)["child_cap"],RefType("ChildCapability","refund"))
        self.assertIn("c",dict(program.remaining))
        for tail in ({**P,"capability":"child_cap"},{**D,"permit":"child_cap"},{**command(out="again"),"capability":"child_cap"}):
            self.error("E-TYPE-101",lambda:flow([command(),tail],refs=REFS))
        self.error("E-TYPE-101",lambda:flow([{**command(),"capability":"r"}],refs=REFS))
        self.error("INVALID_WORKFLOW_REFERENCE_TYPE",lambda:flow([],refs={"forged":RefType("ChildCapability","refund")}))

    def test_child_ref_is_reusable_data_but_not_movable_affine_authority(self):
        program=flow([command(),{"tag":"Alias","ref":"child_cap","out":"copy"},{"tag":"Drop","ref":"copy"}])
        self.assertEqual(dict(program.remaining)["child_cap"],RefType("ChildCapability","refund"))
        self.error("MOVE_REQUIRES_AFFINE_VALUE",lambda:flow([command(),{"tag":"Move","ref":"child_cap","out":"moved"}]))

    def test_exact_shape_and_fixed_tool_profile(self):
        self.error("INVALID_WORKFLOW_COMMAND",lambda:flow([{"tag":"Delegate"}]))
        self.error("INVALID_WORKFLOW_COMMAND",lambda:flow([{**command(),"tenant":"other"}]))
        self.error("INVALID_DELEGATION",lambda:flow([command(orders=["a","a"])]))
        self.error("UNSUPPORTED_WORKFLOW_DELEGATION",lambda:flow([command(tools=["shell"])]))
        self.error("UNSUPPORTED_WORKFLOW_DELEGATION",lambda:flow([command(effects=["InvokeModel"])]))
        self.error("INVALID_DELEGATION",lambda:flow([{**command(),"child_subject":{}}]))

    def test_branch_effects_and_definite_child_reference(self):
        self.error("E-EFFECT-301",lambda:flow([branch([command()],[])],effects=("Read",)))
        self.error("E-AFFINE-201",lambda:flow([branch([command()],[]),{"tag":"Alias","ref":"child_cap","out":"copy"}]))
        program=flow([branch([command()],[command(child="sibling")]),{"tag":"Alias","ref":"child_cap","out":"copy"}])
        self.assertEqual(set(program.inferred_effects),{"Read","ResourceConsume"})
        self.assertEqual(program.report()["backend_requirements"],["registered-delegation"])

    def test_nested_attenuations_are_frozen_in_compiled_ir(self):
        raw=command();program=flow([raw]);before=program.to_json()
        raw["attenuations"]["orders"].append("unapproved")
        self.assertEqual(program.to_json(),before)
        serialized=program.semantic();serialized["body"][0]["attenuations"]["limit_minor"]="999"
        self.assertEqual(program.to_json(),before)
        self.assertEqual(load_workflow(before,default_contract()),program)

    def test_old_versions_reject_delegate_and_frozen_v06_is_unchanged(self):
        for version in (LEGACY_FORMAT,TASK_FORMAT,VALIDATION_FORMAT):
            self.error("UNSUPPORTED_WORKFLOW_COMMAND",lambda:flow(version=version))
        raw=(Path(__file__).parent/"fixtures"/"workflow-v06.ir.json").read_text(encoding="utf-8")
        program=load_workflow(raw,default_contract())
        self.assertEqual(program.to_json(),raw);self.assertEqual(program.format_version,VALIDATION_FORMAT)
        self.assertEqual(program.semantic_digest,"sha256:7a02734a9654b2ffe9eb208ecaf3cb5a4c50e9aeaf4f375bf8c2112fdb126a76")

    def test_memory_delegate_is_once_per_node_and_child_can_act(self):
        k,s,_=setup();provision(k);executor=WorkflowExecutor(k,s);program=flow()
        result=executor.run(program,"parent","run",{"c":"root"},{"flag":True})
        self.assertEqual(result.status,"Finished");step=dict(result.steps[0]);self.assertEqual(step["result"],"Delegated")
        self.assertEqual(executor.run(program,"parent","run",{"c":"root"},{"flag":True}),result)
        self.assertEqual(WorkflowExecutor(k,s).run(program,"parent","run",{"c":"root"},{"flag":True}),result)
        self.assertEqual(sum(e.kind=="CapabilityDelegated" for e in k.events()),1)
        op=prepare(k,authorization(k,s,step["child_cap_ref"]))
        self.assertEqual(op.request.subject,"child");self.assertEqual(k.accounts()["tenant:CNY"].pending,60)

    def test_memory_rejects_runtime_amplification_without_issuing_child(self):
        k,s,_=setup();provision(k)
        program=flow([command(limit_minor="101")])
        result=WorkflowExecutor(k,s).run(program,"parent","run",{"c":"root"},{"flag":True})
        self.assertEqual((result.status,result.error,result.failed_node),("Halted","BUDGET_EXCEEDED","w0.0"))
        self.assertEqual(len(k._grants),1)

    def test_branch_runs_only_selected_delegation_and_drop_does_not_revoke(self):
        k,s,_=setup();provision(k)
        program=flow([branch([command()],[command(child="sibling")]),{"tag":"Drop","ref":"child_cap"}])
        result=WorkflowExecutor(k,s).run(program,"parent","run",{"c":"root"},{"flag":False})
        issued=[dict(step) for step in result.steps if dict(step)["command"]=="Delegate"]
        self.assertEqual(len(issued),1);self.assertEqual(issued[0]["child_subject"],"sibling")
        self.assertFalse(k._grants[issued[0]["child_cap_ref"]].revoked)

if __name__=="__main__":unittest.main()
