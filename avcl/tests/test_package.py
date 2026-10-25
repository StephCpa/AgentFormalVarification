import json
import tempfile
import unittest
from pathlib import Path
from avcl.canonical import Reject
from avcl.ir import refund_predicate
from avcl.package import write_candidate,load_candidate,sha

class CandidatePackageTests(unittest.TestCase):
    def test_bundle_uses_trusted_digest_and_revalidated_checks(self):
        p=refund_predicate()
        with tempfile.TemporaryDirectory() as temp:
            root=write_candidate(Path(temp)/"bundle",p)
            checks=load_candidate(root,p.semantic_digest)
            self.assertTrue(checks.evaluate({"amount":1,"currency":"CNY"}))
            with self.assertRaises(Reject): load_candidate(root,"sha256:untrusted")
            with self.assertRaises(FileExistsError): write_candidate(root,p)

    def test_byte_change_without_new_manifest_is_rejected(self):
        p=refund_predicate()
        with tempfile.TemporaryDirectory() as temp:
            root=write_candidate(Path(temp)/"bundle",p)
            (root/"checks.json").write_text("{}",encoding="utf-8")
            with self.assertRaises(Reject) as cm: load_candidate(root,p.semantic_digest)
            self.assertEqual(cm.exception.code,"PACKAGE_HASH_MISMATCH")

    def test_rehashing_mutated_checks_does_not_authorize_instructions(self):
        p=refund_predicate()
        with tempfile.TemporaryDirectory() as temp:
            root=write_candidate(Path(temp)/"bundle",p)
            checks=json.loads((root/"checks.json").read_text(encoding="utf-8"))
            checks["instructions"][0]["value"]="-1"
            content=json.dumps(checks).encode("utf-8");(root/"checks.json").write_bytes(content)
            manifest=json.loads((root/"manifest.json").read_text(encoding="utf-8"))
            manifest["files"]["checks.json"]=sha(content)
            (root/"manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
            with self.assertRaises(Reject) as cm: load_candidate(root,p.semantic_digest)
            self.assertEqual(cm.exception.code,"CHECK_ARTIFACT_MISMATCH")

    def test_extra_file_or_path_in_manifest_is_not_accepted(self):
        p=refund_predicate()
        with tempfile.TemporaryDirectory() as temp:
            root=write_candidate(Path(temp)/"bundle",p)
            (root/"extra.py").write_text("pass",encoding="utf-8")
            with self.assertRaises(Reject) as cm: load_candidate(root,p.semantic_digest)
            self.assertEqual(cm.exception.code,"INVALID_PACKAGE_FILES")
