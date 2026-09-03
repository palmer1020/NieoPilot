import threading
import unittest
from inspect import signature
from datetime import datetime, timedelta, timezone

from core.bot_thread import BotWorker
from core.dar_route_runner import DarRouteRunner


class RotationButtonDailyPrecheckTests(unittest.TestCase):
    def test_rotation_defaults_disable_eit_and_single_map(self):
        params = signature(DarRouteRunner.run_rotation_mode).parameters

        self.assertFalse(params["rotation_eit_enabled"].default)
        self.assertFalse(params["rotation_nieo_single_map_escape"].default)

    def _worker(self, *, daily_done: bool, run_result: bool = True):
        now = datetime(2026, 7, 29, 8, 0, tzinfo=timezone(timedelta(hours=8)))
        checks = []
        runs = []
        logs = []

        class FakeDailyRunner:
            @staticmethod
            def _beijing_now():
                return now

            @staticmethod
            def has_one_click_daily_complete_today(check_now):
                checks.append(check_now)
                return daily_done

        worker = BotWorker.__new__(BotWorker)
        worker.daily_runner = FakeDailyRunner()
        worker.user_stop_requested = False
        worker.stop_current = False
        worker._stop_event = threading.Event()
        worker.emit_and_log = (
            lambda message, level="INFO": logs.append((message, level))
        )
        worker._run_one_click_daily_for_rotation_precheck = (
            lambda use_foreground, runtime_subset, skip_exp_input: runs.append(
                (use_foreground, runtime_subset, skip_exp_input)
            )
            or run_result
        )
        return worker, now, checks, runs, logs

    def test_completed_business_day_enters_rotation_without_rerun(self):
        worker, now, checks, runs, logs = self._worker(daily_done=True)

        ok = worker.run_rotation_button_daily_precheck(use_foreground=False)

        self.assertTrue(ok)
        self.assertEqual(checks, [now])
        self.assertEqual(runs, [])
        self.assertIn("直接进入轮换", logs[-1][0])

    def test_missing_daily_runs_full_chain_before_rotation(self):
        worker, _now, _checks, runs, logs = self._worker(daily_done=False)

        ok = worker.run_rotation_button_daily_precheck(use_foreground=True)

        self.assertTrue(ok)
        self.assertEqual(runs, [(True, False, True)])
        self.assertIn("一键日常已完成，开始轮换", logs[-1][0])

    def test_manual_uncheck_keeps_experience_input(self):
        worker, _now, _checks, runs, _logs = self._worker(daily_done=False)

        ok = worker.run_rotation_button_daily_precheck(
            use_foreground=False,
            skip_exp_input=False,
        )

        self.assertTrue(ok)
        self.assertEqual(runs, [(False, False, False)])

    def test_failed_daily_still_enters_rotation(self):
        worker, _now, _checks, runs, logs = self._worker(
            daily_done=False,
            run_result=False,
        )

        ok = worker.run_rotation_button_daily_precheck(use_foreground=False)

        self.assertTrue(ok)
        self.assertEqual(runs, [(False, False, True)])
        self.assertIn("继续启动轮换", logs[-1][0])


if __name__ == "__main__":
    unittest.main()
