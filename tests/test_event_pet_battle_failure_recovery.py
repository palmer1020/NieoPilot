import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.dar_route_runner import DarRouteRunner


def _profile() -> SimpleNamespace:
    return SimpleNamespace(
        slug="yite",
        name="伊特",
        route_hint="伊特",
        map_a_id=414,
        to_script="to伊特",
        entry_pet_id=471,
        min_cycles_before_entry=5,
        region_prefix="伊特",
        pick_flight_reverse=0,
        pick_flight_pet_id=166,
        delete_swf_ids=(),
    )


class EventPetBattleFailureRecoveryTests(unittest.TestCase):
    def _runner(self) -> DarRouteRunner:
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._emit = lambda *_args, **_kwargs: None
        runner._is_rotation_mode = False
        runner._should_restart_after_reconnect = False
        return runner

    def test_eit_battle_config_has_skill_one_region(self) -> None:
        runner = self._runner()
        captured = {}

        class Framework:
            def stage2_calibration_and_petitem(self, *args, **kwargs):
                captured["config"] = kwargs["config"]
                return False, None

        runner._unified_framework = Framework()
        runner._capsule_cycle_tiers_for_current_battle = lambda: ("high",)

        result = runner._eit_run_battle(False, threading.Event())

        self.assertEqual(result, "failed")
        self.assertEqual(captured["config"].skill_key, "对战.使用技能一")

    def test_failed_battle_reconnects_without_post_battle_scan(self) -> None:
        runner = self._runner()
        profile = _profile()
        reconnect_reasons = []
        verify_calls = []
        runner._event_pet_post_start_bag_scan = lambda *_args, **_kwargs: True
        runner._event_pet_guard_map = lambda *_args, **_kwargs: True
        runner._event_pet_hunt_dispatch = lambda *_args, **_kwargs: "entered"
        runner._event_pet_battle_dispatch = lambda *_args, **_kwargs: "failed"
        runner._event_pet_trigger_reconnect = (
            lambda *_args, reason, **_kwargs: reconnect_reasons.append(reason)
        )
        runner._event_pet_verify_map_after_battle = (
            lambda *_args, **_kwargs: verify_calls.append(True) or True
        )

        with patch("core.dar_route_runner.kernel_cursor", return_value=123):
            ok = runner.run_event_pet_mode(
                profile,
                threading.Event(),
                False,
                skip_pre_rotation=True,
            )

        self.assertFalse(ok)
        self.assertEqual(reconnect_reasons, ["战斗流程失败"])
        self.assertEqual(verify_calls, [])

    def test_post_battle_map_accepts_only_fresh_pair(self) -> None:
        runner = self._runner()
        reconnect_reasons = []
        runner._get_last_map_id = lambda: 999
        runner._event_pet_trigger_reconnect = (
            lambda *_args, reason, **_kwargs: reconnect_reasons.append(reason)
        )
        rows = [
            (101, 0.0, r"path=resource\map\414.swf"),
            (102, 0.1, r"path=resource\newNpc\multi\0.swf"),
        ]

        with patch(
            "core.dar_route_runner.fetch_kernel_since",
            return_value=rows,
        ) as fetch:
            ok = runner._event_pet_verify_map_after_battle(
                _profile(),
                False,
                threading.Event(),
                battle_start_cursor=100,
            )

        self.assertTrue(ok)
        self.assertEqual(reconnect_reasons, [])
        fetch.assert_called_once_with(100, return_rows=True)

    def test_post_battle_map_rejects_stale_last_known_map(self) -> None:
        runner = self._runner()
        reconnect_reasons = []
        runner._get_last_map_id = lambda: 414
        runner._event_pet_trigger_reconnect = (
            lambda *_args, reason, **_kwargs: reconnect_reasons.append(reason)
        )

        with (
            patch("core.dar_route_runner.fetch_kernel_since", return_value=[]),
            patch("core.dar_route_runner.time.time", side_effect=[0.0, 0.0, 9.0]),
            patch("core.dar_route_runner.time.sleep", return_value=None),
        ):
            ok = runner._event_pet_verify_map_after_battle(
                _profile(),
                False,
                threading.Event(),
                battle_start_cursor=100,
            )

        self.assertFalse(ok)
        self.assertEqual(
            reconnect_reasons,
            ["战后新地图信号缺失或错位"],
        )

    def test_hunt_white_probe_has_absolute_timeout(self) -> None:
        runner = self._runner()
        profile = _profile()
        reconnect_reasons = []
        runner._event_pet_peek_entry_since = (
            lambda *_args, **_kwargs: ("none", 100)
        )
        runner._event_pet_guard_map = lambda *_args, **_kwargs: True
        runner._click_region = lambda *_args, **_kwargs: None
        runner._eit_is_white_probe_ready = lambda: False
        runner._event_pet_trigger_reconnect = (
            lambda *_args, reason, **_kwargs: reconnect_reasons.append(reason)
        )

        with (
            patch("core.dar_route_runner.kernel_cursor", return_value=100),
            patch("core.dar_route_runner.EIT_CLICK1_WHITE_TIMEOUT_SEC", 0.0),
            patch("core.dar_route_runner.time.sleep", return_value=None),
        ):
            result = runner._event_pet_hunt_eit_ui_v2(
                profile,
                False,
                threading.Event(),
            )

        self.assertEqual(result, "reconnect")
        self.assertEqual(reconnect_reasons, ["伊特.1白色探针超时"])


if __name__ == "__main__":
    unittest.main()
