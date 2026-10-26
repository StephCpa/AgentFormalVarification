import copy
import json
import unittest
from dataclasses import replace
from avcl.canonical import Reject
from avcl.types import Event
from avcl.storage_codec import dumps
from avcl.audit import validate_event,check_prefix,encoded
from avcl.audit_export import build_bundle,verify_bundle,load_bundle,encode_bundle,MAX_BYTES


class AuditExportTests(unittest.TestCase):
    def setUp(self):self.payloads=tuple(dumps(Event(i,"Example",None,(("note","中文"),))) for i in range(1,5))

    def reject(self,fn):
        with self.assertRaises(Reject):fn()

    def test_anchor_statuses_and_roundtrip(self):
        first=build_bundle("tenant",self.payloads,limit=2)
        self.assertEqual(verify_bundle(first,expected_tenant="tenant")["status"],"InternallyConsistent")
        report=verify_bundle(load_bundle(encode_bundle(first)),expected_tenant="tenant",expected_checkpoint=first["checkpoint"])
        self.assertEqual((report["status"],report["event_truth"]),("VerifiedAgainstCheckpoint","not_verified"))
        second=build_bundle("tenant",self.payloads,after=2,expected_previous=first["checkpoint"])
        self.assertEqual(verify_bundle(second,expected_tenant="tenant",expected_after=2,expected_previous=first["checkpoint"])["status"],"ContinuationConsistent")
        self.assertEqual(second["checkpoint"],build_bundle("tenant",self.payloads)["checkpoint"])

    def test_tamper_reorder_duplicate_delete_and_truncation_fail(self):
        base=build_bundle("tenant",self.payloads)
        cases=[]
        changed=copy.deepcopy(base);changed["records"][0]["payload"]=self.payloads[0].replace("Example","Changed");cases.append(changed)
        changed=copy.deepcopy(base);changed["records"].reverse();cases.append(changed)
        changed=copy.deepcopy(base);changed["records"][1]=changed["records"][0];cases.append(changed)
        changed=copy.deepcopy(base);changed["records"].pop(1);cases.append(changed)
        changed=build_bundle("tenant",self.payloads,limit=2);cases.append(changed)
        for changed in cases:self.reject(lambda:verify_bundle(changed,expected_tenant="tenant",expected_checkpoint=base["checkpoint"]))

    def test_rehashed_rewrite_is_only_detected_against_external_anchor(self):
        original=build_bundle("tenant",self.payloads)
        rewritten=build_bundle("tenant",tuple(p.replace("Example","Rewritten") for p in self.payloads))
        self.assertEqual(verify_bundle(rewritten,expected_tenant="tenant")["status"],"InternallyConsistent")
        self.reject(lambda:verify_bundle(rewritten,expected_tenant="tenant",expected_checkpoint=original["checkpoint"]))
        self.reject(lambda:build_bundle("tenant",tuple(p.replace("Example","Rewritten") for p in self.payloads),after=4,expected_previous=original["checkpoint"]))

    def test_scope_and_requested_range_are_bound(self):
        page=build_bundle("tenant",self.payloads,after=2)
        self.reject(lambda:verify_bundle(page,expected_tenant="other",expected_after=2))
        self.reject(lambda:verify_bundle(page,expected_tenant="tenant"))
        for kwargs in ({"after":True},{"after":-1},{"after":5},{"limit":0},{"limit":257},{"limit":True}):
            self.reject(lambda:build_bundle("tenant",self.payloads,**kwargs))

    def test_empty_page_and_empty_history_have_explicit_checkpoints(self):
        full=build_bundle("tenant",self.payloads)
        end=build_bundle("tenant",self.payloads,after=4,expected_previous=full["checkpoint"])
        self.assertEqual(end["records"],[])
        self.assertEqual(verify_bundle(end,expected_tenant="tenant",expected_after=4,expected_checkpoint=full["checkpoint"])["event_count"],"0")
        empty=build_bundle("tenant",())
        self.assertEqual(verify_bundle(empty,expected_tenant="tenant")["event_count"],"0")

    def test_strict_document_numbers_keys_shapes_and_size(self):
        for raw in ('{"a":"x","a":"y"}','{"n":1}','{"n":NaN}',"["*2000,'"\ud800"'):
            self.reject(lambda:load_bundle(raw))
        self.reject(lambda:load_bundle(" "*(MAX_BYTES+1)))
        base=build_bundle("tenant",self.payloads)
        for key,value in (("extra","ignored"),("after_sequence","00"),("previous_digest","sha256:invalid"),("records",{})):
            bad={**base,key:value};self.reject(lambda:verify_bundle(bad,expected_tenant="tenant"))

    def test_event_shape_rejects_ambiguous_or_mutable_metadata(self):
        event=Event(1,"Example",None,(("a","one"),))
        for bad in (replace(event,sequence=True),replace(event,kind=[]),replace(event,details=(("a","1"),("a","2"))),
                    replace(event,details=(("b","1"),("a","2"))),replace(event,details=(("a",[]),)),replace(event,details=[("a","one")])):
            self.reject(lambda:validate_event(bad))

    def test_prefix_checks_canonical_types_instead_of_python_equality(self):
        from types import SimpleNamespace
        event=Event(1,"Example",None,())
        self.assertEqual(event,replace(event,sequence=True))
        kernel=SimpleNamespace(_events=[replace(event,sequence=True)],_persisted_event_encodings=encoded((event,)))
        self.reject(lambda:check_prefix(kernel))

    def test_raw_payload_bytes_are_bound_even_when_json_meaning_is_equal(self):
        original=build_bundle("tenant",self.payloads)
        pretty=tuple(json.dumps(json.loads(p),ensure_ascii=False,indent=2) for p in self.payloads)
        alternate=build_bundle("tenant",pretty)
        self.assertNotEqual(original["checkpoint"],alternate["checkpoint"])
        self.assertEqual(verify_bundle(alternate,expected_tenant="tenant")["status"],"InternallyConsistent")


if __name__=="__main__":unittest.main()
