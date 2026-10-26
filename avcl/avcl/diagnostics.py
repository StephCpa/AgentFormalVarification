"""Attach diagnostic provenance without turning source labels into authority."""
from .requirements import merge_requirements


def text_location(value, limit):
    if type(value) is not str or len(value)>limit:return None
    try:value.encode("utf-8",errors="strict")
    except UnicodeError:return None
    return value


def annotate(error,*,requirement_ids=(),node_id=None,source_ref=None,location=None,component=None,clause_id=None):
    error.requirement_ids=merge_requirements(error.requirement_ids,requirement_ids)
    for key,value,limit in (("node_id",node_id,512),("source_ref",source_ref,256),("location",location,1024),
                            ("component",component,64),("clause_id",clause_id,128)):
        if getattr(error,key,None) is None:
            setattr(error,key,text_location(value,limit))
    return error


def describe(error):
    """Data-only local compiler diagnostic; no input values, credentials or repr."""
    return {"format":"avcl/diagnostic/0.1","code":error.code,
            "requirement_ids":list(error.requirement_ids),
            "node_id":getattr(error,"node_id",None),
            "source_ref":getattr(error,"source_ref",None),
            "location":getattr(error,"location",None),
            "component":getattr(error,"component",None),"clause_id":getattr(error,"clause_id",None)}


def predicate_location(node_id):
    if type(node_id) is not str or not node_id.startswith("n0"):return None
    pieces=node_id.split(".")
    if pieces[0]!="n0" or any(not p.isascii() or not p.isdecimal() for p in pieces[1:]):return None
    return "/root"+"".join("/args/"+p for p in pieces[1:])


def workflow_location(node_id):
    if type(node_id) is not str:return None
    pieces=node_id.split(".")
    if pieces[0]!="w0":return None
    result="/body"
    for p in pieces[1:]:
        if p in {"then","else"}:result+="/"+("otherwise" if p=="else" else "then")
        elif p.isascii() and p.isdecimal():result+="/"+p
        else:return None
    return result
