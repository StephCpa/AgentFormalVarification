"""One logical operation, two packets, time 0..4, fixed acceptance cutoff 2.

Server records live for two ticks, never slide on retry. Packet admission is
not arrival: delayed packets survive sender crashes. No order-version defense
is assumed, so this model isolates the deadline/dedup contract itself.
"""
from collections import deque
from dataclasses import dataclass,replace,asdict
import json


@dataclass(frozen=True)
class State:
    now: int=0
    attempts: int=0
    packets: int=0
    record_until: int=0
    effects: int=0
    unknown: bool=False
    pending: int=1


def successors(s,mode="fenced"):
    if s.now<4:
        now=s.now+1
        yield "Tick",replace(s,now=now,record_until=0 if now>=s.record_until else s.record_until)
    if s.attempts<2 and (s.now<2 or mode=="late_sender"):
        yield "AdmitPacket",replace(s,attempts=s.attempts+1,packets=s.packets+1)
    if s.packets:
        if s.now>=2 and mode!="unfenced_server":
            yield "RemoteRejectExpired",replace(s,packets=s.packets-1)
        elif s.record_until>s.now:
            yield "RemoteDeduplicate",replace(s,packets=s.packets-1)
        else:
            yield "RemoteApply",replace(s,packets=s.packets-1,record_until=s.now+2,effects=s.effects+1)
    if not s.unknown:
        yield "LoseReceiptOrWorker",replace(s,unknown=True)
    if s.unknown and s.record_until==0:
        yield "QueryAbsent",replace(s,pending=0) if mode=="absence_releases" else s


def explore(mode="fenced"):
    if mode not in {"fenced","late_sender","unfenced_server","absence_releases"}:raise ValueError(mode)
    initial=State();queue=deque([initial]);paths={initial:()};edges=0;violations={}
    while queue:
        s=queue.popleft()
        for error,failed in (("duplicate_effect",s.effects>1),("actual_exceeds_exposure",s.effects>s.pending)):
            if failed:violations.setdefault(error,{"trace":list(paths[s]),"state":asdict(s)})
        for action,nxt in successors(s,mode):
            edges+=1
            if action=="AdmitPacket" and s.now>=2:
                violations.setdefault("late_sender_admission",{"trace":list(paths[s])+[action],"state":asdict(nxt)})
            if nxt not in paths:paths[nxt]=paths[s]+(action,);queue.append(nxt)
    return {"mode":mode,"states":len(paths),"edges":edges,"violations":violations,"exploration_complete":True},paths


def main():
    correct,_=explore()
    assert not correct["violations"]
    mutants=[]
    for mode,error in (("late_sender","late_sender_admission"),("unfenced_server","duplicate_effect"),("absence_releases","actual_exceeds_exposure")):
        report,_=explore(mode)
        assert error in report["violations"]
        mutants.append(report)
    print(json.dumps({"scope":__doc__,"correct":correct,"mutation_oracles":mutants,"passed":True},indent=2))


if __name__=="__main__":main()
