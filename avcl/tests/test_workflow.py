import json
import unittest
from dataclasses import replace
from itertools import product
from avcl.canonical import Reject
from avcl.contract import default_contract,build_contract
from avcl.ir import FieldType,compile_predicate
from avcl.types import Execution
from avcl.workflow import RefType,compile_workflow,load_workflow,WorkflowDiagnostic
from avcl.workflow_runtime import WorkflowExecutor,FaultPlan
from avcl.simulator import RefundService,Order
from .support import setup,authorize
from .test_encoding_ir import const,field

REFS={"r":RefType("Candidate","refund"),"c":RefType("Capability","refund"),
      "e":RefType("Evidence","refund"),"a":RefType("Approval","refund")}
VALUES={"flag":FieldType("Bool")}
EFFECTS=("Read","Write","Disclose","ResourceConsume")
P={"tag":"Prepare","candidate":"r","capability":"c","evidence":"e","approval":"a","out":"p"}
D={"tag":"Dispatch","permit":"p","out":"op"}
S={"tag":"Send","operation":"op"}
Q={"tag":"Reconcile","operation":"op"}

def branch(yes,no,condition=None):
    if condition is None: condition=compile_predicate(field("flag"),VALUES)
    return {"tag":"If","condition":condition,"then":yes,"otherwise":no}

def make(body,effects=EFFECTS,refs=None,contract=None):
    return compile_workflow("refund-flow",REFS if refs is None else refs,VALUES,effects,body,contract or default_contract())

def context(k,s):
    request,cap,evidence,approval=authorize(k,s)
    return {"r":request,"c":cap,"e":evidence,"a":approval}

class WorkflowTests(unittest.TestCase):
    def assertCode(self,code,fn):
        with self.assertRaises(Reject) as cm: fn()
        self.assertEqual(cm.exception.code,code)

    def test_affine_double_use_copy_and_rebinding_rejected(self):
        for body in [[P,D,{**D,"out":"second"}],
                     [P,{"tag":"Alias","ref":"p","out":"copy"}],
                     [{"tag":"Alias","ref":"a","out":"copy"}],
                     [P,{"tag":"Drop","ref":"p"},D]]:
            self.assertCode("E-AFFINE-201",lambda:make(body))
        self.assertCode("WORKFLOW_VARIABLE_REBOUND",lambda:make([P,{"tag":"Move","ref":"p","out":"p"}]))

    def test_candidate_and_operation_cannot_be_authority_or_permit(self):
        self.assertCode("E-TYPE-101",lambda:make([{**P,"evidence":"r"}]))
        self.assertCode("E-TYPE-101",lambda:make([P,D,{"tag":"Dispatch","permit":"op","out":"again"}]))
        self.assertCode("E-TYPE-101",lambda:make([P,{"tag":"Send","operation":"p"}]))
        refs={**REFS,"forged":RefType("Permit","refund")}
        self.assertCode("INVALID_WORKFLOW_REFERENCE_TYPE",lambda:make([],refs=refs))

    def test_approval_is_affine_capability_and_evidence_are_reusable(self):
        self.assertCode("E-AFFINE-201",lambda:make([P,{**P,"out":"p2"}]))
        flow=make([{"tag":"Alias","ref":"c","out":"capcopy"},{**P,"capability":"capcopy"},D,S])
        k,s,_=setup();result=WorkflowExecutor(k,s).run(flow,"a","run1",context(k,s),{"flag":True})
        self.assertEqual(result.status,"Finished");self.assertEqual(s.real_cost,70);k.assert_invariants()

    def test_move_transfers_the_only_live_permit_name(self):
        flow=make([P,{"tag":"Move","ref":"p","out":"q"},{**D,"permit":"q"},S])
        k,s,_=setup();result=WorkflowExecutor(k,s).run(flow,"a","run1",context(k,s),{"flag":False})
        self.assertEqual(result.status,"Finished");self.assertEqual(len(s.effects),1)
        self.assertCode("E-AFFINE-201",lambda:make([P,{"tag":"Move","ref":"p","out":"q"},D]))

    def test_join_does_not_resurrect_resource_consumed_in_one_branch(self):
        self.assertCode("E-AFFINE-201",lambda:make([P,branch([D],[]),D]))
        for flag in (True,False):
            flow=make([P,branch([{"tag":"Drop","ref":"p"}],[])])
            k,s,_=setup();result=WorkflowExecutor(k,s).run(flow,"a","run1",context(k,s),{"flag":flag})
            self.assertEqual(result.status,"Finished")
            self.assertEqual(k.accounts()["tenant:CNY"].pending,70)
            self.assertEqual(s.real_cost,0);k.assert_invariants()

    def test_both_branch_local_preparations_join_without_duplicate_execution(self):
        flow=make([branch([P],[P]),D,S])
        self.assertIn("p",flow.body[0].survivors)
        for flag in (True,False):
            k,s,_=setup();result=WorkflowExecutor(k,s).run(flow,"a","run1",context(k,s),{"flag":flag})
            self.assertEqual(result.status,"Finished")
            self.assertEqual(sum(e.kind=="Prepared" for e in k.events()),1)
            self.assertEqual(len(s.effects),1);k.assert_invariants()

    def test_effect_union_checks_dead_branches_too(self):
        condition=compile_predicate(const(False),VALUES)
        body=[P,D,branch([S],[],condition)]
        self.assertCode("E-EFFECT-301",lambda:make(body,effects=("ResourceConsume",)))
        flow=make(body)
        self.assertEqual(set(flow.inferred_effects),set(EFFECTS))
        k,s,_=setup();result=WorkflowExecutor(k,s).run(flow,"a","run1",context(k,s),{"flag":False})
        self.assertEqual(result.status,"Finished");self.assertEqual(s.effects,())
        self.assertEqual(k.accounts()["tenant:CNY"].pending,70)

    def test_reconcile_declares_read_disclosure_and_resource_effects(self):
        self.assertCode("E-EFFECT-301",lambda:make([P,D,Q],effects=("ResourceConsume",)))
        flow=make([P,D,Q])
        self.assertEqual(set(flow.inferred_effects),{"Read","Disclose","ResourceConsume"})

    def test_discard_is_not_abort_and_finished_is_not_business_success(self):
        k,s,_=setup();flow=make([P,{"tag":"Drop","ref":"p"}])
        result=WorkflowExecutor(k,s).run(flow,"a","run1",context(k,s),{"flag":True})
        report=result.report(k,"a")
        self.assertEqual(report["status"],"Finished")
        self.assertEqual(report["operations"][0]["execution"],"Prepared")
        self.assertEqual(k.accounts()["tenant:CNY"].pending,70)
        k.abort_prepared("a",result.operation_ids[0]);self.assertEqual(k.accounts()["tenant:CNY"].pending,0)

    def test_explicit_abort_consumes_permit_and_releases_only_prepared(self):
        flow=make([P,{"tag":"Abort","permit":"p"}]);k,s,_=setup()
        result=WorkflowExecutor(k,s).run(flow,"a","run1",context(k,s),{"flag":True})
        self.assertEqual(result.status,"Finished");self.assertEqual(k.accounts()["tenant:CNY"].pending,0)
        self.assertEqual(k.get("a",result.operation_ids[0]).state,Execution.ABORTED)
        self.assertCode("E-AFFINE-201",lambda:make([P,D,{"tag":"Abort","permit":"p"}]))

    def test_send_admission_is_distinct_from_dispatch_and_remote_effect(self):
        k,s,_=setup();result=WorkflowExecutor(k,s).run(make([P,D]),"a","run1",context(k,s),{"flag":True})
        self.assertEqual(result.status,"Finished");self.assertEqual(s.effects,())
        self.assertEqual(k.get("a",result.operation_ids[0]).state,Execution.DISPATCHED)
        self.assertFalse(any(e.kind=="SendAttemptAdmitted" for e in k.events()))

    def test_lost_receipt_reconciles_same_operation_once(self):
        k,s,_=setup();flow=make([P,D,S,Q])
        result=WorkflowExecutor(k,s).run(flow,"a","run1",context(k,s),{"flag":True},FaultPlan(drop_receipt_at=frozenset(["w0.2"])))
        self.assertEqual(result.status,"Finished");self.assertEqual(len(s.effects),1)
        self.assertEqual(k.accounts()["tenant:CNY"].spent,70);self.assertEqual(k.accounts()["tenant:CNY"].pending,0)
        self.assertTrue(any(e.kind=="Unknown" for e in k.events()));k.assert_invariants()

    def test_absent_remote_record_preserves_unknown_exposure(self):
        k,s,_=setup();flow=make([P,D,S,Q])
        result=WorkflowExecutor(k,s).run(flow,"a","run1",context(k,s),{"flag":True},FaultPlan(no_send_at=frozenset(["w0.2"])))
        self.assertEqual(result.status,"Finished");self.assertEqual(s.effects,())
        self.assertEqual(k.get("a",result.operation_ids[0]).state,Execution.UNKNOWN)
        self.assertEqual(k.accounts()["tenant:CNY"].pending,70);k.assert_invariants()

    def test_operation_retries_do_not_reconsume_permit_or_duplicate_remote_effect(self):
        k,s,_=setup();flow=make([P,D,S,S,Q])
        result=WorkflowExecutor(k,s).run(flow,"a","run1",context(k,s),{"flag":True})
        self.assertEqual(result.status,"Finished");self.assertEqual(len(s.effects),1)
        self.assertEqual(sum(e.kind=="DispatchAdmitted" for e in k.events()),1)
        self.assertEqual(sum(e.kind=="SendAttemptAdmitted" for e in k.events()),2);k.assert_invariants()

    def test_run_idempotency_does_not_restart_a_finished_or_halted_program(self):
        for body in ([P,D,S],[P,{"tag":"Drop","ref":"p"}]):
            k,s,_=setup();engine=WorkflowExecutor(k,s);refs=context(k,s);flow=make(body)
            first=engine.run(flow,"a","run1",refs,{"flag":True});events=k.events()
            self.assertEqual(engine.run(flow,"a","run1",refs,{"flag":True}),first)
            self.assertEqual(k.events(),events)
            self.assertCode("WORKFLOW_RUN_CONFLICT",lambda:engine.run(flow,"a","run1",refs,{"flag":False}))

    def test_duplicate_actual_approval_references_rejected_before_any_effect(self):
        refs={**REFS,"a2":RefType("Approval","refund")};flow=make([P],refs=refs)
        k,s,_=setup();inputs=context(k,s);inputs["a2"]=inputs["a"]
        self.assertCode("WORKFLOW_AFFINE_INPUT_ALIAS",lambda:WorkflowExecutor(k,s).run(flow,"a","run1",inputs,{"flag":True}))
        self.assertEqual(k.accounts()["tenant:CNY"].pending,0);self.assertEqual(k.events(),())

    def test_same_business_operation_cannot_create_two_runtime_permit_handles(self):
        from avcl.types import Approval
        refs={**REFS,"a2":RefType("Approval","refund")};flow=make([P,{**P,"approval":"a2","out":"q"}],refs=refs)
        k,s,_=setup();inputs=context(k,s)
        k.trusted.register_approval(Approval("ap-second",inputs["r"].request_digest,1000));inputs["a2"]="ap-second"
        result=WorkflowExecutor(k,s).run(flow,"a","run1",inputs,{"flag":True})
        self.assertEqual((result.status,result.error),("Halted","WORKFLOW_PERMIT_NOT_FRESH"))
        self.assertEqual(len(result.operation_ids),1);self.assertEqual(k.accounts()["tenant:CNY"].pending,70)
        self.assertEqual(s.effects,());k.assert_invariants()

    def test_adapter_cost_violation_halts_subsequent_send_attempt(self):
        class FeeJumpService(RefundService):
            def send(self,op): self._fee=40;return super().send(op)
        k,_,_=setup();s=FeeJumpService({"a":Order("v1","original-a",100)})
        engine=WorkflowExecutor(k,s);flow=make([P,D,S,S]);refs=context(k,s)
        result=engine.run(flow,"a","run1",refs,{"flag":True})
        self.assertEqual((result.status,result.error),("Halted","ASSUMPTION_BROKEN"))
        self.assertEqual(s.real_cost,110);self.assertEqual(k.accounts()["tenant:CNY"].spent,110)
        self.assertEqual(sum(e.kind=="SendAttemptAdmitted" for e in k.events()),1)
        before=k.events();self.assertEqual(engine.run(flow,"a","run1",refs,{"flag":True}),result)
        self.assertEqual(k.events(),before);k.assert_invariants()

    def test_transport_exception_after_effect_is_unknown_then_reconciled(self):
        class TimeoutAfterApply(RefundService):
            def send(self,op):
                super().send(op)
                raise TimeoutError("lost response")
        k,_,_=setup();s=TimeoutAfterApply({"a":Order("v1","original-a",100)})
        result=WorkflowExecutor(k,s).run(make([P,D,S,Q]),"a","run1",context(k,s),{"flag":True})
        self.assertEqual(result.status,"Finished");self.assertEqual(s.real_cost,70)
        self.assertTrue(any(e.kind=="Unknown" for e in k.events()))
        self.assertEqual(k.accounts()["tenant:CNY"].spent,70);k.assert_invariants()

    def test_protocol_error_halts_without_assuming_no_effect(self):
        class ProtocolError(RefundService):
            def send(self,op): raise Reject("REMOTE_PROTOCOL_ERROR")
        k,_,_=setup();s=ProtocolError({"a":Order("v1","original-a",100)})
        result=WorkflowExecutor(k,s).run(make([P,D,S]),"a","run1",context(k,s),{"flag":True})
        self.assertEqual((result.status,result.error),("Halted","REMOTE_PROTOCOL_ERROR"))
        self.assertEqual(k.get("a",result.operation_ids[0]).state,Execution.UNKNOWN)
        self.assertEqual(k.accounts()["tenant:CNY"].pending,70);k.assert_invariants()

    def test_unexpected_adapter_exception_preserves_effect_and_pending(self):
        class CrashingAdapter(RefundService):
            def send(self,op):
                super().send(op)
                raise RuntimeError("sensitive remote response must not be echoed")
        k,_,_=setup();s=CrashingAdapter({"a":Order("v1","original-a",100)})
        result=WorkflowExecutor(k,s).run(make([P,D,S]),"a","run1",context(k,s),{"flag":True})
        self.assertEqual((result.status,result.error),("Halted","ADAPTER_INTERNAL_ERROR"))
        self.assertEqual(s.real_cost,70);self.assertEqual(k.accounts()["tenant:CNY"].pending,70)
        self.assertEqual(k.get("a",result.operation_ids[0]).state,Execution.UNKNOWN)
        self.assertNotIn("sensitive",repr(k.events()));k.assert_invariants()

    def test_policy_change_after_preflight_cannot_create_misbound_prepare(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from unittest.mock import patch
        from avcl.types import Request
        from .test_contract_policy import amended,seal
        k,s,_=setup();refs=context(k,s);flow=make([P,D]);envelope=seal(k,amended(default_contract()))
        validating=Event();resume=Event();original=Request.validate
        def paused(request):
            validating.set()
            if not resume.wait(5): raise AssertionError("test synchronization timed out")
            return original(request)
        with patch.object(Request,"validate",paused),ThreadPoolExecutor(1) as pool:
            future=pool.submit(WorkflowExecutor(k,s).run,flow,"a","run1",refs,{"flag":True})
            try:
                self.assertTrue(validating.wait(5));k.trusted.activate_policy(envelope)
            finally: resume.set()
            result=future.result()
        self.assertEqual((result.status,result.error),("Halted","WORKFLOW_POLICY_CHANGED"))
        self.assertEqual(k.accounts()["tenant:CNY"].pending,0)
        self.assertFalse(any(e.kind=="Prepared" for e in k.events()));k.assert_invariants()

    def test_crossed_receipt_cannot_settle_another_operation(self):
        from .support import prepare
        class CrossedReceipt(RefundService):
            reply=None
            def send(self,op):
                receipt=super().send(op)
                return self.reply if self.reply is not None else receipt
        k,_,_=setup(limit=200)
        s=CrossedReceipt({"a":Order("v1","original-a",100),"b":Order("v1","original-b",100)})
        b=prepare(k,authorize(k,s,"b",60));b=k.dispatch("b",b.id,b.permit_ref);s.reply=s.send(b)
        result=WorkflowExecutor(k,s).run(make([P,D,S]),"a","run1",context(k,s),{"flag":True})
        self.assertEqual(result.error,"UNEXPECTED_ADAPTER_OBSERVATION")
        self.assertEqual(k.get("b",b.id).state,Execution.DISPATCHED)
        self.assertEqual(k.get("a",result.operation_ids[0]).state,Execution.UNKNOWN)
        self.assertEqual(k.accounts()["tenant:CNY"].pending,130);self.assertEqual(s.real_cost,130)
        self.assertTrue(k._quarantined);k.assert_invariants()

    def test_roundtrip_and_branch_annotation_tampering(self):
        flow=make([P,branch([D],[D]),S]);encoded=flow.to_json()
        self.assertEqual(load_workflow(encoded,default_contract()).to_json(),encoded)
        obj=json.loads(encoded);obj["body"][1]["survivors"].append("p")
        self.assertCode("WORKFLOW_ANALYSIS_MISMATCH",lambda:load_workflow(json.dumps(obj),default_contract()))
        changed=replace(flow,source_map=tuple((key,"new.file:1") for key,_ in flow.source_map))
        self.assertEqual(flow.semantic_digest,changed.semantic_digest);self.assertNotEqual(flow.source_digest,changed.source_digest)

    def test_wrong_contract_unknown_command_and_invalid_ref_fail_closed(self):
        flow=make([P]);base=default_contract()
        other=build_contract(base.module_id,2,base.tools,base.validators,base.assumptions,base.clauses)
        self.assertCode("WORKFLOW_CONTRACT_MISMATCH",lambda:load_workflow(flow.to_json(),other))
        for tag in ("Eval","Loop","DirectToolCall","Propose"):
            self.assertCode("UNSUPPORTED_WORKFLOW_COMMAND",lambda:make([{"tag":tag}]))
        self.assertCode("INVALID_WORKFLOW_VARIABLE",lambda:make([{**D,"permit":[]}]))

    def test_branch_matrix_against_independent_availability_oracle(self):
        # 18 skeletons, each checked against both possible branch paths.
        # The oracle tracks only an affine availability bit, not compiler state.
        choices={"keep":[],"drop":[{"tag":"Drop","ref":"p"}],"consume":[D]}
        accepted=0;executions=0
        for left,right,tail in product(choices,choices,(False,True)):
            possible=[]
            for action in (left,right):
                live=True
                if action in ("drop","consume"): live=False
                possible.append(not tail or live)
            body=[P,branch(choices[left],choices[right])]+([{**D,"out":"tail_op"}] if tail else [])
            if not all(possible):
                self.assertCode("E-AFFINE-201",lambda:make(body));continue
            flow=make(body);accepted+=1
            for flag in (True,False):
                k,s,_=setup();result=WorkflowExecutor(k,s).run(flow,"a","matrix",context(k,s),{"flag":flag})
                self.assertEqual(result.status,"Finished");executions+=1;k.assert_invariants()
        self.assertEqual((accepted,executions),(10,20))
