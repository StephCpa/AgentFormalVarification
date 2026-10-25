from pathlib import Path
import sys
import tempfile
import unittest
from scripts.verify import run_logged


class VerificationLoggingTests(unittest.TestCase):
    def test_failed_process_keeps_stdout_and_stderr(self):
        with tempfile.TemporaryDirectory(prefix="avcl-log-test-") as folder:
            path=Path(folder)/"failure.log"
            code,output=run_logged([sys.executable,"-c","import sys;print('evidence',flush=True);print('failure-detail',file=sys.stderr);sys.exit(7)"],path,folder,10)
            self.assertEqual(code,7)
            self.assertIn("evidence",output)
            self.assertIn("failure-detail",output)
            self.assertEqual(path.read_text(),output)

    def test_timeout_keeps_output_emitted_before_timeout(self):
        with tempfile.TemporaryDirectory(prefix="avcl-log-test-") as folder:
            path=Path(folder)/"timeout.log"
            code,output=run_logged([sys.executable,"-c","import time;print('partial-evidence',flush=True);time.sleep(10)"],path,folder,2)
            self.assertEqual(code,-1)
            self.assertIn("partial-evidence",output)
            self.assertIn("VERIFICATION_RUNNER_ERROR",output)
