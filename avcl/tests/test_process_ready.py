import json
import unittest
from unittest.mock import Mock, patch
from integration.process_ready import wait_ready


class ProcessReadinessTests(unittest.TestCase):
    def test_transient_windows_read_lock_is_retried_while_owned_process_is_live(self):
        process=Mock();process.poll.return_value=None
        path=Mock();path.read_text.side_effect=[FileNotFoundError(),PermissionError(),'{"pid":42,"port":1234}']
        with patch("integration.process_ready.time.sleep") as sleep:
            self.assertEqual(wait_ready(process,path),{"pid":42,"port":1234})
        self.assertEqual(sleep.call_count,2)

    def test_terminal_process_is_not_restarted_or_waited_on(self):
        process=Mock();process.poll.return_value=1
        path=Mock()
        with self.assertRaisesRegex(AssertionError,"exited"):
            wait_ready(process,path)
        path.read_text.assert_not_called()

    def test_persistent_read_denial_has_a_deadline(self):
        process=Mock();process.poll.return_value=None
        path=Mock();path.read_text.side_effect=PermissionError()
        with patch("integration.process_ready.time.monotonic",side_effect=[0,16]):
            with self.assertRaisesRegex(AssertionError,"timed out") as caught:
                wait_ready(process,path)
        self.assertIsInstance(caught.exception.__cause__,PermissionError)

    def test_invalid_published_json_is_an_error_not_readiness(self):
        process=Mock();process.poll.return_value=None
        path=Mock();path.read_text.return_value="{"
        with self.assertRaises(json.JSONDecodeError):
            wait_ready(process,path)
