import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from core.dar_route_runner import DarRouteRunner


class RotationLanlanOuterHandoffTests(unittest.TestCase):
    def _runner(self, maintenance_results=None):
        results = iter(maintenance_results or (True,))
        calls = []
        task_stop = threading.Event()
        daily_runner = SimpleNamespace(
            has_yilu_daily_record_today=lambda _now: True,
            has_lanlan_daily_record_today=lambda _now: False,
        )

        def run_maintenance(**kwargs):
            calls.append(dict(kwargs))
            return next(results)

        bot = SimpleNamespace(
            stop_current=False,
            user_stop_requested=False,
            _stop_event=task_stop,
            daily_runner=daily_runner,
            _lanlan_cyan_pet_for_current_time=lambda _now: 347,
            run_hourly_yilu_lanlan_maintenance=run_maintenance,
        )
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = bot
        runner._is_rotation_mode = True
        runner._rotation_full_daily_maintenance = False
        runner._rotation_yilu_lanlan_maintenance_key = None
        runner._pending_rotation_yilu_lanlan_maintenance = None
        runner._pending_rotation_switch = False
        runner._target_mode_after_switch = None
        runner._rotation_time_check_window_active = True
        runner._last_rotation_time_check = 1.0
        runner._active_use_foreground = False
        runner._should_restart_after_reconnect = False
        runner._mode_restart_reason = None
        runner._rotation_swf_synced_mode = None
        runner._wild_swf_synced_key = None
        runner._get_beijing_time = lambda: datetime(
            2026, 7, 28, 1, 0, tzinfo=timezone(timedelta(hours=8))
        )
        runner._emit = lambda *_args, **_kwargs: None
        runner._sleep_abortable = lambda *_args, **_kwargs: None
        return runner, calls, task_stop

    def test_child_check_only_queues_lanlan(self):
        runner, calls, _task_stop = self._runner()

        handled = runner._maybe_run_hourly_yilu_lanlan_maintenance(
            defer_to_outer=True
        )

        self.assertTrue(handled)
        self.assertEqual(calls, [])
        self.assertEqual(
            runner._pending_rotation_yilu_lanlan_maintenance,
            {
                "hour_key": "2026-07-28-01",
                "run_yilu": False,
                "run_lanlan": True,
                "task_label": "岚岚",
            },
        )
        self.assertIsNone(runner._rotation_yilu_lanlan_maintenance_key)

    def test_yilu_can_queue_during_entire_first_ten_minutes(self):
        runner, calls, _task_stop = self._runner()
        runner.bot.daily_runner.has_yilu_daily_record_today = lambda _now: False
        runner.bot.daily_runner.has_lanlan_daily_record_today = lambda _now: True
        runner._get_beijing_time = lambda: datetime(
            2026, 7, 28, 1, 8, tzinfo=timezone(timedelta(hours=8))
        )

        handled = runner._maybe_run_hourly_yilu_lanlan_maintenance(
            defer_to_outer=True
        )

        self.assertTrue(handled)
        self.assertEqual(calls, [])
        self.assertEqual(
            runner._pending_rotation_yilu_lanlan_maintenance,
            {
                "hour_key": "2026-07-28-01",
                "run_yilu": True,
                "run_lanlan": False,
                "task_label": "依卢",
            },
        )

    def test_outer_failure_keeps_request_and_retries(self):
        runner, calls, _task_stop = self._runner((False, True))
        runner._maybe_run_hourly_yilu_lanlan_maintenance(
            defer_to_outer=True
        )

        first_handled = runner._run_rotation_pre_entry_maintenance()

        self.assertTrue(first_handled)
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(
            runner._pending_rotation_yilu_lanlan_maintenance, dict
        )
        self.assertIsNone(runner._rotation_yilu_lanlan_maintenance_key)

        second_handled = runner._run_rotation_pre_entry_maintenance()

        self.assertTrue(second_handled)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(
            runner._pending_rotation_yilu_lanlan_maintenance
        )
        self.assertEqual(
            runner._rotation_yilu_lanlan_maintenance_key,
            "2026-07-28-01",
        )

    def test_expired_yilu_request_is_cleared_without_running(self):
        runner, calls, _task_stop = self._runner()
        runner._pending_rotation_yilu_lanlan_maintenance = {
            "hour_key": "2026-07-28-01",
            "run_yilu": True,
            "run_lanlan": False,
            "task_label": "依卢",
        }
        runner._get_beijing_time = lambda: datetime(
            2026, 7, 28, 1, 10, tzinfo=timezone(timedelta(hours=8))
        )

        handled = runner._run_rotation_pre_entry_maintenance()

        self.assertTrue(handled)
        self.assertEqual(calls, [])
        self.assertIsNone(runner._pending_rotation_yilu_lanlan_maintenance)
        self.assertEqual(
            runner._rotation_yilu_lanlan_maintenance_key,
            "2026-07-28-01",
        )

    def test_expired_yilu_preserves_paired_lanlan(self):
        runner, calls, _task_stop = self._runner()
        runner._pending_rotation_yilu_lanlan_maintenance = {
            "hour_key": "2026-07-28-01",
            "run_yilu": True,
            "run_lanlan": True,
            "task_label": "依卢+岚岚",
        }
        runner._get_beijing_time = lambda: datetime(
            2026, 7, 28, 1, 10, tzinfo=timezone(timedelta(hours=8))
        )

        handled = runner._run_rotation_pre_entry_maintenance()

        self.assertTrue(handled)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["run_yilu"])
        self.assertTrue(calls[0]["run_lanlan"])
        self.assertIsNone(calls[0]["yilu_deadline"])
        self.assertIsNone(runner._pending_rotation_yilu_lanlan_maintenance)

    def test_yilu_failure_crossing_deadline_does_not_retry(self):
        runner, calls, _task_stop = self._runner((False,))
        current = [
            datetime(2026, 7, 28, 1, 9, 50, tzinfo=timezone(timedelta(hours=8)))
        ]
        runner._get_beijing_time = lambda: current[0]
        runner._pending_rotation_yilu_lanlan_maintenance = {
            "hour_key": "2026-07-28-01",
            "run_yilu": True,
            "run_lanlan": False,
            "task_label": "依卢",
        }

        def fail_after_deadline(**kwargs):
            calls.append(dict(kwargs))
            current[0] = datetime(
                2026, 7, 28, 1, 10, tzinfo=timezone(timedelta(hours=8))
            )
            return False

        runner.bot.run_hourly_yilu_lanlan_maintenance = fail_after_deadline

        handled = runner._run_rotation_pre_entry_maintenance()

        self.assertTrue(handled)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["yilu_deadline"],
            datetime(2026, 7, 28, 1, 10, tzinfo=timezone(timedelta(hours=8))),
        )
        self.assertIsNone(runner._pending_rotation_yilu_lanlan_maintenance)

    def test_maintenance_invalidates_previous_rotation_swf_owner(self):
        runner, _calls, _task_stop = self._runner()
        runner._rotation_swf_synced_mode = "rare:埃尔特"
        runner._wild_swf_synced_key = "埃尔特"
        runner._maybe_run_hourly_yilu_lanlan_maintenance(
            defer_to_outer=True
        )

        handled = runner._run_rotation_pre_entry_maintenance()

        self.assertTrue(handled)
        self.assertIsNone(runner._rotation_swf_synced_mode)
        self.assertIsNone(runner._wild_swf_synced_key)

    def test_segment_sync_uses_full_once_then_shared_union(self):
        runner, _calls, _task_stop = self._runner()
        runner.bot._task_swf_full_base_done = False
        runner._try_delete_game_tmp_dir = lambda **_kwargs: None
        runner._resolve_rotation_swf_delete_profile = lambda _mode: None
        runtime_subset_calls = []

        def sync_pet_254(*, runtime_subset=False):
            runtime_subset_calls.append(bool(runtime_subset))
            return True, "ok"

        with patch(
            "core.swf_resource_ops.sync_pet_254",
            side_effect=sync_pet_254,
        ):
            first_ok = runner._sync_swf_for_rotation_segment(
                "rare:埃尔特",
                force=True,
                runtime_subset=True,
            )
            second_ok = runner._sync_swf_for_rotation_segment(
                "rare:埃尔特",
                force=True,
                runtime_subset=False,
            )

        self.assertTrue(first_ok)
        self.assertTrue(second_ok)
        self.assertEqual(runtime_subset_calls, [False, True])

    def test_outer_consumes_restart_request_and_keeps_pending(self):
        runner, calls, task_stop = self._runner()

        def request_restart(**kwargs):
            calls.append(dict(kwargs))
            runner._request_mode_restart(task_stop, "岚岚战斗失败")
            return False

        runner.bot.run_hourly_yilu_lanlan_maintenance = request_restart
        runner._maybe_run_hourly_yilu_lanlan_maintenance(
            defer_to_outer=True
        )

        handled = runner._run_rotation_pre_entry_maintenance()

        self.assertTrue(handled)
        self.assertEqual(len(calls), 1)
        self.assertFalse(task_stop.is_set())
        self.assertFalse(runner.bot.stop_current)
        self.assertFalse(runner._should_restart_after_reconnect)
        self.assertIsNone(runner._mode_restart_reason)
        self.assertIsInstance(
            runner._pending_rotation_yilu_lanlan_maintenance, dict
        )

    def test_full_daily_chain_is_also_deferred_outside_child(self):
        runner, _calls, _task_stop = self._runner()
        full_chain_calls = []
        runner._rotation_full_daily_maintenance = True
        runner._pending_rotation_full_chain_maintenance = None
        runner._hourly_daily_maintenance_key = None
        runner._hourly_daily_maintenance_retry_count = 0
        runner._hourly_daily_maintenance_retry_not_before = 0.0
        runner._get_beijing_time = lambda: datetime(
            2026, 7, 28, 7, 0, tzinfo=timezone(timedelta(hours=8))
        )
        runner.bot.run_rotation_startup_daily_precheck = (
            lambda **kwargs: full_chain_calls.append(dict(kwargs)) or True
        )
        runner.bot._rotation_full_chain_scan_failed_labels = []
        runner.bot._rotation_full_chain_scan_handled = True

        handled = runner._maybe_run_rotation_hourly_full_chain_scan(
            defer_to_outer=True
        )

        self.assertTrue(handled)
        self.assertEqual(full_chain_calls, [])
        self.assertEqual(
            runner._pending_rotation_full_chain_maintenance,
            {"hour_key": "2026-07-28-07"},
        )

        outer_handled = runner._run_rotation_pre_entry_maintenance()

        self.assertTrue(outer_handled)
        self.assertEqual(len(full_chain_calls), 1)
        self.assertIsNone(
            runner._pending_rotation_full_chain_maintenance
        )

    def test_full_chain_restart_request_is_consumed_by_outer(self):
        runner, _calls, task_stop = self._runner()
        runner._rotation_full_daily_maintenance = True
        runner._pending_rotation_full_chain_maintenance = {
            "hour_key": "2026-07-28-07"
        }
        runner._hourly_daily_maintenance_retry_count = 0
        runner._hourly_daily_maintenance_retry_not_before = 0.0
        runner.bot._rotation_full_chain_scan_failed_labels = []

        def request_restart(**_kwargs):
            runner._request_mode_restart(task_stop, "完整日常子流程失败")
            return False

        runner.bot.run_rotation_startup_daily_precheck = request_restart

        handled = runner._run_rotation_pre_entry_maintenance()

        self.assertTrue(handled)
        self.assertFalse(task_stop.is_set())
        self.assertFalse(runner.bot.stop_current)
        self.assertFalse(runner._should_restart_after_reconnect)
        self.assertIsNone(runner._mode_restart_reason)
        self.assertEqual(
            runner._pending_rotation_full_chain_maintenance,
            {"hour_key": "2026-07-28-07"},
        )
        self.assertEqual(runner._hourly_daily_maintenance_retry_count, 1)

    def test_rotation_main_runs_maintenance_after_child_returns(self):
        runner, _calls, task_stop = self._runner()
        events = []
        swf_runtime_subset_calls = []
        runner.reset_swf_sync_state = lambda: None
        runner._resolve_rotation_rare_profile = lambda _slot: SimpleNamespace(
            name="埃尔特"
        )
        runner._detect_rotation_mode = lambda *args, **kwargs: (
            "rare:埃尔特",
            datetime.now(timezone.utc) + timedelta(hours=1),
        )
        def sync_segment(*_args, **kwargs):
            swf_runtime_subset_calls.append(
                bool(kwargs.get("runtime_subset"))
            )
            return True

        runner._sync_swf_for_rotation_segment = sync_segment
        runner._rotation_step1_login = lambda *args, **kwargs: True
        runner._rotation_step1_force_molecule_converter = lambda: False
        runner._prepare_rotation_segment_pick_overrides = (
            lambda *_args: None
        )
        runner._rotation_expected_party_ids_for_segment = (
            lambda _mode: frozenset()
        )
        runner._rotation_pick_mode_for = lambda _mode: "shuangta"
        runner._rotation_clear_backpack_and_pick_or_skip = (
            lambda *args, **kwargs: True
        )
        runner._rotation_step4_set_companion = (
            lambda *args, **kwargs: True
        )
        runner._rotation_follow_cyan_for_mode = lambda _mode: False
        runner._consume_rotation_child_restart = (
            lambda *args, **kwargs: False
        )

        runner.bot.daily_runner.has_lanlan_daily_record_today = (
            lambda _now: True
        )

        def run_maintenance(**_kwargs):
            events.append("maintenance")
            return True

        runner.bot.run_hourly_yilu_lanlan_maintenance = run_maintenance
        child_runs = 0

        def run_child(_mode, _foreground, _child_stop, _next_switch):
            nonlocal child_runs
            child_runs += 1
            if child_runs == 1:
                events.append("child_enter")
                runner._pending_rotation_yilu_lanlan_maintenance = {
                    "hour_key": "2026-07-28-01",
                    "run_yilu": False,
                    "run_lanlan": True,
                    "task_label": "岚岚",
                }
                events.append("child_return")
                return False
            events.append("next_child")
            runner.bot.user_stop_requested = True
            runner.bot.stop_current = True
            task_stop.set()
            return True

        runner._rotation_step5_execute_to_script_and_start_mode = run_child

        runner.run_rotation_mode(
            stop_event=task_stop,
            use_foreground=False,
            rotation_rare_slot="埃尔特",
        )

        self.assertEqual(
            events,
            ["child_enter", "child_return", "maintenance", "next_child"],
        )
        self.assertEqual(swf_runtime_subset_calls, [False, True])


if __name__ == "__main__":
    unittest.main()
