import io
import unittest
from scripts.m1_checks import RecordedResult


class M1RecorderTests(unittest.TestCase):
    def test_class_setup_error_is_retained_without_claiming_a_pass(self):
        class UnavailableFixture(unittest.TestCase):
            @classmethod
            def setUpClass(cls): raise RuntimeError("injected unavailable dependency")
            def test_unreached(self): raise AssertionError("must not execute")
        log = io.StringIO()
        result = unittest.TextTestRunner(stream=log, resultclass=RecordedResult).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(UnavailableFixture))
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0]["status"], "Error")
        self.assertTrue(result.records[0]["fixture_failure"])
        self.assertIn("injected unavailable dependency", log.getvalue())
