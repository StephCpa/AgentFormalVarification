"""Exhaustive two-operation, one-account model with separate remote effects.

No runtime imports. The two broken modes are mutation oracles, not supported
runtime options. Bounds: two requests, costs 70/60, budget 100, one revocation,
no fees, no crashes, reliable terminal receipts, unbounded remote deduplication.
"""
from collections import deque, Counter
from dataclasses import dataclass, replace, asdict
import json

COSTS=(70,60)
LIMIT=100


def set_at(values,i,value): return values[:i]+(value,)+values[i+1:]


@dataclass(frozen=True)
class State:
    local: tuple=("N","N")  # N absent, C checked, P prepared, D dispatched,
    # U unknown, F terminal receipt, X aborted before dispatch.
    pending: tuple=(0,0)
    spent: tuple=(0,0)
    remote: tuple=(0,0)  # 0 absent, 1 effect, 2 durable rejection tombstone
    admissions: tuple=(0,0)
    revoked: bool=False

    @property
    def actual(self): return sum(c for c,r in zip(COSTS,self.remote) if r==1)


def transitions(s, mode="correct"):
    if not s.revoked:
        yield "revoke",replace(s,revoked=True)
    for i,c in enumerate(COSTS):
        status=s.local[i]
        def out(action, **changes): return f"{action}:{i}",replace(s,**changes)
        if status=="N" and not s.revoked and sum(s.pending)+sum(s.spent)+c<=LIMIT:
            if mode=="split_check":
                yield out("check",local=set_at(s.local,i,"C"))
            else:
                yield out("prepare",local=set_at(s.local,i,"P"),pending=set_at(s.pending,i,c))
        if status=="C" and not s.revoked:
            yield out("commit_without_recheck",local=set_at(s.local,i,"P"),pending=set_at(s.pending,i,c))
        if status=="P":
            yield out("abort",local=set_at(s.local,i,"X"),pending=set_at(s.pending,i,0))
            if not s.revoked:
                yield out("dispatch",local=set_at(s.local,i,"D"),admissions=set_at(s.admissions,i,1))
        if s.admissions[i]:
            yield out("repeat_dispatch")
            if s.remote[i]==0:
                yield out("remote_apply",remote=set_at(s.remote,i,1))
                yield out("remote_cancel",remote=set_at(s.remote,i,2))
            else:
                yield out("remote_retry")
            if status=="D":
                pending=set_at(s.pending,i,0) if mode=="early_release" else s.pending
                yield out("timeout",local=set_at(s.local,i,"U"),pending=pending)
            if s.remote[i] and status in ("D","U"):
                yield out("receipt",local=set_at(s.local,i,"F"),pending=set_at(s.pending,i,0),
                          spent=set_at(s.spent,i,c if s.remote[i]==1 else 0))


def violations(s):
    errors=[]
    if sum(s.spent)+sum(s.pending)>LIMIT: errors.append("ledger_exceeds_budget")
    if s.actual>sum(s.spent)+sum(s.pending): errors.append("actual_exceeds_exposure")
    if s.actual>LIMIT: errors.append("actual_exceeds_budget")
    if any(x>1 for x in s.admissions): errors.append("duplicate_admission")
    if any(x<0 for x in s.spent+s.pending): errors.append("negative_account")
    return errors


def explore(mode="correct"):
    initial=State();queue=deque([initial]);paths={initial:()};edges=0;actions=Counter();first=None
    while queue:
        s=queue.popleft()
        for action,nxt in transitions(s,mode):
            edges+=1;actions[action.split(":")[0]]+=1
            path=paths[s]+(action,)
            errors=violations(nxt)
            if errors and first is None:
                first={"trace":list(path),"violations":errors,"state":asdict(nxt),"actual":nxt.actual}
            if nxt not in paths:
                paths[nxt]=path;queue.append(nxt)
    return {"mode":mode,"states":len(paths),"edges":edges,"actions":dict(sorted(actions.items())),
            "counterexample":first,"exploration_complete":True}, paths


def report():
    results=[explore(m)[0] for m in ("correct","split_check","early_release")]
    assert results[0]["counterexample"] is None
    assert all(r["counterexample"] for r in results[1:])
    return {"bounds":{"operations":2,"costs":COSTS,"budget":LIMIT,"revocations":1,
                      "fees":False,"crashes":False,"remote_dedup_expiry":False},"runs":results}


if __name__=="__main__":
    print(json.dumps(report(),ensure_ascii=False,indent=2))
