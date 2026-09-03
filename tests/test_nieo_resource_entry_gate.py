import threading
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.dar_route_runner import DarRouteRunner
from core.nieo_mode_registry import NieoModeProfile


class NieoResourceEntryGateTests(unittest.TestCase):
    @staticmethod
    def make_profile(*, post_recovery_to_script: str = "") -> NieoModeProfile:
        return NieoModeProfile(
            name="晶化气泡",
            route_hint="晶化气泡",
            slug="晶化气泡",
            to_script="to晶化气泡",
            map_a_id=55,
            map_b_id=54,
            map_c_id=55,
            entry_stem="55to54",
            transition_b_to_c="54to55",
            transition_c_to_b="55to54",
            prefix_b="晶化气泡一",
            prefix_c="晶化气泡二",
            action_b="defeat",
            action_c="skip",
            prefix_a="晶化气泡二",
            action_a="skip",
            transition_b_to_a="54to55",
            transition_a_to_b="55to54",
            white_probe_key="尼奥一.白色探针",
            white_probe_map_ids=(55, 54),
            post_recovery_to_script=post_recovery_to_script,
        )

    @staticmethod
    def make_runner() -> DarRouteRunner:
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(
            stop_current=False,
            daily_runner=SimpleNamespace(run_single_script=Mock(return_value=True)),
        )
        runner.regions = {}
        runner.ROTATION_TEST_MODE = False
        runner._configured_nieo_single_map = True
        runner._configured_nieo_profile = None
        runner._nieo_single_map_escape = True
        runner._next_rotation_switch_time = None
        runner._unified_framework = SimpleNamespace()
        runner._wild_adapter = SimpleNamespace()
        runner._battle_logger = SimpleNamespace(new_run=Mock())
        runner._nieo_calibration_records = []
        runner._jita_pos = None
        runner._yameisi_pos = None
        runner._aisifeige_pos = None
        runner._emit = Mock()
        runner._stop_normal_1and1_monitoring = Mock()
        runner._reset_petswf_time_variables = Mock()
        runner._should_abort_user_task = Mock(return_value=False)
        runner._is_user_stop_requested = Mock(return_value=False)
        runner._recover_pets = Mock()
        runner._check_and_delete_swf_files = Mock()
        return runner

    def test_map55_special_gate_requires_white_then_nonwhite(self) -> None:
        runner = self.make_runner()
        runner._wait_for_map_after_cursor = Mock(return_value=True)
        runner._wait_for_map10_white_probe_ready = Mock(return_value=True)

        ok = runner._crystal_bubble_gate_after_to(
            123,
            threading.Event(),
            log_tag="资源入口",
        )

        self.assertTrue(ok)
        runner._wait_for_map10_white_probe_ready.assert_called_once()
        self.assertTrue(
            runner._wait_for_map10_white_probe_ready.call_args.kwargs["two_phase"]
        )

    @patch("core.dar_route_runner.window_manager.ensure_game_hwnd", return_value=True)
    def test_rotation_skip_pre_entry_still_clicks_and_gates_map_b(
        self, _ensure_game_hwnd: Mock
    ) -> None:
        runner = self.make_runner()
        profile = self.make_profile()
        stop_event = threading.Event()
        runner._execute_map_entry_script_by_stem = Mock(return_value=True)

        def gate_map_b(map_id, _stop_event, **_kwargs):
            self.assertEqual(map_id, 54)
            stop_event.set()
            return True

        runner._wait_for_map_id = Mock(side_effect=gate_map_b)

        runner.run_configured_nieo_mode(
            profile,
            stop_event,
            use_foreground=False,
            is_rotation_mode=True,
            next_switch_time=datetime(2026, 7, 24, 18, 0, 0),
            skip_pre_entry=True,
        )

        runner._recover_pets.assert_not_called()
        runner._execute_map_entry_script_by_stem.assert_called_once_with(
            "55to54", False, stop_event
        )
        runner._wait_for_map_id.assert_called_once()
        runner._reset_petswf_time_variables.assert_called_once_with(
            "晶化气泡-轮换启动"
        )

    @patch("core.dar_route_runner.window_manager.ensure_game_hwnd", return_value=True)
    def test_post_recovery_resource_gate_requires_white_then_nonwhite(
        self, _ensure_game_hwnd: Mock
    ) -> None:
        runner = self.make_runner()
        profile = self.make_profile(post_recovery_to_script="to晶化气泡二")
        stop_event = threading.Event()
        runner._wait_for_map_after_cursor = Mock(return_value=True)

        def gate_probe(*_args, **_kwargs):
            stop_event.set()
            return True

        runner._wait_for_map10_white_probe_ready = Mock(side_effect=gate_probe)

        runner.run_configured_nieo_mode(
            profile,
            stop_event,
            use_foreground=False,
            skip_pre_entry=True,
        )

        runner._wait_for_map10_white_probe_ready.assert_called_once()
        self.assertTrue(
            runner._wait_for_map10_white_probe_ready.call_args.kwargs["two_phase"]
        )


if __name__ == "__main__":
    unittest.main()
