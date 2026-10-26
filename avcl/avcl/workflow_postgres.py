"""Checkpointed finite workflows. Local commands commit with ledger changes;
external I/O commits intent first and never repeats an ambiguous write on resume.
"""
from uuid import uuid4
from .canonical import Reject,digest,canonical_bytes,strict_loads
from .storage_codec import dumps,loads
from .types import Request,Execution,Settlement,Observation
from .ir_types import identifier
from .checks import compile_checks
from .contract import assumptions_valid
from .workflow import Workflow,load_workflow,commands,delegation_request
from .workflow_runtime import WorkflowResult,FaultPlan
from .capacity import check_new_workflow
from . import tasks
from . import validation
from .sending import admit_attempt, REMOTE_WINDOW_REJECTIONS, bind_workflow, workflow_send_context
from . import receipt_envelope

def wire(value):return canonical_bytes(value).decode("utf-8")

class StepRejected(Reject):
    def __init__(self,code,pc,revision,node):
        super().__init__(code);self.pc=pc;self.revision=revision;self.node=node

class DurableWorkflowExecutor:
    def __init__(self,kernel,simulator=None):
        if simulator is not None:
            from .simulator import RefundService
            if type(simulator) is not RefundService:raise Reject("UNSUPPORTED_WORKFLOW_ADAPTER")
        self.db=kernel;self.service=simulator

    def _key(self,subject,run_id):
        if type(subject) is not str or not subject or len(subject)>256 or not identifier(run_id):raise Reject("INVALID_WORKFLOW_EXECUTION")
        return (self.db.tenant,subject,run_id)

    def _row(self,c,key):
        row=c.execute("SELECT * FROM workflow_run WHERE tenant_id=%s AND subject=%s AND id=%s FOR UPDATE",key).fetchone()
        if row is None:raise Reject("UNKNOWN_WORKFLOW_RUN")
        return row

    def start(self,program,subject,run_id,refs,values,faults=None):
        key=self._key(subject,run_id)
        if faults is None:faults=FaultPlan()
        if type(program) is not Workflow or type(faults) is not FaultPlan:raise Reject("INVALID_WORKFLOW_EXECUTION")
        def start(k,c):
            prior=c.execute("SELECT submission_digest,contract_digest FROM workflow_run WHERE tenant_id=%s AND subject=%s AND id=%s",key).fetchone()
            contract=k._policy_snapshots[prior["contract_digest"]].compiled.contract if prior else k._release.compiled.contract
            flow=load_workflow(program.to_json(),contract)
            if any(t.tool!="refund" for _,t in flow.refs):raise Reject("UNSUPPORTED_WORKFLOW_ADAPTER")
            if type(refs) is not dict or set(refs)!=set(n for n,_ in flow.refs) or type(values) is not dict or set(values)!=set(n for n,_ in flow.values):raise Reject("WORKFLOW_INPUT_MISMATCH")
            if type(faults.drop_receipt_at) is not frozenset or type(faults.no_send_at) is not frozenset:raise Reject("INVALID_FAULT_PLAN")
            sends={n.node_id for n in commands(flow.body) if n.tag=="Send"}
            if not (faults.drop_receipt_at|faults.no_send_at)<=sends:raise Reject("INVALID_FAULT_PLAN")
            env={};inputs={};approvals=set()
            for name,t in flow.refs:
                value=refs[name]
                if t.kind=="Candidate":
                    if type(value) is not Request or value.tenant!=k.tenant or value.subject!=subject or value.tool!=t.tool:raise Reject("INVALID_WORKFLOW_CANDIDATE")
                    value.validate();inputs[name]=value.payload()
                else:
                    if type(value) is not str or not value or len(value)>256:raise Reject("INVALID_WORKFLOW_REFERENCE")
                    inputs[name]=value
                    if t.kind=="Approval":
                        if value in approvals:raise Reject("WORKFLOW_AFFINE_INPUT_ALIAS")
                        approvals.add(value)
                    elif t.kind=="Task":tasks.owned(k,c,subject,value)
                    elif t.kind=="Context" and prior is None:validation.context(k,c,subject,value)
                env[name]=value
            value_wire={n:t.value_wire(t.normalize(values[n])) for n,t in flow.values}
            fault_wire={"drop":sorted(faults.drop_receipt_at),"no_send":sorted(faults.no_send_at)}
            submitted=digest({"workflow":flow.semantic_digest,"refs":inputs,"values":value_wire,"faults":fault_wire},"avcl/workflow-run/0.4")
            if prior:
                if prior["submission_digest"]!=submitted:raise Reject("WORKFLOW_RUN_CONFLICT")
                return
            check_new_workflow(self.db,c,k)
            c.execute("INSERT INTO workflow_run(tenant_id,subject,id,workflow_digest,contract_digest,submission_digest,program,values_wire,faults,frame,status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Running')",
                (*key,flow.semantic_digest,flow.contract_digest,submitted,flow.to_json(),wire(value_wire),wire(fault_wire),dumps((tuple(sorted(env.items())),(),frozenset(),frozenset()))))
            k._event("WorkflowStarted",subject=subject,run_id=run_id,workflow_digest=flow.semantic_digest,
                     authenticated_receipts=bool(self.service and self.service.requires_envelope))
        self.db._transaction("workflow-start",start)
        return self.snapshot(subject,run_id)

    def _plan(self,k,row):
        authenticated=self._receipt_mode(k,row["subject"],row["id"])
        if self.service is not None and self.service.requires_envelope!=authenticated:raise Reject("WORKFLOW_ADAPTER_MODE_CHANGED")
        flow=load_workflow(row["program"],k._policy_snapshots[row["contract_digest"]].compiled.contract)
        if flow.semantic_digest!=row["workflow_digest"]:raise Reject("CORRUPT_WORKFLOW_PROGRAM")
        encoded=strict_loads(row["values_wire"]);values={n:t.value_from_wire(encoded[n]) for n,t in flow.values}
        plan=[]
        def block(body):
            for node in body:
                if node.tag=="If":
                    flag=compile_checks(node.condition).evaluate({n:values[n] for n,_ in node.condition.fields})
                    plan.append((node,"then" if flag else "otherwise"));block(node.then_body if flag else node.else_body)
                    plan.append((node,"join"))
                else:plan.append((node,None))
        block(flow.body)
        return flow,plan

    @staticmethod
    def _receipt_mode(k,subject,run_id):
        events=[dict(e.details) for e in k._events if e.kind=="WorkflowStarted"
                and dict(e.details).get("subject")==subject and dict(e.details).get("run_id")==run_id]
        if len(events)!=1:raise Reject("CORRUPT_WORKFLOW_RECEIPT_MODE")
        value=events[0].get("authenticated_receipts","False")
        if value not in {"True","False"}:raise Reject("CORRUPT_WORKFLOW_RECEIPT_MODE")
        return value=="True"

    def _frame(self,row):
        env,operations,allocated,used=loads(row["frame"])
        return dict(env),list(operations),set(allocated),set(used)

    def _complete(self,k,c,key,row,node_id,command,outcome,frame,op_id=None,plan_length=None,details=None):
        env,operations,allocated,used=frame
        c.execute("INSERT INTO workflow_step VALUES(%s,%s,%s,%s,%s,%s)",(*key,node_id,row["pc"],wire({"node_id":node_id,"command":command,"result":outcome,"operation_id":op_id,**(details or {})})))
        status="Finished" if plan_length is not None and row["pc"]+1>=plan_length else "Running"
        c.execute("UPDATE workflow_run SET pc=pc+1,revision=revision+1,status=%s,frame=%s,error=NULL,failed_node=NULL WHERE tenant_id=%s AND subject=%s AND id=%s",
            (status,dumps((tuple(sorted(env.items())),tuple(operations),frozenset(allocated),frozenset(used))),*key))
        k._event("WorkflowStepCommitted",op_id,run_id=key[2],node_id=node_id,command=command,result=outcome)
        return {"status":"Progress","node_id":node_id}

    def advance(self,subject,run_id,worker,lease_seconds=30):
        key=self._key(subject,run_id)
        if type(worker) is not str or not worker or len(worker)>256 or type(lease_seconds) is not int or not 1<=lease_seconds<=3600:raise Reject("INVALID_LEASE")
        def step(k,c):
            row=self._row(c,key)
            if row["status"] in {"Finished","Halted"}:return {"status":row["status"]}
            flow,plan=self._plan(k,row)
            if row["pc"]>=len(plan):
                c.execute("UPDATE workflow_run SET status='Finished',revision=revision+1 WHERE tenant_id=%s AND subject=%s AND id=%s",key)
                return {"status":"Finished"}
            node,branch=plan[row["pc"]];node_id=node.node_id+(".join" if branch=="join" else "")
            frame=self._frame(row);env,operations,allocated,used=frame;args=dict(node.args);tag=node.tag;op_id=None
            def consume(name):
                handle=env.pop(name)
                if type(handle) is not tuple or len(handle)!=2 or handle[0] in used:raise Reject("WORKFLOW_PERMIT_ALREADY_CONSUMED")
                used.add(handle[0]);return handle
            try:
                if tag in {"Delegate","Validate","Prepare","Dispatch","Send"} and k._clock_rollback:raise Reject("CLOCK_ROLLBACK")
                if tag=="If":
                    if branch=="join":
                        for name in list(env):
                            if name not in node.survivors:env.pop(name)
                        tag="Join";outcome="AffineJoinApplied"
                    else:outcome=branch
                elif tag=="Delegate":
                    if k._release.compiled.contract.semantic_digest!=flow.contract_digest:raise Reject("WORKFLOW_POLICY_CHANGED")
                    report=k.delegate(subject,delegation_request(flow.semantic_digest,run_id,node_id,args,env[args["capability"]]))
                    env[args["out"]]=report["child_cap_ref"]
                    return self._complete(k,c,key,row,node_id,tag,"Delegated",frame,None,len(plan),
                        {"child_cap_ref":report["child_cap_ref"],"child_subject":report["child_subject"],"parent_ref":report["parent_ref"]})
                elif tag=="Validate":
                    if k._release.compiled.contract.semantic_digest!=flow.contract_digest:raise Reject("WORKFLOW_POLICY_CHANGED")
                    candidate=env[args["candidate"]]
                    if type(candidate) is not Request or candidate.tenant!=k.tenant or candidate.subject!=subject:raise Reject("INVALID_WORKFLOW_CANDIDATE")
                    request_id=digest({"run_id":run_id,"workflow":flow.semantic_digest,"node_id":node_id},"avcl/workflow-validate/0.6")
                    report=validation.validate_candidate(k,c,subject,{"client_request_id":request_id,
                        "candidate":{name:value for name,value in candidate.payload().items() if name not in {"tenant","subject"}},
                        "validator_id":args["validator"],"context_refs":[env[args["context"]]]})
                    env[args["out"]]=report["evidence_refs"][0]
                    return self._complete(k,c,key,row,node_id,tag,report["status"],frame,None,len(plan),
                        {"evidence_ref":env[args["out"]],"checked_ref":report["checked_ref"],"context_ref":env[args["context"]],
                         "validator_id":report["validator_id"],"validator_version":report["validator_version"]})
                elif tag=="Prepare":
                    if k._release.compiled.contract.semantic_digest!=flow.contract_digest:raise Reject("WORKFLOW_POLICY_CHANGED")
                    approval=env.pop(args["approval"])
                    request_id=digest({"run_id":run_id,"workflow":flow.semantic_digest,"node_id":node_id},"avcl/workflow-request/0.4")
                    op=k.prepare(subject,env[args["candidate"]],env[args["capability"]],env[args["evidence"]],approval,request_id)
                    if self._receipt_mode(k,subject,run_id):receipt_envelope.require(k,op.id)
                    if op.state!=Execution.PREPARED or op.admitted or op.id in allocated or op.contract_digest!=flow.contract_digest:raise Reject("WORKFLOW_PERMIT_NOT_FRESH")
                    owner=c.execute("SELECT subject,run_id FROM workflow_operation WHERE tenant_id=%s AND operation_id=%s",(self.db.tenant,op.id)).fetchone()
                    if owner is not None:raise Reject("WORKFLOW_PERMIT_NOT_FRESH")
                    c.execute("INSERT INTO workflow_operation VALUES(%s,%s,%s,%s)",(self.db.tenant,op.id,subject,run_id))
                    bind_workflow(k,op,subject,run_id,flow.semantic_digest)
                    allocated.add(op.id);operations.append(op.id);env[args["out"]]=(op.id,op.permit_ref);op_id=op.id;outcome="Prepared"
                elif tag=="Dispatch":
                    op_id,permit=consume(args["permit"]);k.dispatch(subject,op_id,permit);env[args["out"]]=op_id;outcome="Admitted"
                elif tag=="Abort":
                    op_id,_=consume(args["permit"]);k.abort_prepared(subject,op_id);outcome="AbortedBeforeDispatch"
                elif tag=="Drop":env.pop(args["ref"]);outcome="VariableDropped"
                elif tag=="Move":env[args["out"]]=env.pop(args["ref"]);outcome="Moved"
                elif tag=="Alias":env[args["out"]]=env[args["ref"]];outcome="ReusableAliasCreated"
                elif tag=="Close":
                    request_id=digest({"run_id":run_id,"workflow":flow.semantic_digest,"node_id":node_id},"avcl/workflow-close/0.5")
                    report=tasks.close(self.db,k,c,subject,env[args["task"]],request_id)
                    env[args["out"]]=wire(report)
                    return self._complete(k,c,key,row,node_id,tag,report["status"],frame,None,len(plan),
                        {"task_ref":report["task_ref"],"task_revision":report["revision"],"close_request_id":request_id})
                elif tag in {"Send","Reconcile"}:
                    op_id=env[args["operation"]];op=k.get(subject,op_id)
                    if not op.admitted:raise Reject("NOT_ADMITTED")
                    previous=c.execute("SELECT * FROM workflow_io WHERE tenant_id=%s AND subject=%s AND run_id=%s AND node_id=%s",(*key,node_id)).fetchone()
                    if op.settlement==Settlement.FINAL:
                        if previous:c.execute("UPDATE workflow_io SET status='Recorded' WHERE tenant_id=%s AND subject=%s AND run_id=%s AND node_id=%s",(*key,node_id))
                        outcome="FinalReceiptAlreadyRecorded"
                    elif previous:
                        if previous["lease_until"]>k._clock():return {"status":"WaitingIO"}
                        if op.state==Execution.DISPATCHED:k.trusted.unknown(op.id)
                        c.execute("UPDATE workflow_io SET status='Uncertain',generation=generation+1 WHERE tenant_id=%s AND subject=%s AND run_id=%s AND node_id=%s",(*key,node_id))
                        outcome="UncertainAfterWorkerLoss"
                    else:
                        if tag=="Send":
                            original=k._policy_snapshots[op.contract_digest].compiled.contract
                            assumptions=tuple({a for clause in original.clauses if clause.kind=="safety" for a in clause.assumptions})
                            if k._quarantined or not assumptions_valid(original,assumptions,k._health,k._clock()):raise Reject("ASSUMPTION_BROKEN")
                            outbox=c.execute("SELECT * FROM outbox WHERE tenant_id=%s AND operation_id=%s AND kind='Send' FOR UPDATE",(self.db.tenant,op.id)).fetchone()
                            if outbox is None:raise Reject("MISSING_OUTBOX")
                            if outbox["status"]!="Pending" or outbox["wait_until"]<=k._clock():
                                if op.state==Execution.DISPATCHED:k.trusted.unknown(op.id)
                                return self._complete(k,c,key,row,node_id,tag,"RequiresReconciliation",frame,op.id,len(plan))
                            generation=outbox["generation"]+1
                            with workflow_send_context(k,op,subject,run_id,flow.semantic_digest,node_id):
                                envelope=admit_attempt(k,op,worker=worker,generation=generation)
                            if not envelope:
                                return self._complete(k,c,key,row,node_id,tag,"RequiresReconciliation",frame,op.id,len(plan))
                            c.execute("UPDATE outbox SET status='InFlight',worker=%s,lease_until=%s,generation=%s,attempts=attempts+1 WHERE tenant_id=%s AND operation_id=%s AND kind='Send'",(worker,k._clock()+lease_seconds,generation,self.db.tenant,op.id))
                            c.execute("INSERT INTO send_attempt VALUES(%s,%s,%s,%s,%s,%s,%s)",(self.db.tenant,op.id,generation,worker,op.request.request_digest,op.binding,k._clock()))
                        c.execute("INSERT INTO workflow_io VALUES(%s,%s,%s,%s,%s,%s,'InFlight',%s,1,%s)",(*key,node_id,op.id,tag,worker,k._clock()+lease_seconds))
                        c.execute("UPDATE workflow_run SET status='WaitingIO',revision=revision+1 WHERE tenant_id=%s AND subject=%s AND id=%s",key)
                        return {"status":"IO","node_id":node_id,"kind":tag,"operation":op,"worker":worker,"generation":1,"faults":strict_loads(row["faults"]),"envelope":envelope if tag=="Send" else None}
                else:raise Reject("UNSUPPORTED_WORKFLOW_COMMAND")
                return self._complete(k,c,key,row,node_id,tag,outcome,frame,op_id,len(plan))
            except Reject as exc:raise StepRejected(exc.code,row["pc"],row["revision"],node_id) from None
        try:
            result=self.db._transaction("workflow-step",step)
            if result.get("node_id"):self.db._crash("workflow-node:"+result["node_id"]+".after_commit")
            return result
        except StepRejected as exc:
            def halt(k,c):
                row=self._row(c,key)
                if (row["pc"],row["revision"])!=(exc.pc,exc.revision):return {"status":"Progress"}
                if exc.code=="OPERATION_CAPACITY_EXCEEDED":
                    if row["status"]!="WaitingCapacity" or row["error"]!=exc.code:
                        c.execute("UPDATE workflow_run SET status='WaitingCapacity',error=%s,failed_node=%s,revision=revision+1 WHERE tenant_id=%s AND subject=%s AND id=%s",(exc.code,exc.node,*key))
                        k._event("WorkflowBackpressured",run_id=run_id,node_id=exc.node,reason=exc.code,requirement_id="R10")
                    return {"status":"WaitingCapacity"}
                c.execute("UPDATE workflow_run SET status='Halted',error=%s,failed_node=%s,revision=revision+1 WHERE tenant_id=%s AND subject=%s AND id=%s",(exc.code,exc.node,*key))
                k._event("WorkflowHalted",run_id=run_id,node_id=exc.node,reason=exc.code)
                return {"status":"Halted"}
            return self.db._transaction("workflow-halt",halt)

    def finish_io(self,subject,run_id,node_id,worker,generation,receipt=None,error=None):
        key=self._key(subject,run_id)
        def inspect(k,c):
            row=c.execute("SELECT * FROM workflow_io WHERE tenant_id=%s AND subject=%s AND run_id=%s AND node_id=%s",(*key,node_id)).fetchone()
            if row is None:raise Reject("UNKNOWN_WORKFLOW_IO")
            return k.get(subject,row["operation_id"])
        op=self.db._transaction("workflow-io-inspect",inspect,False)
        if receipt is not None:
            if type(receipt) not in (Observation,str):error="UNEXPECTED_ADAPTER_OBSERVATION"
            else:
                try:self.db.trusted.observe(receipt,expected_operation_id=op.id)
                except Reject as exc:error=exc.code
        def finish(k,c):
            row=self._row(c,key);flow,plan=self._plan(k,row)
            io=c.execute("SELECT * FROM workflow_io WHERE tenant_id=%s AND subject=%s AND run_id=%s AND node_id=%s",(*key,node_id)).fetchone()
            if (io["status"]!="InFlight" or io["worker"]!=worker or type(generation) is not int or io["generation"]!=generation or io["lease_until"]<=k._clock() or row["pc"]>=len(plan) or plan[row["pc"]][0].node_id!=node_id):return {"status":"StaleIO"}
            current=k.get(subject,op.id)
            if (receipt is None or error) and current.state==Execution.DISPATCHED:k.trusted.unknown(op.id)
            c.execute("UPDATE workflow_io SET status=%s WHERE tenant_id=%s AND subject=%s AND run_id=%s AND node_id=%s",("Uncertain" if receipt is None or error else "Recorded",*key,node_id))
            if error:
                k._quarantined=True;k._event("AdapterObservationRejected",op.id,reason=error,requirement_id="R14")
                c.execute("UPDATE workflow_run SET status='Halted',error=%s,failed_node=%s,revision=revision+1 WHERE tenant_id=%s AND subject=%s AND id=%s",(error,node_id,*key))
                return {"status":"Halted"}
            return self._complete(k,c,key,row,node_id,io["kind"],"ReceiptObserved" if receipt is not None else "NoAuthoritativeReceipt",self._frame(row),op.id,len(plan))
        return self.db._transaction("workflow-io-finish",finish)

    def snapshot(self,subject,run_id):
        key=self._key(subject,run_id)
        def read(k,c):
            row=self._row(c,key);_,operations,_,_=self._frame(row)
            steps=c.execute("SELECT payload FROM workflow_step WHERE tenant_id=%s AND subject=%s AND run_id=%s ORDER BY sequence",key).fetchall()
            return WorkflowResult(row["workflow_digest"],run_id,row["status"],tuple(tuple(sorted(strict_loads(s["payload"]).items())) for s in steps),tuple(operations),row["failed_node"],row["error"])
        return self.db._transaction("workflow-snapshot",read,False)

    def resume(self,subject,run_id,worker=None):
        if worker is None:worker="workflow-"+uuid4().hex
        for _ in range(2002):
            result=self.advance(subject,run_id,worker)
            if result["status"] in {"Finished","Halted","WaitingIO","WaitingCapacity"}:return self.snapshot(subject,run_id)
            if result["status"]!="IO":continue
            if self.service is None:return self.snapshot(subject,run_id)
            self.db._crash("workflow-io.after_admission")
            receipt=None;error=None;op=result["operation"];node=result["node_id"]
            try:
                if result["kind"]=="Send":
                    if node not in result["faults"]["no_send"]:receipt=self.service.send(result["envelope"] if self.service.requires_envelope else op)
                    if node in result["faults"]["drop"]:receipt=None
                else:receipt=self.service.query(op.id)
            except OSError:pass
            except Reject as exc:
                if exc.code not in REMOTE_WINDOW_REJECTIONS:error=exc.code
            except Exception:error="ADAPTER_INTERNAL_ERROR"
            self.db._crash("workflow-io.after_effect")
            self.finish_io(subject,run_id,node,worker,result["generation"],receipt,error)
        raise Reject("WORKFLOW_LIMIT")

    def run(self,program,subject,run_id,refs,values,faults=None):
        self.start(program,subject,run_id,refs,values,faults)
        return self.resume(subject,run_id)
