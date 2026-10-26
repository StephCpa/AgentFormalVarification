"""Additional compatibility checks, kept separate from the fixed full suite."""
from pathlib import Path
import sys,json
from dataclasses import replace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'integration')]
from tests.support import setup,authorize,prepare
from tests.test_delegation import provision,payload,authorization,prepare as prepare_child
from tests.test_workflow import make,context,P,D,S
from test_postgres import PersistenceTests
from avcl.types import Settlement
from avcl.contract import Health
from avcl.workflow_postgres import DurableWorkflowExecutor
from avcl.simulator import RefundService,Order
from avcl.canonical import Reject,amount

results=[]
k,s,_=setup();provision(k)
child=k.delegate('parent',payload())['child_cap_ref']
op=prepare_child(k,authorization(k,s,child,value=60,fee=10));op=k.dispatch('child',op.id,op.permit_ref)
receipt=s.send(op);k.trusted.observe(receipt)
k.trusted.observe(replace(receipt,id='old-weaker',final_cost=False))
current=k.get('child',op.id)
assert current.settlement==Settlement.FINAL and len(current.reservations)==4
assert all(r.charged==60 and r.remaining==0 for r in current.reservations)
assert not k._quarantined
results.append({'check':'F1-delegated-accounts','passed':True,'accounts':4,'spent_each':60,'pending_each':0})

PersistenceTests.setUpClass()
t=PersistenceTests();t.setUp()
try:
    s=RefundService({'a':Order('v1','original-a',100)},retention_seconds=120,clock=t.clock,verifier=t.k.trusted.send_verifier())
    ex=DurableWorkflowExecutor(t.k,s);ex.start(make([P,D,S]),'a','halt',context(t.k,s),{'flag':True})
    ex.advance('a','halt','worker');ex.advance('a','halt','worker')
    t.k.trusted.set_assumption_health('remote-idempotency',Health('simulator','refund',0,1000,'Broken'))
    assert ex.resume('a','halt').status=='Halted'
    t.k.trusted.set_assumption_health('remote-idempotency',Health('simulator','refund',0,1000,'Healthy'))
    assert t.fresh().claim_outbox('generic') is None and not s.effects
    results.append({'check':'F2-halted-ownership-after-health-restored','passed':True,'effects':0})
finally:t.tearDown()

t=PersistenceTests();t.setUp()
try:
    maximum=10**18-1
    t.query("UPDATE budget_account SET limit_amount=%s WHERE id='tenant:CNY'",(2*maximum,))
    s=RefundService({'a':Order('v1','original-a',100)},fee=maximum,retention_seconds=120,clock=t.clock,verifier=t.k.trusted.send_verifier())
    op=prepare(t.k,authorize(t.k,s,value=1,fee=maximum));op=t.k.dispatch('a',op.id,op.permit_ref)
    raw=s.send(t.k.claim_outbox('sender',authenticated_receipts=True)['envelope'])
    # Emulate only the old decoder to create an actual persisted rejection row.
    with patch('avcl.receipt_envelope.cumulative_cost',amount):
        try:t.k.trusted.observe(raw,op.id)
        except Reject as e:assert e.code=='INVALID_AMOUNT'
        else:raise AssertionError('old decoder unexpectedly accepted')
    assert t.query('SELECT applied FROM observation_application')[0]['applied'] is False
    assert t.fresh().trusted.observe(raw,op.id).settlement==Settlement.FINAL
    assert t.k.accounts()['tenant:CNY'].spent==10**18 and t.k.accounts()['tenant:CNY'].pending==0
    assert t.k.status()['quarantined']
    results.append({'check':'F3-resubmit-old-rejected-large-receipt','passed':True,'spent':10**18,'pending':0,'historical_quarantine_retained':True})
finally:t.tearDown()

(Path(__file__).parent/'additional-checks.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
print(json.dumps(results))
