import unittest

from core.daily_runner import DailyRunner


class _FakeDarRunner:
    BATTLE_SWITCH_SLOT_COLOR_MIN_DISTANCE = 70.0

    def __init__(self, rgbs):
        self.rgbs = dict(rgbs)

    @staticmethod
    def _battle_switch_slot_key(pet_num):
        return pet_num

    def _mean_rgb_for_region_key(self, key):
        return self.rgbs[key]

    @staticmethod
    def _classify_battle_switch_slot_rgb(rgb):
        if rgb is None:
            return None
        r, g, b = rgb
        if r >= 210 and 95 <= g <= 190 and b <= 90:
            return "orange"
        if b >= 175 and g >= 150 and r <= 205:
            return "blue"
        if r >= 105 and b >= 130 and g <= 130 and (b - g) >= 45:
            return "purple"
        return None

    @staticmethod
    def _battle_switch_rgb_distance(left, right):
        return sum((a - b) ** 2 for a, b in zip(left, right)) ** 0.5


class LanlanSwitchSlotScanTests(unittest.TestCase):
    def test_highlighted_cyan_completes_aligned_pattern(self):
        runner = DailyRunner.__new__(DailyRunner)
        dar_runner = _FakeDarRunner(
            {
                1: (213, 249, 255),
                2: (253, 163, 28),
                3: (253, 163, 28),
                4: (253, 163, 28),
                5: (253, 163, 28),
                6: (154, 59, 177),
            }
        )

        ready, signature, colors, _rgbs, counts, _distance = (
            runner._lanlan_scan_switch_slots(dar_runner)
        )

        self.assertTrue(ready)
        self.assertEqual(signature, ("blue", "orange", "orange", "orange", "orange", "purple"))
        self.assertEqual(colors[1], "blue")
        self.assertEqual(counts, {"orange": 4, "blue": 1, "purple": 1})

    def test_near_white_is_not_treated_as_highlighted_cyan(self):
        runner = DailyRunner.__new__(DailyRunner)
        dar_runner = _FakeDarRunner(
            {
                1: (241, 249, 255),
                2: (253, 163, 28),
                3: (253, 163, 28),
                4: (253, 163, 28),
                5: (253, 163, 28),
                6: (154, 59, 177),
            }
        )

        ready, _signature, colors, _rgbs, counts, _distance = (
            runner._lanlan_scan_switch_slots(dar_runner)
        )

        self.assertFalse(ready)
        self.assertIsNone(colors[1])
        self.assertEqual(counts, {"orange": 4, "blue": 0, "purple": 1})

    def test_new_monitor_highlighted_cyan_values_complete_pattern(self):
        runner = DailyRunner.__new__(DailyRunner)
        for cyan_rgb in ((191, 239, 254), (195, 241, 254)):
            with self.subTest(cyan_rgb=cyan_rgb):
                dar_runner = _FakeDarRunner(
                    {
                        1: cyan_rgb,
                        2: (253, 163, 28),
                        3: (253, 163, 28),
                        4: (253, 163, 28),
                        5: (253, 163, 28),
                        6: (154, 59, 177),
                    }
                )

                ready, _signature, colors, _rgbs, counts, _distance = (
                    runner._lanlan_scan_switch_slots(dar_runner)
                )

                self.assertTrue(ready)
                self.assertEqual(colors[1], "blue")
                self.assertEqual(counts, {"orange": 4, "blue": 1, "purple": 1})


if __name__ == "__main__":
    unittest.main()
