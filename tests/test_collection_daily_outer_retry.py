import unittest

from core.daily_runner import DailyRunner


class CollectionDailyOuterRetryTests(unittest.TestCase):
    def test_failed_complete_attempt_returns_without_outer_retry(self) -> None:
        calls = []
        logs = []
        runner = DailyRunner.__new__(DailyRunner)
        runner._should_abort = lambda: False
        runner._emit = lambda message, level="INFO": logs.append((message, level))

        def run_once(**kwargs):
            calls.append(kwargs)
            runner._collection_daily_restart_reason = "方案1第5步未完成"
            return False

        runner._run_collection_daily_mode_once = run_once

        ok = runner.run_collection_daily_mode(
            use_foreground=False,
            skip_refresh_login=True,
            skip_exp_input=True,
        )

        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)
        self.assertIn("不在底层重连整套日常", logs[-1][0])

    def test_successful_complete_attempt_returns_success(self) -> None:
        calls = []
        runner = DailyRunner.__new__(DailyRunner)
        runner._should_abort = lambda: False
        runner._emit = lambda *_args, **_kwargs: None
        runner._run_collection_daily_mode_once = (
            lambda **kwargs: calls.append(kwargs) or True
        )

        ok = runner.run_collection_daily_mode(use_foreground=True)

        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
