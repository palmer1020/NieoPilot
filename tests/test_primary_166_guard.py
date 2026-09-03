import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.dar_route_runner import (
    PICKMODE_STANDARD_PARTY_ID_LIST,
    DarRouteRunner,
)


class Primary166GuardTests(unittest.TestCase):
    @staticmethod
    def _runner():
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._emit = lambda *_args, **_kwargs: None
        runner._sleep_abortable = lambda *_args, **_kwargs: None
        return runner

    def test_slot_one_skill3_guard_identifies_pet166(self):
        runner = self._runner()
        runner._click_pet_with_selection_check = (
            lambda *_args, **_kwargs: self.fail(
                "开包后精灵一已经选中，不应再次点击选择"
            )
        )
        runner._wait_bag_skill3_probe_stable = (
            lambda *_args, **_kwargs: runner.BAG_SKILL3_PET166_TARGET_RGB
        )

        ok = runner._bag_slot_one_is_pet166_from_open_bag(
            False,
            threading.Event(),
            log_tag="166测试",
        )

        self.assertTrue(ok)

    def test_pre_daily_pet_one_follow_does_not_reselect_after_bag_ready(self):
        runner = self._runner()
        clicks = []
        runner._wait_pet_bag_ui_ready_after_open = (
            lambda *_args, **_kwargs: True
        )
        runner._click_bag_pet_slot_double = (
            lambda *_args, **_kwargs: self.fail(
                "开包后精灵一已经选中，不应再次双击精灵一"
            )
        )
        runner._click_region = (
            lambda key, _use_foreground: clicks.append(key)
        )

        ok = runner._pre_daily_follow_pet_one_after_daily_six_pets(
            False,
            threading.Event(),
            log_tag="日常六宠测试",
        )

        self.assertTrue(ok)
        self.assertEqual(clicks, ["精灵背包.身边跟随"])

    def test_wild_swf_match_but_primary_mismatch_rebuilds_once(self):
        runner = self._runner()
        calls = []
        runner._pickmode_open_bag_ready_for_target = (
            lambda *_args, **_kwargs: calls.append("open") or True
        )
        runner._bag_slot_one_is_pet166_from_open_bag = (
            lambda *_args, **_kwargs: calls.append("check166") or False
        )
        runner._close_pet_bag_with_verify = (
            lambda *_args, **_kwargs: calls.append("close")
        )
        runner._rotation_step2_clear_backpack = (
            lambda *_args, **_kwargs: calls.append("clear") or True
        )
        runner._rotation_step3_place_pets = (
            lambda *_args, **_kwargs: calls.append("rebuild") or True
        )

        with patch(
            "core.dar_route_runner.fetch_kernel_since",
            return_value=["party"],
        ), patch(
            "core.dar_route_runner.iter_party_pet_swf_ids_in_line",
            return_value=iter(PICKMODE_STANDARD_PARTY_ID_LIST),
        ):
            ok = runner._rotation_clear_backpack_and_pick_or_skip(
                "nieo",
                False,
                threading.Event(),
                log_tag="野外测试",
                expected_party_ids=frozenset(PICKMODE_STANDARD_PARTY_ID_LIST),
            )

        self.assertTrue(ok)
        self.assertEqual(calls.count("check166"), 1)
        self.assertEqual(calls[-2:], ["clear", "rebuild"])

    def test_wild_swf_and_primary_match_keeps_bag_open_for_step4(self):
        runner = self._runner()
        runner._pickmode_open_bag_ready_for_target = (
            lambda *_args, **_kwargs: True
        )
        runner._bag_slot_one_is_pet166_from_open_bag = (
            lambda *_args, **_kwargs: True
        )
        runner._close_pet_bag_with_verify = (
            lambda *_args, **_kwargs: self.fail(
                "166通过后不应在步骤4恢复前关闭背包"
            )
        )

        with patch(
            "core.dar_route_runner.fetch_kernel_since",
            return_value=["party"],
        ), patch(
            "core.dar_route_runner.iter_party_pet_swf_ids_in_line",
            return_value=iter(PICKMODE_STANDARD_PARTY_ID_LIST),
        ):
            ok = runner._rotation_clear_backpack_and_pick_or_skip(
                "nieo",
                False,
                threading.Event(),
                log_tag="野外测试",
                expected_party_ids=frozenset(PICKMODE_STANDARD_PARTY_ID_LIST),
            )

        self.assertTrue(ok)
        self.assertTrue(runner._rotation_step4_bag_already_open)
        self.assertTrue(runner._rotation_step4_verified_primary_selected)

    def test_rotation_step4_reuses_selected_166_for_recovery(self):
        runner = self._runner()
        calls = []
        runner._rotation_step4_bag_already_open = True
        runner._rotation_step4_verified_primary_selected = True
        runner._click_region = (
            lambda *_args, **_kwargs: self.fail(
                "沿用已打开背包时不应再次点击打开背包"
            )
        )
        runner._wait_pet_bag_ui_ready_after_open = (
            lambda *_args, **_kwargs: True
        )
        runner._recover_selected_bag_pet_once = (
            lambda slot, *_args, **_kwargs: calls.append(("recover_selected", slot))
        )
        runner._recover_pick_party_after_bag_ready = (
            lambda *_args, **kwargs: calls.append(
                ("recover_rest", kwargs.get("primary_already_recovered"))
            )
            or True
        )

        ok = runner._pickmode_rotation_step4_set_companion(
            False,
            threading.Event(),
        )

        self.assertTrue(ok)
        self.assertEqual(
            calls,
            [("recover_selected", "一"), ("recover_rest", True)],
        )

    def test_wild_swf_mismatch_rebuild_skips_primary_check(self):
        runner = self._runner()
        calls = []
        runner._bag_slot_one_is_pet166_from_open_bag = (
            lambda *_args, **_kwargs: self.fail(
                "SWF mismatch重取前后都不应再检查精灵一"
            )
        )
        runner._rotation_step2_clear_backpack = (
            lambda *_args, **_kwargs: calls.append("clear") or True
        )
        runner._rotation_step3_place_pets = (
            lambda *_args, **_kwargs: calls.append("rebuild") or True
        )

        with patch(
            "core.dar_route_runner.fetch_kernel_since",
            return_value=[],
        ):
            ok = runner._rotation_clear_backpack_and_pick_or_skip(
                "nieo",
                False,
                threading.Event(),
                log_tag="野外测试",
                expected_party_ids=frozenset(PICKMODE_STANDARD_PARTY_ID_LIST),
            )

        self.assertTrue(ok)
        self.assertEqual(calls, ["clear", "rebuild"])

    def test_yilu_swf_match_but_primary_mismatch_rebuilds_and_keeps_bag_open(self):
        runner = self._runner()
        calls = []
        runner._pickmode_recent_party_ids_satisfied = (
            lambda *_args, **_kwargs: True
        )
        runner._pickmode_open_bag_ready_for_target = (
            lambda *_args, **_kwargs: True
        )
        runner._bag_slot_one_is_pet166_from_open_bag = (
            lambda *_args, **_kwargs: calls.append("check166") or False
        )
        runner._close_pet_bag_with_verify = (
            lambda *_args, **_kwargs: calls.append("close")
        )
        runner._rotation_step2_clear_backpack = (
            lambda *_args, **kwargs: calls.append(
                ("clear", kwargs.get("close_after"))
            )
            or True
        )
        runner._click_region = (
            lambda key, *_args, **_kwargs: calls.append(("click", key))
        )
        runner._pickmode_rotation_step3_place_pets_from_open_warehouse = (
            lambda *_args, **_kwargs: calls.append("rebuild") or True
        )
        runner._click_pet_warehouse_close = (
            lambda *_args, **_kwargs: calls.append("close_warehouse")
        )

        ok = runner.ensure_pick_party_from_bag_warehouse_or_skip(
            False,
            threading.Event(),
            log_tag="依卢测试",
            verify_primary_166=True,
        )

        self.assertTrue(ok)
        self.assertEqual(calls.count("check166"), 1)
        self.assertIn(("clear", False), calls)
        self.assertIn("rebuild", calls)

    def test_yilu_primary_match_marks_selected_pet_one_for_recovery(self):
        runner = self._runner()
        runner._pickmode_recent_party_ids_satisfied = (
            lambda *_args, **_kwargs: True
        )
        runner._pickmode_open_bag_ready_for_target = (
            lambda *_args, **_kwargs: True
        )
        runner._bag_slot_one_is_pet166_from_open_bag = (
            lambda *_args, **_kwargs: True
        )

        ok = runner.ensure_pick_party_from_bag_warehouse_or_skip(
            False,
            threading.Event(),
            log_tag="依卢测试",
            verify_primary_166=True,
        )

        self.assertTrue(ok)
        self.assertTrue(runner._pick_party_verified_primary_selected)

    def test_yilu_recovery_uses_verified_pet_one_selection_once(self):
        runner = self._runner()
        calls = []
        runner._pick_party_verified_primary_selected = True
        runner._ensure_pet_bag_ui_ready_after_open = (
            lambda *_args, **_kwargs: True
        )
        runner._recover_selected_bag_pet_once = (
            lambda slot, *_args, **_kwargs: calls.append(("recover_selected", slot))
        )
        runner._scan_pick_bag_party_slots_1_to_6 = (
            lambda *_args, **_kwargs: {
                "ok": True,
                "cyan": "二",
                "purple": "三",
            }
        )
        runner._recover_unique_bag_slots = (
            lambda slots, *_args, **_kwargs: calls.append(
                ("recover_slots", tuple(slots))
            )
        )
        runner._click_bag_pet_slot_double = (
            lambda slot, *_args, **_kwargs: calls.append(("follow_slot", slot))
        )
        runner._click_region = lambda key, *_args, **_kwargs: calls.append(
            ("click", key)
        )
        runner._reset_skill_recovery_counters = lambda *_args, **_kwargs: None

        ok = runner.recover_pick_party_color_slots_from_closed_bag(
            False,
            threading.Event(),
            "依卢测试",
            recover_purple=True,
            set_follow_purple=True,
            bag_already_open=True,
        )

        self.assertTrue(ok)
        self.assertIn(("recover_selected", "一"), calls)
        self.assertIn(("recover_slots", ("三",)), calls)
        self.assertNotIn(("recover_slots", ("一", "三")), calls)

    def test_lanlan_swf_match_but_primary_mismatch_rebuilds_target_party(self):
        runner = self._runner()
        calls = []
        target_party = (166, 197, 1459, 606, 347, 1337)
        runner._pickmode_classify_target_cyan_party = (
            lambda *_args, **_kwargs: ("target", set(target_party), target_party)
        )
        runner._pickmode_open_bag_ready_for_target = (
            lambda *_args, **_kwargs: True
        )
        runner._bag_slot_one_is_pet166_from_open_bag = (
            lambda *_args, **_kwargs: calls.append("check166") or False
        )
        runner._close_pet_bag_with_verify = (
            lambda *_args, **_kwargs: calls.append("close")
        )
        runner._rotation_step2_clear_backpack = (
            lambda *_args, **kwargs: calls.append(
                ("clear", kwargs.get("close_after"))
            )
            or True
        )
        runner._pickmode_open_warehouse_from_ready_bag = (
            lambda *_args, **_kwargs: True
        )
        runner.take_pickmode_pets_from_open_bag_warehouse = (
            lambda pet_ids, *_args, **kwargs: calls.append(
                ("take", tuple(pet_ids), kwargs.get("rebuild_mode"))
            )
            or True
        )
        runner._click_pet_warehouse_close = lambda *_args, **_kwargs: None
        runner.recover_cyan_and_follow_purple_from_open_bag = (
            lambda *_args, **_kwargs: True
        )

        ok = runner.ensure_target_cyan_pick_party_from_bag_warehouse_or_rebuild(
            347,
            False,
            threading.Event(),
            log_tag="岚岚测试",
            base_pet_id=67,
            verify_primary_166=True,
        )

        self.assertTrue(ok)
        self.assertEqual(calls.count("check166"), 1)
        self.assertIn(("clear", False), calls)
        self.assertIn(("take", target_party, True), calls)

    def test_lanlan_primary_match_recovers_currently_selected_pet_one(self):
        runner = self._runner()
        calls = []
        target_party = (166, 197, 1459, 606, 347, 1337)
        runner._pickmode_classify_target_cyan_party = (
            lambda *_args, **_kwargs: ("target", set(target_party), target_party)
        )
        runner._pickmode_open_bag_ready_for_target = (
            lambda *_args, **_kwargs: True
        )
        runner._bag_slot_one_is_pet166_from_open_bag = (
            lambda *_args, **_kwargs: True
        )
        runner._recover_selected_bag_pet_once = (
            lambda slot, *_args, **_kwargs: calls.append(("recover_selected", slot))
        )
        runner.recover_cyan_and_follow_purple_from_open_bag = (
            lambda *_args, **_kwargs: calls.append("recover_rest") or True
        )

        ok = runner.ensure_target_cyan_pick_party_from_bag_warehouse_or_rebuild(
            347,
            False,
            threading.Event(),
            log_tag="岚岚测试",
            base_pet_id=67,
            verify_primary_166=True,
        )

        self.assertTrue(ok)
        self.assertEqual(calls, [("recover_selected", "一"), "recover_rest"])

    def test_sunday_target_party_keeps_all_six_standard_pets(self):
        runner = self._runner()

        party = runner._pickmode_target_party_id_list(1459, base_pet_id=197)

        self.assertEqual(party, tuple(PICKMODE_STANDARD_PARTY_ID_LIST))


if __name__ == "__main__":
    unittest.main()
