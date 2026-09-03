import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.daily_runner import DailyRunner
from core.dar_route_runner import DarRouteRunner
from core.utils import (
    BAG_COUNT_SCAN_TIMEOUT_SEC,
    BAG_EMPTY_DEEP_BLUE_CONFIRM_SEC,
    analyze_pet_bag_slot_colors,
    classify_pet_bag_slot_rgb,
    scan_pet_bag_count,
    wait_pet_bag_ui_ready_after_open,
)


class _FakeRegions:
    def get(self, key):
        if key == "精灵背包.清空精灵一" or key in {
            f"精灵背包.{pos}" for pos in range(1, 7)
        }:
            return object()
        return None


class PetBagCountScanTests(unittest.TestCase):
    def test_count_scan_has_finite_default_timeout(self) -> None:
        self.assertEqual(BAG_COUNT_SCAN_TIMEOUT_SEC, 10.0)

    def test_classifies_three_pet_colors_and_deep_blue_empty(self) -> None:
        self.assertEqual(classify_pet_bag_slot_rgb((254, 104, 1)), "orange")
        self.assertEqual(classify_pet_bag_slot_rgb((148, 223, 252)), "cyan")
        self.assertEqual(classify_pet_bag_slot_rgb((186, 238, 253)), "cyan")
        self.assertEqual(classify_pet_bag_slot_rgb((202, 244, 254)), "cyan")
        self.assertEqual(classify_pet_bag_slot_rgb((71, 28, 83)), "purple")
        self.assertEqual(classify_pet_bag_slot_rgb((24, 73, 146)), "deep_blue")

    def test_accepts_only_contiguous_pet_prefix(self) -> None:
        valid = analyze_pet_bag_slot_colors(
            ("orange", "cyan", "purple", "orange", "deep_blue", "deep_blue")
        )
        self.assertTrue(valid["ok"])
        self.assertEqual(valid["count"], 4)

        hole = analyze_pet_bag_slot_colors(
            ("orange", "deep_blue", "cyan", "purple", "deep_blue", "deep_blue")
        )
        self.assertFalse(hole["ok"])
        self.assertIsNone(hole["count"])
        self.assertIn("空槽之后仍有宠", hole["reason"])

    def test_bag_ready_automatically_records_stable_pet_count(self) -> None:
        rgb_by_key = {
            "精灵背包.清空精灵一": (255, 153, 1),
            "精灵背包.1": (254, 104, 1),
            "精灵背包.2": (148, 223, 252),
            "精灵背包.3": (71, 28, 83),
            "精灵背包.4": (254, 104, 1),
            "精灵背包.5": (24, 73, 146),
            "精灵背包.6": (24, 73, 146),
        }
        captured = []

        with patch(
            "core.utils.mean_rgb_for_region_key",
            side_effect=lambda _regions, key: rgb_by_key.get(key),
        ):
            ready = wait_pet_bag_ui_ready_after_open(
                _FakeRegions(),
                timeout_s=0.1,
                poll_s=0.001,
                bag_scan_callback=captured.append,
            )

        self.assertTrue(ready)
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0]["ok"])
        self.assertEqual(captured[0]["count"], 4)

    def test_bag_ready_fails_when_count_scan_times_out(self) -> None:
        captured = []
        unstable = {
            "ok": False,
            "count": None,
            "colors": (None,) * 6,
            "data": {},
            "reason": "槽位状态未稳定",
        }

        with (
            patch(
                "core.utils.mean_rgb_for_region_key",
                return_value=(255, 153, 1),
            ),
            patch("core.utils.scan_pet_bag_count", return_value=unstable),
        ):
            ready = wait_pet_bag_ui_ready_after_open(
                _FakeRegions(),
                timeout_s=0.1,
                poll_s=0.001,
                bag_scan_callback=captured.append,
            )

        self.assertFalse(ready)
        self.assertEqual(captured, [unstable])

    def test_empty_bag_requires_continuous_deep_blue_confirmation(self) -> None:
        captured = []
        emitted = []

        with patch(
            "core.utils.mean_rgb_for_region_key",
            return_value=(24, 73, 146),
        ):
            ready = wait_pet_bag_ui_ready_after_open(
                _FakeRegions(),
                emit_fn=lambda text, level: emitted.append((level, text)),
                timeout_s=0.03,
                poll_s=0.001,
                bag_scan_callback=captured.append,
                allow_empty_bag=True,
                empty_confirm_s=0.01,
            )

        self.assertTrue(ready)
        self.assertEqual(BAG_EMPTY_DEEP_BLUE_CONFIRM_SEC, 10.0)
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0]["ok"])
        self.assertEqual(captured[0]["count"], 0)
        self.assertTrue(
            any("确认背包为0只精灵" in text for _level, text in emitted)
        )

    def test_empty_bag_confirmation_starts_at_first_deep_blue_sample(self) -> None:
        captured = []
        probe_reads = 0

        def read_rgb(_regions, _key):
            nonlocal probe_reads
            probe_reads += 1
            if probe_reads == 1:
                return (194, 168, 129)
            return (24, 73, 146)

        with patch(
            "core.utils.mean_rgb_for_region_key",
            side_effect=read_rgb,
        ):
            ready = wait_pet_bag_ui_ready_after_open(
                _FakeRegions(),
                timeout_s=0.01,
                poll_s=0.001,
                bag_scan_callback=captured.append,
                allow_empty_bag=True,
                empty_confirm_s=0.01,
            )

        self.assertTrue(ready)
        self.assertGreater(probe_reads, 1)
        self.assertEqual(captured[0]["count"], 0)

    def test_deep_blue_is_not_accepted_outside_clear_bag_flow(self) -> None:
        captured = []

        with patch(
            "core.utils.mean_rgb_for_region_key",
            return_value=(24, 73, 146),
        ):
            ready = wait_pet_bag_ui_ready_after_open(
                _FakeRegions(),
                timeout_s=0.01,
                poll_s=0.001,
                bag_scan_callback=captured.append,
                allow_empty_bag=False,
                empty_confirm_s=0.001,
            )

        self.assertFalse(ready)
        self.assertEqual(captured, [])

    def test_count_scan_waits_through_transient_signature_without_warning(self) -> None:
        signatures = (
            (
                (254, 104, 1),
                (148, 223, 252),
                (254, 103, 0),
                (254, 103, 0),
                (71, 28, 83),
                (254, 104, 0),
            ),
            (
                (254, 104, 1),
                (254, 103, 0),
                (148, 223, 252),
                (254, 103, 0),
                (71, 28, 83),
                (254, 104, 0),
            ),
        )
        read_count = 0
        emitted = []

        def read_rgb(_regions, _key):
            nonlocal read_count
            scan_index = min(read_count // 6, len(signatures) - 1)
            slot_index = read_count % 6
            read_count += 1
            return signatures[scan_index][slot_index]

        with (
            patch("core.utils.mean_rgb_for_region_key", side_effect=read_rgb),
            patch("core.utils.time.sleep", return_value=None),
        ):
            result = scan_pet_bag_count(
                _FakeRegions(),
                emit_fn=lambda text, level: emitted.append((level, text)),
                poll_s=0.001,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 6)
        self.assertGreaterEqual(read_count, 18)
        self.assertFalse(any(level == "WARN" for level, _text in emitted))

    def test_finite_count_scan_timeout_never_returns_unstable_success(self) -> None:
        rgb_by_key = {
            "精灵背包.1": (254, 104, 1),
            "精灵背包.2": (148, 223, 252),
            "精灵背包.3": (254, 103, 0),
            "精灵背包.4": (254, 103, 0),
            "精灵背包.5": (71, 28, 83),
            "精灵背包.6": (254, 104, 0),
        }
        emitted = []

        with (
            patch(
                "core.utils.mean_rgb_for_region_key",
                side_effect=lambda _regions, key: rgb_by_key.get(key),
            ),
            patch(
                "core.utils.time.time",
                side_effect=(0.0, 0.0, 1.0, 2.0),
            ),
            patch("core.utils.time.sleep", return_value=None),
        ):
            result = scan_pet_bag_count(
                _FakeRegions(),
                emit_fn=lambda text, level: emitted.append((level, text)),
                timeout_s=0.5,
                poll_s=0.001,
            )

        self.assertFalse(result["ok"])
        self.assertIsNone(result["count"])
        self.assertFalse(any(level == "WARN" for level, _text in emitted))

    def test_daily_putback_uses_detected_tail_slot(self) -> None:
        verified_slots = []
        drr = SimpleNamespace(
            wait_bag_putback_slot_deep_blue=lambda slot, _event, _tag: (
                verified_slots.append(slot) or True
            )
        )
        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(dar_route_runner=drr)
        runner._emit = lambda *_args, **_kwargs: None
        runner._click_region_btn_fallback = lambda *_args, **_kwargs: True
        runner._click_region_safe = lambda *_args, **_kwargs: True
        runner._should_abort = lambda: False
        runner._new_daily_stop_event = threading.Event

        def ready_with_three_pets(_regions, **kwargs):
            kwargs["bag_scan_callback"](
                {
                    "ok": True,
                    "count": 3,
                    "colors": (
                        "orange",
                        "cyan",
                        "purple",
                        "deep_blue",
                        "deep_blue",
                        "deep_blue",
                    ),
                }
            )
            return True

        with (
            patch(
                "core.daily_runner.wait_pet_bag_ui_ready_after_open",
                side_effect=ready_with_three_pets,
            ),
            patch("core.daily_runner.time.sleep", return_value=None),
        ):
            ok = runner._new_daily_bag_return_and_follow(
                object(),
                False,
                verify_pet_pos="二",
            )

        self.assertTrue(ok)
        self.assertEqual(verified_slots, ["三"])

    def test_daily_putback_stops_when_expected_pet_count_is_missing(self) -> None:
        clicked = []
        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(dar_route_runner=SimpleNamespace())
        runner._emit = lambda *_args, **_kwargs: None
        runner._click_region_btn_fallback = (
            lambda _regions, key, _foreground: clicked.append(key) or True
        )
        runner._should_abort = lambda: False

        def ready_with_two_pets(_regions, **kwargs):
            kwargs["bag_scan_callback"](
                {
                    "ok": True,
                    "count": 2,
                    "colors": (
                        "orange",
                        "purple",
                        "deep_blue",
                        "deep_blue",
                        "deep_blue",
                        "deep_blue",
                    ),
                }
            )
            return True

        with patch(
            "core.daily_runner.wait_pet_bag_ui_ready_after_open",
            side_effect=ready_with_two_pets,
        ):
            ok = runner._new_daily_bag_return_and_follow(
                object(),
                False,
                verify_pet_pos="三",
                expected_pet_count=3,
            )

        self.assertFalse(ok)
        self.assertEqual(clicked, ["精灵背包.打开精灵背包"])

    def test_daily_putback_waits_for_delayed_expected_pet_count(self) -> None:
        verified_slots = []
        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(
            dar_route_runner=SimpleNamespace(
                wait_bag_putback_slot_deep_blue=lambda slot, _event, _tag: (
                    verified_slots.append(slot) or True
                )
            )
        )
        runner._emit = lambda *_args, **_kwargs: None
        runner._click_region_btn_fallback = lambda *_args, **_kwargs: True
        runner._click_region_safe = lambda *_args, **_kwargs: True
        runner._should_abort = lambda: False
        runner._new_daily_stop_event = threading.Event

        two_pets = {
            "ok": True,
            "count": 2,
            "colors": (
                "orange",
                "purple",
                "deep_blue",
                "deep_blue",
                "deep_blue",
                "deep_blue",
            ),
        }
        three_pets = {
            "ok": True,
            "count": 3,
            "colors": (
                "orange",
                "purple",
                "orange",
                "deep_blue",
                "deep_blue",
                "deep_blue",
            ),
        }

        def ready_with_two_pets(_regions, **kwargs):
            kwargs["bag_scan_callback"](two_pets)
            return True

        with (
            patch(
                "core.daily_runner.wait_pet_bag_ui_ready_after_open",
                side_effect=ready_with_two_pets,
            ),
            patch(
                "core.daily_runner.scan_pet_bag_count",
                side_effect=[two_pets, three_pets],
            ) as rescan,
            patch("core.daily_runner.time.sleep", return_value=None),
        ):
            ok = runner._new_daily_bag_return_and_follow(
                object(),
                False,
                verify_pet_pos="三",
                expected_pet_count=3,
                expected_pet_count_wait_timeout_s=12.0,
            )

        self.assertTrue(ok)
        self.assertEqual(rescan.call_count, 2)
        self.assertEqual(verified_slots, ["三"])

    def test_clear_six_pet_backpack_verifies_tail_slots_six_to_one(self) -> None:
        clicked = []
        verified_slots = []
        stop_event = threading.Event()
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._aisifeige_pos = None
        runner._jita_pos = None
        runner._yameisi_pos = None
        runner._emit = lambda *_args, **_kwargs: None
        runner._click_region = (
            lambda key, _use_foreground: clicked.append(key)
        )

        def ready(_event, **kwargs):
            self.assertTrue(kwargs["allow_empty_bag"])
            runner._last_pet_bag_count_scan = {
                "ok": True,
                "count": 6,
            }
            return True

        runner._wait_pet_bag_ui_ready_after_open = ready
        runner._sleep_abortable = lambda *_args, **_kwargs: None
        runner.wait_bag_putback_slot_deep_blue = (
            lambda slot, _event, _tag: verified_slots.append(slot) or True
        )
        runner._close_pet_bag_with_verify = lambda *_args, **_kwargs: None

        ok = runner._rotation_step2_clear_backpack(
            False,
            stop_event,
            log_tag="test",
        )

        self.assertTrue(ok)
        self.assertEqual(verified_slots, ["六", "五", "四", "三", "二", "一"])
        self.assertEqual(clicked[0], "精灵背包.打开精灵背包按钮")
        self.assertEqual(
            clicked[1:],
            ["精灵背包.放回仓库按钮"] * 6,
        )


if __name__ == "__main__":
    unittest.main()
