"""Two separate bounded compositions, not a whole-system refinement proof.

Receipt model: one operation, actual cost 2, upper bound 3, two generated
facts with independent delivery and replay. Sender model: one workflow,
boolean branch, shared queue consumer, one packet, and restart self-steps.
No cryptography, SQL scheduling, multiple sources or arbitrary costs modeled.
"""
from collections import deque
from dataclasses import dataclass, replace, asdict
import json


@dataclass(frozen=True)
class ReceiptState:
    actual: int=0
    generated: int=0
    closed: bool=False
    spent: int=0
    pending: int=3
    final: bool=False
    final_seen: bool=False


def receipt_steps(s, broken=False):
    if s.actual==0:yield 'RemoteEffect',replace(s,actual=2)
    if s.actual==2 and not s.closed and not s.generated&1:
        yield 'GenerateOlderPartial',replace(s,generated=s.generated|1)
    if s.generated&1 and not s.closed:yield 'RemoteClose',replace(s,closed=True)
    if s.closed and not s.generated&2:yield 'GenerateNewerFinal',replace(s,generated=s.generated|2)
    if s.generated&1:
        yield 'DeliverOlder',s if s.final and not broken else replace(s,spent=2,pending=1,final=False)
    if s.generated&2:yield 'DeliverFinal',replace(s,spent=2,pending=0,final=True,final_seen=True)
    yield 'Restart',s


def receipt_errors(s):
    if s.final_seen and (not s.final or s.pending!=0):yield 'terminal_knowledge_retracted'
    if s.actual>s.spent+s.pending:yield 'actual_not_covered'


@dataclass(frozen=True)
class SendState:
    pc: int=0
    selected: bool=False
    admitted: bool=False
    intent: bool=False
    claimed: bool=False
    packet: bool=False
    effect: bool=False


def send_steps(s, broken=False):
    if s.pc==0:yield 'PrepareOwned',replace(s,pc=1)
    if s.pc==1:yield 'Dispatch',replace(s,pc=2,admitted=True)
    if s.pc==2:
        yield 'ChooseFalse',replace(s,pc=4)
        yield 'ChooseTrue',replace(s,pc=3,selected=True)
    if s.pc==3:
        yield 'SendIntentCommit',replace(s,pc=4,intent=True,claimed=True,packet=not s.effect)
        yield 'SendIntentRollback',s
    if s.admitted and not s.claimed:
        if broken:yield 'GenericClaim',replace(s,claimed=True,packet=True)
        else:yield 'GenericClaimBlocked',s
    if s.packet:yield 'RemoteApply',replace(s,packet=False,effect=True)
    yield 'Restart',s


def send_errors(s):
    if (s.packet or s.effect) and not s.intent:yield 'send_without_workflow_intent'
    if s.intent and not s.selected:yield 'send_on_unselected_branch'


def explore(kind,broken=False):
    initial,steps,errors=(ReceiptState(),receipt_steps,receipt_errors) if kind=='receipt' else (SendState(),send_steps,send_errors)
    queue=deque([initial]);paths={initial:()};violations={};edges=0
    while queue:
        s=queue.popleft()
        for error in errors(s):violations.setdefault(error,{'trace':list(paths[s]),'state':asdict(s)})
        for action,nxt in steps(s,broken):
            edges+=1
            if nxt not in paths:paths[nxt]=paths[s]+(action,);queue.append(nxt)
    return {'kind':kind,'broken':broken,'states':len(paths),'edges':edges,
            'exploration_complete':True,'violations':violations},paths


def main():
    correct=[explore(k)[0] for k in ('receipt','send')]
    mutations=[explore(k,True)[0] for k in ('receipt','send')]
    assert all(not r['violations'] for r in correct)
    assert all(r['violations'] for r in mutations)
    print(json.dumps({'scope':__doc__,'correct':correct,'mutation_oracles':mutations,'passed':True},indent=2))


if __name__=='__main__':main()
