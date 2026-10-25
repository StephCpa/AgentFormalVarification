"""Independent two-operation capacity model and deliberately incorrect gates.

One future reconciliation obligation per operation; no fees, crashes or adapter
violations. Escalation transfers responsibility but preserves active exposure.
"""
from collections import deque
import json

ACTIVE={"Prepared","Dispatched","Unknown","HandedOff"}
LIMIT=1

def successors(state,mode):
    active=sum(s in ACTIVE for s in state)
    gate=(sum(s=="Unknown" for s in state) if mode=="count_only_existing_obligations" else
          sum(s in ACTIVE-{"HandedOff"} for s in state) if mode=="release_on_handoff" else active)
    for index,status in enumerate(state):
        changes=[]
        if status=="Absent" and gate<LIMIT:changes.append(("Prepare","Prepared"))
        if status=="Prepared":changes.extend((("Dispatch","Dispatched"),("Abort","Final")))
        if status=="Dispatched":changes.extend((("Timeout","Unknown"),("ObserveFinal","Final")))
        if status=="Unknown":changes.extend((("AcceptHandoff","HandedOff"),("ObserveFinal","Final")))
        if status=="HandedOff":changes.append(("ObserveFinal","Final"))
        for action,new in changes:
            yield (f"{action}({index})",state[:index]+(new,)+state[index+1:])

def explore(mode):
    initial=("Absent","Absent");queue=deque([initial]);paths={initial:[]};edges=0;violations={}
    while queue:
        state=queue.popleft();active=sum(s in ACTIVE for s in state);open_count=state.count("Unknown")
        for name,violated in (("reserved_future_slots",active>LIMIT),("actual_open_obligations",open_count>LIMIT)):
            if violated and name not in violations:violations[name]={"trace":paths[state],"state":state,"active":active,"open":open_count}
        for action,nxt in successors(state,mode):
            edges+=1
            if nxt not in paths:paths[nxt]=paths[state]+[action];queue.append(nxt)
    return {"mode":mode,"states":len(paths),"edges":edges,"violations":violations}

def main():
    correct=explore("reserve_before_prepare")
    late=explore("count_only_existing_obligations")
    handoff=explore("release_on_handoff")
    assert correct["violations"]=={}
    assert "actual_open_obligations" in late["violations"]
    assert "reserved_future_slots" in handoff["violations"]
    assert any("AcceptHandoff" in action for action in handoff["violations"]["reserved_future_slots"]["trace"])
    print(json.dumps({"scope":"two operations; one future obligation each; capacity one; no adapter violations; independent abstract model, not SQL refinement",
        "correct":correct,"mutation_oracles":[late,handoff],"passed":True},indent=2))

if __name__=="__main__":main()
