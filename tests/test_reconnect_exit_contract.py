import ast
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.bot_thread import BotWorker
from core.daily_runner import DailyRunner
from core.dar_route_runner import DarRouteRunner, _RotationSegmentStopSignal
from gui.dashboard import Dashboard


class ReconnectExitContractTests(unittest.TestCase):
    def make_runner(self) -> DarRouteRunner:
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner._refresh_reconnect_lock = threading.RLock()
        runner._refresh_reconnect_executing = False
        runner._reconnect_scripts_executing = False
        runner._should_restart_after_reconnect = False
        runner._current_mode = None
        runner._event_pet_active_profile = None
        runner._configured_nieo_profile = None
        runner._emit = lambda *args, **kwargs: None
        return runner

    def test_retry_limit_hands_restart_to_outer_controller(self) -> None:
        runner = self.make_runner()
        runner._reconnect_scripts_executing = True
        runner._should_restart_after_reconnect = True
        stop_event = threading.Event()

        ok = runner._execute_refresh_reconnect(
            object(),
            False,
            stop_event,
            retry_count=runner.REFRESH_RECONNECT_MAX_RETRIES,
        )

        self.assertFalse(ok)
        self.assertTrue(stop_event.is_set())
        self.assertFalse(runner._reconnect_scripts_executing)
        self.assertTrue(runner._should_restart_after_reconnect)

    def test_reentrant_request_exits_old_flow_and_requests_restart(self) -> None:
        runner = self.make_runner()
        runner._refresh_reconnect_executing = True
        stop_event = threading.Event()

        ok = runner._execute_refresh_reconnect(object(), False, stop_event)

        self.assertFalse(ok)
        self.assertTrue(stop_event.is_set())
        self.assertTrue(runner._should_restart_after_reconnect)

    def test_map_timeout_exits_to_outer_restart_without_inline_reconnect(self) -> None:
        runner = self.make_runner()
        reconnect_calls = []
        runner._execute_refresh_reconnect = (
            lambda *args, **kwargs: reconnect_calls.append((args, kwargs))
        )
        stop_event = threading.Event()
        profile = SimpleNamespace(name="测试模式")

        ok = runner._handle_map_entry_timeout(
            profile, False, stop_event, is_rotation_mode=False
        )

        self.assertTrue(ok)
        self.assertEqual(reconnect_calls, [])
        self.assertTrue(stop_event.is_set())
        self.assertTrue(runner._should_restart_after_reconnect)

        reason = runner.consume_mode_restart_request(stop_event)

        self.assertEqual(reason, "测试模式-进入地图失败")
        self.assertFalse(stop_event.is_set())
        self.assertFalse(runner._should_restart_after_reconnect)

    def test_target_click_timeout_requests_outer_restart(self) -> None:
        runner = self.make_runner()
        runner.bot = SimpleNamespace(
            stop_current=False,
            user_stop_requested=False,
        )
        stop_event = threading.Event()

        requested = runner._handle_target_click_failure(stop_event)

        self.assertTrue(requested)
        self.assertTrue(stop_event.is_set())
        self.assertTrue(runner._should_restart_after_reconnect)
        self.assertEqual(runner._mode_restart_reason, "目标点击超时")

    def test_target_click_stop_does_not_request_restart(self) -> None:
        runner = self.make_runner()
        runner.bot = SimpleNamespace(
            stop_current=False,
            user_stop_requested=False,
        )
        stop_event = threading.Event()
        stop_event.set()

        requested = runner._handle_target_click_failure(stop_event)

        self.assertFalse(requested)
        self.assertFalse(runner._should_restart_after_reconnect)

    def test_inline_refresh_reconnect_calls_are_limited_to_top_level_and_nono_retry(
        self,
    ) -> None:
        source_path = Path(__file__).parents[1] / "core" / "dar_route_runner.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8-sig"))
        owners = []

        class Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.functions = []

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self.functions.append(node.name)
                self.generic_visit(node)
                self.functions.pop()

            def visit_Call(self, node: ast.Call) -> None:
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "_execute_refresh_reconnect"
                ):
                    owners.append(self.functions[-1] if self.functions else "<module>")
                self.generic_visit(node)

        Visitor().visit(tree)

        self.assertEqual(
            owners,
            [
                "_execute_refresh_flow_and_wait_login",
                "_execute_refresh_reconnect",
            ],
        )

    def test_dashboard_refresh_helper_starts_a_top_level_reconnect(self) -> None:
        runner = self.make_runner()
        captured = {}

        def reconnect(**kwargs):
            captured.update(kwargs)
            return False

        runner._execute_refresh_reconnect = reconnect
        stop_event = threading.Event()

        ok = runner._execute_refresh_flow_and_wait_login(
            object(), False, stop_event, retry_count=1, max_retries=3
        )

        self.assertFalse(ok)
        self.assertEqual(captured["retry_count"], 0)
        self.assertEqual(captured["max_retries"], 3)
        self.assertFalse(captured["is_retry"])

    def test_normal_mode_restart_finishes_preparation_before_second_run(
        self,
    ) -> None:
        restart_reasons = iter(["地图不匹配", None])
        worker = SimpleNamespace(
            user_stop_requested=False,
            stop_current=False,
            _stop_event=threading.Event(),
            dar_route_runner=SimpleNamespace(
                consume_mode_restart_request=lambda _event: next(restart_reasons)
            ),
            emit_and_log=lambda *args, **kwargs: None,
        )
        calls = []
        preparation_results = iter([False, True])

        with patch("core.bot_thread.time.sleep", return_value=None):
            BotWorker._run_dar_mode_with_restart(
                worker,
                "测试模式",
                lambda: calls.append("run"),
                prepare_after_restart=lambda: (
                    calls.append("prepare") or next(preparation_results)
                ),
            )

        self.assertEqual(calls, ["run", "prepare", "prepare", "run"])

    def test_daily_mode_guard_reconnects_then_reruns_same_mode(self) -> None:
        restart_reasons = iter(["背包UI未就绪", None])
        calls = []
        daily_runner = SimpleNamespace(_outer_mode_restart_enabled=False)
        worker = SimpleNamespace(
            user_stop_requested=False,
            stop_current=False,
            _stop_event=threading.Event(),
            daily_runner=daily_runner,
            dar_route_runner=SimpleNamespace(
                consume_mode_restart_request=lambda _event: next(restart_reasons)
            ),
            emit_and_log=lambda *args, **kwargs: None,
        )
        worker._run_dar_mode_with_restart = (
            lambda label, run, prepare_after_restart=None: (
                BotWorker._run_dar_mode_with_restart(
                    worker,
                    label,
                    run,
                    prepare_after_restart,
                )
            )
        )
        worker._prepare_daily_mode_after_restart = (
            lambda label, use_foreground: calls.append(
                ("prepare", label, use_foreground)
            )
            or True
        )

        def run_once():
            calls.append(
                ("run", daily_runner._outer_mode_restart_enabled)
            )

        with patch("core.bot_thread.time.sleep", return_value=None):
            BotWorker._run_daily_mode_with_restart(
                worker,
                "特训循环",
                run_once,
                False,
            )

        self.assertEqual(
            calls,
            [
                ("run", True),
                ("prepare", "特训循环", False),
                ("run", True),
            ],
        )
        self.assertFalse(daily_runner._outer_mode_restart_enabled)

    def test_rotation_child_restart_is_consumed_at_rotation_boundary(self) -> None:
        runner = self.make_runner()
        runner.bot = SimpleNamespace(
            stop_current=False,
            user_stop_requested=False,
        )
        stop_event = threading.Event()
        runner._request_mode_restart(stop_event, "地图不匹配")

        consumed = runner._consume_rotation_child_restart(
            stop_event,
            source="到点切换子模式",
        )

        self.assertTrue(consumed)
        self.assertFalse(stop_event.is_set())
        self.assertFalse(runner._should_restart_after_reconnect)

    def test_rotation_pre_entry_maintenance_clears_synthetic_switch(self) -> None:
        runner = self.make_runner()
        runner._pending_rotation_switch = True
        runner._target_mode_after_switch = "rare:乌索"
        runner._maybe_run_hourly_yilu_lanlan_maintenance = lambda: True

        handled = runner._run_rotation_pre_entry_maintenance()

        self.assertTrue(handled)
        self.assertFalse(runner._pending_rotation_switch)
        self.assertIsNone(runner._target_mode_after_switch)

    def test_scheduled_rotation_does_not_report_success_after_child_restart(
        self,
    ) -> None:
        runner = self.make_runner()
        runner.bot = SimpleNamespace(
            stop_current=False,
            user_stop_requested=False,
        )
        runner._rotation_reconnect_executing = False
        runner._stop_normal_1and1_monitoring = lambda: None
        runner._reset_petswf_time_variables = lambda *args, **kwargs: None
        runner._sync_swf_for_rotation_segment = lambda *args, **kwargs: True
        runner._rotation_step1_login = lambda *args, **kwargs: True
        runner._rotation_step1_force_molecule_converter = lambda: False
        runner._prepare_rotation_segment_pick_overrides = lambda *args: None
        runner._rotation_clear_backpack_and_pick_or_skip = (
            lambda *args, **kwargs: True
        )
        runner._rotation_pick_mode_for = lambda _mode: "shuangta"
        runner._rotation_expected_party_ids_for_segment = (
            lambda _mode: frozenset()
        )
        runner._rotation_step4_set_companion = lambda *args, **kwargs: True
        runner._detect_rotation_mode = lambda *args, **kwargs: (
            "rare:乌索",
            object(),
        )
        logs = []
        runner._emit = lambda text, level="INFO": logs.append((text, level))

        def run_child(*args, **kwargs):
            runner._should_restart_after_reconnect = True
            return True

        runner._rotation_step5_execute_to_script_and_start_mode = run_child

        runner._execute_rotation_reconnect(
            False,
            threading.Event(),
            "rare:乌索",
            reason="轮换模式-到点切换",
        )

        self.assertFalse(
            any("轮换重连完成" in text for text, _level in logs)
        )
        self.assertTrue(
            any("子模式已请求重启" in text for text, _level in logs)
        )

    def test_rotation_login_failure_returns_to_rotation_mainline(self) -> None:
        runner = self.make_runner()
        runner.bot = SimpleNamespace(stop_current=False)
        runner._consume_nono_fusion_connection_ready = lambda: False
        runner._try_delete_game_tmp_dir = lambda **kwargs: None
        runner._step1_trinity_clicks_and_wait_login_swf = (
            lambda *args, **kwargs: "no_login"
        )
        stop_event = threading.Event()

        with patch("core.dar_route_runner.window_manager.find_window", return_value=False):
            ok = runner._rotation_step1_login(False, stop_event)

        self.assertFalse(ok)
        self.assertFalse(stop_event.is_set())
        self.assertFalse(runner._should_restart_after_reconnect)

    def test_rotation_segment_stop_is_scoped_away_from_task_stop(self) -> None:
        task_stop = threading.Event()
        segment_stop = _RotationSegmentStopSignal(task_stop)

        segment_stop.set()

        self.assertTrue(segment_stop.is_set())
        self.assertTrue(segment_stop.segment_is_set())
        self.assertFalse(task_stop.is_set())

    def test_daily_script_abort_reason_accepts_scoped_rotation_stop(self) -> None:
        task_stop = threading.Event()
        segment_stop = _RotationSegmentStopSignal(task_stop)
        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(
            user_stop_requested=False,
            stop_current=False,
            _stop_event=task_stop,
        )

        self.assertIsNone(runner._abort_reason(segment_stop))

        segment_stop.set()
        self.assertEqual(runner._abort_reason(segment_stop), "当前流程停止事件")
        self.assertFalse(task_stop.is_set())

    def test_scheduled_reconnect_child_cannot_poison_task_stop_event(self) -> None:
        runner = self.make_runner()
        task_stop = threading.Event()
        runner.bot = SimpleNamespace(
            stop_current=False,
            user_stop_requested=False,
            _stop_event=task_stop,
        )
        runner._rotation_reconnect_executing = False
        runner._stop_normal_1and1_monitoring = lambda: None
        runner._reset_petswf_time_variables = lambda *args, **kwargs: None
        runner._sync_swf_for_rotation_segment = lambda *args, **kwargs: True
        runner._rotation_step1_login = lambda *args, **kwargs: True
        runner._rotation_step1_force_molecule_converter = lambda: False
        runner._prepare_rotation_segment_pick_overrides = lambda *args: None
        runner._rotation_clear_backpack_and_pick_or_skip = (
            lambda *args, **kwargs: True
        )
        runner._rotation_pick_mode_for = lambda _mode: "nieo"
        runner._rotation_expected_party_ids_for_segment = (
            lambda _mode: frozenset()
        )
        runner._rotation_step4_set_companion = lambda *args, **kwargs: True
        runner._detect_rotation_mode = lambda *args, **kwargs: (
            "nieo",
            datetime.now(timezone.utc) + timedelta(hours=1),
        )
        captured = {}

        def run_child(_mode, _foreground, child_stop, _next_switch):
            captured["stop"] = child_stop
            child_stop.set()
            runner._pending_rotation_switch = True
            runner._target_mode_after_switch = "eit"
            return True

        runner._rotation_step5_execute_to_script_and_start_mode = run_child

        runner._execute_rotation_reconnect(
            False,
            task_stop,
            "nieo",
            reason="测试整点切换",
        )

        self.assertIsInstance(captured["stop"], _RotationSegmentStopSignal)
        self.assertIs(captured["stop"].task_stop_event, task_stop)
        self.assertTrue(captured["stop"].segment_is_set())
        self.assertFalse(task_stop.is_set())

    def test_rotation_to_script_receives_the_scoped_segment_stop(self) -> None:
        runner = self.make_runner()
        task_stop = threading.Event()
        segment_stop = _RotationSegmentStopSignal(task_stop)
        captured = {}

        def run_single_script(script_name, **kwargs):
            captured["script_name"] = script_name
            captured.update(kwargs)
            return True

        runner.bot = SimpleNamespace(
            stop_current=False,
            user_stop_requested=False,
            _stop_event=task_stop,
            daily_runner=SimpleNamespace(run_single_script=run_single_script),
        )
        runner._special_essence_gate_after_to = lambda *args, **kwargs: None
        runner._sleep_abortable = lambda *args, **kwargs: None
        runner._check_last_map_and_newnpc = lambda *_args, **_kwargs: (11, True)

        with patch("core.dar_route_runner.kernel_cursor", return_value=0):
            ok = runner._rotation_step5_execute_to_script_and_start_mode(
                "nieo",
                False,
                segment_stop,
                datetime.now(timezone.utc) + timedelta(hours=1),
                entry_check_only=True,
            )

        self.assertTrue(ok)
        self.assertEqual(captured["script_name"], "to尼奥")
        self.assertIs(captured["stop_event"], segment_stop)
        self.assertFalse(task_stop.is_set())

    def test_rotation_mainline_keeps_canonical_task_event_after_switch(self) -> None:
        runner = self.make_runner()
        task_stop = threading.Event()
        runner.bot = SimpleNamespace(
            stop_current=False,
            user_stop_requested=False,
            _stop_event=task_stop,
        )
        runner.reset_swf_sync_state = lambda: None
        runner._resolve_rotation_rare_profile = (
            lambda _slot: SimpleNamespace(name="测试稀有")
        )
        runner._run_rotation_pre_entry_maintenance = lambda: False
        runner._detect_rotation_mode = lambda *args, **kwargs: (
            "nieo",
            datetime.now(timezone.utc) + timedelta(hours=1),
        )
        runner._sync_swf_for_rotation_segment = lambda *args, **kwargs: True
        runner._rotation_step1_force_molecule_converter = lambda: False
        runner._prepare_rotation_segment_pick_overrides = lambda *args: None
        runner._rotation_expected_party_ids_for_segment = (
            lambda _mode: frozenset()
        )
        runner._rotation_pick_mode_for = lambda _mode: "nieo"
        runner._rotation_clear_backpack_and_pick_or_skip = (
            lambda *args, **kwargs: True
        )
        runner._rotation_step4_set_companion = lambda *args, **kwargs: True
        runner._consume_rotation_child_restart = lambda *args, **kwargs: False
        runner._execute_rotation_reconnect = lambda *args, **kwargs: None
        runner._sleep_abortable = lambda *args, **kwargs: None
        login_events = []

        def login(_foreground, current_stop, **_kwargs):
            login_events.append(current_stop)
            if len(login_events) == 2:
                runner.bot.user_stop_requested = True
                runner.bot.stop_current = True
                task_stop.set()
                return False
            return True

        def run_first_segment(_mode, _foreground, child_stop, _next_switch):
            self.assertIsInstance(child_stop, _RotationSegmentStopSignal)
            child_stop.set()
            runner._pending_rotation_switch = True
            runner._target_mode_after_switch = "eit"
            return True

        runner._rotation_step1_login = login
        runner._rotation_step5_execute_to_script_and_start_mode = run_first_segment

        runner.run_rotation_mode(
            stop_event=task_stop,
            use_foreground=False,
        )

        self.assertEqual(len(login_events), 2)
        self.assertIs(login_events[0], task_stop)
        self.assertIs(login_events[1], task_stop)
        self.assertTrue(task_stop.is_set())
        self.assertTrue(runner.bot.user_stop_requested)

    def test_nieo_successful_entry_does_not_clear_slow_battle_counter(self) -> None:
        runner = self.make_runner()
        runner._nieo_consecutive_entry_failures = 2
        runner._petswf_to_petitem_consecutive_over_threshold = 1

        runner._mark_nieo_entry_success()

        self.assertEqual(runner._nieo_consecutive_entry_failures, 0)
        self.assertEqual(runner._petswf_to_petitem_consecutive_over_threshold, 1)

    def test_nieo_two_consecutive_slow_battles_request_restart(self) -> None:
        runner = self.make_runner()
        runner.bot = SimpleNamespace(
            user_stop_requested=False,
            stop_current=False,
        )
        runner._petswf_to_petitem_min_duration = 1.0
        runner._petswf_to_petitem_current_duration = 7.0
        runner._petswf_to_petitem_consecutive_over_threshold = 0
        runner._nieo_consecutive_entry_failures = 0
        runner.PETSWF_TO_PETITEM_MIN_MULTIPLIER = 3.0
        runner.PETSWF_TO_PETITEM_CONSECUTIVE_LIMIT = 2
        runner.NIEO_PETSWF_HARD_LIMIT_MIN_SEC = 6.5
        runner.NIEO_PETSWF_HARD_LIMIT_MAX_SEC = 8.5
        runner._battle_count = 2
        runner._last_reconnect_battle_count = 0
        handoffs = []
        runner._handoff_after_nieo_reconnect = (
            lambda event: handoffs.append(event)
        )
        stop_event = threading.Event()

        first = runner._check_nieo_reconnect_condition(False, stop_event)
        runner._mark_nieo_entry_success()
        second = runner._check_nieo_reconnect_condition(False, stop_event)

        self.assertFalse(first)
        self.assertTrue(second)
        self.assertEqual(runner._petswf_to_petitem_consecutive_over_threshold, 2)
        self.assertEqual(handoffs, [stop_event])

    def test_failed_daily_handoff_log_does_not_claim_completion(self) -> None:
        logs = []
        emitted_tasks = []
        fake_dashboard = SimpleNamespace(
            _auto_rotation_handoff_daily_completed=False,
            _build_rotation_mode_tasks=lambda **_kwargs: {
                "rotation_test_mode": False,
                "rotation_rare_slot": "shuangta",
            },
            _wild_profile_label=lambda _slot: "双塔",
            log_message=lambda text, level: logs.append((text, level)),
            _lock_ui=lambda: None,
            _emit_start=lambda tasks: emitted_tasks.append(tasks),
        )

        Dashboard._auto_start_rotation_mode(fake_dashboard)

        self.assertTrue(any("一键日常未完整完成" in text for text, _ in logs))
        self.assertFalse(any("日常任务完成" in text for text, _ in logs))
        self.assertEqual(logs[-1][1], "WARN")
        self.assertEqual(len(emitted_tasks), 1)


if __name__ == "__main__":
    unittest.main()
