"""Independent audit probes; asserts reproduction of defects, not correctness."""
from pathlib import Path
import sys,json,hashlib,subprocess
from dataclasses import replace
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'integration')]
from tests.support import setup,authorize,prepare
from tests.test_workflow import make,context,P,D,S,Q,branch
from tests.test_contract_policy import amended,seal
from tests.test_encoding_ir import field
from avcl.contract import default_contract,Clause
from avcl.ir import compile_predicate,FieldType
from avcl.types import Observation,Execution,Capability
from avcl.simulator import RefundService,Order
from avcl.sending import admit_attempt
from avcl.workflow_runtime import WorkflowExecutor
from avcl.workflow_postgres import DurableWorkflowExecutor
from avcl.tasks import TaskAuthority,PostgresTasks
from avcl.canonical import Reject
from test_postgres import PersistenceTests

RESULTS=[]
def record(name,**data):
    entry={'probe':name,**data};RESULTS.append(entry)
    print(json.dumps(entry,ensure_ascii=True),flush=True)
    (Path(__file__).parent/'results.json').write_text(json.dumps(RESULTS,indent=2),encoding='utf-8')

def state(k,opid):
    op=k.get('a',opid);a=k.accounts()['tenant:CNY']
    quarantine=k.status()['quarantined'] if hasattr(k,'status') else k._quarantined
    return dict(execution=str(op.state),settlement=str(op.settlement),spent=a.spent,pending=a.pending,quarantined=quarantine)

def service(k,clock,fee=0):
    return RefundService({'a':Order('v1','original-a',100)},fee=fee,retention_seconds=120,clock=clock,verifier=k.trusted.send_verifier())

def receipts(k,s,clock,fee,durable=False,cap=None):
    auth=authorize(k,s,value=70,fee=fee)
    if cap:auth=(auth[0],cap,*auth[2:])
    op=prepare(k,auth);op=k.dispatch('a',op.id,op.permit_ref)
    if durable:packet=k.claim_outbox('probe-sender',authenticated_receipts=True)['envelope']
    else:
        k.trusted.require_authenticated_receipts(op.id)
        with k._lock:packet=admit_attempt(k,op)
    s.send(packet) # actual effect once; generate two truthful, independently identified facts
    old=Observation('old-nonfinal',op.id,op.request.request_digest,Execution.CONFIRMED,
                    (('tenant:CNY',70),('order:a:CNY',70)),False,True)
    final=replace(old,id='new-final',final_cost=True)
    signer=s._verifier._receipt_signer
    clock.now=2
    return op,signer.seal(old,op.binding,1),signer.seal(final,op.binding,2)

def f1_memory(fee):
    k,_,clock=setup();s=service(k,clock)
    op,old,final=receipts(k,s,clock,fee)
    k.trusted.observe(final,op.id);before=state(k,op.id)
    k.trusted.observe(old,op.id);after=state(k,op.id);k.assert_invariants()
    assert before['settlement']=='Final'
    assert after['settlement']==('Partial' if fee==0 else 'Disputed')
    record('F1-memory',fee_bound=fee,before=before,after=after,real_cost=s.real_cost,effects=len(s.effects),invariants_passed=True)

def pg_snapshot(t,opid,task):
    k=t.fresh();s=state(k,opid)
    s.update(task=PostgresTasks(k).get('a',task)['status'],outbox=k.status()['outbox'][0]['status'],capacity=k.capacity()['usage'])
    return s

def f1_pg(t,fee):
    k=t.k
    clause=Clause('refund-success','completion','success','refund',('close',),('R10',),(),False,
                  compile_predicate(field('confirmed'),{'confirmed':FieldType('Bool')},'R10'))
    k.trusted.activate_policy(seal(k,amended(default_contract(),extra=(clause,))))
    k.trusted.register_capability(Capability('probe-cap-a','demo','a',frozenset({'a'}),10000,1000,k.status()['epoch']))
    task=TaskAuthority(k).register('a','task-a',frozenset({'case-a'}))
    k.trusted.set_capacity(1,1,1)
    s=service(k,t.clock);op,old,final=receipts(k,s,t.clock,fee,True,'probe-cap-a')
    k.trusted.observe(final,op.id);PostgresTasks(k).close('a',task,'close1')
    before=pg_snapshot(t,op.id,task)
    k.trusted.observe(old,op.id);after=pg_snapshot(t,op.id,task)
    t.fresh().assert_invariants()
    assert before['task']=='Completed'
    assert after['task']==('Pending' if fee==0 else 'Failed')
    assert after['outbox']=='NeedsReconcile' and after['capacity']['open_obligations']==1
    record('F1-postgres',fee_bound=fee,before=before,after_reopen=after,real_cost=s.real_cost,effects=len(s.effects),invariants_passed=True,
           historical_close_records=len(t.query('SELECT * FROM task_close_request')))

def f2(t,variant):
    s=service(t.k,t.clock)
    if variant=='dispatch-only':flow=make([P,D],effects=('ResourceConsume',));flag=False
    else:flow=make([P,D,branch([S],[])]);flag=variant=='true-branch-control'
    executor=DurableWorkflowExecutor(t.k,s)
    result=executor.run(flow,'a','probe-run',context(t.k,s),{'flag':flag})
    steps=[dict(x) for x in result.steps]
    before=len(s.effects);job=t.fresh().claim_outbox('ordinary-sender',authenticated_receipts=True)
    if job:t.fresh().trusted.observe(s.send(job['envelope']),job['operation'].id)
    after=len(s.effects)
    if flag:assert before==after==1 and job is None
    else:assert before==0 and after==1 and not any(x.get('command')=='Send' for x in steps)
    record('F2-postgres',variant=variant,workflow_status=result.status,inferred_effects=flow.inferred_effects,
           steps=steps,effects_before_sender=before,effects_after_sender=after,sender_claimed=job is not None,real_cost=s.real_cost)

def f3():
    fee=10**18-1;k,_,clock=setup(limit=10**18,order_limit=100)
    s=service(k,clock,fee);op=prepare(k,authorize(k,s,value=1,fee=fee));op=k.dispatch('a',op.id,op.permit_ref)
    k.trusted.require_authenticated_receipts(op.id)
    with k._lock:packet=admit_attempt(k,op)
    raw=s.send(packet)
    try:k.trusted.observe(raw,op.id)
    except Reject as exc:error=exc.code
    else:raise AssertionError('expected amount rejection')
    assert error=='INVALID_AMOUNT' and s.real_cost==10**18
    record('F3-memory',error=error,real_cost=s.real_cost,effects=len(s.effects),after=state(k,op.id))

def f4(k,s,durable=False):
    executor=DurableWorkflowExecutor(k,s) if durable else WorkflowExecutor(k,s)
    flow=make([P,D,S,Q]);refs=context(k,s);values={'flag':True}
    first=executor.run(flow,'a','replay',refs,values)
    assert executor.run(flow,'a','replay',refs,values)==first
    k.trusted.activate_policy(seal(k,amended(default_contract())))
    try:
        replay=executor.run(flow,'a','replay',refs,values);outcome=replay.status
    except Reject as exc:outcome=exc.code
    assert outcome==('Finished' if durable else 'WORKFLOW_CONTRACT_MISMATCH')
    record('F4-postgres-control' if durable else 'F4-memory',first=first.status,replay_after_policy_update=outcome,
           effects=len(s.effects),real_cost=s.real_cost,after=state(k,first.operation_ids[0]))

if __name__=='__main__':
    for fee in (0,10):f1_memory(fee)
    f3();k,s,_=setup();f4(k,s)
    PersistenceTests.setUpClass()
    cases=[lambda t:f1_pg(t,0),lambda t:f1_pg(t,10)]
    cases.extend(lambda t,v=v:f2(t,v) for v in ('dispatch-only','false-branch','true-branch-control'))
    cases.append(lambda t:f4(t.k,t.s,True))
    for case in cases:
        t=PersistenceTests();t.setUp()
        try:case(t)
        finally:t.tearDown()
    report=json.loads((ROOT/'evidence/20261025T202705.069248Z/verification.json').read_text(encoding='utf-8'))
    hashes=report['source_sha256'];mismatches=[]
    for p,expected in hashes.items():
        path=ROOT/p
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:mismatches.append(p)
    record('baseline',source_files=len(hashes),mismatches=mismatches,
           head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip())
    assert not mismatches
