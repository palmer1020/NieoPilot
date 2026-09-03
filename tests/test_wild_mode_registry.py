import os
import unittest

from core.wild_mode_registry import (
    get_profile,
    list_rare_select_options,
    resolve_wild_capture_profile,
)


class WildModeRegistryTests(unittest.TestCase):
    def test_wusuo_has_one_selectable_profile_and_migrates_legacy_key(self) -> None:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        options = list_rare_select_options(project_root)
        option_keys = [key for _label, key in options]

        self.assertIn("乌索", option_keys)
        self.assertNotIn("wusuo_312", option_keys)
        self.assertFalse(
            os.path.exists(os.path.join(project_root, "assets", "wild_modes", "wusuo_312.json"))
        )

        profile = resolve_wild_capture_profile(project_root, "wusuo_312")
        self.assertEqual(profile.slug, "乌索")
        self.assertEqual(profile.target_pet_id, 528)
        self.assertIsNone(profile.target_pet_ids)
        self.assertEqual(profile.target_mp3_ids, (528, 312))
        self.assertIs(get_profile(project_root, "wusuo_312"), get_profile(project_root, "乌索"))


if __name__ == "__main__":
    unittest.main()
