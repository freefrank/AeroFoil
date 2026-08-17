import threading
import time
import unittest

from app.utils import debounce


class DebounceRunnerTests(unittest.TestCase):
    def test_runner_survives_exception_and_keeps_executing(self):
        """A raising debounced function must not kill the runner thread.

        Regression test: previously an exception inside fn() terminated the
        runner thread while state["running"] stayed True, so every later call
        was silently dropped until the process restarted.
        """
        calls = []
        second_run = threading.Event()

        @debounce(0.05)
        def flaky():
            calls.append(time.time())
            if len(calls) == 1:
                raise RuntimeError("boom")
            second_run.set()

        flaky()
        time.sleep(0.3)  # let the first (raising) execution happen
        self.assertEqual(len(calls), 1, "first debounced execution did not run")

        flaky()
        self.assertTrue(
            second_run.wait(2),
            "debounced function stopped executing after an exception",
        )
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
