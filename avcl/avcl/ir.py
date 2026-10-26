"""Versioned pure IR with typechecked total evaluation and stable source IDs."""
from dataclasses import dataclass
from .canonical import Reject,canonical_bytes,digest,strict_loads
from .ir_types import FieldType,Some,Tagged,Record,identifier,type_from_wire,same_type,join_type,BOUND
from .evaluation import use_budget, EvaluationLimit, binary, record_get
from .diagnostics import annotate, predicate_location

VERSION="avcl/predicate/0.2"
SEMANTICS="avcl/pure/0.2"
BOOL=FieldType("Bool")

class Diagnostic(Reject):
    def __init__(self,code,requirement_id,node_id):
        super().__init__(code, requirement_ids=("R09", requirement_id))
        self.requirement_id,self.node_id=requirement_id,node_id

@dataclass(frozen=True)
class Node:
    tag: str
    type: FieldType
    node_id: str
    value: object=None
    children: tuple=()

    def wire(self):
        obj={"tag":self.tag,"node_id":self.node_id}
        if self.tag=="Const": obj.update(type=self.type.wire(),value=self.type.value_wire(self.value))
        elif self.tag=="Field": obj["name"]=self.value
        else:
            obj["args"]=[c.wire() for c in self.children]
            if self.tag=="Get": obj["field"]=self.value
        return obj

def nodes(root):
    yield root
    for child in root.children: yield from nodes(child)

@dataclass(frozen=True)
class Predicate:
    root: Node
    fields: tuple
    requirement_id: str
    source_map: tuple

    def values(self,values,*,budget=None):
        budget=use_budget(budget)
        if (type(values) is not dict or len(values)!=len(self.fields) or
                any(type(k) is not str or len(k)>128 for k in values)):
            raise Diagnostic("EVALUATION_FIELDS",self.requirement_id,self.root.node_id)
        try:
            budget.charge((len(values)+1)*sum(1+len(k) for k in values))
            if set(values)!=set(k for k,_ in self.fields):
                raise Diagnostic("EVALUATION_FIELDS",self.requirement_id,self.root.node_id)
            return {k:t.normalize(values[k],budget=budget) for k,t in self.fields}
        except EvaluationLimit as exc:
            raise exc.locate(self.requirement_id,self.root.node_id)
        except Diagnostic:raise
        except (Reject,TypeError,ValueError):
            raise Diagnostic("EVALUATION_TYPE_OR_RANGE",self.requirement_id,self.root.node_id) from None

    def evaluate(self,values,*,budget=None):
        budget=use_budget(budget);env=self.values(values,budget=budget)
        def run(n):
            try:
                budget.charge()
                return evaluate_node(n)
            except EvaluationLimit as exc:
                raise exc.locate(self.requirement_id,n.node_id)
        def evaluate_node(n):
            if n.tag=="Const": return n.value
            if n.tag=="Field": return env[n.value]
            if n.tag=="Get": return record_get(run(n.children[0]),n.value,budget)
            if n.tag=="And": return run(n.children[0]) and run(n.children[1])
            if n.tag=="Or": return run(n.children[0]) or run(n.children[1])
            if n.tag=="If":
                if run(n.children[0]):
                    result=run(n.children[1]);budget.charge()  # VM's true-branch join JUMP.
                    return result
                return run(n.children[2])
            if n.tag=="Not": return not run(n.children[0])
            if n.tag=="IsSome": return run(n.children[0]) is not None
            if n.tag=="Unwrap": return run(n.children[0]).value
            a,b=(run(c) for c in n.children)
            ops={"Eq":"EQ","Le":"LE","Lt":"LT","Add":"ADD","Sub":"SUB","MulConst":"MUL","InSet":"IN"}
            if n.tag in ops:return binary(ops[n.tag],a,b,budget)
            raise Diagnostic("UNSUPPORTED_NODE",self.requirement_id,n.node_id)
        return run(self.root)

    def semantic(self):
        return {"ir_version":VERSION,"semantic_version":SEMANTICS,"requirement_id":self.requirement_id,
                "fields":{k:t.wire() for k,t in self.fields},"root":self.root.wire()}

    @property
    def semantic_digest(self): return digest(self.semantic(),"avcl/ir/0.2")
    @property
    def source_digest(self): return digest(dict(self.source_map),"avcl/source-map/0.2")
    def to_json(self): return canonical_bytes({**self.semantic(),"source_map":dict(self.source_map)}).decode("utf-8")
    @property
    def artifact_digest(self): return digest(strict_loads(self.to_json()),"avcl/artifact/0.2")

def option_key(node):
    # All supported nodes are pure and evaluate over an immutable input
    # snapshot. Ignore source position, retain the full typed expression.
    return (node.tag,node.type,node.value,tuple(option_key(c) for c in node.children))

def known_some(node,truth):
    if node.tag=="Not": return known_some(node.children[0],not truth)
    if node.tag=="IsSome" and truth:
        key=option_key(node.children[0]);return {key} if key else set()
    if (node.tag=="And" and truth) or (node.tag=="Or" and not truth):
        return known_some(node.children[0],truth)|known_some(node.children[1],truth)
    return set()

def compile_predicate(raw,fields,requirement_id="R09",source="<builder>"):
    def error(code,node="n0",location=None):
        raise annotate(Diagnostic(code,requirement_id,node),source_ref=source,location=location or predicate_location(node),component="predicate")
    if not identifier(requirement_id) or type(source) is not str or len(source)>256: error("INVALID_METADATA",None,"/")
    if type(fields) is not dict or not fields or len(fields)>64: error("INVALID_FIELD_SCHEMA",None,"/fields")
    try:
        for k,t in fields.items():
            if not identifier(k) or type(t) is not FieldType: error("INVALID_FIELD_SCHEMA")
            t.validate()
        fields={k:type_from_wire(t.wire()) for k,t in fields.items()}
    except Reject: error("INVALID_FIELD_SCHEMA",None,"/fields")
    nominal={}
    def register(t,path):
        if t.kind in {"Id","Enum"}:
            if t.name in nominal and nominal[t.name]!=t: error("CONFLICTING_NOMINAL_TYPE",path)
            nominal[t.name]=t
        for _,child in t.fields: register(child,path)
        if t.element is not None: register(t.element,path)
    for t in fields.values(): register(t,"n0")
    count=0
    def build(obj,path="n0",facts=frozenset(),depth=0):
        nonlocal count
        count+=1
        if count>1000 or depth>64: error("PREDICATE_LIMIT",path)
        if type(obj) is not dict or type(obj.get("tag")) is not str: error("INVALID_AST",path)
        tag=obj["tag"]
        if tag=="Const":
            if set(obj) not in ({"tag","value"},{"tag","value","type"}): error("INVALID_AST",path)
            v=obj["value"];t=obj.get("type")
            if t is None:
                kind={bool:"Bool",int:"Int",str:"String"}.get(type(v))
                if kind is None or (kind=="Int" and abs(v)>BOUND): error("INVALID_LITERAL",path)
                t=FieldType(kind,lower=v,upper=v) if kind=="Int" else FieldType(kind)
            try:
                if type(t) is not FieldType: error("INVALID_LITERAL",path)
                t=type_from_wire(t.validate().wire());v=t.normalize(v)
            except Reject: error("INVALID_LITERAL",path)
            register(t,path)
            return Node(tag,t,path,v)
        if tag=="Field":
            if set(obj)!={"tag","name"} or type(obj["name"]) is not str or obj["name"] not in fields: error("UNKNOWN_FIELD",path)
            return Node(tag,fields[obj["name"]],path,obj["name"])
        arities={"Get":1,"Not":1,"IsSome":1,"Unwrap":1,"If":3,"And":2,"Or":2,"Eq":2,
                 "Le":2,"Lt":2,"Add":2,"Sub":2,"MulConst":2,"InSet":2}
        if tag not in arities: error("UNSUPPORTED_NODE",path)
        expected={"tag","args","field"} if tag=="Get" else {"tag","args"}
        if set(obj)!=expected or type(obj.get("args")) is not list or len(obj["args"])!=arities[tag]: error("INVALID_AST",path)
        children=[]
        for i,arg in enumerate(obj["args"]):
            extra=set()
            if i==1 and tag in {"And","Or","If"}: extra=known_some(children[0],tag!="Or")
            if i==2 and tag=="If": extra=known_some(children[0],False)
            children.append(build(arg,f"{path}.{i}",facts|extra,depth+1))
        ts=[c.type for c in children];kinds=[t.kind for t in ts];t=BOOL;value=None
        if tag=="Get":
            if ts[0].kind!="Record" or type(obj["field"]) is not str or obj["field"] not in dict(ts[0].fields): error("UNKNOWN_FIELD",path)
            value=obj["field"];t=dict(ts[0].fields)[value]
        elif tag in {"Not","And","Or"}:
            if any(k!="Bool" for k in kinds): error("PREDICATE_TYPE_ERROR",path)
        elif tag=="If":
            if kinds[0]!="Bool" or not same_type(ts[1],ts[2]): error("PREDICATE_TYPE_ERROR",path)
            t=join_type(ts[1],ts[2])
        elif tag in {"IsSome","Unwrap"}:
            if kinds[0]!="Option": error("PREDICATE_TYPE_ERROR",path)
            if tag=="Unwrap":
                if option_key(children[0]) not in facts: error("UNGUARDED_OPTION",path)
                t=ts[0].element
        elif tag=="InSet":
            if kinds[1]!="Set" or not same_type(ts[0],ts[1].element): error("PREDICATE_TYPE_ERROR",path)
        elif tag=="Eq":
            if not same_type(ts[0],ts[1]): error("PREDICATE_TYPE_ERROR",path)
        else:
            if kinds!=["Int","Int"]: error("PREDICATE_TYPE_ERROR",path)
            if tag=="MulConst" and children[0].tag!="Const": error("NONLINEAR_UNSUPPORTED",path)
            if tag=="Add": t=FieldType("Int",ts[0].lower+ts[1].lower,ts[0].upper+ts[1].upper)
            if tag=="Sub": t=FieldType("Int",ts[0].lower-ts[1].upper,ts[0].upper-ts[1].lower)
            if tag=="MulConst":
                ends=(children[0].value*ts[1].lower,children[0].value*ts[1].upper)
                t=FieldType("Int",min(ends),max(ends))
        return Node(tag,t,path,value,tuple(children))
    root=build(raw)
    if root.type.kind!="Bool": error("PREDICATE_NOT_BOOLEAN")
    result=Predicate(root,tuple(sorted(fields.items())),requirement_id,tuple((n.node_id,source) for n in nodes(root)))
    try: result.to_json()
    except Reject as exc: error(exc.code)
    return result

def load_predicate(raw,*,source="<predicate-input>"):
    obj=None
    try:
        obj=strict_loads(raw)
        return _load_predicate(obj)
    except Reject as exc:
        req=obj.get("requirement_id") if type(obj) is dict else None
        sm=obj.get("source_map") if type(obj) is dict else None
        node=getattr(exc,"node_id",None)
        origin=sm.get(node) if type(sm) is dict and node is not None else None
        # A loader's revalidation uses a synthetic builder label. Prefer the
        # corresponding supplied source-map entry, or the caller's file label.
        if getattr(exc,"source_ref",None)=="<builder>":exc.source_ref=None
        raise annotate(exc,requirement_ids=("R09",req),source_ref=origin if type(origin) is str else source,
                       location=predicate_location(node) or "/",component="predicate")


def _load_predicate(obj):
    if type(obj) is not dict or set(obj)!={"ir_version","semantic_version","requirement_id","fields","root","source_map"}:
        raise Reject("INVALID_IR_DOCUMENT")
    if obj["ir_version"]!=VERSION or obj["semantic_version"]!=SEMANTICS: raise Reject("UNSUPPORTED_IR_VERSION")
    if type(obj["fields"]) is not dict: raise Reject("INVALID_FIELD_SCHEMA")
    fields={}
    for k,v in obj["fields"].items():
        try:fields[k]=type_from_wire(v)
        except Reject as exc:
            raise annotate(exc,location="/fields/"+k.replace("~","~0").replace("/","~1"))
    count=0
    def decode(n,path="n0",depth=0):
        nonlocal count
        count+=1
        def fail(code):raise Diagnostic(code,obj.get("requirement_id"),path)
        if count>1000 or depth>64: fail("PREDICATE_LIMIT")
        if type(n) is not dict or n.get("node_id")!=path: fail("INVALID_NODE_ID")
        out={k:v for k,v in n.items() if k!="node_id"}
        if out.get("tag")=="Const":
            if set(out)!={"tag","type","value"}: fail("INVALID_AST")
            try:
                t=type_from_wire(out["type"]);out.update(type=t,value=t.value_from_wire(out["value"]))
            except Reject as exc:raise annotate(exc,node_id=path,location=predicate_location(path))
        elif "args" in out:
            if type(out["args"]) is not list: fail("INVALID_AST")
            out["args"]=[decode(v,f"{path}.{i}",depth+1) for i,v in enumerate(out["args"])]
        return out
    result=compile_predicate(decode(obj["root"]),fields,obj["requirement_id"])
    sm=obj["source_map"]
    if type(sm) is not dict or set(sm)!=set(n.node_id for n in nodes(result.root)) or any(type(v) is not str or len(v)>256 for v in sm.values()):
        raise annotate(Reject("INVALID_SOURCE_MAP"),location="/source_map")
    return Predicate(result.root,result.fields,result.requirement_id,tuple(sorted(sm.items())))

def refund_predicate():
    return compile_predicate({"tag":"And","args":[
        {"tag":"Lt","args":[{"tag":"Const","value":0},{"tag":"Field","name":"amount"}]},
        {"tag":"Eq","args":[{"tag":"Field","name":"currency"},{"tag":"Const","value":"CNY"}]}
    ]},{"amount":FieldType("Int"),"currency":FieldType("String")},"R09")
