import unittest
from avcl.storage_codec import dumps,loads
from avcl.canonical import Reject
from avcl.types import Execution
from tests.support import setup,authorize,prepare

class StorageCodecTests(unittest.TestCase):
    def test_round_trip_preserves_exact_record_types(self):
        k,s,_=setup();op=prepare(k,authorize(k,s))
        self.assertEqual(loads(dumps(op)),op)
        self.assertIs(type(loads(dumps(op)).state),Execution)
        value=(True,1,"1",None,frozenset({"a","b"}))
        self.assertEqual(loads(dumps(value)),value)

    def test_closed_tags_reject_unknown_and_malformed_data(self):
        for raw in ['{"$record":[],"fields":{}}','{"$enum":[],"value":"Prepared"}',
                    '{"$enum":"Execution","value":"bad"}','{"$record":"Popen","fields":{}}',
                    '{"$record":"Request","fields":{}}','{"$int":"01"}','{"$set":["a","a"]}']:
            with self.subTest(raw=raw),self.assertRaises(Reject):loads(raw)

    def test_policy_historical_restore_authenticates_signature(self):
        k,_,_=setup();release=k._release;authority=k._policy_authority
        self.assertEqual(authority.restore(release.envelope),release)
        with self.assertRaises(Reject):authority.restore(release.envelope.replace('"CNY"','"USD"'))
