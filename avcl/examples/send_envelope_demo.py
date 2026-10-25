"""Authenticated effect, rejected tampering, record expiry, retained liability."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from avcl.canonical import Reject,canonical_bytes,strict_loads
from avcl.runtime import ReferenceKernel
from avcl.send_envelope import envelope_digest
from avcl.sending import admit_attempt
from avcl.simulator import RefundService,Order
from avcl.types import Request,Capability,Approval


def main():
    if len(sys.argv)!=2:raise SystemExit("usage: send_envelope_demo.py OUTPUT_DIRECTORY")
    out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=False)
    time=[0];k=ReferenceKernel(clock=lambda:time[0])
    k.trusted.add_account("tenant:CNY",100)
    k.trusted.add_account("order:a:CNY",100)
    k.trusted.register_capability(Capability("cap","demo","a",frozenset({"a"}),100,1000,1))
    service=RefundService({"a":Order("v1","original-a",100)},retention_seconds=5,
                          clock=lambda:time[0],verifier=k.trusted.send_verifier())
    request=Request("demo","a","task","case","a","v1","original-a","20","0")
    evidence=service.validate(request,"ev",1000);k.trusted.register_evidence(evidence)
    k.trusted.register_approval(Approval("approval",request.request_digest,1000))
    op=k.prepare("a",request,"cap","ev","approval","request",ttl=5)
    op=k.dispatch("a",op.id,op.permit_ref)
    k.trusted.require_authenticated_receipts(op.id)
    with k._lock:packet=admit_attempt(k,op)
    rows=[]
    def record(action,decision):
        a=k.accounts()["tenant:CNY"]
        rows.append({"action":action,"decision":decision,"time":time[0],"actual":service.real_cost,
                     "spent":a.spent,"pending":a.pending,"effect_count":len(service.effects)})
    def rejected(raw):
        try:service.send(raw)
        except Reject as exc:return exc.code
        raise AssertionError("unauthorized packet accepted")
    altered=strict_loads(packet);altered["request"]["destination"]="wrong-recipient"
    record("tampered_destination",rejected(canonical_bytes(altered).decode()))
    receipt=service.send(packet);k.trusted.unknown(op.id)
    record("valid_send_receipt_lost","Unknown")
    tampered_receipt={**strict_loads(receipt),"final_cost":False}
    try:k.trusted.observe(canonical_bytes(tampered_receipt).decode(),op.id)
    except Reject as exc:record("tampered_receipt",exc.code)
    else:raise AssertionError("tampered receipt accepted")
    assert service.send(packet)==receipt
    record("same_packet_replay","Deduplicated")
    time[0]=5
    assert service.query(op.id) is None
    record("remote_record_expired","NoAuthoritativeReceipt")
    record("expired_packet_replay",rejected(packet))
    extended={**strict_loads(packet),"not_after":"100"}
    record("forged_deadline_extension",rejected(canonical_bytes(extended).decode()))
    k.trusted.observe(receipt)
    record("independently_retained_receipt","Final")
    k.assert_invariants()
    assert all(r["actual"]<=r["spent"]+r["pending"] for r in rows)
    assert rows[0]["decision"]=="INVALID_SEND_AUTHENTICATION"
    assert rows[-3]["decision"]=="REMOTE_ACCEPTANCE_CLOSED"
    assert rows[-2]["decision"]=="INVALID_SEND_AUTHENTICATION"
    assert (service.real_cost,k.accounts()["tenant:CNY"].pending,len(service.effects))==(20,0,1)
    report={"schema":"avcl/send-envelope-demo/0.2","scope":"local request and receipt shared-key authentication; logical clock; no production identity, TLS, or key isolation",
            "packet_digest":envelope_digest(packet),"trace":rows,"passed":True}
    (out/"send-envelope-report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=="__main__":main()
