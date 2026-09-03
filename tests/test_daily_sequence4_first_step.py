import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.daily_runner import DailyRunner
from core.dar_route_runner import DarRouteRunner


class DailySequence4FirstStepTests(unittest.TestCase):
    def test_first_step_runs_bag_setup_before_returning_to_base(self) -> None:
        calls = []
        stop_event = threading.Event()

        class FakeDarRunner:
            def run_refresh_login_until_map(
                self,
                use_foreground,
                event,
                *,
                include_base_and_map_gate,
            ):
                self.assert_event = event
                calls.append(("refresh", include_base_and_map_gate))
                return True

        drr = FakeDarRunner()
        bot = SimpleNamespace(
            dar_route_runner=drr,
            stop_current=False,
        )
        runner = DailyRunner(bot)
        runner._new_daily_stop_event = lambda: stop_event
        runner._new_daily_clear_backpack = (
            lambda use_foreground, *, log_tag, close_after: (
                calls.append(("clear", log_tag, close_after)) or True
            )
        )

        def take_pets(
            use_foreground,
            *,
            category,
            reverse_positions,
            right_clicks,
            log_tag,
            reverse_order,
            include_jita,
            jita_first,
            from_open_bag,
        ):
            calls.append(
                (
                    "pick",
                    category,
                    reverse_positions,
                    include_jita,
                    jita_first,
                    from_open_bag,
                )
            )
            return True

        runner._new_daily_warehouse_take_reverse_positions = take_pets
        runner._new_daily_follow_then_return_pet_one = (
            lambda use_foreground, *, log_tag, bag_already_open: (
                calls.append(("follow_one_return", log_tag, bag_already_open))
                or True
            )
        )
        runner._new_daily_follow_then_return_purple = (
            lambda *_args, **_kwargs: self.fail("方案4第一步不得再依赖紫色跟随")
        )
        runner._new_daily_base_gate_and_confirm = (
            lambda regions, use_foreground, *, log_tag: calls.append(("base", log_tag))
            or True
        )
        runner._click_region_safe = (
            lambda regions, key, use_foreground: calls.append(("base_right", key)) or True
        )

        with patch("core.daily_runner.time.sleep", return_value=None):
            ok = runner._new_daily_sequence4_first_step(
                {},
                False,
                log_tag="test",
            )

        self.assertTrue(ok)
        self.assertEqual(
            [item[0] for item in calls],
            ["refresh", "clear", "pick", "follow_one_return", "base", "base_right"],
        )
        self.assertEqual(calls[0], ("refresh", False))
        self.assertEqual(calls[1][2], False)
        self.assertEqual(
            calls[2],
            ("pick", "普通系", (3, 5, 12), True, True, True),
        )
        self.assertTrue(calls[3][2])

    def test_first_step_from_previous_variant_skips_only_reconnect(self) -> None:
        calls = []
        stop_event = threading.Event()

        class FakeDarRunner:
            def run_refresh_login_until_map(self, *_args, **_kwargs):
                calls.append("refresh")
                return True

            def _close_pet_bag_with_verify(self, *_args, **_kwargs):
                calls.append("close_bag")

        runner = DailyRunner(
            SimpleNamespace(
                dar_route_runner=FakeDarRunner(),
                stop_current=False,
            )
        )
        runner._new_daily_stop_event = lambda: stop_event
        runner._new_daily_clear_backpack = (
            lambda *_args, **_kwargs: calls.append("clear") or True
        )
        runner._new_daily_warehouse_take_reverse_positions = (
            lambda *_args, **_kwargs: calls.append("pick") or True
        )
        runner._new_daily_follow_then_return_pet_one = (
            lambda *_args, **_kwargs: self.fail("顺接不得重新设置机塔跟随")
        )
        runner._new_daily_follow_then_return_purple = (
            lambda *_args, **_kwargs: self.fail("顺接不得扫描紫色")
        )
        runner._new_daily_base_gate_and_confirm = (
            lambda *_args, **_kwargs: calls.append("base") or True
        )
        runner._click_region_safe = (
            lambda *_args, **_kwargs: calls.append("base_right") or True
        )

        with patch("core.daily_runner.time.sleep", return_value=None):
            ok = runner._new_daily_sequence4_first_step(
                {},
                False,
                log_tag="test",
                reconnect_before_setup=False,
                inherit_jita_follow=True,
            )

        self.assertTrue(ok)
        self.assertNotIn("refresh", calls)
        self.assertEqual(
            calls,
            ["clear", "pick", "close_bag", "base", "base_right"],
        )

    def test_sequence4_dispatch_reconnects_only_when_it_is_chain_entry(self) -> None:
        runner = DailyRunner(SimpleNamespace(stop_current=False))
        calls = []
        runner.run_new_daily_sequence_4 = (
            lambda *_args, **kwargs: calls.append(
                (
                    kwargs["reconnect_first_step"],
                    kwargs["inherit_jita_follow"],
                )
            )
            or True
        )

        self.assertTrue(
            runner.run_new_daily_mode(
                False,
                variant="4",
                start_step=1,
                from_daily_chain=True,
                is_chain_entry_variant=True,
            )
        )
        self.assertTrue(
            runner.run_new_daily_mode(
                False,
                variant="4",
                start_step=1,
                from_daily_chain=True,
                is_chain_entry_variant=False,
            )
        )
        self.assertTrue(
            runner.run_new_daily_mode(
                False,
                variant="4",
                start_step=1,
                from_daily_chain=False,
                is_chain_entry_variant=False,
            )
        )

        self.assertEqual(
            calls,
            [(True, False), (False, True), (True, False)],
        )

    def test_cut_in_picks_jita_before_normal_pets(self) -> None:
        calls = []
        stop_event = threading.Event()

        class FakeDarRunner:
            def open_pickmode_bag_warehouse_from_ready_bag(
                self, *_args, **_kwargs
            ):
                calls.append("open")
                return True

            def _pickmode_place_jita_dual_mechanical(
                self, *_args, **_kwargs
            ):
                calls.append("jita")
                return True

            def _click_region(self, key, _use_foreground):
                calls.append(("click", key))

            def _rotation_place_pets_same_category_by_reverse(
                self, *_args, **_kwargs
            ):
                calls.append("normal")
                return True

            def _click_pet_warehouse_close(self, *_args, **_kwargs):
                calls.append("close")

        runner = DailyRunner(
            SimpleNamespace(
                dar_route_runner=FakeDarRunner(),
                regions=SimpleNamespace(get=lambda _key: (0, 0, 1, 1)),
                stop_current=False,
            )
        )
        runner._new_daily_stop_event = lambda: stop_event
        runner._warehouse_click_all_until_tail_color_ready = (
            lambda *_args, **_kwargs: True
        )

        with patch("core.daily_runner.time.sleep", return_value=None):
            ok = runner._new_daily_warehouse_take_reverse_positions(
                False,
                category="普通系",
                reverse_positions=(3, 5, 12),
                right_clicks=15,
                log_tag="test",
                include_jita=True,
                jita_first=True,
                from_open_bag=True,
            )

        self.assertTrue(ok)
        self.assertLess(calls.index("jita"), calls.index("normal"))

    def test_cut_in_follows_and_returns_fixed_pet_one_without_color_scan(self) -> None:
        calls = []
        stop_event = threading.Event()

        class FakeDarRunner:
            _jita_pos = "一"
            _yameisi_pos = "一"

            def _ensure_pet_bag_ui_ready_after_open(
                self, *_args, **_kwargs
            ):
                calls.append("ready")
                return True

            def _click_pet_with_selection_check(
                self, pos_cn, _use_foreground, _event
            ):
                calls.append(("select", pos_cn))
                return True

            def _click_region(self, key, _use_foreground):
                calls.append(("click", key))

            def _sleep_abortable(self, *_args, **_kwargs):
                return None

            def _pickmode_open_bag_ready_for_target(
                self, *_args, **_kwargs
            ):
                calls.append("reopen")
                return True

            def put_back_bag_slot_from_open_bag(
                self,
                pos_cn,
                _use_foreground,
                _event,
                _log_tag,
                **kwargs,
            ):
                calls.append(("put_back", pos_cn, kwargs))
                return True

            def scan_pick_bag_party_color_slots_any(self, *_args, **_kwargs):
                self.fail("固定槽位流程不得扫描颜色")

            def _close_pet_bag_with_verify(self, *_args, **_kwargs):
                calls.append("close")

        drr = FakeDarRunner()
        runner = DailyRunner(
            SimpleNamespace(
                dar_route_runner=drr,
                stop_current=False,
            )
        )
        runner._new_daily_stop_event = lambda: stop_event
        runner._wait_after_follow_before_next_ui = lambda _tag: True

        ok = runner._new_daily_follow_then_return_pet_one(
            False,
            log_tag="test",
            bag_already_open=True,
        )

        self.assertTrue(ok)
        self.assertIn(("select", "一"), calls)
        self.assertIn(("click", "精灵背包.身边跟随"), calls)
        put_back = next(item for item in calls if item[0] == "put_back")
        self.assertEqual(put_back[1], "一")
        self.assertTrue(put_back[2]["verify_slot_deep_blue"])
        self.assertEqual(put_back[2]["deep_blue_verify_pos"], "四")
        self.assertIsNone(drr._jita_pos)

    def test_chain_from_one_marks_only_variant_one_as_chain_entry(self) -> None:
        runner = DailyRunner(SimpleNamespace(stop_current=False))
        calls = []
        runner._should_abort = lambda: False
        runner._new_daily_step_gap = lambda: True

        def run_mode(*_args, **kwargs):
            calls.append(
                (
                    kwargs["variant"],
                    kwargs["is_chain_entry_variant"],
                )
            )
            return True

        runner.run_new_daily_mode = run_mode

        ok = runner.run_new_daily_chain_1_to_9(
            False,
            start_variant="1",
            start_step=1,
        )

        self.assertTrue(ok)
        self.assertIn(("1", True), calls)
        self.assertIn(("4", False), calls)

    def test_follow_then_return_puts_back_only_scanned_purple_slot(self) -> None:
        calls = []
        stop_event = threading.Event()

        class FakeDarRunner:
            _jita_pos = "四"
            _yameisi_pos = "四"

            def set_follow_purple_jita_from_closed_bag(
                self,
                use_foreground,
                event,
                *,
                log_tag,
                bag_already_open,
            ):
                calls.append(("follow", log_tag, bag_already_open))
                return True

            def _pickmode_open_bag_ready_for_target(
                self, use_foreground, event, *, log_tag
            ):
                calls.append(("open", log_tag))
                return True

            def scan_pick_bag_party_color_slots_any(
                self,
                event,
                log_tag,
                *,
                timeout_s,
                min_cyan,
                min_purple,
            ):
                calls.append(("scan", min_cyan, min_purple))
                return {"ok": True, "purple": "四"}

            def put_back_bag_slot_from_open_bag(
                self,
                pos_cn,
                use_foreground,
                event,
                log_tag,
                *,
                verify_hp,
            ):
                calls.append(("put_back", pos_cn, verify_hp))
                return True

            def _close_pet_bag_with_verify(
                self,
                use_foreground,
                event,
                bag_open_key,
                bag_open_btn_key,
                *,
                log_tag,
            ):
                calls.append(("close", log_tag))

        drr = FakeDarRunner()
        runner = DailyRunner(
            SimpleNamespace(
                dar_route_runner=drr,
                stop_current=False,
            )
        )
        runner._new_daily_stop_event = lambda: stop_event

        ok = runner._new_daily_follow_then_return_purple(
            False,
            log_tag="test",
        )

        self.assertTrue(ok)
        self.assertEqual(
            [item[0] for item in calls],
            ["follow", "open", "scan", "put_back", "close"],
        )
        self.assertEqual(calls[2], ("scan", 0, 1))
        self.assertEqual(calls[3], ("put_back", "四", False))
        self.assertIsNone(drr._jita_pos)
        self.assertIsNone(drr._yameisi_pos)

    def test_purple_follow_accepts_partial_party_without_cyan(self) -> None:
        calls = []
        stop_event = threading.Event()
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner._emit = lambda text, level="INFO": None
        runner._click_region = (
            lambda key, use_foreground: calls.append(("click", key))
        )
        runner._ensure_pet_bag_ui_ready_after_open = (
            lambda event, use_foreground, bag_key, bag_btn_key, *, log_tag: True
        )

        def scan_any(
            event,
            log_tag,
            *,
            timeout_s,
            min_cyan,
            min_purple,
        ):
            calls.append(("scan", min_cyan, min_purple))
            return {"ok": True, "purple": "四"}

        runner.scan_pick_bag_party_color_slots_any = scan_any
        runner._click_bag_pet_slot_double = (
            lambda pos_cn, use_foreground: calls.append(("select", pos_cn))
        )
        runner._sleep_abortable = lambda event, seconds: None

        ok = runner.set_follow_purple_jita_from_closed_bag(
            False,
            stop_event,
            log_tag="test",
        )

        self.assertTrue(ok)
        self.assertIn(("scan", 0, 1), calls)
        self.assertIn(("select", "四"), calls)
        self.assertIn(("click", "精灵背包.身边跟随"), calls)

    def test_purple_follow_reuses_open_bag_after_warehouse_close(self) -> None:
        clicked = []
        stop_event = threading.Event()
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner._emit = lambda *_args, **_kwargs: None
        runner._click_region = (
            lambda key, _use_foreground: clicked.append(key)
        )
        runner._ensure_pet_bag_ui_ready_after_open = (
            lambda *_args, **_kwargs: True
        )
        runner.scan_pick_bag_party_color_slots_any = (
            lambda *_args, **_kwargs: {"ok": True, "purple": "四"}
        )
        runner._click_bag_pet_slot_double = lambda *_args, **_kwargs: None
        runner._sleep_abortable = lambda *_args, **_kwargs: None

        ok = runner.set_follow_purple_jita_from_closed_bag(
            False,
            stop_event,
            log_tag="test",
            bag_already_open=True,
        )

        self.assertTrue(ok)
        self.assertNotIn("精灵背包.打开精灵背包按钮", clicked)
        self.assertNotIn("精灵背包.打开精灵背包", clicked)
        self.assertEqual(clicked, ["精灵背包.身边跟随"])

    def test_nonbase_party_rebuild_keeps_bag_open_after_warehouse(self) -> None:
        calls = []
        stop_event = threading.Event()
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._emit = lambda *_args, **_kwargs: None
        runner._pickmode_expected_party_ids = lambda: frozenset({1})
        runner._pickmode_recent_party_ids_satisfied = (
            lambda *_args, **_kwargs: False
        )

        def clear(*_args, **kwargs):
            calls.append(("clear", kwargs.get("close_after")))
            return True

        runner._rotation_step2_clear_backpack = clear
        runner._click_region = (
            lambda key, _use_foreground: calls.append(("click", key))
        )
        runner._sleep_abortable = lambda *_args, **_kwargs: None
        runner._pickmode_rotation_step3_place_pets_from_open_warehouse = (
            lambda *_args, **_kwargs: True
        )
        runner._click_pet_warehouse_close = (
            lambda *_args, **_kwargs: calls.append(("close_warehouse",))
        )
        runner._close_pet_bag_with_verify = (
            lambda *_args, **_kwargs: self.fail("非基地仓库收尾不应在跟随前关闭背包")
        )

        ok = runner.ensure_pick_party_from_bag_warehouse_or_skip(
            False,
            stop_event,
            log_tag="test",
        )

        self.assertTrue(ok)
        self.assertIn(("clear", False), calls)
        self.assertIn(("click", "精灵背包.精灵仓库"), calls)
        self.assertIn(("close_warehouse",), calls)


if __name__ == "__main__":
    unittest.main()
