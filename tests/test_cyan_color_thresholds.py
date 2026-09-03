import unittest

from core.calibration_test_runner import CalibrationTestRunner
from core.daily_runner import DailyRunner
from core.dar_route_runner import DarRouteRunner
from core.unified_battle_framework import UnifiedBattleFramework


class CyanColorThresholdTests(unittest.TestCase):
    def test_new_monitor_cyan_is_accepted_by_all_generic_classifiers(self):
        for rgb in ((191, 239, 254), (195, 241, 254)):
            with self.subTest(rgb=rgb):
                self.assertEqual(
                    DarRouteRunner._classify_battle_switch_slot_rgb(None, rgb), "blue"
                )
                self.assertEqual(DailyRunner._psychic_exp_slot_color(rgb), "cyan")
                self.assertEqual(DailyRunner._pick_pet_exp_slot_color(rgb), "cyan")
                self.assertEqual(
                    UnifiedBattleFramework._classify_calibration_cell_rgb(rgb), "cyan"
                )
                self.assertTrue(CalibrationTestRunner._is_calibration_colored_pixel(*rgb))

    def test_near_white_remains_excluded_from_cyan(self):
        rgb = (241, 249, 255)
        self.assertIsNone(DarRouteRunner._classify_battle_switch_slot_rgb(None, rgb))
        self.assertEqual(DailyRunner._psychic_exp_slot_color(rgb), "unknown")
        self.assertEqual(DailyRunner._pick_pet_exp_slot_color(rgb), "unknown")
        self.assertEqual(
            UnifiedBattleFramework._classify_calibration_cell_rgb(rgb), "unknown"
        )
        self.assertFalse(CalibrationTestRunner._is_calibration_colored_pixel(*rgb))


if __name__ == "__main__":
    unittest.main()
