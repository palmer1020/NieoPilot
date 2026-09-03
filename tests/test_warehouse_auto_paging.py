import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.daily_runner import DailyRunner


class WarehouseAutoPagingTests(unittest.TestCase):
    def _runner(self) -> DailyRunner:
        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(
            dar_route_runner=SimpleNamespace(
                _warehouse_right_rgb_is_end=lambda rgb: rgb == (119, 192, 235)
            )
        )
        runner._emit = lambda *_args, **_kwargs: None
        runner._should_abort = lambda: False
        return runner

    def test_nav_state_reuses_latest_end_and_gray_available_colors(self) -> None:
        runner = self._runner()
        with patch(
            "core.daily_runner.mean_rgb_for_region_key",
            side_effect=[(119, 192, 235), (183, 183, 183), (12, 34, 56)],
        ):
            self.assertEqual(
                runner._warehouse_nav_state(object(), "精灵仓库.右")[0],
                "end",
            )
            self.assertEqual(
                runner._warehouse_nav_state(object(), "精灵仓库.右")[0],
                "available",
            )
            self.assertEqual(
                runner._warehouse_nav_state(object(), "精灵仓库.右")[0],
                "unknown",
            )

    def test_right_clicks_continue_until_end_color(self) -> None:
        runner = self._runner()
        clicks = []
        runner._click_region_safe = (
            lambda _regions, key, use_foreground: clicks.append(
                (key, use_foreground)
            )
            or True
        )
        with (
            patch(
                "core.daily_runner.mean_rgb_for_region_key",
                side_effect=[
                    (223, 223, 223),
                    (183, 183, 183),
                    (119, 192, 235),
                ],
            ),
            patch("core.daily_runner.time.sleep", return_value=None),
        ):
            ok = runner._warehouse_click_right_until_end(
                object(),
                False,
                log_tag="测试",
            )

        self.assertTrue(ok)
        self.assertEqual(
            clicks,
            [
                ("精灵仓库.右", False),
                ("精灵仓库.右", False),
            ],
        )

    def test_fusion_marks_category_exhausted_at_left_end(self) -> None:
        runner = self._runner()
        runner._fusion_nav_warehouse_state = (
            lambda *_args, **_kwargs: (True, "精灵仓库.超能系", 1)
        )
        runner._warehouse_nav_state = (
            lambda _regions, key: ("end", f"{key}->到头")
        )
        state = {
            "category_key": "精灵仓库.超能系",
            "color": "purple",
            "exhausted": False,
        }
        with patch(
            "core.daily_runner.mean_rgb_for_region_key",
            return_value=(255, 255, 255),
        ):
            ok, picked, _category, _page, exhausted = (
                runner._fusion_pick_warehouse_color_progress(
                    object(),
                    False,
                    state,
                    wanted_count=3,
                    current_category=None,
                    current_page=1,
                )
            )

        self.assertTrue(ok)
        self.assertEqual(picked, 0)
        self.assertTrue(exhausted)
        self.assertTrue(state["exhausted"])

    def test_exp_warehouse_only_initializes_all_and_category_once(self) -> None:
        runner = self._runner()
        clicks = []
        all_initializations = []
        runner._click_region_safe = (
            lambda _regions, key, _use_foreground: clicks.append(key) or True
        )
        runner._warehouse_click_all_until_slot9_orange = (
            lambda *_args, **_kwargs: all_initializations.append(True) or True
        )
        runner._psychic_exp_fail_refresh_retry = lambda _reason: False
        regions = SimpleNamespace(get=lambda _key: object())

        with patch("core.daily_runner.time.sleep", return_value=None):
            first_ok = runner._psychic_exp_open_warehouse_psychic(
                regions,
                False,
                log_tag="经验首次",
                category_key="精灵仓库.超能系",
                initialize_warehouse=True,
            )
            reopen_ok = runner._psychic_exp_open_warehouse_psychic(
                regions,
                False,
                log_tag="经验续扫",
                category_key="精灵仓库.超能系",
                initialize_warehouse=False,
            )

        self.assertTrue(first_ok)
        self.assertTrue(reopen_ok)
        self.assertEqual(all_initializations, [True])
        self.assertEqual(
            clicks,
            [
                "精灵仓库.打开",
                "精灵仓库.单属性",
                "精灵仓库.超能系",
                "精灵仓库.打开",
            ],
        )

    def test_exp_forward_scan_rechecks_same_slot_after_pick_and_reopen(self) -> None:
        runner = self._runner()
        runner._psychic_exp_clipboard_set_text = lambda _text: True
        runner._psychic_exp_prepare_refresh_login = lambda *_args, **_kwargs: True
        runner._click_region_safe = lambda *_args, **_kwargs: True
        runner._new_daily_clear_backpack = lambda *_args, **_kwargs: True
        open_modes = []
        runner._psychic_exp_open_warehouse_psychic = (
            lambda *_args, **kwargs: open_modes.append(
                kwargs.get("initialize_warehouse", True)
            )
            or True
        )
        picked_slots = []
        runner._psychic_exp_take_purple_slot = (
            lambda _regions, _use_foreground, slot: picked_slots.append(slot)
            or True
        )
        batch_sizes = []

        def run_batch(*_args, **kwargs):
            batch_sizes.append(kwargs.get("batch_count", 6))
            return True

        runner._psychic_exp_run_batch = run_batch
        runner._warehouse_nav_state = (
            lambda _regions, key: ("end", f"{key}->到头")
        )
        runner._psychic_exp_slot_color = (
            lambda rgb: "purple" if rgb == (120, 80, 180) else "white"
        )
        regions = SimpleNamespace(get=lambda _key: object())
        runner.bot.regions = regions

        slot_colors = [(120, 80, 180)] * 7 + [(255, 255, 255)] * 9
        with (
            patch("core.daily_runner.window_manager.find_window", return_value=True),
            patch(
                "core.daily_runner.mean_rgb_for_region_key",
                side_effect=slot_colors,
            ),
            patch("core.daily_runner.time.sleep", return_value=None),
        ):
            ok = runner.run_psychic_exp_purple_mode(
                use_foreground=False,
                warehouse_category_key="精灵仓库.超能系",
            )

        self.assertTrue(ok)
        self.assertEqual(open_modes, [True, False])
        self.assertEqual(picked_slots, [1] * 7)
        self.assertEqual(batch_sizes, [6, 1])


if __name__ == "__main__":
    unittest.main()
