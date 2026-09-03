import threading
import unittest
from types import SimpleNamespace

from core.dar_route_runner import DarRouteRunner


class BagSkill3ProbeModeTests(unittest.TestCase):
    @staticmethod
    def _runner() -> DarRouteRunner:
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._emit = lambda *_args, **_kwargs: None
        runner._sleep_abortable = lambda *_args, **_kwargs: None
        return runner

    def test_scan_returns_only_stable_orange_slots_2_to_6(self) -> None:
        runner = self._runner()
        rgb_by_slot = {
            2: (2, 0, 0),
            3: (3, 0, 0),
            4: (4, 0, 0),
            5: (5, 0, 0),
            6: (6, 0, 0),
        }
        color_by_rgb = {
            (2, 0, 0): "orange",
            (3, 0, 0): "cyan",
            (4, 0, 0): "orange",
            (5, 0, 0): "purple",
            (6, 0, 0): None,
        }
        runner._bag_party_slot_probe_key = lambda pos: str(pos)
        runner._mean_rgb_for_region_key = lambda key: rgb_by_slot[int(key)]
        runner._classify_pick_bag_slot_rgb = lambda rgb: color_by_rgb[rgb]

        result = runner._scan_bag_orange_slots_2_to_6(
            threading.Event(),
            "测试",
        )

        self.assertEqual(result, [2, 4])

    def test_skill3_probe_waits_for_three_close_samples(self) -> None:
        runner = self._runner()
        samples = iter(
            [
                (10, 10, 10),
                (185, 160, 160),
                (192, 165, 165),
                (191, 166, 165),
                (193, 164, 165),
            ]
        )
        runner._mean_rgb_for_region_key = lambda _key: next(samples)

        result = runner._wait_bag_skill3_probe_stable(
            threading.Event(),
            "测试",
        )

        self.assertEqual(result, (192, 165, 165))
        self.assertTrue(runner._bag_skill3_matches_primary_target(result))

    def test_pet166_probe_accepts_both_observed_render_colors(self) -> None:
        runner = self._runner()

        self.assertTrue(runner._bag_skill3_matches_pet166((192, 165, 165)))
        self.assertTrue(runner._bag_skill3_matches_pet166((201, 176, 176)))
        self.assertFalse(runner._bag_skill3_matches_pet166((211, 186, 186)))

    def test_mode_explores_every_recorded_slot_after_setting_primary(self) -> None:
        runner = self._runner()
        selected = []
        clicked = []
        probe_results = iter(
            [
                (192, 165, 165),
                (120, 90, 70),
                (190, 167, 165),
            ]
        )
        runner._pickmode_open_bag_ready_for_target = (
            lambda *_args, **_kwargs: True
        )
        runner._scan_bag_orange_slots_2_to_6 = (
            lambda *_args, **_kwargs: [2, 4, 6]
        )
        runner._click_pet_with_selection_check = (
            lambda pos, *_args, **_kwargs: selected.append(pos) or True
        )
        runner._wait_bag_skill3_probe_stable = (
            lambda *_args, **_kwargs: next(probe_results)
        )
        runner._click_region = (
            lambda key, *_args, **_kwargs: clicked.append(key)
        )

        ok = runner.run_bag_putback_test(
            "orange_skill3_primary",
            False,
            threading.Event(),
        )

        self.assertTrue(ok)
        self.assertEqual(selected, ["二", "四", "六"])
        self.assertEqual(
            clicked,
            ["精灵背包.设为首发", "精灵背包.设为首发"],
        )

    def test_mode_stops_if_selected_slot_probe_never_stabilizes(self) -> None:
        runner = self._runner()
        selected = []
        runner._pickmode_open_bag_ready_for_target = (
            lambda *_args, **_kwargs: True
        )
        runner._scan_bag_orange_slots_2_to_6 = (
            lambda *_args, **_kwargs: [3, 5]
        )
        runner._click_pet_with_selection_check = (
            lambda pos, *_args, **_kwargs: selected.append(pos) or True
        )
        runner._wait_bag_skill3_probe_stable = (
            lambda *_args, **_kwargs: None
        )
        runner._click_region = (
            lambda *_args, **_kwargs: self.fail("探针未稳定时不应点击设为首发")
        )

        ok = runner.run_bag_putback_test(
            "orange_skill3_primary",
            False,
            threading.Event(),
        )

        self.assertFalse(ok)
        self.assertEqual(selected, ["三"])


if __name__ == "__main__":
    unittest.main()
