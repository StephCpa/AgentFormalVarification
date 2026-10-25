"""Closed data-only codec for durable reference records; never pickle/eval."""
from dataclasses import fields
import re
from . import types as model
from .contract import Health
from .canonical import Reject,canonical_bytes,strict_loads

CLASSES={c.__name__:c for c in (model.Request,model.Capability,model.Evidence,model.Approval,
    model.Operation,model.Reservation,model.Observation,model.Event,model.DelegationGrant,Health)}
ENUMS={c.__name__:c for c in (model.Execution,model.Settlement)}

def encode(value):
    if value is None or type(value) in (str,bool): return value
    if type(value) is int: return {"$int":str(value)}
    if type(value) in ENUMS.values(): return {"$enum":type(value).__name__,"value":str(value)}
    if type(value) is tuple: return {"$tuple":[encode(v) for v in value]}
    if type(value) is frozenset: return {"$set":sorted((encode(v) for v in value),key=canonical_bytes)}
    if type(value) in CLASSES.values():
        return {"$record":type(value).__name__,"fields":{f.name:encode(getattr(value,f.name)) for f in fields(value)}}
    raise Reject("UNSUPPORTED_STORAGE_VALUE")

def decode(value,depth=0):
    if depth>64: raise Reject("INVALID_STORAGE_DEPTH")
    if value is None or type(value) in (str,bool): return value
    if type(value) is not dict: raise Reject("INVALID_STORAGE_VALUE")
    if set(value)=={"$int"}:
        raw=value["$int"]
        if type(raw) is not str or re.fullmatch(r"0|-?[1-9][0-9]*",raw) is None: raise Reject("INVALID_STORAGE_INTEGER")
        return int(raw)
    if set(value)=={"$enum","value"} and type(value["$enum"]) is str and value["$enum"] in ENUMS:
        try: return ENUMS[value["$enum"]](value["value"])
        except (ValueError,TypeError): raise Reject("INVALID_STORAGE_ENUM") from None
    if set(value) in ({"$tuple"},{"$set"}):
        tag=next(iter(value))
        if type(value[tag]) is not list: raise Reject("INVALID_STORAGE_SEQUENCE")
        items=tuple(decode(v,depth+1) for v in value[tag])
        if tag=="$set":
            try: result=frozenset(items)
            except TypeError: raise Reject("INVALID_STORAGE_SEQUENCE") from None
            if len(result)!=len(items): raise Reject("INVALID_STORAGE_SEQUENCE")
            return result
        return items
    if set(value)=={"$record","fields"} and type(value["$record"]) is str and value["$record"] in CLASSES:
        cls=CLASSES[value["$record"]];content=value["fields"]
        if type(content) is not dict or set(content)!={f.name for f in fields(cls)}: raise Reject("INVALID_STORAGE_RECORD")
        return cls(**{k:decode(v,depth+1) for k,v in content.items()})
    raise Reject("INVALID_STORAGE_VALUE")

def dumps(value): return canonical_bytes(encode(value)).decode("utf-8")
def loads(raw): return decode(strict_loads(raw))
