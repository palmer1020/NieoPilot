import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from core.bot_thread import BotWorker
from core.daily_runner import DailyRunner
from core.dar_route_runner import DarRouteRunner


BEIJING_TZ = timezone(timedelta(hours=8))


class WeekendReleaseRotationTests(unittest.TestCase):
    def test_release_weekly_record_is_shared_by_saturday_and_sunday(self):
        saturday = datetime(2026, 9, 5, 0, 0, tzinfo=BEIJING_TZ)
        sunday = saturday + timedelta(days=1)
        following_saturday = saturday + timedelta(days=7)
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = DailyRunner.__new__(DailyRunner)
            runner._emit = lambda *_args, **_kwargs: None
            runner._records_dir = lambda: temp_dir
            runner._beijing_now = lambda: saturday

            self.assertFalse(runner.has_one_click_release_weekly_record(saturday))
            self.assertTrue(
                runner.append_one_click_release_weekly_record(note="rotation test")
            )
            self.assertTrue(runner.has_one_click_release_weekly_record(saturday))
            self.assertTrue(runner.has_one_click_release_weekly_record(sunday))
            self.assertFalse(
                runner.has_one_click_release_weekly_record(following_saturday)
            )

    def test_full_rotation_precheck_runs_release_after_yilu_lanlan(self):
        now = datetime(2026, 9, 5, 0, 0, tzinfo=BEIJING_TZ)
        events = []
        worker = BotWorker.__new__(BotWorker)
        worker.stop_current = False
        worker.user_stop_requested = False
        worker._stop_event = threading.Event()
        worker.emit_and_log = lambda *_args, **_kwargs: None
        worker.daily_runner = SimpleNamespace(
            _beijing_now=lambda: now,
            has_yilu_daily_record_today=lambda _now: False,
            has_lanlan_daily_record_today=lambda _now: False,
            has_light_mantis_weekly_record=lambda _now: True,
            has_one_click_daily_complete_today=lambda _now: True,
            hatch_one_click_daily_due_state=lambda _now: {"due": False, "reason": "not due"},
            has_one_click_release_weekly_record=lambda _now: False,
        )
        worker._lanlan_cyan_pet_for_current_time = lambda _now: 683
        worker.run_hourly_yilu_lanlan_maintenance = (
            lambda **_kwargs: events.append("依卢岚岚") or True
        )
        worker._run_weekend_release_for_rotation_precheck = (
            lambda *_args, **_kwargs: events.append("周末放生") or True
        )

        self.assertTrue(worker.run_rotation_startup_daily_precheck(False))
        self.assertEqual(events, ["依卢岚岚", "周末放生"])

    def test_weekend_release_reconnects_releases_then_records(self):
        now = datetime(2026, 9, 5, 1, 0, tzinfo=BEIJING_TZ)
        events = []
        worker = BotWorker.__new__(BotWorker)
        worker.stop_current = False
        worker._stop_event = threading.Event()
        worker.emit_and_log = lambda *_args, **_kwargs: None
        worker.daily_runner = SimpleNamespace(
            _beijing_now=lambda: now,
            has_one_click_release_weekly_record=lambda _now: False,
            run_one_click_release_mode=lambda **_kwargs: events.append("release") or True,
            append_one_click_release_weekly_record=lambda **_kwargs: events.append("record") or True,
        )
        worker._prepare_hourly_daily_swf = (
            lambda *_args, **_kwargs: events.append("swf") or True
        )
        worker._clear_game_tmp_cache = lambda **_kwargs: events.append("cache")
        worker.dar_route_runner = SimpleNamespace(
            run_refresh_login_until_map=lambda *_args, **_kwargs: events.append("refresh") or True
        )

        self.assertTrue(
            worker._run_weekend_release_for_rotation_precheck(False)
        )
        self.assertEqual(events, ["swf", "cache", "refresh", "release", "record"])

    def test_normal_rotation_queues_weekend_release_even_when_dailies_done(self):
        now = datetime(2026, 9, 5, 1, 0, tzinfo=BEIJING_TZ)
        task_stop = threading.Event()
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(
            stop_current=False,
            user_stop_requested=False,
            _stop_event=task_stop,
            daily_runner=SimpleNamespace(
                has_yilu_daily_record_today=lambda _now: True,
                has_lanlan_daily_record_today=lambda _now: True,
                has_one_click_release_weekly_record=lambda _now: False,
            ),
            _lanlan_cyan_pet_for_current_time=lambda _now: 683,
        )
        runner._is_rotation_mode = True
        runner._rotation_full_daily_maintenance = False
        runner._rotation_yilu_lanlan_maintenance_key = None
        runner._pending_rotation_yilu_lanlan_maintenance = None
        runner._get_beijing_time = lambda: now
        runner._emit = lambda *_args, **_kwargs: None

        self.assertTrue(
            runner._maybe_run_hourly_yilu_lanlan_maintenance(defer_to_outer=True)
        )
        self.assertEqual(
            runner._pending_rotation_yilu_lanlan_maintenance,
            {
                "hour_key": "2026-09-05-01",
                "run_yilu": False,
                "run_lanlan": False,
                "run_weekend_release": True,
                "task_label": "周末放生",
            },
        )


if __name__ == "__main__":
    unittest.main()
