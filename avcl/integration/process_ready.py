"""Wait for an owned process's atomically published readiness JSON."""
import json
import time


def wait_ready(process, path, timeout=15):
    deadline = time.monotonic() + timeout
    last_error = None
    while True:
        if process.poll() is not None:
            raise AssertionError("Local API process exited before readiness")
        try:
            # Windows can briefly deny opening an atomically renamed file.
            # Existence alone is not readiness; malformed JSON is not retried.
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, PermissionError) as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            raise AssertionError("Local API readiness timed out") from last_error
        time.sleep(0.05)
