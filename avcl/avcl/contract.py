"""Immutable action contracts above the pure predicate IR.

Safety clauses are admission checks; completion clauses are only evaluated by
the explicit completion API. Other clause kinds fail closed until their
monitoring backend exists. This is not yet a workflow/affine type checker.
"""
from dataclasses import dataclass
from .canonical import Reject,canonical_bytes,digest,strict_loads
from .ir import Predicate,load_predicate,refund_predicate
from .ir_types import identifier
from .checks import compile_checks

FORMAT="avcl/contract/0.3"
EFFECTS=frozenset({"Read","Write","Disclose","InvokeModel","Spawn","ResourceConsume"})

def names(items):
    return type(items) is tuple and all(identifier(v) for v in items) and len(set(items))==len(items)

def moment(value): return type(value) is int and 0<=value<10**18

def decimal(value):
    from .canonical import amount
    return amount(value)

@dataclass(frozen=True)
class Tool:
    id: str
    version: str
    effects: tuple

@dataclass(frozen=True)
class Validator:
    id: str
    predicate_id: str
    version: str

@dataclass(frozen=True)
class Assumption:
    id: str
    authority: str
    scope: str
    valid_from: int
    expires: int
    monitor: str

@dataclass(frozen=True)
class Health:
    authority: str
    scope: str
    valid_from: int
    expires: int
    status: str

@dataclass(frozen=True)
class Clause:
    id: str
    kind: str
    rule: str
    tool: str
    events: tuple
    requirement_ids: tuple
    assumptions: tuple
    frozen: bool
    predicate: Predicate

    def semantic(self):
        return {"id":self.id,"kind":self.kind,"rule":self.rule,"tool":self.tool,"events":list(self.events),
                "requirement_ids":list(self.requirement_ids),"assumptions":list(self.assumptions),
                "frozen":self.frozen,"predicate":self.predicate.semantic()}

    @property
    def semantic_digest(self): return digest(self.semantic(),"avcl/clause/0.3")

@dataclass(frozen=True)
class Contract:
    module_id: str
    policy_version: int
    tools: tuple
    validators: tuple
    assumptions: tuple
    clauses: tuple
    account_limits: tuple=()

    def semantic(self):
        return {"format":FORMAT,"module_id":self.module_id,"policy_version":str(self.policy_version),
                "tools":[{"id":t.id,"version":t.version,"effects":list(t.effects)} for t in self.tools],
                "validators":[{"id":v.id,"predicate_id":v.predicate_id,"version":v.version} for v in self.validators],
                "assumptions":[{"id":a.id,"authority":a.authority,"scope":a.scope,"valid_from":str(a.valid_from),
                                "expires":str(a.expires),"monitor":a.monitor} for a in self.assumptions],
                "clauses":[c.semantic() for c in self.clauses],
                "account_limits":{name:str(value) for name,value in self.account_limits}}

    @property
    def semantic_digest(self): return digest(self.semantic(),"avcl/contract/0.3")
    @property
    def source_digest(self): return digest(self.sources(),"avcl/contract-sources/0.3")
    def sources(self): return {c.id:dict(c.predicate.source_map) for c in self.clauses}
    def to_json(self): return canonical_bytes({**self.semantic(),"source_map":self.sources()}).decode("utf-8")

    def requirements(self): return frozenset(r for c in self.clauses for r in c.requirement_ids)

    def input_types(self,tool,event):
        result={}
        for c in self.clauses:
            if c.tool==tool and event in c.events:
                for key,t in c.predicate.fields:
                    if key in result and result[key]!=t: raise Reject("CONFLICTING_CONTEXT_TYPE")
                    result[key]=t
        return result

def build_contract(module_id,policy_version,tools,validators,assumptions,clauses,account_limits=()):
    if not identifier(module_id) or not moment(policy_version) or policy_version==0: raise Reject("INVALID_CONTRACT_ID")
    for items,cls in [(tools,Tool),(validators,Validator),(assumptions,Assumption),(clauses,Clause)]:
        if type(items) is not tuple or len(items)>200 or any(type(v) is not cls for v in items): raise Reject("INVALID_DECLARATION")
        if not names(tuple(v.id for v in items)): raise Reject("DUPLICATE_OR_INVALID_DECLARATION")
    if not tools or not clauses: raise Reject("EMPTY_CONTRACT")
    for tool in tools:
        if type(tool.version) is not str or not tool.version or len(tool.version)>128 or not names(tool.effects) or not set(tool.effects)<=EFFECTS:
            raise Reject("INVALID_TOOL_DECLARATION")
    for v in validators:
        if not identifier(v.predicate_id) or type(v.version) is not str or not v.version or len(v.version)>128: raise Reject("INVALID_VALIDATOR")
    for a in assumptions:
        if not all(identifier(v) for v in (a.authority,a.scope,a.monitor)) or not moment(a.valid_from) or not moment(a.expires) or a.valid_from>=a.expires:
            raise Reject("INVALID_ASSUMPTION")
    known_tools={t.id for t in tools};known_assumptions={a.id for a in assumptions};normalized=[]
    for c in clauses:
        if type(c.kind) is not str or c.kind not in {"safety","completion"}: raise Reject("UNSUPPORTED_CLAUSE_KIND")
        if type(c.tool) is not str or type(c.rule) is not str: raise Reject("INVALID_CLAUSE")
        if c.tool not in known_tools or not names(c.events) or not names(c.requirement_ids) or not c.requirement_ids or not names(c.assumptions):
            raise Reject("INVALID_CLAUSE")
        if not set(c.assumptions)<=known_assumptions or type(c.frozen) is not bool or type(c.predicate) is not Predicate: raise Reject("INVALID_CLAUSE")
        if c.kind=="safety":
            if c.rule not in {"requires","forbid","invariant"} or not c.events or not set(c.events)<={"prepare","dispatch"}:
                raise Reject("INVALID_SAFETY_SELECTOR")
        elif c.rule!="success" or c.events!=("close",) or c.frozen:
            raise Reject("COMPLETION_IS_NOT_AUTHORIZATION")
        p=load_predicate(c.predicate.to_json())
        if p.requirement_id not in c.requirement_ids: raise Reject("REQUIREMENT_MAPPING_MISMATCH")
        normalized.append(Clause(c.id,c.kind,c.rule,c.tool,tuple(sorted(c.events)),tuple(sorted(c.requirement_ids)),
                                 tuple(sorted(c.assumptions)),c.frozen,p))
    if type(account_limits) is not tuple or any(type(p) is not tuple or len(p)!=2 or type(p[0]) is not str or not p[0] or len(p[0])>256 or not moment(p[1]) for p in account_limits):
        raise Reject("INVALID_ACCOUNT_LIMITS")
    if len({p[0] for p in account_limits})!=len(account_limits): raise Reject("INVALID_ACCOUNT_LIMITS")
    normalized_tools=tuple(Tool(t.id,t.version,tuple(sorted(t.effects))) for t in tools)
    result=Contract(module_id,policy_version,tuple(sorted(normalized_tools,key=lambda t:t.id)),tuple(sorted(validators,key=lambda v:v.id)),
                    tuple(sorted(assumptions,key=lambda a:a.id)),tuple(sorted(normalized,key=lambda c:c.id)),tuple(sorted(account_limits)))
    for tool in known_tools:
        for event in ("prepare","dispatch"):
            if not any(c.kind=="safety" and c.tool==tool and event in c.events for c in normalized): raise Reject("UNCOVERED_TOOL_EVENT")
            result.input_types(tool,event)
        result.input_types(tool,"close")
    result.to_json()
    return result

def exact(obj,keys):
    if type(obj) is not dict or set(obj)!=set(keys.split()): raise Reject("INVALID_CONTRACT_DOCUMENT")
    return obj

def sequence(obj):
    if type(obj) is not list or len(obj)>200: raise Reject("INVALID_DECLARATION")
    return obj

def string_tuple(value):
    return tuple(sequence(value))

def load_contract(raw):
    o=exact(strict_loads(raw),"format module_id policy_version tools validators assumptions clauses account_limits source_map")
    if o["format"]!=FORMAT: raise Reject("UNSUPPORTED_CONTRACT_VERSION")
    tools=[];validators=[];assumptions=[];clauses=[]
    for item in sequence(o["tools"]):
        t=exact(item,"id version effects");tools.append(Tool(t["id"],t["version"],string_tuple(t["effects"])))
    for item in sequence(o["validators"]):
        v=exact(item,"id predicate_id version");validators.append(Validator(v["id"],v["predicate_id"],v["version"]))
    for item in sequence(o["assumptions"]):
        a=exact(item,"id authority scope valid_from expires monitor")
        assumptions.append(Assumption(a["id"],a["authority"],a["scope"],decimal(a["valid_from"]),decimal(a["expires"]),a["monitor"]))
    sources=o["source_map"]
    if type(sources) is not dict: raise Reject("INVALID_SOURCE_MAP")
    for item in sequence(o["clauses"]):
        c=exact(item,"id kind rule tool events requirement_ids assumptions frozen predicate")
        if type(c["id"]) is not str or c["id"] not in sources or type(c["predicate"]) is not dict or "source_map" in c["predicate"]:
            raise Reject("INVALID_SOURCE_MAP")
        p=load_predicate(canonical_bytes({**c["predicate"],"source_map":sources[c["id"]]}).decode("utf-8"))
        clauses.append(Clause(c["id"],c["kind"],c["rule"],c["tool"],string_tuple(c["events"]),string_tuple(c["requirement_ids"]),
                              string_tuple(c["assumptions"]),c["frozen"],p))
    if set(sources)!={c.id for c in clauses} or type(o["account_limits"]) is not dict: raise Reject("INVALID_CONTRACT_DOCUMENT")
    return build_contract(o["module_id"],decimal(o["policy_version"]),tuple(tools),tuple(validators),tuple(assumptions),tuple(clauses),
                          tuple((k,decimal(v)) for k,v in o["account_limits"].items()))

def assumptions_valid(contract,ids,health,now):
    declarations={a.id:a for a in contract.assumptions}
    for name in ids:
        a=declarations[name];h=health.get(name)
        if (type(h) is not Health or h.status!="Healthy" or h.authority!=a.authority or h.scope!=a.scope or
                not moment(h.valid_from) or not moment(h.expires) or not a.valid_from<=now<a.expires or not h.valid_from<=now<h.expires):
            return False
    return True

@dataclass(frozen=True)
class CompiledContract:
    contract: Contract
    checks: tuple

    def evaluate(self,event,tool,values,health,now):
        if event not in {"prepare","dispatch","close"} or type(values) is not dict or type(health) is not dict or not moment(now):
            raise Reject("INVALID_CONTRACT_CONTEXT")
        reports=[];compiled=dict(self.checks)
        for c in self.contract.clauses:
            if c.tool!=tool or event not in c.events: continue
            status="Unknown";reason=None;passed=False
            if not assumptions_valid(self.contract,c.assumptions,health,now):
                status="AssumptionBroken";reason="assumption_unavailable_expired_or_broken"
            else:
                try:
                    fields={key:values[key] for key,_ in c.predicate.fields}
                    value=compiled[c.id].evaluate(fields);passed=not value if c.rule=="forbid" else value
                    status="RuntimeSatisfied" if passed else "Violated"
                except (Reject,KeyError) as exc: reason=exc.code if isinstance(exc,Reject) else "MISSING_CONTEXT_FIELD"
            reports.append({"clause_id":c.id,"requirement_ids":list(c.requirement_ids),"kind":c.kind,"event":event,
                "status":status,"passed":passed,"reason":reason,"contract_digest":self.contract.semantic_digest})
        return {"event":event,"allowed":bool(reports) and all(r["passed"] for r in reports),"reports":reports}

def compile_contract(contract):
    normalized=load_contract(contract.to_json())
    return CompiledContract(normalized,tuple((c.id,compile_checks(c.predicate)) for c in normalized.clauses))

def default_contract(version=1):
    p=refund_predicate()
    return build_contract("refund-policy",version,(Tool("refund","sim/0.1",("Read","Write","Disclose","ResourceConsume")),),
        (Validator("order-service","RefundEligible","sim/0.1"),),
        (Assumption("remote-idempotency","simulator","refund",0,1000,"remote-effect-oracle"),),
        (Clause("baseline-refund","safety","requires","refund",("prepare","dispatch"),("R09",),
                ("remote-idempotency",),True,p),))
