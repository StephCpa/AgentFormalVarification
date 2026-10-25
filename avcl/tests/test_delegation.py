from dataclasses import replace
import json
import unittest
from avcl.canonical import Reject
from avcl.types import Capability,Request,Approval,Observation,Execution,Settlement
from tests.support import setup

def provision(k,limit=100):
    for subject in ("parent","child","sibling","grandchild"):k.trusted.register_subject(subject)
    k.trusted.register_capability(Capability("root","demo","parent",frozenset({"a","b"}),100,900,1))
    return k.trusted.enable_delegation("root",limit)

def payload(child="child",parent="root",request_id="delegate",**changes):
    spec={"orders":["a","b"],"tools":["refund"],"effects":["Write","Disclose","ResourceConsume"],
          "max_amount_minor":"100","limit_minor":"100","expires":"900","can_delegate":False}
    spec.update(changes)
    return json.dumps({"client_request_id":request_id,"parent_ref":parent,"child_subject":child,"attenuations":spec})

def authorization(k,service,ref,subject="child",order="a",value=60,fee=0,case="child-case"):
    request=Request("demo",subject,"task-"+subject,case,order,"v1","original-"+order,str(value),str(fee))
    ev=service.validate(request,"ev-"+case,900)
    k.trusted.register_evidence(ev);k.trusted.register_approval(Approval("ap-"+case,request.request_digest,900))
    return request,ref,ev.ref,"ap-"+case

def prepare(k,auth,key="request"):
    return k.prepare(auth[0].subject,*auth,key)

class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.k,self.service,self.clock=setup();self.root_account=provision(self.k)

    def error(self,code,fn):
        with self.assertRaises(Reject) as caught:fn()
        self.assertEqual(caught.exception.code,code)

    def child(self,**changes):return self.k.delegate("parent",payload(**changes))["child_cap_ref"]

    def test_opt_in_registered_identity_and_owner_required(self):
        self.error("AUTH_DENIED",lambda:self.k.delegate("a",payload(parent="cap-a")))
        self.error("AUTH_DENIED",lambda:self.k.delegate("impostor",payload()))
        self.error("AUTH_DENIED",lambda:self.child(child="unknown"))
        self.error("DELEGATION_NOT_ATTENUATED",lambda:self.child(child="parent"))

    def test_every_scope_dimension_is_attenuated(self):
        for change in ({"orders":["c"]},{"tools":["shell"]},{"effects":["Read"]},{"expires":"901"},{"max_amount_minor":"101"}):
            self.error("DELEGATION_NOT_ATTENUATED",lambda:self.child(**change))
        self.error("BUDGET_EXCEEDED",lambda:self.child(limit_minor="101"))
        self.assertEqual(len(self.k._caps),3)

    def test_strict_payload_rejects_extra_fields_numbers_and_duplicates(self):
        for change in ({"orders":["a","a"]},{"can_delegate":"yes"},{"limit_minor":"0"},{"scope":"all"}):
            self.error("INVALID_DELEGATION",lambda:self.child(**change))
        self.error("JSON_NUMBERS_UNSUPPORTED",lambda:self.child(limit_minor=10))
        self.error("INVALID_DELEGATION",lambda:self.child(request_id="x"*129))

    def test_shared_root_and_child_quota_reserve_once_per_account(self):
        ref=self.child(limit_minor="60");op=prepare(self.k,authorization(self.k,self.service,ref))
        self.assertEqual(len(op.reservations),4)
        self.assertTrue(all(r.remaining==60 for r in op.reservations))
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,60)
        self.assertEqual(self.k.accounts()[self.root_account].pending,60)
        self.k.assert_invariants()

    def test_sibling_limits_do_not_mint_root_budget(self):
        one=self.child();two=self.child(child="sibling",request_id="sibling")
        prepare(self.k,authorization(self.k,self.service,one))
        auth=authorization(self.k,self.service,two,"sibling","b",60,case="sibling")
        self.error("BUDGET_EXCEEDED",lambda:prepare(self.k,auth))
        self.assertIsNone(self.k._approvals[auth[3]].operation_id)
        self.assertEqual(self.k.accounts()["tenant:CNY"].pending,60)

    def test_parent_and_descendants_share_parent_quota(self):
        ref=self.child(limit_minor="70",can_delegate=True)
        grand=self.k.delegate("child",payload("grandchild",ref,"grand",limit_minor="50"))["child_cap_ref"]
        prepare(self.k,authorization(self.k,self.service,grand,"grandchild",value=50))
        self.error("BUDGET_EXCEEDED",lambda:prepare(self.k,authorization(self.k,self.service,ref,order="b",value=30,case="parent-share")))
        self.error("BUDGET_EXCEEDED",lambda:self.k.delegate("child",payload("sibling",ref,"late",limit_minor="21")))

    def test_fee_upper_bound_counts_against_every_ancestor(self):
        self.service._fee=5
        ref=self.child(limit_minor="64")
        auth=authorization(self.k,self.service,ref,value=60,fee=5)
        self.error("BUDGET_EXCEEDED",lambda:prepare(self.k,auth))
        self.assertTrue(all(a.pending==0 for a in self.k.accounts().values()))

    def test_effect_subset_cannot_execute_full_refund(self):
        ref=self.child(effects=["Write"])
        self.error("SCOPE_DENIED",lambda:prepare(self.k,authorization(self.k,self.service,ref)))

    def test_revocation_between_prepare_and_dispatch_retains_then_aborts(self):
        ref=self.child();op=prepare(self.k,authorization(self.k,self.service,ref))
        self.k.trusted.revoke_capability("root")
        self.error("AUTH_DENIED",lambda:self.k.dispatch("child",op.id,op.permit_ref))
        self.assertTrue(all(r.remaining==60 for r in self.k.get("child",op.id).reservations))
        self.k.abort_prepared("child",op.id)
        self.assertTrue(all(a.pending==0 for a in self.k.accounts().values()))

    def test_unknown_after_revocation_keeps_all_exposure_until_receipt(self):
        ref=self.child();op=prepare(self.k,authorization(self.k,self.service,ref))
        op=self.k.dispatch("child",op.id,op.permit_ref);receipt=self.service.send(op)
        self.k.trusted.unknown(op.id);self.k.trusted.revoke_capability("root")
        self.assertEqual(self.k.accounts()[self.root_account].pending,60)
        result=self.k.trusted.observe(receipt)
        self.assertEqual(result.settlement,Settlement.FINAL)
        self.assertTrue(all(r.charged==60 and r.remaining==0 for r in result.reservations))
        self.assertEqual(self.service.real_cost,60);self.k.assert_invariants()

    def test_partial_and_late_overage_charge_all_quota_accounts(self):
        ref=self.child(limit_minor="70");op=prepare(self.k,authorization(self.k,self.service,ref,value=70))
        op=self.k.dispatch("child",op.id,op.permit_ref)
        partial=Observation("part",op.id,op.request.request_digest,Execution.PARTIAL,(("tenant:CNY",20),("order:a:CNY",20)),False,False)
        result=self.k.trusted.observe(partial)
        self.assertTrue(all((r.charged,r.remaining)==(20,50) for r in result.reservations))
        late=Observation("late",op.id,op.request.request_digest,Execution.CONFIRMED,(("tenant:CNY",80),("order:a:CNY",70)),True,True)
        result=self.k.trusted.observe(late)
        self.assertEqual(result.settlement,Settlement.DISPUTED)
        self.assertEqual(self.k.accounts()[self.root_account].spent,80)
        self.assertTrue(self.k.accounts()[self.root_account].breached);self.k.assert_invariants()

    def test_adapter_cannot_supply_local_quota_costs(self):
        ref=self.child();op=prepare(self.k,authorization(self.k,self.service,ref));op=self.k.dispatch("child",op.id,op.permit_ref)
        receipt=self.service.send(op)
        self.error("INVALID_COST_VECTOR",lambda:self.k.trusted.observe(replace(receipt,cumulative_costs=receipt.cumulative_costs+((self.root_account,0),))))
        self.assertEqual(self.k.accounts()[self.root_account].pending,60)

    def test_idempotency_does_not_renew_revoked_or_expired_authority(self):
        raw=payload();first=self.k.delegate("parent",raw)
        self.assertEqual(self.k.delegate("parent",raw),first)
        self.error("IDEMPOTENCY_CONFLICT",lambda:self.child(limit_minor="90"))
        self.k.trusted.revoke_capability(first["child_cap_ref"])
        self.error("AUTH_DENIED",lambda:self.k.delegate("parent",raw))
        self.assertEqual(sum(e.kind=="CapabilityDelegated" for e in self.k.events()),1)

    def test_disabled_ancestor_subject_and_expiry_deny_new_admission(self):
        ref=self.child();auth=authorization(self.k,self.service,ref)
        self.k.trusted.disable_subject("parent")
        self.error("AUTH_DENIED",lambda:prepare(self.k,auth))
        self.clock.now=900
        self.error("AUTH_DENIED",lambda:self.child(request_id="expired"))

    def test_depth_is_bounded_and_subject_cycles_rejected(self):
        parent="root";subject="parent"
        for depth in range(1,9):
            child="depth-"+str(depth);self.k.trusted.register_subject(child)
            parent=self.k.delegate(subject,payload(child,parent,"depth",can_delegate=True))["child_cap_ref"];subject=child
        self.error("DELEGATION_DEPTH_EXCEEDED",lambda:self.k.delegate(subject,payload("sibling",parent,"too-deep")))
        self.assertEqual(len(self.k._grants),9)

    def test_missing_or_cyclic_ancestor_fails_closed(self):
        ref=self.child();auth=authorization(self.k,self.service,ref)
        old=self.k._grants["root"]
        self.k._grants["root"]=replace(old,parent_ref="missing")
        self.error("INVALID_DELEGATION_CHAIN",lambda:prepare(self.k,auth))
        self.k._grants["root"]=replace(old,parent_ref=ref)
        self.error("INVALID_DELEGATION_CHAIN",lambda:prepare(self.k,auth))

    def test_root_cannot_be_attached_after_existing_reservations(self):
        from tests.support import authorize,prepare as base_prepare
        self.k.trusted.register_subject("a")
        base_prepare(self.k,authorize(self.k,self.service))
        self.error("DELEGATION_ROOT_ALREADY_USED",lambda:self.k.trusted.enable_delegation("cap-a",30))
        self.assertNotIn("cap-a",self.k._grants)

    def test_leaf_cannot_redelegate_and_root_spend_limits_later_grants(self):
        ref=self.child()
        self.error("AUTH_DENIED",lambda:self.k.delegate("child",payload("grandchild",ref,"next")))
        prepare(self.k,authorization(self.k,self.service,"root","parent",value=60))
        self.error("BUDGET_EXCEEDED",lambda:self.child(child="sibling",request_id="remaining",limit_minor="41"))

    def test_policy_update_cannot_rewrite_delegated_quota(self):
        from avcl.contract import default_contract
        from tests.test_contract_policy import seal
        before=self.k.accounts()
        policy=replace(default_contract(),policy_version=2,account_limits=((self.root_account,110),))
        self.error("DELEGATION_QUOTA_IMMUTABLE",lambda:self.k.trusted.activate_policy(seal(self.k,policy)))
        self.assertEqual(self.k.accounts(),before)
        self.assertEqual(self.k.epoch,1)

if __name__=="__main__":unittest.main()
