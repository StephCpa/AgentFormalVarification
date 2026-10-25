"""Host-owned synchronous workflow executor over the reference kernel.

Transport is the independent refund simulator. Query cost is zero in that
simulator; paid real reconciliation requires a separate maintenance ledger.
Execution never rolls back already recorded external effects or reservations.
"""
from dataclasses import dataclass
from threading import RLock
from .canonical import Reject,digest
from .types import Request,Execution,Observation
from .ir_types import identifier
from .checks import compile_checks
from .contract import assumptions_valid
from .workflow import load_workflow,commands,Workflow,delegation_request
from .sending import admit_attempt, REMOTE_WINDOW_REJECTIONS

@dataclass(frozen=True)
class FaultPlan:
    drop_receipt_at: frozenset=frozenset()
    no_send_at: frozenset=frozenset()

@dataclass(frozen=True)
class PermitHandle:
    operation_id: str
    permit_ref: str

@dataclass(frozen=True)
class WorkflowResult:
    workflow_digest: str
    run_id: str
    status: str
    steps: tuple
    operation_ids: tuple
    failed_node: str | None=None
    error: str | None=None

    def report(self,kernel,subject):
        return {"workflow_digest":self.workflow_digest,"run_id":self.run_id,"status":self.status,
            "steps":[dict(s) for s in self.steps],"failed_node":self.failed_node,"error":self.error,
            "operations":[{"id":op.id,"execution":str(op.state),"settlement":str(op.settlement),
                           "remaining":[{"account":r.account,"amount":str(r.remaining)} for r in op.reservations]}
                          for op in (kernel.get(subject,i) for i in self.operation_ids)],
            "scope":"Finished means the command program ended, not business completion or released budget"}

class WorkflowExecutor:
    def __init__(self,kernel,simulator):
        self._kernel=kernel;self._simulator=simulator
        self._lock=RLock();self._runs={}

    def run(self,program,subject,run_id,refs,values,faults=None):
        if faults is None: faults=FaultPlan()
        if type(program) is not Workflow or not identifier(run_id) or type(subject) is not str or not subject:
            raise Reject("INVALID_WORKFLOW_EXECUTION")
        if type(faults) is not FaultPlan or type(faults.drop_receipt_at) is not frozenset or type(faults.no_send_at) is not frozenset:
            raise Reject("INVALID_FAULT_PLAN")
        with self._lock:
            with self._kernel._lock:
                contract=self._kernel._release.compiled.contract
                program=load_workflow(program.to_json(),contract)
            if any(t.kind=="Context" for _,t in program.refs):raise Reject("WORKFLOW_REQUIRES_DURABLE_VALIDATION_BACKEND")
            if any(t.kind=="Task" for _,t in program.refs):raise Reject("WORKFLOW_REQUIRES_DURABLE_TASK_BACKEND")
            if any(t.tool!="refund" for _,t in program.refs): raise Reject("UNSUPPORTED_WORKFLOW_ADAPTER")
            if type(refs) is not dict or set(refs)!=set(k for k,_ in program.refs) or type(values) is not dict or set(values)!=set(k for k,_ in program.values):
                raise Reject("WORKFLOW_INPUT_MISMATCH")
            send_nodes={n.node_id for n in commands(program.body) if n.tag=="Send"}
            if not (faults.drop_receipt_at|faults.no_send_at)<=send_nodes: raise Reject("INVALID_FAULT_PLAN")
            normalized_values={k:t.normalize(values[k]) for k,t in program.values}
            env={};input_wire={};approvals=set()
            for name,t in program.refs:
                value=refs[name]
                if t.kind=="Candidate":
                    if type(value) is not Request or value.tenant!=self._kernel.tenant or value.subject!=subject or value.tool!=t.tool: raise Reject("INVALID_WORKFLOW_CANDIDATE")
                    value.validate();input_wire[name]=value.payload()
                else:
                    if type(value) is not str or not value or len(value)>256: raise Reject("INVALID_WORKFLOW_REFERENCE")
                    input_wire[name]=value
                    if t.kind=="Approval":
                        if value in approvals: raise Reject("WORKFLOW_AFFINE_INPUT_ALIAS")
                        approvals.add(value)
                env[name]=value
            submitted=digest({"workflow":program.semantic_digest,"refs":input_wire,
                "values":{k:t.value_wire(normalized_values[k]) for k,t in program.values},
                "faults":{"drop":sorted(faults.drop_receipt_at),"no_send":sorted(faults.no_send_at)}},"avcl/workflow-run/0.4")
            key=(subject,run_id)
            if key in self._runs:
                previous,result=self._runs[key]
                if previous!=submitted: raise Reject("WORKFLOW_RUN_CONFLICT")
                return result
            steps=[];operation_ids=[];allocated=set();used=set();failed_node=None
            branch_checks={n.node_id:compile_checks(n.condition) for n in commands(program.body) if n.tag=="If"}
            def remember(op):
                if op.id not in operation_ids: operation_ids.append(op.id)
            def consume(name):
                handle=env.pop(name)
                if type(handle) is not PermitHandle or handle.operation_id in used: raise Reject("WORKFLOW_PERMIT_ALREADY_CONSUMED")
                used.add(handle.operation_id)
                return handle
            def block(body):
                nonlocal failed_node
                for node in body:
                    failed_node=node.node_id;args=dict(node.args);tag=node.tag;op_id=None;details={}
                    if tag=="If":
                        context={k:normalized_values[k] for k,_ in node.condition.fields}
                        condition=branch_checks[node.node_id].evaluate(context)
                        steps.append(tuple(sorted({"node_id":node.node_id,"command":"If","result":"then" if condition else "otherwise"}.items())))
                        block(node.then_body if condition else node.else_body)
                        # Affine weakening at joins discards variables, never ledger rows.
                        for name in list(env):
                            if name not in node.survivors: env.pop(name)
                        continue
                    if tag=="Delegate":
                        with self._kernel._lock:
                            if self._kernel._release.compiled.contract.semantic_digest!=program.contract_digest:
                                raise Reject("WORKFLOW_POLICY_CHANGED")
                            report=self._kernel.delegate(subject,delegation_request(program.semantic_digest,run_id,node.node_id,args,env[args["capability"]]))
                        env[args["out"]]=report["child_cap_ref"];outcome="Delegated"
                        details={"child_cap_ref":report["child_cap_ref"],"child_subject":report["child_subject"],"parent_ref":report["parent_ref"]}
                    elif tag=="Prepare":
                        approval=env.pop(args["approval"])
                        request_id=digest({"run_id":run_id,"workflow":program.semantic_digest,"node_id":node.node_id},"avcl/workflow-request/0.4")
                        with self._kernel._lock:
                            if self._kernel._release.compiled.contract.semantic_digest!=program.contract_digest:
                                raise Reject("WORKFLOW_POLICY_CHANGED")
                            op=self._kernel.prepare(subject,env[args["candidate"]],env[args["capability"]],env[args["evidence"]],approval,request_id)
                            if self._simulator.requires_envelope:self._kernel.trusted.require_authenticated_receipts(op.id)
                        remember(op)
                        if op.state!=Execution.PREPARED or op.admitted or op.id in allocated or op.contract_digest!=program.contract_digest:
                            raise Reject("WORKFLOW_PERMIT_NOT_FRESH")
                        allocated.add(op.id);env[args["out"]]=PermitHandle(op.id,op.permit_ref);op_id=op.id;outcome="Prepared"
                    elif tag=="Dispatch":
                        handle=consume(args["permit"])
                        op=self._kernel.dispatch(subject,handle.operation_id,handle.permit_ref)
                        env[args["out"]]=op.id;op_id=op.id;outcome="Admitted"
                    elif tag=="Abort":
                        handle=consume(args["permit"]);op=self._kernel.abort_prepared(subject,handle.operation_id)
                        op_id=op.id;outcome="AbortedBeforeDispatch"
                    elif tag=="Drop":
                        env.pop(args["ref"]);outcome="VariableDropped"
                    elif tag=="Move":
                        env[args["out"]]=env.pop(args["ref"]);outcome="Moved"
                    elif tag=="Alias":
                        env[args["out"]]=env[args["ref"]];outcome="ReusableAliasCreated"
                    elif tag in {"Send","Reconcile"}:
                        op_id=env[args["operation"]]
                        op=self._kernel.get(subject,op_id)
                        if not op.admitted: raise Reject("NOT_ADMITTED")
                        if tag=="Send":
                            # Establish a local send-admission point. Transport runs
                            # outside the kernel lock; an already admitted send may
                            # complete after a concurrent quarantine/revocation.
                            with self._kernel._lock:
                                original=self._kernel._policy_snapshots[op.contract_digest].compiled.contract
                                dependencies=tuple({a for c in original.clauses if c.kind=="safety" and c.tool==op.request.tool for a in c.assumptions})
                                if self._kernel._quarantined or not assumptions_valid(original,dependencies,self._kernel._health,self._kernel._clock()):
                                    raise Reject("ASSUMPTION_BROKEN")
                                allowed=admit_attempt(self._kernel,op,workflow_digest=program.semantic_digest,node_id=node.node_id)
                            try:
                                if not allowed or node.node_id in faults.no_send_at: receipt=None
                                else: receipt=self._simulator.send(allowed if self._simulator.requires_envelope else op)
                            except OSError:
                                receipt=None  # Transport exceptions do not establish no effect.
                            except Reject as exc:
                                current=self._kernel.get(subject,op.id)
                                if current.state==Execution.DISPATCHED: self._kernel.trusted.unknown(op.id)
                                if exc.code in REMOTE_WINDOW_REJECTIONS:receipt=None
                                else:
                                    if self._simulator.requires_envelope:
                                        with self._kernel._lock:
                                            self._kernel._quarantined=True
                                            self._kernel._event("SendEnvelopeRejected",op.id,reason=exc.code,requirement_id="R14")
                                    raise
                            except Exception as exc:
                                with self._kernel._lock:
                                    current=self._kernel.get(subject,op.id)
                                    if current.state==Execution.DISPATCHED: self._kernel.trusted.unknown(op.id)
                                    self._kernel._event("AdapterException",op.id,error_type=type(exc).__name__)
                                raise Reject("ADAPTER_INTERNAL_ERROR") from None
                            if node.node_id in faults.drop_receipt_at: receipt=None
                        else:
                            try: receipt=self._simulator.query(op.id)
                            except OSError: receipt=None
                            except Exception as exc:
                                with self._kernel._lock:
                                    current=self._kernel.get(subject,op.id)
                                    if current.state==Execution.DISPATCHED: self._kernel.trusted.unknown(op.id)
                                    self._kernel._event("AdapterException",op.id,error_type=type(exc).__name__)
                                raise Reject("ADAPTER_INTERNAL_ERROR") from None
                        if receipt is None:
                            current=self._kernel.get(subject,op.id)
                            if current.state==Execution.DISPATCHED: self._kernel.trusted.unknown(op.id)
                            outcome="NoAuthoritativeReceipt"
                        else:
                            try:
                                if (type(receipt) not in (Observation,str) or
                                        (type(receipt) is Observation and (receipt.operation_id!=op.id or receipt.request_digest!=op.request.request_digest))):
                                    raise Reject("UNEXPECTED_ADAPTER_OBSERVATION")
                                self._kernel.trusted.observe(receipt,expected_operation_id=op.id)
                            except Reject as exc:
                                with self._kernel._lock:
                                    current=self._kernel.get(subject,op.id)
                                    if current.state==Execution.DISPATCHED: self._kernel.trusted.unknown(op.id)
                                    self._kernel._quarantined=True
                                    self._kernel._event("AdapterObservationRejected",op.id,reason=exc.code,requirement_id="R14")
                                raise
                            outcome="ReceiptObserved"
                    else: raise Reject("UNSUPPORTED_WORKFLOW_COMMAND")
                    steps.append(tuple(sorted({"node_id":node.node_id,"command":tag,"result":outcome,"operation_id":op_id,**details}.items())))
            try:
                block(program.body)
                result=WorkflowResult(program.semantic_digest,run_id,"Finished",tuple(steps),tuple(operation_ids))
            except Reject as exc:
                result=WorkflowResult(program.semantic_digest,run_id,"Halted",tuple(steps),tuple(operation_ids),failed_node,exc.code)
            self._runs[key]=(submitted,result)
            return result
