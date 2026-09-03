import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

from core.daily_runner import (
    DailyRunner,
    FOLLOW_TO_NEXT_UI_DELAY_SEC,
    LIGHT_MANTIS_ENTRY_KEY,
    LIGHT_MANTIS_RANDOM_CONFIRM_CENTER_KEY,
)
from core.dar_route_runner import DarRouteRunner
from core.unified_battle_framework import UnifiedBattleFramework


class _FakeBattleFramework:
    def __init__(self) -> None:
        self.actions = []
        self._stage3_exit_reason = ""
        self._battle_capsule_counts = {}
        self._capsule_cycle_index = 0
        self._battle_start_time = 0.0
        self._battle_duration = 0.0
        self._round_idx = 0

    def _start_kernel_listen(self, *, clear_queue):
        return None

    def _merge_kernel_buffer_after_stage2_gap(self):
        return None

    def _load_probe_templates(self):
        return object()

    def _execute_action(self, action, config, *, round_idx):
        self.actions.append((action, round_idx))

    def _stop_kernel_listen(self):
        return None


class LightMantisBattleLogicTests(unittest.TestCase):
    def _runner(self):
        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._unified_framework = _FakeBattleFramework()
        runner._emit = lambda *_args, **_kwargs: None
        runner._should_abort = lambda: False
        return runner

    def test_197_first_round_uses_skill_one(self) -> None:
        runner = self._runner()

        action = runner._light_mantis_first_action(1, log_tag="光螳螂测试")

        self.assertEqual(action, "skill")

    def test_battle_starts_in_197_red_probe_phase(self) -> None:
        runner = self._runner()
        wait_results = iter(("blue", "battle_end"))
        target_colors = []
        runner._lanlan_wait_round_blue_or_switch = (
            lambda *_args, target_switch_color, **_kwargs: (
                target_colors.append(target_switch_color) or next(wait_results)
            )
        )
        runner._yilu_fear_probe_red = lambda: True

        result = runner._run_light_mantis_battle_loop(
            {},
            SimpleNamespace(round_timeout_sec=60.0, use_foreground=False),
            SimpleNamespace(),
            log_tag="光螳螂测试",
        )

        self.assertEqual(result, "ended")
        self.assertEqual(target_colors, ["purple", "purple"])
        self.assertEqual(runner._unified_framework.actions, [("skill2", 2)])

    def test_197_defeat_switches_only_purple_then_escapes(self) -> None:
        runner = self._runner()
        runner._lanlan_wait_round_blue_or_switch = (
            lambda *_args, **_kwargs: "switched_purple"
        )
        runner._yilu_fear_probe_red = lambda: True

        result = runner._run_light_mantis_battle_loop(
            {},
            SimpleNamespace(round_timeout_sec=60.0, use_foreground=False),
            SimpleNamespace(),
            log_tag="光螳螂测试",
        )

        self.assertEqual(result, "retry_after_escape")
        self.assertEqual(runner._unified_framework.actions, [("escape", 1)])

    def test_light_mantis_yellow_completion_skips_recovery(self) -> None:
        runner = self._runner()
        runner.bot.dar_route_runner = SimpleNamespace()
        runner._ensure_unified_framework = lambda *_args, **_kwargs: True
        stage2_kwargs = {}

        def stage2(*_args, **kwargs):
            stage2_kwargs.update(kwargs)
            return True, "ok"

        runner._unified_framework.stage2_calibration_and_petitem = stage2
        runner._run_light_mantis_entry_prefix = (
            lambda *_args, **_kwargs: object()
        )
        runner._run_light_mantis_battle_loop = (
            lambda *_args, **_kwargs: "ended"
        )
        runner._detect_victory_probe_result = (
            lambda *_args, **_kwargs: "yellow"
        )
        runner._click_region_safe = lambda *_args, **_kwargs: True
        runner._wait_1and1_clear = lambda *_args, **_kwargs: True
        runner._recover_daily_pick_color_slots = (
            lambda *_args, **_kwargs: self.fail(
                "光螳螂黄色完成后不应恢复精灵"
            )
        )

        ok = runner._run_light_mantis_until_yellow(
            {},
            False,
            log_tag="光螳螂测试",
        )

        self.assertTrue(ok)
        self.assertTrue(stage2_kwargs["check_calibration_after_fight_signal"])

    def test_lanlan_yellow_completion_skips_recovery(self) -> None:
        runner = self._runner()
        runner.bot.dar_route_runner = SimpleNamespace(
            _switch_pet_for_rare_mode=lambda *_args, **_kwargs: True
        )
        runner._ensure_unified_framework = lambda *_args, **_kwargs: True
        runner._unified_framework._check_calibration_probes = lambda: False
        runner._unified_framework.stage2_calibration_and_petitem = (
            lambda *_args, **_kwargs: (True, "ok")
        )
        runner._wait_region_pure_white = lambda *_args, **_kwargs: True
        runner._click_region_safe = lambda *_args, **_kwargs: True
        runner._lanlan_skill_plan_for_now = lambda: {
            "first_skill2_count": 3,
            "key": "saturday_683",
        }
        runner._run_lanlan_battle_loop = (
            lambda *_args, **_kwargs: "ended"
        )
        runner._detect_victory_probe_result = (
            lambda *_args, **_kwargs: "yellow"
        )
        runner._lanlan_has_daily_record_today = lambda: False
        runner._append_lanlan_daily_record = (
            lambda **_kwargs: True
        )
        runner._wait_1and1_clear = lambda *_args, **_kwargs: True
        runner._recover_daily_pick_color_slots = (
            lambda *_args, **_kwargs: self.fail(
                "岚岚黄色完成后不应恢复精灵"
            )
        )

        ok = runner._run_lanlan_skill2_until_yellow(
            {},
            False,
            log_tag="岚岚测试",
        )

        self.assertTrue(ok)

    def test_light_mantis_waits_after_follow_before_opening_map(self) -> None:
        calls = []
        runner = self._runner()
        required_keys = (
            "日常.地图",
            "日常.90太空站",
            "光螳螂.0",
            "光螳螂.白色探针",
            "光螳螂.1",
            "光螳螂.2",
            "光螳螂.3",
            "光螳螂.4",
            "勇者之塔.精灵大乱斗",
            "勇者之塔.1v1",
            "对话框.通用探针",
            "对话框.普通确认探针",
            "对话框.普通确认",
            "对话框.左边确认",
            "对战.使用技能一",
            "对战.使用技能二",
            "对战.使用技能四",
            "对战信息.敌方害怕",
            "对战.胜利探针",
            "对话框.对战胜利确认",
        )
        runner.bot.regions = {key: object() for key in required_keys}
        runner._wait_after_follow_before_next_ui = (
            lambda _tag: calls.append("follow_wait") or True
        )
        runner._new_daily_click_map_then_delay = (
            lambda *_args, **_kwargs: calls.append("open_map") or True
        )
        runner._click_region_safe = lambda *_args, **_kwargs: True
        runner._wait_map_npc_then_delay = lambda *_args, **_kwargs: True
        runner._run_light_mantis_until_yellow = lambda *_args, **_kwargs: True
        runner._append_light_mantis_weekly_record = lambda **_kwargs: True
        runner.bot.dar_route_runner = SimpleNamespace(
            restore_pet166_primary_after_197_success_or_reseat=(
                lambda *_args, **_kwargs: calls.append("restore_primary") or True
            )
        )

        with patch("core.daily_runner.window_manager.find_window", return_value=True):
            ok = runner.run_light_mantis_mode(use_foreground=False)

        self.assertTrue(ok)
        self.assertEqual(calls[:2], ["follow_wait", "open_map"])
        self.assertIn("restore_primary", calls)

    def test_entry_prefix_confirms_until_entry_point_is_orange(self) -> None:
        runner = self._runner()
        runner._wait_light_mantis_white_after_click0 = (
            lambda *_args, **_kwargs: True
        )
        runner._wait_left_1and1_clear = lambda *_args, **_kwargs: True
        clicks = []
        runner._click_region_safe = (
            lambda _regions, key, _foreground: clicks.append(key) or True
        )
        runner._click_light_mantis_random_confirm_square = (
            lambda *_args, **_kwargs: clicks.append("普通确认±100随机")
            or (626.0, 434.0)
        )
        entry_rgbs = iter(
            (
                (47, 167, 238),
                (47, 167, 238),
                (47, 167, 238),
                (47, 167, 238),
                (254, 103, 0),
            )
        )

        with (
            patch("core.daily_runner.mean_rgb_for_region_key", side_effect=entry_rgbs),
            patch("core.daily_runner.time.sleep", return_value=None),
            patch("core.logger.kernel_cursor", return_value=321),
        ):
            cursor = runner._run_light_mantis_entry_prefix(
                {},
                False,
                log_tag="光螳螂测试",
            )

        self.assertEqual(cursor, 321)
        self.assertEqual(
            clicks,
            [
                "光螳螂.1",
                "光螳螂.2",
                "光螳螂.3",
                "普通确认±100随机",
                "普通确认±100随机",
                "普通确认±100随机",
                "普通确认±100随机",
                LIGHT_MANTIS_ENTRY_KEY,
            ],
        )

    def test_entry_gate_random_click_uses_confirm_center_plus_minus_100(self) -> None:
        runner = self._runner()
        region = SimpleNamespace(inner_bbox=lambda: (600.0, 400.0, 640.0, 440.0))
        regions = {LIGHT_MANTIS_RANDOM_CONFIRM_CENTER_KEY: region}

        with (
            patch(
                "core.daily_runner.random.uniform",
                side_effect=(520.0, 520.0),
            ) as uniform_mock,
            patch("core.daily_runner.window_manager.click_background") as click_mock,
        ):
            point = runner._click_light_mantis_random_confirm_square(
                regions,
                False,
            )

        self.assertEqual(point, (520.0, 520.0))
        self.assertEqual(
            uniform_mock.call_args_list,
            [unittest.mock.call(520.0, 720.0), unittest.mock.call(320.0, 520.0)],
        )
        click_mock.assert_called_once_with(520.0, 520.0)

    def test_light_mantis_stage2_checks_calibration_after_fight_signal(self) -> None:
        framework = UnifiedBattleFramework.__new__(UnifiedBattleFramework)
        framework.bot = SimpleNamespace(stop_current=False, is_paused=False)
        framework._kernel_q = deque(
            ["path=resource/fightResource/pet/swf/197.swf"]
        )
        framework._petswf_to_petitem_durations = []
        framework._start_kernel_listen = lambda: None
        framework._stop_kernel_listen = lambda: None
        framework._emit = lambda *_args, **_kwargs: None
        framework._check_calibration_probes = lambda: True
        framework._calculate_x_values = lambda: ([0, 1, 0, 0], {})
        framework._analyze_distribution = lambda _values: ("0+1+0+0", 2)
        calibration_clicks = []

        def calibrate(target_idx, use_foreground):
            calibration_clicks.append((target_idx, use_foreground))
            framework._kernel_q.append(
                "path=resource/item/petItem/icon/300011.swf"
            )

        framework._calibrate_click_group = calibrate

        with (
            patch(
                "core.unified_battle_framework.fetch_kernel_since",
                return_value=[],
            ),
            patch("core.unified_battle_framework.kernel_cursor", return_value=99),
            patch("core.unified_battle_framework.time.sleep", return_value=None),
        ):
            ok, _result = framework.stage2_calibration_and_petitem(
                skip_stage1=True,
                initial_cursor=50,
                timeout_s=1.0,
                check_calibration_after_fight_signal=True,
            )

        self.assertTrue(ok)
        self.assertEqual(calibration_clicks, [(2, False)])

    def test_entry_orange_gate_accepts_project_orange_tolerance(self) -> None:
        runner = self._runner()

        self.assertTrue(runner._light_mantis_entry_is_orange((254, 103, 0)))
        self.assertTrue(runner._light_mantis_entry_is_orange((230, 127, 24)))
        self.assertFalse(runner._light_mantis_entry_is_orange((229, 127, 24)))
        self.assertFalse(runner._light_mantis_entry_is_orange(None))

    def test_primary_cyan_fast_path_only_follows_purple(self) -> None:
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner.regions = {"精灵背包.选中四": object()}
        runner._emit = lambda *_args, **_kwargs: None
        runner.scan_pick_bag_party_color_slots_any = lambda *_args, **_kwargs: {
            "ok": True,
            "cyan": "一",
            "purple": "四",
        }
        runner._recover_unique_bag_slots = (
            lambda *_args, **_kwargs: self.fail(
                "青色已在精灵一时不应再执行恢复"
            )
        )
        selected = []
        runner._click_pet_with_selection_check = (
            lambda slot, *_args, **_kwargs: selected.append(slot) or True
        )
        clicks = []
        runner._click_region = lambda key, *_args, **_kwargs: clicks.append(key)
        runner._sleep_abortable = lambda *_args, **_kwargs: None
        runner._reset_skill_recovery_counters = lambda *_args, **_kwargs: None

        ok = runner.recover_cyan_and_follow_purple_from_open_bag(
            False,
            __import__("threading").Event(),
            "光螳螂测试",
            set_cyan_primary=True,
            recover_pet_one=False,
            recover_cyan=True,
            set_follow_purple=True,
            skip_cyan_recovery_when_primary=True,
        )

        self.assertTrue(ok)
        self.assertEqual(selected, ["四"])
        self.assertEqual(clicks, ["精灵背包.身边跟随"])

    def test_follow_transition_wait_is_half_second(self) -> None:
        runner = self._runner()

        with patch("core.daily_runner.time.sleep") as sleep_mock:
            ok = runner._wait_after_follow_before_next_ui("测试")

        self.assertTrue(ok)
        sleep_mock.assert_called_once_with(FOLLOW_TO_NEXT_UI_DELAY_SEC)
        self.assertEqual(FOLLOW_TO_NEXT_UI_DELAY_SEC, 0.5)

    def test_197_success_primary_restore_stops_after_first_skill3_match(self) -> None:
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._emit = lambda *_args, **_kwargs: None
        runner._pickmode_open_bag_ready_for_target = (
            lambda *_args, **_kwargs: True
        )
        runner._scan_bag_orange_slots_2_to_6 = (
            lambda *_args, **_kwargs: [2, 3]
        )
        selected = []
        runner._click_pet_with_selection_check = (
            lambda pos, *_args, **_kwargs: selected.append(pos) or True
        )
        runner._sleep_abortable = lambda *_args, **_kwargs: None
        runner._wait_bag_skill3_probe_stable = (
            lambda *_args, **_kwargs: runner.BAG_SKILL3_PRIMARY_TARGET_RGB
        )
        clicks = []
        runner._click_region = (
            lambda key, *_args, **_kwargs: clicks.append(key)
        )
        closed = []
        runner._close_pet_bag_with_verify = (
            lambda *_args, **_kwargs: closed.append(True)
        )

        ok = runner.restore_pet166_primary_after_197_success_or_reseat(
            False,
            __import__("threading").Event(),
        )

        self.assertTrue(ok)
        self.assertEqual(selected, ["二"])
        self.assertEqual(clicks, ["精灵背包.设为首发"])
        self.assertEqual(closed, [True])


if __name__ == "__main__":
    unittest.main()
