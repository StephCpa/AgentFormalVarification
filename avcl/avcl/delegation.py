"""Online shared-ledger delegation; no escrow or offline spending authority."""
from dataclasses import replace
import secrets
from .canonical import Reject,amount,digest,strict_loads
from .types import Capability,DelegationGrant,Account

TOOLS=frozenset({"refund"})
EFFECTS=frozenset({"Write","Disclose","ResourceConsume"})
MAX_DEPTH=8

def text(value):
    return type(value) is str and 0<len(value)<=256 and all(ord(c)>=32 and not 0xD800<=ord(c)<=0xDFFF for c in value)

def chain(k,ref,live=True):
    """Includes root. Structural checks also apply to revoked historical grants."""
    result=[];seen=set()
    while ref is not None:
        if ref in seen or len(result)>MAX_DEPTH:raise Reject("INVALID_DELEGATION_CHAIN")
        seen.add(ref)
        cap=k._caps.get(ref);grant=k._grants.get(ref)
        if cap is None or grant is None or grant.capability_ref!=ref:raise Reject("INVALID_DELEGATION_CHAIN")
        if cap.tenant!=k.tenant or grant.account not in k._accounts:raise Reject("INVALID_DELEGATION_CHAIN")
        if live and (grant.revoked or cap.epoch!=k.epoch or cap.expires<=k._clock() or not k._subjects.get(cap.subject,False)):
            raise Reject("AUTH_DENIED")
        if result:
            child,child_grant=result[-1]
            if (not grant.can_delegate or not child.orders<=cap.orders or child.max_amount>cap.max_amount or
                    child.expires>cap.expires or not child_grant.tools<=grant.tools or not child_grant.effects<=grant.effects or
                    k._accounts[child_grant.account].limit>k._accounts[grant.account].limit):raise Reject("INVALID_DELEGATION_CHAIN")
        result.append((cap,grant));ref=grant.parent_ref
    if len({g.account for _,g in result})!=len(result):raise Reject("INVALID_DELEGATION_CHAIN")
    return result

def admission(k,request,cap_ref):
    if cap_ref not in k._grants:return ()
    links=chain(k,cap_ref)
    if request.tool not in links[0][1].tools or not EFFECTS<=links[0][1].effects:raise Reject("SCOPE_DENIED")
    return tuple(grant.account for _,grant in links)

def register_subject(k,subject):
    if not text(subject) or subject in k._subjects:raise Reject("INVALID_SUBJECT")
    k._subjects[subject]=True
    k._event("DelegationSubjectRegistered",subject=subject,requirement_id="R11")

def disable_subject(k,subject):
    if subject not in k._subjects:raise Reject("AUTH_DENIED")
    k._subjects[subject]=False
    k._event("DelegationSubjectDisabled",subject=subject,requirement_id="R11")

def enable(k,cap_ref,limit,can_delegate=True,tools=TOOLS,effects=EFFECTS):
    cap=k._lookup(k._caps,cap_ref)
    if (type(limit) is not int or limit<=0 or type(can_delegate) is not bool or type(tools) is not frozenset or
            not tools or not tools<=TOOLS or type(effects) is not frozenset or not effects<=EFFECTS):raise Reject("INVALID_DELEGATION")
    if cap_ref in k._grants or any(op.capability_ref==cap_ref for op in k._operations.values()):raise Reject("DELEGATION_ROOT_ALREADY_USED")
    if cap.tenant!=k.tenant or cap.epoch!=k.epoch or cap.expires<=k._clock() or not k._subjects.get(cap.subject):raise Reject("AUTH_DENIED")
    root=k._lookup(k._accounts,"tenant:CNY")
    if root.breached or limit>root.limit-root.spent-root.pending:raise Reject("BUDGET_EXCEEDED", requirement_ids=("R11",))
    account="delegation:"+secrets.token_hex(32)+":CNY"
    k._accounts[account]=Account(limit)
    k._grants[cap_ref]=DelegationGrant(cap_ref,None,account,can_delegate,tools,effects)
    k._event("DelegationRootRegistered",capability_ref=cap_ref,account=account,limit=limit,requirement_id="R11")
    return account

def revoke(k,cap_ref):
    grant=k._grants.get(cap_ref)
    if grant is None:raise Reject("AUTH_DENIED")
    if not grant.revoked:
        k._grants[cap_ref]=replace(grant,revoked=True)
        k._event("CapabilityRevoked",capability_ref=cap_ref,requirement_id="R11")

def parse(raw):
    body=strict_loads(raw)
    if type(body) is not dict or set(body)!={"client_request_id","parent_ref","child_subject","attenuations"}:raise Reject("INVALID_DELEGATION")
    if any(not text(body[key]) for key in ("client_request_id","parent_ref","child_subject")) or len(body["client_request_id"])>128:raise Reject("INVALID_DELEGATION")
    spec=body["attenuations"]
    if type(spec) is not dict or set(spec)!={"orders","tools","effects","max_amount_minor","limit_minor","expires","can_delegate"}:raise Reject("INVALID_DELEGATION")
    for key in ("orders","tools","effects"):
        value=spec[key]
        if type(value) is not list or len(value)>64 or not all(text(v) for v in value) or len(set(value))!=len(value):raise Reject("INVALID_DELEGATION")
    if not spec["orders"] or not spec["tools"] or type(spec["can_delegate"]) is not bool:raise Reject("INVALID_DELEGATION")
    numbers={key:amount(spec[key]) for key in ("max_amount_minor","limit_minor","expires")}
    if any(value==0 for value in numbers.values()):raise Reject("INVALID_DELEGATION")
    return body,spec,numbers

def project(k,ref):
    cap=k._caps[ref];g=k._grants[ref]
    return {"child_cap_ref":ref,"parent_ref":g.parent_ref,"child_subject":cap.subject,"orders":sorted(cap.orders),
            "tools":sorted(g.tools),"effects":sorted(g.effects),"max_amount_minor":str(cap.max_amount),
            "limit_minor":str(k._accounts[g.account].limit),"expires":str(cap.expires),"can_delegate":g.can_delegate}

def delegate(k,subject,raw):
    body,spec,numbers=parse(raw)
    if getattr(k,"_clock_rollback",False):raise Reject("CLOCK_ROLLBACK")
    if k._quarantined:raise Reject("ASSUMPTION_BROKEN")
    if not k._release.issued_at<=k._clock()<k._release.expires:raise Reject("POLICY_EXPIRED_OR_NOT_YET_VALID")
    parent_ref=body["parent_ref"];parent=k._caps.get(parent_ref)
    if parent is None or parent.subject!=subject or parent_ref not in k._grants:raise Reject("AUTH_DENIED")
    links=chain(k,parent_ref);grant=links[0][1]
    if not grant.can_delegate or not k._subjects.get(body["child_subject"],False):raise Reject("AUTH_DENIED")
    if len(links)>MAX_DEPTH:raise Reject("DELEGATION_DEPTH_EXCEEDED")
    if (body["child_subject"] in {cap.subject for cap,_ in links} or not frozenset(spec["orders"])<=parent.orders or
            numbers["max_amount_minor"]>parent.max_amount or not k._clock()<numbers["expires"]<=parent.expires or
            not frozenset(spec["tools"])<=grant.tools or not frozenset(spec["effects"])<=grant.effects):raise Reject("DELEGATION_NOT_ATTENUATED")
    submitted=digest(body,"avcl/delegation/0.1");key=(subject,body["client_request_id"])
    prior=k._delegation_requests.get(key)
    if prior:
        if prior[0]!=submitted:raise Reject("IDEMPOTENCY_CONFLICT")
        chain(k,prior[1]);return project(k,prior[1])
    accounts=[k._accounts[g.account] for _,g in links]+[k._lookup(k._accounts,"tenant:CNY")]
    if any(a.breached or numbers["limit_minor"]>a.limit-a.spent-a.pending for a in accounts):raise Reject("BUDGET_EXCEEDED", requirement_ids=("R11",))
    ref="cap-"+secrets.token_hex(32);account="delegation:"+secrets.token_hex(32)+":CNY"
    child=Capability(ref,k.tenant,body["child_subject"],frozenset(spec["orders"]),numbers["max_amount_minor"],numbers["expires"],k.epoch)
    k._caps[ref]=child;k._accounts[account]=Account(numbers["limit_minor"])
    k._grants[ref]=DelegationGrant(ref,parent_ref,account,spec["can_delegate"],frozenset(spec["tools"]),frozenset(spec["effects"]))
    k._delegation_requests[key]=(submitted,ref)
    k._event("CapabilityDelegated",parent_ref=parent_ref,capability_ref=ref,subject=subject,child_subject=child.subject,
             account=account,limit=numbers["limit_minor"],request_id=body["client_request_id"],requirement_id="R11")
    return project(k,ref)

def expand_costs(k,op,costs):
    """Derive local quota usage from the trusted adapter's total, never its input."""
    if op.capability_ref not in k._grants:return costs
    expected={"tenant:CNY","order:"+op.request.order+":CNY"}
    if set(costs)!=expected:raise Reject("INVALID_COST_VECTOR")
    accounts={g.account for _,g in chain(k,op.capability_ref,False)}
    if {r.account for r in op.reservations}!=expected|accounts:raise Reject("INVALID_DELEGATION_RESERVATION")
    return {**costs,**{account:costs["tenant:CNY"] for account in accounts}}
