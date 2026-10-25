"""Finite shared-budget and ancestor-revocation model; not SQL refinement.

Two already authenticated child grants, per-child limit two, shared limit
three, one cost-two operation per child. Final receipts are authoritative.
"""
from collections import deque
import json

EXPOSED={"Prepared","Dispatched","Unknown","Final"}

def successors(state,mode):
    statuses,revoked=state;exposure=2*sum(s in EXPOSED for s in statuses)
    if not revoked:yield "RevokeAncestor",(statuses,True)
    for index,status in enumerate(statuses):
        changes=[]
        if status=="Absent" and not revoked and (mode=="omit_shared_gate" or exposure+2<=3):changes.append(("Prepare","Prepared"))
        if status=="Prepared":
            changes.append(("Abort","Aborted"))
            if not revoked or mode=="omit_ancestor_recheck":changes.append(("Dispatch","Dispatched"))
        if status=="Dispatched":changes.extend((("Timeout","Unknown"),("ObserveFinal","Final")))
        if status=="Unknown":changes.append(("ObserveFinal","Final"))
        for action,new in changes:yield f"{action}({index})",(statuses[:index]+(new,)+statuses[index+1:],revoked)

def explore(mode):
    initial=(("Absent","Absent"),False);queue=deque([initial]);paths={initial:[]};edges=0;violations={}
    while queue:
        state=queue.popleft();statuses,revoked=state
        if 2*sum(s in EXPOSED for s in statuses)>3 and "shared_budget" not in violations:
            violations["shared_budget"]={"trace":paths[state],"state":state}
        for action,nxt in successors(state,mode):
            edges+=1
            if revoked and action.startswith("Dispatch") and "revocation_cut" not in violations:
                violations["revocation_cut"]={"trace":paths[state]+[action],"state":nxt}
            if nxt not in paths:paths[nxt]=paths[state]+[action];queue.append(nxt)
    return {"mode":mode,"states":len(paths),"edges":edges,"violations":violations}

def main():
    correct=explore("shared_ledger");budget=explore("omit_shared_gate");authority=explore("omit_ancestor_recheck")
    assert correct["violations"]=={}
    assert "shared_budget" in budget["violations"]
    assert "revocation_cut" in authority["violations"]
    print(json.dumps({"scope":"two child grants; cost two each; child limits two; shared limit three; ancestor revoke; no fees or crashes; not a proof of general P7 or SQL refinement",
        "correct":correct,"mutation_oracles":[budget,authority],"passed":True},indent=2))

if __name__=="__main__":main()
