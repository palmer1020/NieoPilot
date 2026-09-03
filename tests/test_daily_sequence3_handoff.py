import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.daily_runner import DailyRunner


class DailySequence3HandoffTests(unittest.TestCase):
    def test_follow_after_orange_does_not_reselect_pet_one(self):
        events = []

        class FakeDarRunner:
            def _click_pet_with_selection_check(self, *_args, **_kwargs):
                raise AssertionError("精灵一橙色就绪后不应重复执行选中检查")

        runner = DailyRunner(
            SimpleNamespace(
                dar_route_runner=FakeDarRunner(),
                stop_current=False,
            )
        )
        runner._emit = lambda *_args, **_kwargs: None
        runner._click_region_btn_fallback = (
            lambda _regions, key, _use_foreground: events.append(("open", key))
            or True
        )
        runner._click_region_safe = (
            lambda _regions, key, _use_foreground: events.append(("click", key))
            or True
        )
        runner._should_abort = lambda: False

        with patch(
            "core.daily_runner.wait_pet_bag_ui_ready_after_open",
            return_value=True,
        ), patch("core.daily_runner.time.sleep", return_value=None):
            ok = runner._new_daily_bag_follow_after_orange(
                SimpleNamespace(get=lambda _key: (0, 0, 1, 1)),
                False,
                log_tag="test",
            )

        self.assertTrue(ok)
        self.assertEqual(
            events,
            [
                ("open", "精灵背包.打开精灵背包"),
                ("click", "精灵背包.身边跟随"),
            ],
        )

    def test_step7_puts_back_gorilla_then_follows_shifted_jita(self):
        events = []
        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(
            regions=SimpleNamespace(get=lambda _key: (0, 0, 1, 1))
        )
        runner.script_dir = "."
        runner._emit = lambda message, level="INFO": events.append(
            ("log", message, level)
        )
        runner._new_daily_gap_before_step = lambda *_args: True
        runner._new_daily_bag_follow_after_orange = (
            lambda *_args, **_kwargs: self.fail(
                "鼓励后不能跳过放回猩猩直接跟随"
            )
        )
        runner._new_daily_bag_return_and_follow = (
            lambda _regions, use_foreground, *, log_tag, verify_pet_pos, expected_pet_count, expected_pet_count_wait_timeout_s: events.append(
                (
                    "putback_then_follow",
                    use_foreground,
                    log_tag,
                    verify_pet_pos,
                    expected_pet_count,
                    expected_pet_count_wait_timeout_s,
                )
            )
            or True
        )

        with patch("core.daily_runner.window_manager.find_window", return_value=True), patch(
            "core.daily_runner.os.path.isfile",
            return_value=True,
        ):
            ok = runner.run_new_daily_sequence_3(
                use_foreground=False,
                start_step=7,
            )

        self.assertTrue(ok)
        self.assertIn(
            (
                "putback_then_follow",
                False,
                "新日常·3·放回猩猩跟随机塔",
                "三",
                3,
                30.0,
            ),
            events,
        )
        self.assertTrue(
            any(
                event[0] == "log"
                and "放回精灵一猩猩" in event[1]
                for event in events
            )
        )


if __name__ == "__main__":
    unittest.main()
