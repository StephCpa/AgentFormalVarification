"""Finite workflow IR with affine contexts and conservative branch effects.

This front end checks a structured, loop-free command subset. It grants no
authority: the executor still uses kernel references and transaction gates.
"""
from dataclasses import dataclass
from .canonical import Reject,canonical_bytes,digest,strict_loads
from .contract import EFFECTS,Contract,load_contract,names
from .ir import Predicate,load_predicate
from .ir_types import FieldType,identifier,type_from_wire
from . import delegation

FORMAT="avcl/workflow/0.7"
VALIDATION_FORMAT="avcl/workflow/0.6"
TASK_FORMAT="avcl/workflow/0.5"
LEGACY_FORMAT="avcl/workflow/0.4"
SUPPORTED_FORMATS=(LEGACY_FORMAT,TASK_FORMAT,VALIDATION_FORMAT,FORMAT)
AFFINE={"Permit","Approval"}
REQUIREMENTS={"Prepare":("R01","R02","R04"),"Dispatch":("R08",),"Abort":("R07",),
              "Send":("R02","R14","R16"),"Reconcile":("R05",),"Drop":("R08",),
              "Move":("R08",),"Alias":("R08",),"If":("R09",),"Close":("R10",),"Validate":("R01","R02","R03"),"Delegate":("R01","R04","R11")}

class WorkflowDiagnostic(Reject):
    def __init__(self,code,node_id,tag,detail=""):
        super().__init__(code)
        self.node_id=node_id;self.requirement_ids=REQUIREMENTS.get(tag,("R09",));self.detail=detail

@dataclass(frozen=True)
class RefType:
    kind: str
    tool: str

@dataclass(frozen=True)
class Command:
    tag: str
    node_id: str
    args: tuple=()
    condition: object=None
    then_body: tuple=()
    else_body: tuple=()
    survivors: tuple=()

    def wire(self):
        out={"tag":self.tag,"node_id":self.node_id,**dict(self.args)}
        if self.tag=="Delegate":out["attenuations"]=strict_loads(out["attenuations"])
        if self.tag=="If":
            out.update(condition=self.condition.semantic(),then=[n.wire() for n in self.then_body],
                       otherwise=[n.wire() for n in self.else_body],survivors=list(self.survivors))
        return out

def commands(body):
    for n in body:
        yield n
        yield from commands(n.then_body)
        yield from commands(n.else_body)

def delegation_request(workflow_digest,run_id,node_id,args,parent_ref):
    request_id=digest({"run_id":run_id,"workflow":workflow_digest,"node_id":node_id},"avcl/workflow-delegate/0.7")
    return canonical_bytes({"client_request_id":request_id,"parent_ref":parent_ref,
        "child_subject":args["child_subject"],"attenuations":strict_loads(args["attenuations"])}).decode("utf-8")

@dataclass(frozen=True)
class Workflow:
    id: str
    contract_digest: str
    refs: tuple
    values: tuple
    effects: tuple
    inferred_effects: tuple
    body: tuple
    remaining: tuple
    source_map: tuple
    format_version: str=FORMAT

    def semantic(self):
        return {"format":self.format_version,"id":self.id,"contract_digest":self.contract_digest,
                "refs":{k:{"kind":t.kind,"tool":t.tool} for k,t in self.refs},
                "values":{k:t.wire() for k,t in self.values},"effects":list(self.effects),
                "body":[n.wire() for n in self.body]}

    def sources(self):
        return {"commands":dict(self.source_map),"conditions":{n.node_id:dict(n.condition.source_map)
                for n in commands(self.body) if n.condition is not None}}

    @property
    def semantic_digest(self): return digest(self.semantic(),self.format_version)
    @property
    def source_digest(self): return digest(self.sources(),self.format_version.replace("workflow/","workflow-source/"))
    def to_json(self): return canonical_bytes({**self.semantic(),"source_map":self.sources()}).decode("utf-8")

    def report(self):
        return {"workflow_id":self.id,"workflow_digest":self.semantic_digest,"contract_digest":self.contract_digest,
                "checker":self.format_version.replace("workflow/","affine-effects/"),"compilation_status":"Accepted","meta_theory_status":"Pending",
                "backend_requirements":[feature for kind,feature in (("Context","durable-validation"),("Task","durable-task-close")) if any(t.kind==kind for _,t in self.refs)]+(["registered-delegation"] if any(n.tag=="Delegate" for n in commands(self.body)) else []),
                "inferred_effects":list(self.inferred_effects),"declared_effects":list(self.effects),
                "remaining_affine_variables":[k for k,t in self.remaining if t.kind in AFFINE],
                "scope":"structured command typing only; no authorization, liveness or deployment proof"}

def compile_workflow(workflow_id,refs,values,effects,body,contract,source="<workflow-builder>",format_version=FORMAT):
    if type(format_version) is not str or format_version not in SUPPORTED_FORMATS:raise Reject("UNSUPPORTED_WORKFLOW_VERSION")
    if type(contract) is not Contract: raise Reject("INVALID_WORKFLOW_CONTRACT")
    contract=load_contract(contract.to_json());tools={t.id:t for t in contract.tools}
    if not identifier(workflow_id) or type(source) is not str or len(source)>256: raise Reject("INVALID_WORKFLOW_METADATA")
    if type(refs) is not dict or type(values) is not dict or len(refs)+len(values)>128 or set(refs)&set(values):
        raise Reject("INVALID_WORKFLOW_INPUTS")
    input_kinds={"Candidate","Capability","Evidence","Approval"}
    if format_version!=LEGACY_FORMAT:input_kinds.add("Task")
    if format_version in {VALIDATION_FORMAT,FORMAT}:input_kinds.add("Context")
    for name,t in refs.items():
        if (not identifier(name) or type(t) is not RefType or type(t.kind) is not str or
                t.kind not in input_kinds or type(t.tool) is not str or t.tool not in tools):
            raise Reject("INVALID_WORKFLOW_REFERENCE_TYPE")
    normalized={}
    for name,t in values.items():
        if not identifier(name) or type(t) is not FieldType: raise Reject("INVALID_WORKFLOW_VALUE_TYPE")
        normalized[name]=type_from_wire(t.validate().wire())
    if not names(effects) or not set(effects)<=EFFECTS: raise Reject("INVALID_WORKFLOW_EFFECTS")
    counter=0
    def block(raw,env,seen,path,depth):
        nonlocal counter
        if type(raw) is not list or len(raw)>1000 or depth>32: raise Reject("WORKFLOW_LIMIT")
        output=[];used_effects=set()
        for i,item in enumerate(raw):
            counter+=1;node_id=f"{path}.{i}"
            if counter>1000: raise Reject("WORKFLOW_LIMIT")
            if type(item) is not dict or type(item.get("tag")) is not str: raise Reject("INVALID_WORKFLOW_COMMAND")
            tag=item["tag"]
            def fail(code,detail=""): raise WorkflowDiagnostic(code,node_id,tag,detail)
            shapes={"Prepare":{"candidate","capability","evidence","approval","out"},
                    "Dispatch":{"permit","out"},"Abort":{"permit"},"Drop":{"ref"},
                    "Move":{"ref","out"},"Alias":{"ref","out"},"Send":{"operation"},
                    "Reconcile":{"operation"},"If":{"condition","then","otherwise"}}
            if format_version!=LEGACY_FORMAT:shapes["Close"]={"task","out"}
            if format_version in {VALIDATION_FORMAT,FORMAT}:shapes["Validate"]={"candidate","context","validator","out"}
            if format_version==FORMAT:shapes["Delegate"]={"capability","child_subject","attenuations","out"}
            if tag not in shapes: fail("UNSUPPORTED_WORKFLOW_COMMAND")
            if set(item)!={"tag"}|shapes[tag]: fail("INVALID_WORKFLOW_COMMAND")
            def read(key,kind=None):
                name=item[key]
                if not identifier(name): fail("INVALID_WORKFLOW_VARIABLE")
                if name not in env: fail("E-AFFINE-201" if name in seen else "UNKNOWN_WORKFLOW_VARIABLE")
                t=env[name]
                if kind is not None and t.kind!=kind: fail("E-TYPE-101")
                return name,t
            def bind(t):
                name=item["out"]
                if not identifier(name) or name in seen: fail("WORKFLOW_VARIABLE_REBOUND")
                seen.add(name);env[name]=t
            if tag=="If":
                if type(item["condition"]) is not Predicate: fail("INVALID_BRANCH_PREDICATE")
                condition=load_predicate(item["condition"].to_json())
                if any(name not in normalized or normalized[name]!=t for name,t in condition.fields): fail("INVALID_BRANCH_CONTEXT")
                left,le,ls,lf=block(item["then"],dict(env),set(seen),node_id+".then",depth+1)
                right,re,rs,rf=block(item["otherwise"],dict(env),set(seen),node_id+".else",depth+1)
                env={k:le[k] for k in le.keys()&re.keys() if le[k]==re[k]}
                seen=ls|rs;used_effects|=lf|rf
                output.append(Command(tag,node_id,condition=condition,then_body=left,else_body=right,survivors=tuple(sorted(env))))
                continue
            if tag=="Delegate":
                _,parent=read("capability","Capability")
                try:
                    _,spec,_=delegation.parse(canonical_bytes({"client_request_id":"compile","parent_ref":"parent",
                        "child_subject":item["child_subject"],"attenuations":item["attenuations"]}).decode("utf-8"))
                except Reject as exc:fail(exc.code)
                if parent.tool!="refund" or set(spec["tools"])!={parent.tool} or not set(spec["effects"])<=delegation.EFFECTS:
                    fail("UNSUPPORTED_WORKFLOW_DELEGATION")
                item={**item,"attenuations":canonical_bytes(spec).decode("utf-8")}
                bind(RefType("ChildCapability",parent.tool));used_effects|={"Read","ResourceConsume"}
            elif tag=="Validate":
                _,candidate=read("candidate","Candidate");_,context=read("context","Context")
                if candidate.tool!=context.tool:fail("WORKFLOW_TOOL_SCOPE_MISMATCH")
                if (candidate.tool!="refund" or item["validator"]!="order-service" or not any(
                    (v.id,v.predicate_id,v.version)==("order-service","RefundEligible","sim/0.1") for v in contract.validators)):
                    fail("UNSUPPORTED_WORKFLOW_VALIDATOR")
                bind(RefType("Evidence",candidate.tool));used_effects|={"Read","ResourceConsume"}
            elif tag=="Prepare":
                _,candidate=read("candidate","Candidate")
                for key,kind in [("capability","Capability"),("evidence","Evidence"),("approval","Approval")]:
                    _,t=read(key,kind)
                    if t.tool!=candidate.tool: fail("WORKFLOW_TOOL_SCOPE_MISMATCH")
                env.pop(item["approval"]);bind(RefType("Permit",candidate.tool));used_effects.add("ResourceConsume")
            elif tag in {"Dispatch","Abort"}:
                name,t=read("permit","Permit");env.pop(name);used_effects.add("ResourceConsume")
                if tag=="Dispatch": bind(RefType("Operation",t.tool))
            elif tag=="Drop":
                name,_=read("ref");env.pop(name)
            elif tag in {"Move","Alias"}:
                name,t=read("ref")
                if tag=="Alias" and t.kind in AFFINE: fail("E-AFFINE-201","affine values cannot be copied")
                if tag=="Move":
                    if t.kind not in AFFINE: fail("MOVE_REQUIRES_AFFINE_VALUE")
                    env.pop(name)
                bind(t)
            elif tag in {"Send","Reconcile"}:
                _,t=read("operation","Operation")
                used_effects |= set(tools[t.tool].effects) if tag=="Send" else {"Read","Disclose","ResourceConsume"}
            elif tag=="Close":
                _,t=read("task","Task");bind(RefType("TaskReport",t.tool))
                used_effects|={"Read","ResourceConsume"}
            output.append(Command(tag,node_id,tuple(sorted((k,v) for k,v in item.items() if k!="tag"))))
        return tuple(output),env,seen,used_effects
    compiled,env,_,inferred=block(body,dict(refs),set(refs)|set(values),"w0",0)
    if not inferred<=set(effects):
        raise WorkflowDiagnostic("E-EFFECT-301","w0","Send",",".join(sorted(inferred-set(effects))))
    result=Workflow(workflow_id,contract.semantic_digest,tuple(sorted(refs.items())),tuple(sorted(normalized.items())),
                    tuple(sorted(effects)),tuple(sorted(inferred)),compiled,tuple(sorted(env.items())),
                    tuple((n.node_id,source) for n in commands(compiled)),format_version)
    result.to_json()
    return result

def load_workflow(raw,contract):
    obj=strict_loads(raw)
    if type(obj) is not dict or set(obj)!={"format","id","contract_digest","refs","values","effects","body","source_map"}:
        raise Reject("INVALID_WORKFLOW_DOCUMENT")
    if type(obj["format"]) is not str or obj["format"] not in SUPPORTED_FORMATS: raise Reject("UNSUPPORTED_WORKFLOW_VERSION")
    if obj["contract_digest"]!=contract.semantic_digest: raise Reject("WORKFLOW_CONTRACT_MISMATCH")
    if type(obj["refs"]) is not dict or type(obj["values"]) is not dict or type(obj["effects"]) is not list:
        raise Reject("INVALID_WORKFLOW_INPUTS")
    refs={}
    for name,t in obj["refs"].items():
        if type(t) is not dict or set(t)!={"kind","tool"}: raise Reject("INVALID_WORKFLOW_REFERENCE_TYPE")
        refs[name]=RefType(t["kind"],t["tool"])
    values={k:type_from_wire(t) for k,t in obj["values"].items()}
    source=obj["source_map"]
    if type(source) is not dict or set(source)!={"commands","conditions"} or any(type(v) is not dict for v in source.values()):
        raise Reject("INVALID_WORKFLOW_SOURCE_MAP")
    count=0
    def decode(body,path="w0",depth=0):
        nonlocal count
        if type(body) is not list or depth>32: raise Reject("WORKFLOW_LIMIT")
        result=[]
        for i,node in enumerate(body):
            count+=1;node_id=f"{path}.{i}"
            if count>1000: raise Reject("WORKFLOW_LIMIT")
            if type(node) is not dict or node.get("node_id")!=node_id: raise Reject("INVALID_WORKFLOW_NODE_ID")
            out={k:v for k,v in node.items() if k!="node_id"}
            if out.get("tag")=="If":
                if set(out)!={"tag","condition","then","otherwise","survivors"} or type(out["condition"]) is not dict or node_id not in source["conditions"]:
                    raise Reject("INVALID_WORKFLOW_COMMAND")
                out.pop("survivors")
                out["condition"]=load_predicate(canonical_bytes({**out["condition"],"source_map":source["conditions"][node_id]}).decode("utf-8"))
                out["then"]=decode(out["then"],node_id+".then",depth+1)
                out["otherwise"]=decode(out["otherwise"],node_id+".else",depth+1)
            result.append(out)
        return result
    wf=compile_workflow(obj["id"],refs,values,tuple(obj["effects"]),decode(obj["body"]),contract,format_version=obj["format"])
    if obj["body"]!=wf.semantic()["body"]: raise Reject("WORKFLOW_ANALYSIS_MISMATCH")
    if set(source["commands"])!={n.node_id for n in commands(wf.body)} or set(source["conditions"])!={n.node_id for n in commands(wf.body) if n.tag=="If"}:
        raise Reject("INVALID_WORKFLOW_SOURCE_MAP")
    if any(type(v) is not str or len(v)>256 for v in source["commands"].values()): raise Reject("INVALID_WORKFLOW_SOURCE_MAP")
    return Workflow(wf.id,wf.contract_digest,wf.refs,wf.values,wf.effects,wf.inferred_effects,wf.body,wf.remaining,tuple(sorted(source["commands"].items())),wf.format_version)
