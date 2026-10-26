"""Finite, offline IR checks; exact declared domains or explicit Unknown.

This enumerator is not an SMT solver. It emits no solver Sat/Unsat result and
never promotes enumeration to ProvedUnder. Runtime evaluation is separate.
"""
from datetime import datetime,timezone
from enum import StrEnum
from itertools import combinations,product
from math import comb
from .ir_types import Some,Tagged,Record
from .checks import compile_checks,COMPILER
from .canonical import Reject

class PropertyStatus(StrEnum):
    PROVED_UNDER="ProvedUnder"
    BOUNDED_CHECKED="BoundedChecked"
    RUNTIME_SATISFIED="RuntimeSatisfied"
    PENDING="Pending"
    VIOLATED="Violated"
    ASSUMPTION_BROKEN="AssumptionBroken"
    UNKNOWN="Unknown"

class DomainTooLarge(Exception): pass

def cardinality(t,limit):
    if t.kind=="Bool": n=2
    elif t.kind=="Int": n=t.upper-t.lower+1
    elif t.kind=="Enum": n=len(t.members)
    elif t.kind=="Option": n=1+cardinality(t.element,limit)
    elif t.kind=="Record":
        n=1
        for _,field in t.fields:
            n*=cardinality(field,limit)
            if n>limit: raise DomainTooLarge()
    elif t.kind=="Set":
        size=cardinality(t.element,limit);n=0
        for i in range(min(size,t.max_size)+1):
            n+=comb(size,i)
            if n>limit: raise DomainTooLarge()
    else: raise DomainTooLarge()
    if n>limit: raise DomainTooLarge()
    return n

def domain(t):
    if t.kind=="Bool": return [False,True]
    if t.kind=="Int": return list(range(t.lower,t.upper+1))
    if t.kind=="Enum": return [Tagged(t.name,v) for v in t.members]
    if t.kind=="Option": return [None]+[Some(v) for v in domain(t.element)]
    if t.kind=="Set":
        values=domain(t.element)
        return [frozenset(items) for n in range(min(len(values),t.max_size)+1) for items in combinations(values,n)]
    if t.kind=="Record":
        return [Record(tuple((name,value) for (name,_),value in zip(t.fields,values)))
                for values in product(*(domain(field) for _,field in t.fields))]
    raise DomainTooLarge()

def verify_finite(predicate,max_cases=10000,*,evaluation_limit=None):
    from .evaluation import WorkBudget, DEFAULT_WORK_LIMIT, WORK_MODEL
    limit=DEFAULT_WORK_LIMIT if evaluation_limit is None else evaluation_limit
    WorkBudget(limit)
    if type(max_cases) is not int or not 1<=max_cases<=100000: raise Reject("INVALID_VERIFICATION_BOUND")
    checks=compile_checks(predicate);predicate=checks.predicate
    base={"requirement_ids":[predicate.requirement_id],"ir_digest":predicate.semantic_digest,
          "source_digest":predicate.source_digest,"checker_build":COMPILER,
          "assumption_ids":["pure-ir-typing","python-runtime"],"solver_status":None,
          "bounds":{"fields":{k:t.wire() for k,t in predicate.fields},"max_cases":str(max_cases),
                    "evaluation_work_model":WORK_MODEL,"evaluation_work_limit":str(limit)},
          "checked_at":datetime.now(timezone.utc).isoformat()}
    def result(prop,status,**kwargs): return {**base,"property_id":prop,"status":str(status),**kwargs}
    try:
        count=1
        for _,t in predicate.fields:
            count*=cardinality(t,max_cases)
            if count>max_cases: raise DomainTooLarge()
    except DomainTooLarge:
        return [result(prop,PropertyStatus.UNKNOWN,checked_cases="0",reason="declared_domain_exceeds_enumeration_profile")
                for prop in ("checks-equivalent","allowed-action-exists")]
    accepted=None;rejected=None;checked=0
    for values in product(*(domain(t) for _,t in predicate.fields)):
        env={k:v for (k,_),v in zip(predicate.fields,values)};checked+=1
        try:
            a=predicate.evaluate(env,budget=WorkBudget(limit));b=checks.evaluate(env,budget=WorkBudget(limit))
        except Reject as exc:
            return [result(prop,PropertyStatus.UNKNOWN,checked_cases=str(checked),reason=exc.code,
                           complete_declared_domain=False) for prop in ("checks-equivalent","allowed-action-exists")]
        witness={k:t.value_wire(v) for (k,t),v in zip(predicate.fields,values)}
        if a!=b:
            return [result("checks-equivalent",PropertyStatus.VIOLATED,checked_cases=str(checked),witness=witness),
                    result("allowed-action-exists",PropertyStatus.UNKNOWN,checked_cases=str(checked),reason="backend_disagreement")]
        if a and accepted is None: accepted=witness
        if not a and rejected is None: rejected=witness
    return [result("checks-equivalent",PropertyStatus.BOUNDED_CHECKED,checked_cases=str(checked),complete_declared_domain=True),
            result("allowed-action-exists",PropertyStatus.BOUNDED_CHECKED if accepted is not None else PropertyStatus.VIOLATED,
                   checked_cases=str(checked),complete_declared_domain=True,witness=accepted,rejected_witness=rejected)]

def runtime_report(predicate,values,*,budget=None):
    from .evaluation import use_budget
    budget=use_budget(budget)
    try:
        allowed=compile_checks(predicate).evaluate(values,budget=budget)
        status=PropertyStatus.RUNTIME_SATISFIED if allowed else PropertyStatus.VIOLATED
        reason=None
    except Reject as exc:
        allowed=False;status=PropertyStatus.UNKNOWN;reason=exc.code
    return {"property_id":"current-input-satisfies-predicate","requirement_ids":[predicate.requirement_id],
            "ir_digest":predicate.semantic_digest,"status":str(status),"allow":allowed,"reason":reason,
            "evaluation_work":budget.report(),
            "scope":"this evaluated input only; no authorization or future-state guarantee"}
