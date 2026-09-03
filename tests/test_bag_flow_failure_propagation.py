import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.bot_thread import BotWorker
from core.daily_runner import (
    DailyRunner,
    MASTER_CUP_568_SKILL_SEQUENCE,
    MASTER_CUP_568_TYPES,
    MASTER_CUP_DEFAULT_OWN_PARTY_IDS,
    MASTER_CUP_NORM_FIRE_PRE_SETUP_SPEC,
    MASTER_CUP_PRE_SETUP_SPECS,
    MASTER_CUP_SUPPORTED_TYPES,
)
from core.dar_route_runner import DarRouteRunner


class BagFlowFailurePropagationTests(unittest.TestCase):
    def test_gacha_rotation_handoff_starts_selected_rotation_only_once(self) -> None:
        calls = []
        logs = []

        class FakeDarRunner:
            ROTATION_RECONNECT_INTERVAL_MINUTES_NIEO = 60.0
            ROTATION_RECONNECT_INTERVAL_MINUTES_SHUANGTA = 60.0
            PETSWF_TO_PETITEM_HARD_LIMIT_SEC = 8.5

            def run_rotation_mode(self, **kwargs):
                calls.append(kwargs)
                return True

        worker = BotWorker.__new__(BotWorker)
        worker.user_stop_requested = False
        worker.stop_current = False
        worker._stop_event = threading.Event()
        worker._gacha_rotation_handoff_started = False
        worker._task_lock = threading.Lock()
        worker.active_tasks = {
            "rotation_test_mode": True,
            "rotation_interval_minutes_nieo": 2.0,
            "rotation_interval_minutes_shuangta": 3.0,
            "petswf_hard_limit_sec": 4.5,
            "rotation_rare_slot": "nieo",
            "rotation_resource_enabled": True,
            "rotation_resource_slug": "rare:尼奥",
            "rotation_mantis_enabled": True,
            "rotation_eit_enabled": True,
            "rotation_nieo_single_map_escape": False,
            "rotation_nieo_follow_cyan": True,
        }
        worker.emit_and_log = (
            lambda message, level="INFO": logs.append((message, level))
        )
        worker._ensure_newnpc_multi_4_hidden = lambda **_kwargs: None
        worker.dar_route_runner = FakeDarRunner()

        first = worker._run_gacha_rotation_handoff(
            total=99999,
            use_foreground=False,
            reason="重连后三次内再次失败",
        )
        second = worker._run_gacha_rotation_handoff(
            total=99999,
            use_foreground=False,
            reason="重复交接",
        )

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["use_foreground"])
        self.assertTrue(calls[0]["is_test_mode"])
        self.assertEqual(calls[0]["rotation_rare_slot"], "nieo")
        self.assertTrue(calls[0]["rotation_resource_enabled"])
        self.assertEqual(calls[0]["rotation_resource_slug"], "rare:尼奥")
        self.assertTrue(calls[0]["rotation_mantis_enabled"])
        self.assertTrue(calls[0]["rotation_eit_enabled"])
        self.assertFalse(calls[0]["rotation_nieo_single_map_escape"])
        self.assertTrue(calls[0]["rotation_nieo_follow_cyan"])
        self.assertFalse(calls[0]["rotation_full_daily_maintenance"])
        self.assertFalse(calls[0]["initial_swf_full"])
        self.assertEqual(
            worker.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_NIEO,
            2.0,
        )
        self.assertEqual(
            worker.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_SHUANGTA,
            3.0,
        )
        self.assertEqual(
            worker.dar_route_runner.PETSWF_TO_PETITEM_HARD_LIMIT_SEC,
            4.5,
        )
        self.assertIn("扭蛋共99999次", logs[0][0])

    def test_gacha_reconnect_escalates_when_third_attempt_fails(self) -> None:
        events = []

        class FakeDailyRunner:
            _last_gacha_completed_cycles = 0
            _last_gacha_failure_reason = ""

            def run_gacha_reconnect_to_ready(self, *_args, reconnect_round):
                events.append(("reconnect", reconnect_round))
                return True

            def run_gacha_probe_test(
                self,
                *,
                times,
                background_mode,
                failure_handoff,
                initial_reconnect,
            ):
                events.append(
                    (
                        "gacha",
                        times,
                        background_mode,
                        failure_handoff,
                        initial_reconnect,
                    )
                )
                self._last_gacha_completed_cycles = 2
                self._last_gacha_failure_reason = "第三次浅青超时"
                return False

        worker = BotWorker.__new__(BotWorker)
        worker.user_stop_requested = False
        worker.stop_current = False
        worker._stop_event = threading.Event()
        worker.daily_runner = FakeDailyRunner()
        worker.emit_and_log = lambda *_args, **_kwargs: None
        worker._run_gacha_rotation_handoff = (
            lambda **kwargs: events.append(("rotation", kwargs)) or True
        )

        ok = worker.request_gacha_recovery_after_failure(
            total=100,
            completed_cycles=20,
            use_foreground=False,
            reason="首次失败",
        )

        self.assertFalse(ok)
        self.assertEqual(
            events[:2],
            [
                ("reconnect", 1),
                ("gacha", 80, True, False, False),
            ],
        )
        self.assertEqual(events[2][0], "rotation")
        self.assertIn("第3次失败", events[2][1]["reason"])

    def test_initial_gacha_reconnect_escalates_on_first_attempt_failure(
        self,
    ) -> None:
        events = []
        worker = BotWorker.__new__(BotWorker)
        worker.user_stop_requested = False
        worker.stop_current = False
        worker._stop_event = threading.Event()
        worker.daily_runner = SimpleNamespace(
            run_gacha_reconnect_to_ready=lambda *_args, **_kwargs: self.fail(
                "首次执行前已经重连，第1次即失败时不应再做扭蛋重连"
            )
        )
        worker.emit_and_log = lambda *_args, **_kwargs: None
        worker._run_gacha_rotation_handoff = (
            lambda **kwargs: events.append(kwargs) or True
        )

        ok = worker.request_gacha_recovery_after_failure(
            total=100,
            completed_cycles=0,
            session_after_reconnect=True,
            use_foreground=False,
            reason="首次重连后第1次失败",
        )

        self.assertFalse(ok)
        self.assertEqual(len(events), 1)
        self.assertIn("首次重连后第1次失败", events[0]["reason"])

    def test_gacha_reconnect_retries_when_failure_is_after_first_three(self) -> None:
        events = []

        class FakeDailyRunner:
            _last_gacha_completed_cycles = 0
            _last_gacha_failure_reason = ""

            def __init__(self):
                self.calls = 0

            def run_gacha_reconnect_to_ready(self, *_args, reconnect_round):
                events.append(("reconnect", reconnect_round))
                return True

            def run_gacha_probe_test(
                self,
                *,
                times,
                background_mode,
                failure_handoff,
                initial_reconnect,
            ):
                self.calls += 1
                events.append(
                    (
                        "gacha",
                        times,
                        background_mode,
                        failure_handoff,
                        initial_reconnect,
                    )
                )
                if self.calls == 1:
                    self._last_gacha_completed_cycles = 3
                    self._last_gacha_failure_reason = "第四次失败"
                    return False
                return True

        worker = BotWorker.__new__(BotWorker)
        worker.user_stop_requested = False
        worker.stop_current = False
        worker._stop_event = threading.Event()
        worker.daily_runner = FakeDailyRunner()
        worker.emit_and_log = lambda *_args, **_kwargs: None
        worker._run_gacha_rotation_handoff = (
            lambda **_kwargs: self.fail("第四次以后失败不应直接进入轮换")
        )

        ok = worker.request_gacha_recovery_after_failure(
            total=100,
            completed_cycles=20,
            use_foreground=True,
            reason="首次失败",
        )

        self.assertTrue(ok)
        self.assertEqual(
            events,
            [
                ("reconnect", 1),
                ("gacha", 80, False, False, False),
                ("reconnect", 2),
                ("gacha", 77, False, False, False),
            ],
        )

    def test_gacha_reconnect_route_returns_to_terrace_and_waits_one_second(
        self,
    ) -> None:
        events = []
        stop_event = object()

        class FakeDarRunner:
            def run_refresh_login_until_map(
                self,
                use_foreground,
                received_stop_event,
                *,
                include_base_and_map_gate,
            ):
                events.append(
                    (
                        "refresh",
                        use_foreground,
                        received_stop_event,
                        include_base_and_map_gate,
                    )
                )
                return True

        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(
            regions=object(),
            dar_route_runner=FakeDarRunner(),
            _stop_event=stop_event,
        )
        runner._emit = lambda *_args, **_kwargs: None
        runner._should_abort = lambda: False
        runner._fusion_latest_map_id = lambda: 5
        runner._chip_gold_open_map_until_white = (
            lambda *_args, **_kwargs: events.append(("open_map",)) or True
        )
        runner._chip_gold_enter_map = (
            lambda *_args, target_key, **_kwargs: (
                events.append(("enter_map", target_key)) or True
            )
        )
        runner._chip_gold_follow_purple_from_closed_bag = (
            lambda *_args, **_kwargs: events.append(("follow_purple",)) or True
        )
        runner._click_region_safe = (
            lambda _regions, key, _foreground: events.append(("click", key)) or True
        )

        with patch(
            "core.daily_runner.time.sleep",
            side_effect=lambda seconds: events.append(("sleep", seconds)),
        ):
            ok = runner.run_gacha_reconnect_to_ready(
                False,
                reconnect_round=1,
            )

        self.assertTrue(ok)
        self.assertEqual(
            events,
            [
                ("refresh", False, stop_event, False),
                ("open_map",),
                ("enter_map", "地图.瞭望露台"),
                ("follow_purple",),
                ("click", "荣誉兑换.to扭蛋"),
                ("sleep", 1.0),
            ],
        )

    def test_fusion_clear_uses_confirmed_rotation_clear_and_keeps_bag_open(self) -> None:
        calls = []

        class FakeDarRunner:
            def _rotation_step2_clear_backpack(
                self,
                use_foreground,
                stop_event,
                *,
                log_tag,
                close_after,
            ):
                calls.append((use_foreground, stop_event, log_tag, close_after))
                return True

        stop_event = threading.Event()
        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(
            dar_route_runner=FakeDarRunner(),
            _stop_event=stop_event,
        )
        runner._emit = lambda *_args, **_kwargs: None

        ok = runner._fusion_clear_backpack_keep_open(
            object(),
            False,
            log_tag="融合测试",
        )

        self.assertTrue(ok)
        self.assertEqual(calls, [(False, stop_event, "融合测试", False)])

    def test_master_cup_standard_types_use_568_except_flight_and_norm(self) -> None:
        self.assertEqual(MASTER_CUP_NORM_FIRE_PRE_SETUP_SPEC["pet_id"], 40)
        self.assertEqual(BotWorker._master_cup_cyan_pet_for_cup("诺姆"), 40)
        self.assertEqual(BotWorker._master_cup_cyan_pet_for_cup("飞行系"), 268)
        for cup in MASTER_CUP_568_TYPES:
            self.assertEqual(BotWorker._master_cup_cyan_pet_for_cup(cup), 568)
            self.assertEqual(
                MASTER_CUP_PRE_SETUP_SPECS[cup],
                {
                    "warehouse_mode_key": "精灵仓库.单属性",
                    "warehouse_category": "普通系",
                    "pet_id": 568,
                    "scan_first_cyan": True,
                },
            )

    def test_master_cup_568_sequence_escapes_after_five_rounds(self) -> None:
        actions = [
            DailyRunner._master_cup_skill_action(
                "机械系",
                round_idx,
                skill_sequence=MASTER_CUP_568_SKILL_SEQUENCE,
                escape_after_skill_sequence=True,
            )
            for round_idx in range(1, 7)
        ]
        self.assertEqual(
            actions,
            ["skill4", "skill2", "skill4", "skill2", "skill2", "escape"],
        )

    def test_master_cup_568_enemy_id_rule_uses_occurrence_counts(self) -> None:
        own = {166: 1, 197: 1, 1459: 1, 606: 1, 568: 1, 1337: 1}
        only_568 = dict(own)
        only_568[568] = 2
        self.assertTrue(DailyRunner._master_cup_should_escape_568_only(only_568))

        mixed = dict(only_568)
        mixed[777] = 1
        self.assertFalse(DailyRunner._master_cup_should_escape_568_only(mixed))
        self.assertEqual(
            DailyRunner._master_cup_enemy_pet_ids_for_568(mixed),
            {568, 777},
        )

        same_as_own = dict(only_568)
        same_as_own[166] = 2
        self.assertFalse(DailyRunner._master_cup_should_escape_568_only(same_as_own))
        self.assertEqual(
            DailyRunner._master_cup_enemy_pet_ids_for_568(same_as_own),
            {166, 568},
        )

        default_party = {pet_id: 1 for pet_id in MASTER_CUP_DEFAULT_OWN_PARTY_IDS}
        default_party[568] = 1
        self.assertTrue(
            DailyRunner._master_cup_should_escape_568_only(
                default_party,
                set(MASTER_CUP_DEFAULT_OWN_PARTY_IDS),
            )
        )

    def test_master_cup_pet_id_collection_preserves_duplicate_568(self) -> None:
        runner = DailyRunner.__new__(DailyRunner)
        runner._should_abort = lambda: False
        lines = [
            *[
                f"path=resource\\fightResource\\pet\\swf\\{pet_id}.swf"
                for pet_id in (166, 197, 1459, 606, 568, 1337, 568, 777)
            ],
            "path=resource\\fightResource\\skill\\swf\\4.swf",
        ]
        with patch("core.logger.fetch_kernel_since", return_value=lines):
            counts = runner._collect_master_cup_battle_pet_id_counts(0)

        self.assertEqual(counts[568], 2)
        self.assertEqual(
            DailyRunner._master_cup_enemy_pet_ids_for_568(counts),
            {568, 777},
        )

    def test_master_cup_568_takes_first_normal_cyan_and_follows_purple(self) -> None:
        events = []

        class FakeDarRunner:
            def open_pickmode_bag_warehouse_from_ready_bag(self, *_args, **_kwargs):
                events.append("open_warehouse")
                return True

            def recover_cyan_and_follow_purple_from_open_bag(self, *_args, **kwargs):
                events.append(("follow", kwargs))
                return True

        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(
            dar_route_runner=FakeDarRunner(),
            stop_current=False,
            _stop_event=threading.Event(),
        )
        runner._emit = lambda *_args, **_kwargs: None
        runner._master_cup_open_bag_ready = lambda *_args, **_kwargs: True
        runner._master_cup_put_back_cyan_from_open_bag = (
            lambda *_args, **_kwargs: events.append("put_back_cyan") or True
        )
        runner._master_cup_close_warehouse_keep_bag_open = (
            lambda *_args, **_kwargs: events.append("close_warehouse") or True
        )
        runner._hatch_exp_take_first_category_color_forward = (
            lambda *_args, **kwargs: events.append(("take_first", kwargs)) or True
        )

        ok = runner._master_cup_replace_cyan_with_first_category_cyan(
            object(),
            MASTER_CUP_PRE_SETUP_SPECS["机械系"],
            False,
            log_tag="机械系568测试",
        )

        self.assertTrue(ok)
        take = next(item for item in events if isinstance(item, tuple) and item[0] == "take_first")
        self.assertEqual(take[1]["category_key"], "精灵仓库.普通系")
        self.assertEqual(take[1]["target_color"], "cyan")
        follow = next(item for item in events if isinstance(item, tuple) and item[0] == "follow")
        self.assertTrue(follow[1]["set_cyan_primary"])
        self.assertTrue(follow[1]["recover_cyan"])
        self.assertFalse(follow[1]["recover_pet_one"])
        self.assertTrue(follow[1]["set_follow_purple"])

    def test_master_cup_all_escapes_skip_pet_one_recovery(self) -> None:
        cleaned = []
        recovered = []
        runner = DailyRunner.__new__(DailyRunner)
        runner._emit = lambda *_args, **_kwargs: None
        runner._unified_framework = SimpleNamespace(
            stage4_post_battle=lambda config: cleaned.append(config) or True
        )
        runner._recover_pet_one = (
            lambda regions, use_foreground: recovered.append(
                (regions, use_foreground)
            )
            or True
        )
        regions = object()

        with patch("core.daily_runner.time.sleep", return_value=None):
            for cup in sorted(MASTER_CUP_SUPPORTED_TYPES):
                first_round_ok = runner._master_cup_handle_escape_post_battle(
                    regions,
                    False,
                    f"{cup}-first-config",
                    tag=f"{cup}首回合逃跑",
                    escape_round=1,
                )
                later_round_ok = runner._master_cup_handle_escape_post_battle(
                    regions,
                    True,
                    f"{cup}-later-config",
                    tag=f"{cup}后续回合逃跑",
                    escape_round=8,
                )
                self.assertTrue(first_round_ok, cup)
                self.assertTrue(later_round_ok, cup)

        expected_cleaned = []
        for cup in sorted(MASTER_CUP_SUPPORTED_TYPES):
            expected_cleaned.extend(
                [f"{cup}-first-config", f"{cup}-later-config"]
            )
        self.assertEqual(cleaned, expected_cleaned)
        self.assertEqual(recovered, [])

    def test_master_cup_later_escape_never_recovers_pet_one(self) -> None:
        recovered = []
        runner = DailyRunner.__new__(DailyRunner)
        runner._emit = lambda *_args, **_kwargs: None
        runner._unified_framework = SimpleNamespace(
            stage4_post_battle=lambda _config: True
        )
        runner._recover_pet_one = (
            lambda *_args, **_kwargs: recovered.append("恢复") or True
        )
        with patch("core.daily_runner.time.sleep", return_value=None):
            ok = runner._master_cup_handle_escape_post_battle(
                object(),
                False,
                object(),
                tag="大师杯后续逃跑",
                escape_round=6,
            )
        self.assertTrue(ok)
        self.assertEqual(recovered, [])

    def test_master_cup_yellow_runs_1a1_without_opening_bag(self) -> None:
        events = []
        runner = DailyRunner.__new__(DailyRunner)
        runner._emit = lambda *_args, **_kwargs: None
        runner._wait_for_1and1_cleanup = (
            lambda _use_foreground, **_kwargs: events.append("1A1") or True
        )
        runner._recover_pet_one = (
            lambda _regions, _use_foreground: events.append("打开背包恢复") or True
        )

        with patch("core.daily_runner.time.sleep", return_value=None):
            ok = runner._master_cup_recover_after_result(
                object(),
                False,
                probe_result="yellow",
                tag="大师杯黄胜",
            )

        self.assertTrue(ok)
        self.assertEqual(events, ["1A1"])

    def test_master_cup_does_not_open_bag_if_yellow_1a1_fails(self) -> None:
        runner = DailyRunner.__new__(DailyRunner)
        runner._emit = lambda *_args, **_kwargs: None
        runner._wait_for_1and1_cleanup = (
            lambda *_args, **_kwargs: False
        )
        runner._recover_pet_one = (
            lambda *_args, **_kwargs: self.fail("1A1失败后不应打开背包")
        )

        ok = runner._master_cup_recover_after_result(
            object(),
            False,
            probe_result="yellow",
            tag="大师杯黄胜",
        )

        self.assertFalse(ok)

    def test_master_cup_defaults_to_escape_568_except_norm_target_stage(self) -> None:
        self.assertEqual(
            DailyRunner._master_cup_resolve_escape_pet_id(
                None,
                allow_568_battle=False,
            ),
            568,
        )
        self.assertIsNone(
            DailyRunner._master_cup_resolve_escape_pet_id(
                None,
                allow_568_battle=True,
            )
        )

        calls = []
        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(regions=object())
        runner._emit = lambda *_args, **_kwargs: None
        runner._should_abort = lambda: False
        runner.run_master_cup_mode = (
            lambda **kwargs: calls.append(kwargs) or True
        )

        ok = DailyRunner._run_master_cup_norm_mode(
            runner,
            False,
            False,
        )

        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["cup_type"], "火系")
        self.assertEqual(calls[0]["yellow_target_count"], 10)
        self.assertTrue(calls[0]["allow_568_battle"])
        self.assertEqual(calls[0]["target_pet_id"], 568)
        self.assertEqual(calls[0]["pre_setup_spec"]["pet_id"], 40)
        self.assertFalse(calls[0]["restore_light_after_finish"])

    def test_weekly_runs_terrace_gold_suke_refresh_lab_purchase_then_honor(
        self,
    ) -> None:
        events = []

        class Regions:
            def get(self, _key):
                return object()

        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(regions=Regions())
        runner.script_dir = "fix_script"
        runner._emit = lambda *_args, **_kwargs: None
        runner._should_abort = lambda: False
        runner._fusion_latest_map_id = lambda: 103
        runner._chip_gold_refresh_login = (
            lambda *_args, **_kwargs: events.append("刷新") or True
        )
        runner._chip_gold_follow_purple_from_closed_bag = (
            lambda *_args, log_tag, **_kwargs: events.append(log_tag) or True
        )
        runner._chip_gold_open_shop = (
            lambda *_args, **_kwargs: events.append("商店") or True
        )
        runner._chip_gold_buy_chip = (
            lambda *_args, chip_key, **_kwargs: events.append(chip_key) or True
        )
        runner._chip_gold_run_crystal_suke_cycles = (
            lambda *_args, **_kwargs: events.append("晶化气泡苏克×10") or True
        )
        runner._chip_gold_open_map_until_white = (
            lambda *_args, log_tag, **_kwargs: (
                events.append(("打开地图", log_tag)) or True
            )
        )
        runner._chip_gold_enter_map = (
            lambda *_args, target_key, **_kwargs: (
                events.append(("进入地图", target_key)) or True
            )
        )
        runner.run_single_script = (
            lambda *_args, **_kwargs: events.append("金豆") or True
        )
        runner.run_honor_exchange_mode = (
            lambda **_kwargs: events.append("荣誉兑换") or True
        )
        runner._click_region_safe = (
            lambda *_args, **_kwargs: events.append("关闭商店") or True
        )

        with (
            patch("core.daily_runner.window_manager.find_window", return_value=True),
            patch("core.daily_runner.time.sleep", return_value=None),
        ):
            ok = runner.run_chip_gold_honor_mode(
                use_foreground=False,
                gacha_filled_times=3,
            )

        self.assertTrue(ok)
        self.assertEqual(
            events,
            [
                "一键周常·瞭望露台紫色跟随",
                "金豆",
                "金豆",
                "金豆",
                "晶化气泡苏克×10",
                "刷新",
                ("打开地图", "一键周常·实验室地图"),
                ("进入地图", "地图.实验室"),
                "一键周常·实验室紫色跟随",
                "商店",
                "商店.专用芯片",
                "商店.通用芯片",
                "关闭商店",
                ("打开地图", "一键周常·瞭望露台地图"),
                ("进入地图", "地图.瞭望露台"),
                "荣誉兑换",
            ],
        )

    def test_weekly_refresh_skips_base_gate_then_follows_purple_from_closed_bag(
        self,
    ) -> None:
        events = []
        stop_event = object()

        class DarRunner:
            def run_refresh_login_until_map(
                self,
                use_foreground,
                received_stop_event,
                *,
                include_base_and_map_gate,
            ):
                events.append(
                    (
                        "刷新",
                        use_foreground,
                        received_stop_event,
                        include_base_and_map_gate,
                    )
                )
                return True

            def set_follow_purple_jita_from_closed_bag(
                self,
                use_foreground,
                received_stop_event,
                *,
                log_tag,
            ):
                events.append(
                    ("跟随紫色", use_foreground, received_stop_event, log_tag)
                )
                return True

        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(
            dar_route_runner=DarRunner(),
            _stop_event=stop_event,
        )
        runner._emit = lambda *_args, **_kwargs: None

        self.assertTrue(
            runner._chip_gold_refresh_login(
                True,
                log_tag="一键周常·苏克后刷新",
            )
        )
        self.assertTrue(
            runner._chip_gold_follow_purple_from_closed_bag(
                True,
                log_tag="一键周常·实验室紫色跟随",
            )
        )
        self.assertEqual(
            events,
            [
                ("刷新", True, stop_event, False),
                ("跟随紫色", True, stop_event, "一键周常·实验室紫色跟随"),
            ],
        )

    def test_chip_gold_suke_cycle_uses_requested_click_and_wait_order(self) -> None:
        events = []
        runner = DailyRunner.__new__(DailyRunner)
        runner._emit = lambda *_args, **_kwargs: None
        runner._should_abort = lambda: False
        runner._click_region_safe = (
            lambda _regions, key, _foreground: events.append(("click", key)) or True
        )
        runner._chip_gold_wait_suke_white = (
            lambda *_args, **_kwargs: events.append(("wait_white",)) or True
        )
        runner._wait_1and1_clear = (
            lambda *_args, **_kwargs: events.append(("clear_1and1",)) or True
        )

        with patch(
            "core.daily_runner.time.sleep",
            side_effect=lambda seconds: events.append(("sleep", seconds)),
        ):
            ok = runner._chip_gold_run_suke_cycle(
                object(),
                False,
                cycle=1,
                total=10,
                log_tag="一键周常·晶化气泡苏克",
            )

        self.assertTrue(ok)
        self.assertEqual(
            events,
            [
                ("click", "苏克兑换.0"),
                ("wait_white",),
                ("sleep", 0.5),
                ("click", "苏克兑换.1"),
                ("sleep", 0.8),
                ("click", "苏克兑换.2"),
                ("clear_1and1",),
            ],
        )

    def test_chip_gold_suke_wait_uses_exchange_white_probe(self) -> None:
        scanned_keys = []
        runner = DailyRunner.__new__(DailyRunner)
        runner._emit = lambda *_args, **_kwargs: None
        runner._should_abort = lambda: False
        runner._wait_if_paused = lambda: None

        with (
            patch(
                "core.daily_runner.mean_rgb_for_region_key",
                side_effect=lambda _regions, key: (
                    scanned_keys.append(key) or (255, 255, 255)
                ),
            ),
            patch("core.daily_runner.time.sleep", return_value=None),
        ):
            ok = runner._chip_gold_wait_suke_white(
                object(),
                timeout_s=1.0,
                log_tag="一键周常·晶化气泡苏克",
            )

        self.assertTrue(ok)
        self.assertEqual(
            scanned_keys,
            ["苏克兑换.白色探针", "苏克兑换.白色探针"],
        )

    def test_chip_gold_crystal_route_gates_map55_then_runs_all_ten_cycles(
        self,
    ) -> None:
        events = []

        class Regions:
            def get(self, _key):
                return object()

        class DarRunner:
            def _crystal_bubble_gate_after_to(
                self,
                cursor,
                stop_event,
                *,
                log_tag,
            ):
                events.append(("gate", cursor, stop_event, log_tag))
                return True

        stop_event = threading.Event()
        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(
            regions=Regions(),
            dar_route_runner=DarRunner(),
            _stop_event=stop_event,
        )
        runner._emit = lambda *_args, **_kwargs: None
        runner._should_abort = lambda: False
        runner._new_daily_base_gate_and_confirm = (
            lambda *_args, **_kwargs: events.append("回基地") or True
        )
        runner._click_region_safe = (
            lambda _regions, key, _foreground: events.append(("click", key)) or True
        )
        runner.run_single_script = (
            lambda name, **_kwargs: events.append(("script", name)) or True
        )
        runner._chip_gold_run_suke_cycle = (
            lambda *_args, cycle, **_kwargs: events.append(("cycle", cycle)) or True
        )

        with (
            patch("core.logger.kernel_cursor", return_value=123),
            patch("core.daily_runner.time.sleep", return_value=None),
        ):
            ok = runner._chip_gold_run_crystal_suke_cycles(
                runner.bot.regions,
                False,
                log_tag="一键周常·晶化气泡苏克",
            )

        self.assertTrue(ok)
        self.assertEqual(events[0:4], [
            "回基地",
            ("click", "刷新.基地"),
            ("click", "刷新.基地右侧"),
            ("script", "to晶化气泡"),
        ])
        self.assertEqual(events[4][0:2], ("gate", 123))
        self.assertIs(events[4][2], stop_event)
        self.assertEqual(
            [event for event in events if event[0] == "cycle"],
            [("cycle", cycle) for cycle in range(1, 11)],
        )

    def test_chip_gold_suke_cycle_failures_do_not_stop_later_cycles(self) -> None:
        attempted = []

        class Regions:
            def get(self, _key):
                return object()

        class DarRunner:
            def _crystal_bubble_gate_after_to(self, *_args, **_kwargs):
                return True

        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(
            regions=Regions(),
            dar_route_runner=DarRunner(),
            _stop_event=threading.Event(),
        )
        runner._emit = lambda *_args, **_kwargs: None
        runner._should_abort = lambda: False
        runner._new_daily_base_gate_and_confirm = lambda *_args, **_kwargs: True
        runner._click_region_safe = lambda *_args, **_kwargs: True
        runner.run_single_script = lambda *_args, **_kwargs: True

        def run_cycle(*_args, cycle, **_kwargs):
            attempted.append(cycle)
            if cycle == 4:
                raise RuntimeError("test cycle error")
            return cycle % 2 == 0

        runner._chip_gold_run_suke_cycle = run_cycle

        with (
            patch("core.logger.kernel_cursor", return_value=1),
            patch("core.daily_runner.time.sleep", return_value=None),
        ):
            ok = runner._chip_gold_run_crystal_suke_cycles(
                runner.bot.regions,
                False,
                log_tag="一键周常·晶化气泡苏克",
            )

        self.assertTrue(ok)
        self.assertEqual(attempted, list(range(1, 11)))

    def test_chip_gold_failed_gold_script_still_runs_remaining_and_honor(
        self,
    ) -> None:
        script_attempts = []
        honor_calls = []

        class Regions:
            def get(self, _key):
                return object()

        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(regions=Regions())
        runner.script_dir = "fix_script"
        runner._emit = lambda *_args, **_kwargs: None
        runner._should_abort = lambda: False
        runner._fusion_latest_map_id = lambda: 5
        runner.run_one_click_release_mode = lambda **_kwargs: True
        runner._chip_gold_open_shop = lambda *_args, **_kwargs: True
        runner._chip_gold_buy_chip = lambda *_args, **_kwargs: True
        runner._chip_gold_buy_gacha_cards = lambda *_args, **_kwargs: True
        runner._chip_gold_run_crystal_suke_cycles = lambda *_args, **_kwargs: True
        runner._chip_gold_refresh_login = lambda *_args, **_kwargs: True
        runner._chip_gold_follow_purple_from_closed_bag = lambda *_args, **_kwargs: True
        runner._chip_gold_open_map_until_white = lambda *_args, **_kwargs: True
        runner._chip_gold_enter_map = lambda *_args, **_kwargs: True
        runner._click_region_safe = lambda *_args, **_kwargs: True
        runner.run_honor_exchange_mode = (
            lambda **_kwargs: honor_calls.append(True) or True
        )

        def run_script(*_args, **_kwargs):
            attempt = len(script_attempts) + 1
            script_attempts.append(attempt)
            if attempt == 3:
                raise RuntimeError("test script error")
            return attempt in {2, 5}

        runner.run_single_script = run_script

        with (
            patch("core.daily_runner.window_manager.find_window", return_value=True),
            patch("core.daily_runner.time.sleep", return_value=None),
        ):
            ok = runner.run_chip_gold_honor_mode(use_foreground=False)

        self.assertTrue(ok)
        self.assertEqual(script_attempts, [1])
        self.assertEqual(honor_calls, [True])

    def test_chip_gold_gacha_flow_inputs_then_handles_two_normal_1a1(
        self,
    ) -> None:
        events = []
        runner = DailyRunner.__new__(DailyRunner)
        runner._emit = lambda *_args, **_kwargs: None
        runner._should_abort = lambda: False
        runner._wait_if_paused = lambda: None
        runner._click_region_safe = (
            lambda _regions, key, _foreground: events.append(("click", key)) or True
        )
        runner._chip_gold_type_text_direct = (
            lambda text, **_kwargs: events.append(("input", text)) or True
        )
        runner._wait_1and1_clear = (
            lambda *_args, **kwargs: (
                events.append(("normal1a1", kwargs["log_tag"])) or True
            )
        )

        def probe_rgb(_regions, key):
            if key == "商店.黄色探针":
                return (255, 204, 0)
            if key == "商店.蓝色探针":
                return (47, 167, 238)
            return None

        with (
            patch("core.daily_runner.mean_rgb_for_region_key", side_effect=probe_rgb),
            patch(
                "core.daily_runner.time.sleep",
                side_effect=lambda seconds: (
                    events.append(("sleep", seconds))
                    if seconds in (0.2, 0.8)
                    else None
                ),
            ),
        ):
            ok = runner._chip_gold_buy_gacha_cards(
                object(),
                True,
                log_tag="一键周常·扭蛋牌",
            )

        self.assertTrue(ok)
        self.assertEqual(
            events,
            [
                ("click", "商店.右"),
                ("click", "商店.扭蛋牌"),
                ("click", "商店.输入框"),
                ("sleep", 0.2),
                ("input", "999999"),
                ("normal1a1", "一键周常·扭蛋牌·输入后普通1A1"),
                ("click", "商店.左边确认"),
                ("sleep", 0.8),
                ("normal1a1", "一键周常·扭蛋牌·确认后普通1A1"),
            ],
        )

    def test_one_click_release_confirmation_runs_left_then_normal_without_entry_context(
        self,
    ) -> None:
        clicks = []

        class Regions:
            def get(self, _key):
                return object()

        class DarRunner:
            def __init__(self):
                self.left_calls = 0
                self.normal_calls = 0

            def _check_left_1and1and1_probes(self):
                self.left_calls += 1
                return self.left_calls == 1

            def _check_normal_1and1and1_probes(self):
                self.normal_calls += 1
                return self.normal_calls == 1

        dar_runner = DarRunner()
        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(
            regions=Regions(),
            dar_route_runner=dar_runner,
        )
        runner._emit = lambda *_args, **_kwargs: None
        runner._should_abort = lambda: False
        runner._click_region_safe = (
            lambda _regions, key, _foreground: clicks.append(key) or True
        )

        def slot_rgb(_regions, key):
            self.assertEqual(key, "精灵仓库.1")
            if dar_runner.left_calls >= 3:
                return (47, 167, 238)
            return (254, 103, 0)

        with patch(
            "core.daily_runner.mean_rgb_for_region_key",
            side_effect=slot_rgb,
        ):
            ok = runner._one_click_release_dismiss_confirmations(
                False,
                log_tag="一键放生·确认测试",
            )

        self.assertTrue(ok)
        self.assertEqual(
            clicks,
            ["对话框.左边确认", "对话框.普通确认"],
        )
        self.assertGreaterEqual(dar_runner.left_calls, 3)
        self.assertGreaterEqual(dar_runner.normal_calls, 3)

    def test_honor_exchange_clicks_to_gacha_first_then_runs_99999_gacha(
        self,
    ) -> None:
        events = []

        class Regions:
            def get(self, _key):
                return object()

        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(regions=Regions())
        runner._emit = lambda *_args, **_kwargs: None
        runner._ensure_unified_framework = lambda *_args, **_kwargs: True
        runner._click_region_safe = (
            lambda _regions, key, _foreground: events.append(("click", key)) or True
        )
        runner._wait_region_honor_white = (
            lambda *_args, **_kwargs: events.append(("wait", "white")) or True
        )
        runner._wait_honor_right_state_by_clicking = (
            lambda _regions, click_key, *_args, **_kwargs: (
                events.append(("wait_right", click_key)) or True
            )
        )
        runner._click_honor_chip_until_left_1and1 = (
            lambda *_args, **_kwargs: events.append(("honor_chip",)) or True
        )
        runner._wait_1and1_clear = (
            lambda *_args, **_kwargs: events.append(("normal_1and1",)) or True
        )
        runner.run_gacha_probe_test = (
            lambda **kwargs: events.append(("gacha", kwargs)) or True
        )

        with (
            patch("core.daily_runner.window_manager.find_window", return_value=True),
            patch("core.daily_runner.time.sleep", return_value=None),
        ):
            ok = runner.run_honor_exchange_mode(use_foreground=False)

        self.assertTrue(ok)
        self.assertEqual(
            events,
            [
                ("click", "荣誉兑换.to扭蛋"),
                ("click", "荣誉兑换.打开"),
                ("wait", "white"),
                ("wait_right", "荣誉兑换.其他"),
                ("wait_right", "荣誉兑换.右"),
                ("honor_chip",),
                ("normal_1and1",),
                ("click", "荣誉兑换.关闭"),
                (
                    "gacha",
                    {
                        "times": 99999,
                        "background_mode": True,
                    },
                ),
            ],
        )

    def test_master_cup_mismatch_uses_confirmed_dynamic_clear(self) -> None:
        calls = []
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._emit = lambda *_args, **_kwargs: None
        runner._pickmode_classify_target_cyan_party = (
            lambda *_args, **_kwargs: ("mismatch", set(), [])
        )

        def clear(
            use_foreground,
            stop_event,
            *,
            log_tag,
            close_after,
            **kwargs,
        ):
            calls.append((log_tag, close_after, kwargs))
            return False

        runner._rotation_step2_clear_backpack = clear
        ok = runner.ensure_target_cyan_pick_party_from_bag_warehouse_or_rebuild(
            631,
            False,
            threading.Event(),
            log_tag="大师杯测试",
        )

        self.assertFalse(ok)
        self.assertEqual(calls, [("大师杯测试·清包", False, {})])

    def test_master_cup_restore_67_does_not_follow_purple_or_recover(self) -> None:
        calls = []
        runner = DailyRunner.__new__(DailyRunner)

        def replace(*args, **kwargs):
            calls.append((args, kwargs))
            return True

        runner._master_cup_replace_cyan_from_current_bag = replace

        ok = runner._master_cup_restore_67_after_run(
            object(),
            False,
            log_tag="大师杯完成换回197",
        )

        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0][1]["set_cyan_primary"])
        self.assertFalse(calls[0][1]["set_follow_purple"])
        self.assertFalse(calls[0][1]["recover_target_after_take"])

    def test_master_cup_replace_without_follow_closes_bag(self) -> None:
        recover_calls = []
        close_calls = []

        class FakeDarRunner:
            def open_pickmode_bag_warehouse_from_ready_bag(
                self, *_args, **_kwargs
            ):
                return True

            def take_pickmode_pets_from_open_bag_warehouse(
                self, *_args, **_kwargs
            ):
                return True

        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(
            dar_route_runner=FakeDarRunner(),
            _stop_event=threading.Event(),
        )
        runner._emit = lambda *_args, **_kwargs: None
        runner._master_cup_open_bag_ready = lambda *_args, **_kwargs: True
        runner._master_cup_put_back_cyan_from_open_bag = (
            lambda *_args, **_kwargs: True
        )
        runner._master_cup_close_warehouse_keep_bag_open = (
            lambda *_args, **_kwargs: True
        )
        runner._master_cup_recover_cyan_follow_from_open_bag = (
            lambda *_args, **kwargs: recover_calls.append(kwargs) or True
        )
        runner._master_cup_close_bag = (
            lambda *_args, **kwargs: close_calls.append(kwargs)
        )

        ok = runner._master_cup_replace_cyan_from_current_bag(
            object(),
            {"pet_id": 197},
            False,
            log_tag="大师杯完成换回197",
            set_cyan_primary=False,
            set_follow_purple=False,
        )

        self.assertTrue(ok)
        self.assertEqual(len(recover_calls), 1)
        self.assertFalse(recover_calls[0]["set_follow_purple"])
        self.assertEqual(len(close_calls), 1)
        self.assertIn("恢复后关闭背包", close_calls[0]["log_tag"])

    def test_replace_cyan_without_follow_closes_bag(self) -> None:
        recover_calls = []
        close_calls = []
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._pickmode_open_bag_ready_for_target = (
            lambda *_args, **_kwargs: True
        )
        runner.put_back_cyan_slots_from_open_bag = (
            lambda *_args, **_kwargs: True
        )
        runner._pickmode_open_warehouse_from_ready_bag = (
            lambda *_args, **_kwargs: True
        )
        runner.take_pickmode_pets_from_open_bag_warehouse = (
            lambda *_args, **_kwargs: True
        )
        runner._click_pet_warehouse_close = lambda *_args, **_kwargs: None
        runner._sleep_abortable = lambda *_args, **_kwargs: None
        runner.recover_cyan_and_follow_purple_from_open_bag = (
            lambda *_args, **kwargs: recover_calls.append(kwargs) or True
        )
        runner._close_pet_bag_with_verify = (
            lambda *_args, **kwargs: close_calls.append(kwargs)
        )

        ok = runner.replace_current_cyan_with_pick_pet_from_closed_bag(
            197,
            False,
            threading.Event(),
            log_tag="岚岚完成换回197",
            set_follow_purple=False,
        )

        self.assertTrue(ok)
        self.assertEqual(len(recover_calls), 1)
        self.assertFalse(recover_calls[0]["set_follow_purple"])
        self.assertEqual(len(close_calls), 1)
        self.assertIn("恢复后关闭背包", close_calls[0]["log_tag"])

    def test_one_time_mode_cyan_replace_skips_recovery_and_closes_bag(self) -> None:
        close_calls = []
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._emit = lambda *_args, **_kwargs: None
        runner._pickmode_open_bag_ready_for_target = (
            lambda *_args, **_kwargs: True
        )
        runner.put_back_cyan_slots_from_open_bag = (
            lambda *_args, **_kwargs: True
        )
        runner._pickmode_open_warehouse_from_ready_bag = (
            lambda *_args, **_kwargs: True
        )
        runner.take_pickmode_pets_from_open_bag_warehouse = (
            lambda *_args, **_kwargs: True
        )
        runner._click_pet_warehouse_close = lambda *_args, **_kwargs: None
        runner._sleep_abortable = lambda *_args, **_kwargs: None
        runner.recover_cyan_and_follow_purple_from_open_bag = (
            lambda *_args, **_kwargs: self.fail(
                "一次性模式完成后取回197不应再恢复"
            )
        )
        runner._close_pet_bag_with_verify = (
            lambda *_args, **kwargs: close_calls.append(kwargs)
        )

        ok = runner.replace_current_cyan_with_pick_pet_from_closed_bag(
            197,
            False,
            threading.Event(),
            log_tag="岚岚完成换回197",
            set_follow_purple=False,
            recover_target_after_take=False,
        )

        self.assertTrue(ok)
        self.assertEqual(len(close_calls), 1)
        self.assertIn("取宠后关闭背包", close_calls[0]["log_tag"])

    def test_yilu_final_release_does_not_recover_pets(self) -> None:
        clicks = []
        records = []
        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._emit = lambda *_args, **_kwargs: None
        runner._should_abort = lambda: False
        runner._request_outer_mode_restart = lambda *_args, **_kwargs: False
        runner._click_region_safe = (
            lambda _regions, key, *_args, **_kwargs: clicks.append(key) or True
        )
        runner._run_yilu_release_selected_light_pet_from_open_bag = (
            lambda *_args, **_kwargs: True
        )
        runner._recover_daily_pick_color_slots = (
            lambda *_args, **_kwargs: self.fail(
                "依卢一次性完成后不应恢复精灵"
            )
        )
        runner._append_yilu_daily_record = (
            lambda **kwargs: records.append(kwargs) or True
        )

        with (
            patch(
                "core.daily_runner.wait_pet_bag_ui_ready_after_open",
                return_value=True,
            ),
            patch("core.daily_runner.time.sleep"),
        ):
            ok = runner._run_yilu_release_from_closed_bag(
                object(),
                False,
                log_tag="依卢战后",
                append_record=True,
            )

        self.assertTrue(ok)
        self.assertEqual(
            clicks,
            ["精灵背包.打开精灵背包", "精灵背包.打开精灵背包"],
        )
        self.assertEqual(len(records), 1)

    def test_pick_six_pets_stops_on_first_category_failure(self) -> None:
        categories = []
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._emit = lambda *_args, **_kwargs: None

        def place(category, *_args, **_kwargs):
            categories.append(category)
            return False

        runner._rotation_place_pets_same_category_by_reverse = place
        runner._pickmode_place_jita_dual_mechanical = (
            lambda *_args, **_kwargs: self.fail("机塔不应在分类失败后继续取")
        )

        ok = runner._pickmode_rotation_step3_place_pets_from_open_warehouse(
            False,
            threading.Event(),
            log_tag="轮换测试",
        )

        self.assertFalse(ok)
        self.assertEqual(categories, ["飞行系"])

    def test_rotation_clear_and_pick_propagates_pick_failure(self) -> None:
        emitted = []
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._emit = lambda text, level="INFO": emitted.append((level, text))
        runner._rotation_step2_clear_backpack = (
            lambda *_args, **_kwargs: True
        )
        runner._rotation_step3_place_pets = lambda *_args, **_kwargs: False

        ok = runner._rotation_clear_backpack_and_pick_or_skip(
            "nieo",
            False,
            threading.Event(),
            log_tag="轮换测试",
        )

        self.assertFalse(ok)
        self.assertFalse(any("清包取宠完成" in text for _level, text in emitted))

    def test_pick_companion_stops_when_bag_is_not_ready(self) -> None:
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._emit = lambda *_args, **_kwargs: None
        runner._click_region = lambda *_args, **_kwargs: None
        runner._wait_pet_bag_ui_ready_after_open = (
            lambda *_args, **_kwargs: False
        )
        runner._recover_pick_party_after_bag_ready = (
            lambda *_args, **_kwargs: self.fail("背包未就绪时不应恢复或跟随")
        )

        ok = runner._pickmode_rotation_step4_set_companion(
            False,
            threading.Event(),
        )

        self.assertFalse(ok)

    def test_strict_pick_bag_identification_propagates_scan_failure(self) -> None:
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._emit = lambda *_args, **_kwargs: None
        runner._scan_pick_bag_party_slots_1_to_6 = (
            lambda *_args, **_kwargs: {"ok": False, "data": {}}
        )
        runner._recover_unique_bag_slots = (
            lambda *_args, **_kwargs: self.fail(
                "严格识别失败后不应继续恢复并伪报成功"
            )
        )

        ok = runner._recover_and_identify_pickmode_bag_slots(
            False,
            threading.Event(),
            "严格识别测试",
        )

        self.assertFalse(ok)

    def test_pick_companion_cyan_success_uses_scanned_cyan_slot(self) -> None:
        emitted = []
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._emit = lambda text, level="INFO": emitted.append((level, text))
        runner._click_region = lambda *_args, **_kwargs: None
        runner._wait_pet_bag_ui_ready_after_open = (
            lambda *_args, **_kwargs: True
        )

        def recover(*_args, **_kwargs):
            runner._aisifeige_pos = "三"
            return True

        runner._recover_pick_party_after_bag_ready = recover

        ok = runner._pickmode_rotation_step4_set_companion(
            False,
            threading.Event(),
            follow_cyan=True,
        )

        self.assertTrue(ok)
        self.assertIn(
            ("SUCCESS", "[Pick-步骤4] 青色跟随槽位：精灵三"),
            emitted,
        )

    def test_pick_companion_success_is_not_overridden_by_missing_log_cache(self) -> None:
        emitted = []
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._emit = lambda text, level="INFO": emitted.append((level, text))
        runner._click_region = lambda *_args, **_kwargs: None
        runner._wait_pet_bag_ui_ready_after_open = (
            lambda *_args, **_kwargs: True
        )
        runner._recover_pick_party_after_bag_ready = (
            lambda *_args, **_kwargs: True
        )

        ok = runner._pickmode_rotation_step4_set_companion(
            False,
            threading.Event(),
            follow_cyan=True,
        )

        self.assertTrue(ok)
        self.assertIn(
            ("WARN", "[Pick-步骤4] 青色跟随动作已完成，但槽位缓存不可用"),
            emitted,
        )

    def test_daily_recovery_stops_when_bag_is_not_ready(self) -> None:
        clicked = []
        runner = DailyRunner.__new__(DailyRunner)
        runner._unified_framework = object()
        runner._emit = lambda *_args, **_kwargs: None
        runner._should_abort = lambda: False
        runner._click_region_safe = (
            lambda _regions, key, _use_foreground: clicked.append(key) or True
        )

        with patch(
            "core.daily_runner.wait_pet_bag_ui_ready_after_open",
            return_value=False,
        ):
            ok = runner._recover_pet_one(object(), False)

        self.assertFalse(ok)
        self.assertEqual(clicked, ["精灵背包.打开精灵背包"])

    def test_daily_recovery_requests_outer_restart_when_guarded(self) -> None:
        clicked = []
        restart_reasons = []
        stop_event = threading.Event()

        class FakeDarRunner:
            def _request_mode_restart(self, event, reason):
                restart_reasons.append(reason)
                event.set()
                return True

        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(
            stop_current=False,
            _stop_event=stop_event,
            dar_route_runner=FakeDarRunner(),
        )
        runner._outer_mode_restart_enabled = True
        runner._unified_framework = object()
        runner._emit = lambda *_args, **_kwargs: None
        runner._click_region_safe = (
            lambda _regions, key, _use_foreground: clicked.append(key) or True
        )

        with patch(
            "core.daily_runner.wait_pet_bag_ui_ready_after_open",
            return_value=False,
        ):
            ok = runner._recover_pet_one(object(), False, log_tag="1v1x2恢复")

        self.assertFalse(ok)
        self.assertTrue(stop_event.is_set())
        self.assertEqual(restart_reasons, ["1v1x2恢复-背包UI未就绪"])
        self.assertEqual(clicked, ["精灵背包.打开精灵背包"])

    def test_event_pet_bag_failure_requests_mode_restart(self) -> None:
        reasons = []
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner._click_region = lambda *_args, **_kwargs: None
        runner._wait_pet_bag_ui_ready_after_open = (
            lambda *_args, **_kwargs: False
        )
        runner._recover_and_identify_pickmode_bag_slots = (
            lambda *_args, **_kwargs: self.fail("背包未就绪时不应继续识别")
        )
        runner._request_mode_restart = (
            lambda _event, reason: reasons.append(reason) or True
        )
        runner._emit = lambda *_args, **_kwargs: None

        ok = runner._event_pet_post_start_bag_scan(
            False,
            threading.Event(),
            "活动精灵测试",
        )

        self.assertFalse(ok)
        self.assertEqual(reasons, ["活动精灵测试-背包UI未就绪"])

    def test_periodic_recovery_bag_failure_requests_mode_restart(self) -> None:
        reasons = []
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner._click_region = lambda *_args, **_kwargs: None
        runner._wait_pet_bag_ui_ready_after_open = (
            lambda *_args, **_kwargs: False
        )
        runner._request_mode_restart = (
            lambda _event, reason: reasons.append(reason) or True
        )
        runner._emit = lambda *_args, **_kwargs: None

        ok = runner._pem_recover_pet_one_maintenance(
            False,
            threading.Event(),
        )

        self.assertFalse(ok)
        self.assertEqual(reasons, ["纯净能量定期恢复-背包UI未就绪"])

    def test_cyan_already_in_slot_one_skips_redundant_primary_action(self) -> None:
        logs = []
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner.regions = {}
        runner._emit = lambda message, level="INFO": logs.append((message, level))
        runner.scan_pick_bag_party_color_slots_any = (
            lambda *_args, **_kwargs: {
                "ok": True,
                "cyan": "一",
                "purple": None,
            }
        )
        runner._click_pet_with_selection_check = (
            lambda *_args, **_kwargs: self.fail("197已在精灵一时不应重新选中")
        )
        runner._click_region = (
            lambda *_args, **_kwargs: self.fail("197已在精灵一时不应点击设为首发")
        )
        runner._reset_skill_recovery_counters = lambda *_args, **_kwargs: None

        ok = runner.recover_cyan_and_follow_purple_from_open_bag(
            False,
            threading.Event(),
            "光螳螂前置测试",
            set_cyan_primary=True,
            recover_pet_one=False,
            recover_cyan=False,
            set_follow_purple=False,
        )

        self.assertTrue(ok)
        self.assertTrue(
            any("青色已在精灵一，跳过重复设置首发" in message for message, _ in logs)
        )

    def test_color_recovery_deduplicates_pet_one_and_cyan_slot_one(self) -> None:
        recovered = []
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner.regions = {}
        runner._emit = lambda *_args, **_kwargs: None
        runner.scan_pick_bag_party_color_slots_any = (
            lambda *_args, **_kwargs: {
                "ok": True,
                "cyan": "一",
                "purple": None,
            }
        )
        runner._recover_bag_pet_slot_once = (
            lambda slot, *_args, **_kwargs: recovered.append(slot)
        )
        runner._reset_skill_recovery_counters = lambda *_args, **_kwargs: None

        ok = runner.recover_cyan_and_follow_purple_from_open_bag(
            False,
            threading.Event(),
            "颜色恢复去重测试",
            set_cyan_primary=False,
            recover_pet_one=True,
            recover_cyan=True,
            set_follow_purple=False,
        )

        self.assertTrue(ok)
        self.assertEqual(recovered, ["一"])

    def test_nieo_follow_cyan_selects_cyan_slot_instead_of_purple(self) -> None:
        followed = []
        clicks = []
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._nieo_follow_cyan = True
        runner._emit = lambda *_args, **_kwargs: None
        runner._scan_pick_bag_party_slots_1_to_6 = (
            lambda *_args, **_kwargs: {
                "ok": True,
                "cyan": "五",
                "purple": "四",
            }
        )
        runner._recover_unique_bag_slots = lambda *_args, **_kwargs: None
        runner._click_pet_with_selection_check = (
            lambda slot, *_args, **_kwargs: followed.append(slot) or True
        )
        runner._sleep_abortable = lambda *_args, **_kwargs: None
        runner._click_region = (
            lambda key, *_args, **_kwargs: clicks.append(key)
        )
        runner._reset_skill_recovery_counters = lambda *_args, **_kwargs: None

        ok = runner._recover_pick_party_after_bag_ready(
            False,
            threading.Event(),
            "尼奥青色跟随测试",
            include_primary=False,
            set_follow_primary=True,
        )

        self.assertTrue(ok)
        self.assertEqual(followed, ["五"])
        self.assertEqual(clicks, ["精灵背包.身边跟随"])

    def test_follow_does_not_click_button_when_slot_selection_fails(self) -> None:
        clicks = []
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._emit = lambda *_args, **_kwargs: None
        runner._scan_pick_bag_party_slots_1_to_6 = (
            lambda *_args, **_kwargs: {
                "ok": True,
                "cyan": "二",
                "purple": "五",
            }
        )
        runner._recover_unique_bag_slots = lambda *_args, **_kwargs: None
        runner._click_pet_with_selection_check = (
            lambda *_args, **_kwargs: False
        )
        runner._click_region = (
            lambda key, *_args, **_kwargs: clicks.append(key)
        )

        ok = runner._recover_pick_party_after_bag_ready(
            False,
            threading.Event(),
            "跟随选中失败测试",
            include_primary=False,
            set_follow_primary=True,
            follow_primary_color="cyan",
        )

        self.assertFalse(ok)
        self.assertEqual(clicks, [])

    def test_nieo_family_reconnect_inherits_direct_or_rotation_cyan_choice(self) -> None:
        forwarded = []
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner._emit = lambda *_args, **_kwargs: None
        runner._rotation_step4_set_companion = (
            lambda *_args, **kwargs: forwarded.append(kwargs["follow_cyan"]) or True
        )

        for direct, rotation, expected in (
            (True, False, True),
            (False, True, True),
            (False, False, False),
        ):
            runner._nieo_follow_cyan = direct
            runner._rotation_nieo_follow_cyan = rotation
            self.assertTrue(
                runner._set_nieo_family_reconnect_companion(
                    False,
                    threading.Event(),
                )
            )
            self.assertEqual(forwarded[-1], expected)

        runner._nieo_follow_cyan = True
        runner._rotation_nieo_follow_cyan = False
        runner._nieo_defer_cyan_until_a_active = True
        self.assertTrue(
            runner._set_nieo_family_reconnect_companion(
                False,
                threading.Event(),
            )
        )
        self.assertFalse(forwarded[-1])

    def test_deferred_cyan_follow_only_opens_bag_when_a_recovery_was_skipped(
        self,
    ) -> None:
        calls = []
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner._emit = lambda *_args, **_kwargs: None
        runner._follow_cyan_from_closed_bag = (
            lambda *_args, **_kwargs: calls.append("cyan") or True
        )

        self.assertTrue(
            runner._complete_deferred_cyan_follow_on_a(
                True,
                False,
                False,
                threading.Event(),
                log_tag="A图",
            )
        )
        self.assertEqual(calls, [])

        self.assertTrue(
            runner._complete_deferred_cyan_follow_on_a(
                True,
                True,
                False,
                threading.Event(),
                log_tag="A图",
            )
        )
        self.assertEqual(calls, ["cyan"])

    def test_closed_bag_recovery_deduplicates_fixed_and_color_slots(self) -> None:
        recovered = []
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._emit = lambda *_args, **_kwargs: None
        runner._click_region = lambda *_args, **_kwargs: None
        runner._ensure_pet_bag_ui_ready_after_open = lambda *_args, **_kwargs: True
        runner._scan_pick_bag_party_slots_1_to_6 = (
            lambda *_args, **_kwargs: {
                "ok": True,
                "cyan": "一",
                "purple": "五",
            }
        )
        runner._recover_bag_pet_slot_once = (
            lambda slot, *_args, **_kwargs: recovered.append(slot)
        )
        runner._close_pet_bag_with_verify = lambda *_args, **_kwargs: None
        runner._reset_skill_recovery_counters = lambda *_args, **_kwargs: None

        ok = runner.recover_pick_party_color_slots_from_closed_bag(
            False,
            threading.Event(),
            "关闭背包恢复去重测试",
            recover_cyan=True,
            recover_purple=False,
            set_follow_purple=False,
        )

        self.assertTrue(ok)
        self.assertEqual(recovered, ["一"])

    def test_closed_bag_follow_does_not_click_bag_button_after_follow(self) -> None:
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._emit = lambda *_args, **_kwargs: None
        runner._click_region = lambda *_args, **_kwargs: None
        runner._ensure_pet_bag_ui_ready_after_open = lambda *_args, **_kwargs: True
        runner._recover_pick_party_after_bag_ready = (
            lambda *_args, **_kwargs: True
        )
        runner._close_pet_bag_with_verify = (
            lambda *_args, **_kwargs: self.fail(
                "身边跟随自动关包后不应再次点击背包按钮"
            )
        )

        ok = runner._recover_pick_party_from_closed_bag(
            False,
            threading.Event(),
            "晶化气泡技能恢复",
            include_primary=False,
            set_follow_primary=True,
        )

        self.assertTrue(ok)

    def test_opening_recovery_follow_does_not_click_bag_button_again(self) -> None:
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._pickmode_base_recovery_done = False
        runner._emit = lambda *_args, **_kwargs: None
        runner._click_region = lambda *_args, **_kwargs: None
        runner._ensure_pet_bag_ui_ready_after_open = lambda *_args, **_kwargs: True
        runner._recover_pick_party_after_bag_ready = (
            lambda *_args, **_kwargs: True
        )
        runner._sleep_abortable = lambda *_args, **_kwargs: None
        runner._close_pet_bag_with_verify = (
            lambda *_args, **_kwargs: self.fail(
                "战前跟随自动关包后不应再次点击背包按钮"
            )
        )

        ok = runner._recover_pets(
            False,
            threading.Event(),
            skip_return_storage=True,
        )

        self.assertTrue(ok)

    def test_opening_recovery_follow_failure_requests_restart_without_success_log(self) -> None:
        emitted = []
        restart_reasons = []
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._pickmode_base_recovery_done = False
        runner._emit = lambda text, level="INFO": emitted.append((level, text))
        runner._click_region = lambda *_args, **_kwargs: None
        runner._ensure_pet_bag_ui_ready_after_open = (
            lambda *_args, **_kwargs: True
        )
        runner._recover_pick_party_after_bag_ready = (
            lambda *_args, **_kwargs: False
        )
        runner._request_reconnect_after_recovery_failure = (
            lambda _profile, _foreground, _event, reason: restart_reasons.append(reason)
        )

        ok = runner._recover_pets(
            False,
            threading.Event(),
            skip_return_storage=True,
        )

        self.assertFalse(ok)
        self.assertEqual(
            restart_reasons,
            ["first-recover恢复/跟随未完成"],
        )
        self.assertFalse(
            any(
                level == "SUCCESS" and "战前恢复流程完成" in text
                for level, text in emitted
            )
        )

    def test_light_mantis_rotation_precheck_uses_single_party_setup(self) -> None:
        calls = []
        party_kwargs = []
        worker = BotWorker.__new__(BotWorker)
        worker._stop_event = threading.Event()
        worker.emit_and_log = lambda *_args, **_kwargs: None
        worker._prepare_hourly_daily_swf = lambda *_args, **_kwargs: True
        worker._clear_game_tmp_cache = lambda **_kwargs: None

        class FakeDarRunner:
            def run_refresh_login_until_map(self, *_args, **_kwargs):
                calls.append("refresh")
                return True

            def ensure_target_cyan_pick_party_from_bag_warehouse_or_rebuild(
                self, *_args, **kwargs
            ):
                calls.append("party_setup")
                party_kwargs.append(kwargs)
                return True

            def recover_pick_party_color_slots_from_closed_bag(
                self, *_args, **_kwargs
            ):
                self.fail("光螳螂目标组前置后不应再执行第二层恢复")

        fake_dar = FakeDarRunner()
        fake_dar.fail = self.fail
        worker.dar_route_runner = fake_dar
        worker.daily_runner = SimpleNamespace(
            run_light_mantis_mode=lambda **_kwargs: calls.append("run") or True
        )

        ok = worker._run_light_mantis_for_rotation_precheck(False)

        self.assertTrue(ok)
        self.assertEqual(calls, ["refresh", "party_setup", "run"])
        self.assertTrue(party_kwargs[0]["skip_cyan_recovery_when_primary"])

    def test_weekly_mantis_precheck_runs_when_weekly_record_is_missing(self) -> None:
        calls = []
        worker = BotWorker.__new__(BotWorker)
        worker._stop_event = threading.Event()
        worker.stop_current = False
        worker.user_stop_requested = False
        worker.emit_and_log = lambda *_args, **_kwargs: None
        worker.daily_runner = SimpleNamespace(
            _beijing_now=lambda: object(),
            has_light_mantis_weekly_record=lambda _now: False,
        )
        worker._run_light_mantis_for_rotation_precheck = (
            lambda use_foreground, **kwargs: (
                calls.append((use_foreground, kwargs)) or True
            )
        )

        ok = worker._run_light_mantis_before_weekly_if_due(True)

        self.assertTrue(ok)
        self.assertEqual(
            calls,
            [(True, {"log_context": "一键周常前置"})],
        )

    def test_weekly_mantis_precheck_skips_when_weekly_record_exists(self) -> None:
        worker = BotWorker.__new__(BotWorker)
        worker._stop_event = threading.Event()
        worker.stop_current = False
        worker.user_stop_requested = False
        worker.emit_and_log = lambda *_args, **_kwargs: None
        worker.daily_runner = SimpleNamespace(
            _beijing_now=lambda: object(),
            has_light_mantis_weekly_record=lambda _now: True,
        )
        worker._run_light_mantis_for_rotation_precheck = (
            lambda *_args, **_kwargs: self.fail(
                "本周已有完成记录时不应再次执行光螳螂"
            )
        )

        self.assertTrue(worker._run_light_mantis_before_weekly_if_due(False))

    def test_weekly_manual_stop_after_mantis_does_not_run_or_restart_later_steps(self) -> None:
        calls = []
        worker = BotWorker.__new__(BotWorker)
        worker._stop_event = threading.Event()
        worker.stop_current = False
        worker.user_stop_requested = False
        worker.emit_and_log = lambda *_args, **_kwargs: None

        def stop_during_mantis(_use_foreground):
            calls.append("mantis")
            worker.user_stop_requested = True
            worker.stop_current = True
            worker._stop_event.set()
            return False

        worker._run_light_mantis_before_weekly_if_due = stop_during_mantis
        worker._run_master_cup_before_weekly_if_due = (
            lambda *_args, **_kwargs: self.fail("停止后不应执行大师杯")
        )
        worker._prepare_one_click_release_swf_or_stop = (
            lambda: self.fail("停止后不应执行周常SWF前置")
        )
        worker.dar_route_runner = SimpleNamespace(
            run_refresh_login_until_map=lambda *_args, **_kwargs: self.fail(
                "停止后不应刷新重连"
            )
        )
        worker.daily_runner = SimpleNamespace(
            run_chip_gold_honor_mode=lambda **_kwargs: self.fail(
                "停止后不应执行周常主体"
            )
        )

        ok = worker._run_one_click_weekly_task({}, False)

        self.assertFalse(ok)
        self.assertEqual(calls, ["mantis"])
        self.assertTrue(worker.user_stop_requested)
        self.assertTrue(worker.stop_current)
        self.assertTrue(worker._stop_event.is_set())

    def test_weekly_master_cup_precheck_skips_existing_record(self) -> None:
        logs = []
        worker = BotWorker.__new__(BotWorker)
        worker._stop_event = threading.Event()
        worker.stop_current = False
        worker.user_stop_requested = False
        worker.emit_and_log = lambda message, level="INFO": logs.append(
            (message, level)
        )
        worker.daily_runner = SimpleNamespace(
            _beijing_now=lambda: object(),
            get_master_cup_weekly_record=lambda _now: {
                "time": "2026-07-24T09:49:31.225+08:00",
                "cup_type": "电系",
                "norm_ran": "false",
            },
        )
        worker._run_configured_master_cup = (
            lambda *_args, **_kwargs: self.fail(
                "本周已有完成记录时不应再次执行大师杯"
            )
        )

        self.assertTrue(
            worker._run_master_cup_before_weekly_if_due(
                {"master_cup_type": "草系"},
                False,
            )
        )
        self.assertTrue(any("系别=电系" in message for message, _ in logs))
        self.assertTrue(any("诺姆=否" in message for message, _ in logs))

    def test_weekly_master_cup_zero_target_skips_without_side_effects(self) -> None:
        logs = []
        worker = BotWorker.__new__(BotWorker)
        worker.emit_and_log = lambda message, level="INFO": logs.append(
            (message, level)
        )
        worker.daily_runner = SimpleNamespace(
            _beijing_now=lambda: object(),
            get_master_cup_weekly_record=lambda _now: None,
        )
        worker._run_configured_master_cup = (
            lambda *_args, **_kwargs: self.fail(
                "周常大师杯场次为0时不应准备SWF、进入大师杯或写完成记录"
            )
        )

        ok = worker._run_master_cup_before_weekly_if_due(
            {
                "master_cup_type": "机械系",
                "master_cup_yellow_target": 0,
                "master_cup_pre_setup": True,
            },
            False,
        )

        self.assertTrue(ok)
        self.assertTrue(any("场次=0" in message for message, _ in logs))

    def test_configured_master_cup_records_successful_run(self) -> None:
        calls = []
        worker = BotWorker.__new__(BotWorker)
        worker.emit_and_log = lambda *_args, **_kwargs: None
        worker._prepare_master_cup_swf_or_stop = (
            lambda cup_type, norm_mode: calls.append(
                ("prepare", cup_type, norm_mode)
            )
            or True
        )
        worker.daily_runner = SimpleNamespace(
            run_master_cup_mode=lambda **kwargs: calls.append(("run", kwargs))
            or True,
            append_master_cup_weekly_record=lambda **kwargs: calls.append(
                ("record", kwargs)
            )
            or True,
        )
        tasks = {
            "master_cup_type": "诺姆",
            "master_cup_yellow_target": 72,
            "master_cup_pre_setup": True,
            "master_cup_norm_mode": False,
        }

        self.assertTrue(
            worker._run_configured_master_cup(
                tasks,
                False,
                log_context="一键周常前置-大师杯",
            )
        )
        self.assertEqual(calls[0], ("prepare", "诺姆", True))
        self.assertEqual(calls[1][1]["cup_type"], "诺姆")
        self.assertEqual(calls[1][1]["yellow_target_count"], 10)
        self.assertTrue(calls[1][1]["norm_mode"])
        self.assertEqual(calls[2][1]["cup_type"], "诺姆")
        self.assertEqual(calls[2][1]["yellow_target"], 10)
        self.assertTrue(calls[2][1]["norm_ran"])
        self.assertEqual(calls[2][1]["note"], "一键周常前置-大师杯")

    def test_master_cup_norm_is_selected_only_from_dropdown_type(self) -> None:
        worker = BotWorker.__new__(BotWorker)

        mechanical = worker._master_cup_settings_from_tasks(
            {
                "master_cup_type": "机械系",
                "master_cup_yellow_target": 36,
                "master_cup_pre_setup": True,
                "master_cup_norm_mode": True,
            }
        )
        norm = worker._master_cup_settings_from_tasks(
            {
                "master_cup_type": "诺姆",
                "master_cup_yellow_target": 999,
                "master_cup_pre_setup": True,
            }
        )
        weekly_zero = worker._master_cup_settings_from_tasks(
            {
                "master_cup_type": "机械系",
                "master_cup_yellow_target": 0,
                "master_cup_pre_setup": True,
            },
            allow_zero=True,
        )
        direct_zero = worker._master_cup_settings_from_tasks(
            {
                "master_cup_type": "机械系",
                "master_cup_yellow_target": 0,
                "master_cup_pre_setup": True,
            }
        )

        self.assertEqual(mechanical, ("机械系", 36, True, False))
        self.assertEqual(norm, ("诺姆", 10, True, True))
        self.assertEqual(weekly_zero, ("机械系", 0, True, False))
        self.assertEqual(direct_zero, ("机械系", 36, True, False))


if __name__ == "__main__":
    unittest.main()
