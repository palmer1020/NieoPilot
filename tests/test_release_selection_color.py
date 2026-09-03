import unittest

from core.daily_runner import (
    _is_release_display_light_blue,
    _is_release_selection_yellow,
)


class ReleaseSelectionColorTests(unittest.TestCase):
    def test_accepts_observed_slot_one_yellow(self):
        self.assertTrue(_is_release_selection_yellow((180, 224, 85)))

    def test_rejects_non_yellow_selection_colors(self):
        self.assertFalse(_is_release_selection_yellow((120, 180, 220)))
        self.assertFalse(_is_release_selection_yellow((180, 180, 85)))

    def test_accepts_observed_light_blue_display_variant(self):
        self.assertTrue(_is_release_display_light_blue((202, 217, 233)))
        self.assertFalse(_is_release_display_light_blue((9, 57, 108)))


if __name__ == "__main__":
    unittest.main()
