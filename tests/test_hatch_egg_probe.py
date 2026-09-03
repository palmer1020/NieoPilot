import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.daily_runner import DailyRunner, HATCH_CLOSE_KEY


class HatchEggProbeTests(unittest.TestCase):
    def test_accepts_observed_light_yellow_and_light_white_samples(self):
        self.assertTrue(DailyRunner._hatch_rgb_is_light_white((255, 255, 153)))
        self.assertTrue(DailyRunner._hatch_rgb_is_light_white((226, 255, 256)))
        self.assertTrue(DailyRunner._hatch_rgb_is_light_white((180, 215, 215)))

    def test_rejects_colors_below_both_brightness_ranges(self):
        self.assertFalse(DailyRunner._hatch_rgb_is_light_white((219, 255, 153)))
        self.assertFalse(DailyRunner._hatch_rgb_is_light_white((255, 244, 153)))
        self.assertFalse(DailyRunner._hatch_rgb_is_light_white((255, 255, 144)))
        self.assertFalse(DailyRunner._hatch_rgb_is_light_white(None))

    def test_initial_stable_red_probe_closes_and_skips(self):
        bot = SimpleNamespace(
            user_stop_requested=False,
            stop_current=False,
            _stop_event=None,
            emit_and_log=lambda *_args: None,
        )
        runner = DailyRunner(bot)
        with (
            patch(
                "core.daily_runner.mean_rgb_for_region_key",
                side_effect=[(255, 0, 0), (255, 0, 0), (255, 0, 0), (200, 220, 220)],
            ),
            patch.object(runner, "_click_region_safe", return_value=True) as click_mock,
            patch("core.daily_runner.time.sleep"),
        ):
            result = runner._hatch_close_if_already_running({}, False, log_tag="test")

        self.assertTrue(result)
        click_mock.assert_called_once_with({}, HATCH_CLOSE_KEY, False)

    def test_initial_non_red_probe_continues_normal_hatch(self):
        bot = SimpleNamespace(
            user_stop_requested=False,
            stop_current=False,
            _stop_event=None,
            emit_and_log=lambda *_args: None,
        )
        runner = DailyRunner(bot)
        with (
            patch(
                "core.daily_runner.mean_rgb_for_region_key",
                side_effect=[(78, 78, 78), (200, 220, 220)],
            ),
            patch.object(runner, "_click_region_safe") as click_mock,
        ):
            result = runner._hatch_close_if_already_running({}, False, log_tag="test")

        self.assertIsNone(result)
        click_mock.assert_not_called()

    def test_post_confirm_red_probe_closes_and_skips_remaining_flow(self):
        bot = SimpleNamespace(
            user_stop_requested=False,
            stop_current=False,
            _stop_event=None,
            emit_and_log=lambda *_args: None,
        )
        runner = DailyRunner(bot)
        with (
            patch("core.daily_runner.mean_rgb_for_region_key", return_value=(254, 0, 2)),
            patch.object(runner, "_click_region_safe", return_value=True) as click_mock,
            patch.object(runner, "_wait_1and1_clear") as wait_mock,
            patch.object(runner, "_append_hatch_xls_record", return_value=True) as record_mock,
        ):
            result = runner._hatch_claim_end_if_ready({}, False, log_tag="test")

        self.assertEqual(result, (True, True))
        click_mock.assert_called_once_with({}, HATCH_CLOSE_KEY, False)
        wait_mock.assert_not_called()
        record_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
