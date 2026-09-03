import unittest
from unittest.mock import patch

from core.daily_runner import ONE_CLICK_RELEASE_CATEGORIES
from core.swf_resource_ops import (
    sync_one_click_release_pet_254_set,
    sync_weekly_purple_follow_pet_254_set,
)


class OneClickReleaseCategoryTests(unittest.TestCase):
    def test_weekly_terrace_only_colors_purple_follow_target(self) -> None:
        with patch(
            "core.swf_resource_ops._sync_named_pet_template_map",
            return_value=(True, "ok"),
        ) as sync:
            self.assertEqual(sync_weekly_purple_follow_pet_254_set(), (True, "ok"))

        mode_name, mapping = sync.call_args.args
        self.assertEqual(mode_name, "weekly-purple-follow")
        self.assertEqual(mapping, {"1337": "252"})

    def test_normal_and_ice_run_before_shadow(self) -> None:
        self.assertEqual(
            ONE_CLICK_RELEASE_CATEGORIES,
            (
                ("单属性", "机械系"),
                ("单属性", "超能系"),
                ("单属性", "普通系"),
                ("单属性", "冰系"),
                ("单属性", "暗影系"),
                ("双属性", "水超能"),
            ),
        )

    def test_water_shadow_is_not_in_release_categories(self) -> None:
        self.assertNotIn(
            ("双属性", "水暗影"),
            ONE_CLICK_RELEASE_CATEGORIES,
        )

    def test_shadow_pet_667_is_colored_cyan(self) -> None:
        with patch(
            "core.swf_resource_ops._sync_named_pet_template_map",
            return_value=(True, "ok"),
        ) as sync:
            self.assertEqual(sync_one_click_release_pet_254_set(), (True, "ok"))

        mode_name, mapping = sync.call_args.args
        self.assertEqual(mode_name, "one-click-release")
        self.assertEqual(mapping["667"], "519")
        self.assertNotIn("64", mapping)
        for pet_id in ("65", "604", "607", "650"):
            with self.subTest(pet_id=pet_id):
                self.assertEqual(mapping[pet_id], "519")


if __name__ == "__main__":
    unittest.main()
