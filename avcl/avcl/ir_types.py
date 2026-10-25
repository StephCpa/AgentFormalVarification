"""Closed immutable value types for the pure IR. JSON integers are strings."""
from dataclasses import dataclass
import re
from .canonical import Reject

BOUND=10**18-1

def identifier(v):
    return type(v) is str and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}",v) is not None

def integer(v):
    if type(v) is not str or re.fullmatch(r"0|-?[1-9][0-9]{0,17}",v) is None:
        raise Reject("INVALID_IR_INTEGER")
    return int(v)

@dataclass(frozen=True)
class Tagged:
    name: str
    value: str

@dataclass(frozen=True)
class Some:
    value: object

@dataclass(frozen=True)
class Record:
    entries: tuple
    def get(self,name): return dict(self.entries)[name]

@dataclass(frozen=True)
class FieldType:
    kind: str
    lower: int=0
    upper: int=BOUND
    name: str=""
    members: tuple=()
    fields: tuple=()
    element: object=None
    max_size: int=64

    def validate(self,depth=0,budget=None):
        if budget is None: budget=[0]
        budget[0]+=1
        if budget[0]>1000 or depth>16 or type(self.kind) is not str or self.kind not in {"Bool","Int","String","Id","Enum","Record","Option","Set"}:
            raise Reject("INVALID_FIELD_SCHEMA")
        if type(self.lower) is not int or type(self.upper) is not int or not -BOUND<=self.lower<=self.upper<=BOUND:
            raise Reject("INVALID_FIELD_SCHEMA")
        if type(self.members) is not tuple or type(self.fields) is not tuple or type(self.max_size) is not int or not 0<=self.max_size<=64:
            raise Reject("INVALID_FIELD_SCHEMA")
        if self.kind in {"Id","Enum"}:
            if not identifier(self.name): raise Reject("INVALID_FIELD_SCHEMA")
        elif self.name: raise Reject("INVALID_FIELD_SCHEMA")
        if self.kind=="Enum":
            if not self.members or len(self.members)>64 or any(not identifier(v) for v in self.members) or len(set(self.members))!=len(self.members):
                raise Reject("INVALID_FIELD_SCHEMA")
        elif self.members: raise Reject("INVALID_FIELD_SCHEMA")
        if self.kind=="Record":
            if not self.fields or len(self.fields)>64: raise Reject("INVALID_FIELD_SCHEMA")
            names=[]
            for pair in self.fields:
                if type(pair) is not tuple or len(pair)!=2 or not identifier(pair[0]) or type(pair[1]) is not FieldType:
                    raise Reject("INVALID_FIELD_SCHEMA")
                names.append(pair[0]);pair[1].validate(depth+1,budget)
            if len(set(names))!=len(names): raise Reject("INVALID_FIELD_SCHEMA")
        elif self.fields: raise Reject("INVALID_FIELD_SCHEMA")
        if self.kind in {"Option","Set"}:
            if type(self.element) is not FieldType: raise Reject("INVALID_FIELD_SCHEMA")
            self.element.validate(depth+1,budget)
        elif self.element is not None: raise Reject("INVALID_FIELD_SCHEMA")
        return self

    def normalize(self,value):
        bad=False
        if self.kind=="Bool": bad=type(value) is not bool
        elif self.kind=="Int": bad=type(value) is not int or not self.lower<=value<=self.upper
        elif self.kind=="String":
            bad=type(value) is not str or len(value)>256 or any(0xD800<=ord(c)<=0xDFFF for c in value)
        elif self.kind in {"Id","Enum"}:
            bad=(type(value) is not Tagged or value.name!=self.name or type(value.value) is not str or not value.value or len(value.value)>256 or any(0xD800<=ord(c)<=0xDFFF for c in value.value))
            if not bad and self.kind=="Enum": bad=value.value not in self.members
        elif self.kind=="Record":
            if type(value) is Record:
                if (type(value.entries) is not tuple or any(type(p) is not tuple or len(p)!=2 or type(p[0]) is not str for p in value.entries)):
                    raise Reject("EVALUATION_TYPE_OR_RANGE")
                entries=dict(value.entries)
                if len(entries)!=len(value.entries): raise Reject("EVALUATION_TYPE_OR_RANGE")
            elif type(value) is dict: entries=value
            else: raise Reject("EVALUATION_TYPE_OR_RANGE")
            if set(entries)!=set(k for k,_ in self.fields): raise Reject("EVALUATION_TYPE_OR_RANGE")
            return Record(tuple((k,t.normalize(entries[k])) for k,t in sorted(self.fields)))
        elif self.kind=="Option":
            if value is None: return None
            if type(value) is not Some: raise Reject("EVALUATION_TYPE_OR_RANGE")
            return Some(self.element.normalize(value.value))
        elif self.kind=="Set":
            if type(value) is not frozenset or len(value)>self.max_size: raise Reject("EVALUATION_TYPE_OR_RANGE")
            return frozenset(self.element.normalize(v) for v in value)
        else: bad=True
        if bad: raise Reject("EVALUATION_TYPE_OR_RANGE")
        return value

    def check(self,value):
        try: self.normalize(value);return True
        except (Reject,TypeError,ValueError): return False

    def wire(self):
        result={"kind":self.kind}
        if self.kind=="Int": result.update(lower=str(self.lower),upper=str(self.upper))
        if self.kind in {"Id","Enum"}: result["name"]=self.name
        if self.kind=="Enum": result["members"]=list(self.members)
        if self.kind=="Record": result["fields"]={k:t.wire() for k,t in sorted(self.fields)}
        if self.kind in {"Option","Set"}: result["element"]=self.element.wire()
        if self.kind=="Set": result["max_size"]=str(self.max_size)
        return result

    def value_wire(self,value):
        if self.kind=="Int": return str(value)
        if self.kind in {"Id","Enum"}: return value.value
        if self.kind=="Record": return {k:t.value_wire(value.get(k)) for k,t in self.fields}
        if self.kind=="Option": return {"tag":"None"} if value is None else {"tag":"Some","value":self.element.value_wire(value.value)}
        if self.kind=="Set":
            from .canonical import canonical_bytes
            return sorted((self.element.value_wire(v) for v in value),key=canonical_bytes)
        return value

    def value_from_wire(self,value):
        if self.kind=="Int": value=integer(value)
        elif self.kind in {"Id","Enum"}: value=Tagged(self.name,value)
        elif self.kind=="Record":
            if type(value) is not dict or set(value)!=set(k for k,_ in self.fields): raise Reject("INVALID_IR_VALUE")
            value={k:t.value_from_wire(value[k]) for k,t in self.fields}
        elif self.kind=="Option":
            if type(value) is not dict: raise Reject("INVALID_IR_VALUE")
            if value=={"tag":"None"}: return None
            if set(value)!={"tag","value"} or value["tag"]!="Some": raise Reject("INVALID_IR_VALUE")
            value=Some(self.element.value_from_wire(value["value"]))
        elif self.kind=="Set":
            if type(value) is not list or len(value)>self.max_size: raise Reject("INVALID_IR_VALUE")
            decoded=frozenset(self.element.value_from_wire(v) for v in value)
            if len(decoded)!=len(value): raise Reject("DUPLICATE_SET_ELEMENT")
            value=decoded
        return self.normalize(value)

def type_from_wire(obj,depth=0):
    if depth>16 or type(obj) is not dict or type(obj.get("kind")) is not str: raise Reject("INVALID_FIELD_SCHEMA")
    kind=obj["kind"];kw={"kind":kind};keys={"kind"}
    if kind=="Int":
        keys|={"lower","upper"}
        if not keys<=set(obj): raise Reject("INVALID_FIELD_SCHEMA")
        kw.update(lower=integer(obj["lower"]),upper=integer(obj["upper"]))
    if kind in {"Id","Enum"}: keys.add("name");kw["name"]=obj.get("name")
    if kind=="Enum":
        keys.add("members")
        if type(obj.get("members")) is not list: raise Reject("INVALID_FIELD_SCHEMA")
        kw["members"]=tuple(obj["members"])
    if kind=="Record":
        keys.add("fields")
        if type(obj.get("fields")) is not dict: raise Reject("INVALID_FIELD_SCHEMA")
        kw["fields"]=tuple((k,type_from_wire(v,depth+1)) for k,v in sorted(obj["fields"].items()))
    if kind in {"Option","Set"}: keys.add("element");kw["element"]=type_from_wire(obj.get("element"),depth+1)
    if kind=="Set": keys.add("max_size");kw["max_size"]=integer(obj.get("max_size"))
    if set(obj)!=keys: raise Reject("INVALID_FIELD_SCHEMA")
    return FieldType(**kw).validate()

def same_type(a,b):
    if a.kind!=b.kind: return False
    if a.kind=="Int": return True
    if a.kind=="Record":
        return set(dict(a.fields))==set(dict(b.fields)) and all(same_type(t,dict(b.fields)[k]) for k,t in a.fields)
    if a.kind in {"Option","Set"}: return same_type(a.element,b.element)
    return a==b

def join_type(a,b):
    """Least interval/cardinality envelope for same-sort conditional branches.

    Derived expression integer intervals may exceed the input business bounds;
    arithmetic is mathematical, never machine wraparound.
    """
    if a.kind=="Int": return FieldType("Int",min(a.lower,b.lower),max(a.upper,b.upper))
    if a.kind=="Record":
        return FieldType("Record",fields=tuple((k,join_type(t,dict(b.fields)[k])) for k,t in sorted(a.fields)))
    if a.kind=="Option": return FieldType("Option",element=join_type(a.element,b.element))
    if a.kind=="Set": return FieldType("Set",element=join_type(a.element,b.element),max_size=max(a.max_size,b.max_size))
    return a
