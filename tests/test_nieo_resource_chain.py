import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.bot_thread import BotWorker, NIEO_RESOURCE_CHAIN_SLUGS
from core.dar_route_runner import DarRouteRunner


def _profiles():
    return {
        slug: SimpleNamespace(
            slug=slug,
            name=slug,
            to_script=f"to{slug}",
            map_a_id=index + 10,
            stay_on_b_map=(slug == "水生海草"),
        )
        for index, slug in enumerate(NIEO_RESOURCE_CHAIN_SLUGS)
    }


class _FakeDarRunner:
    def __init__(self, completed_slugs=None):
        self.completed_slugs = set(completed_slugs or NIEO_RESOURCE_CHAIN_SLUGS)
        self.configure_calls = []
        self.pre_entries = []
        self.pre_follow_cyan = []
        self.runs = []
        self.run_single_map = []
        self.run_follow_cyan = []
        self._requested = False

    def configure_resource_yellow_rotation_handoff(
        self,
        enabled,
        completion_action="进入普通轮换重连模式",
    ):
        self.configure_calls.append((bool(enabled), completion_action))
        self._requested = False

    def _execute_nieo_pre_rotation_reconnect(self, **kwargs):
        self.pre_entries.append(
            (kwargs["to_script"], kwargs["expected_map_id"])
        )
        self.pre_follow_cyan.append(bool(kwargs.get("follow_cyan")))
        return True

    def run_configured_nieo_mode(self, profile, **kwargs):
        self.runs.append(profile.slug)
        self.run_single_map.append(bool(kwargs["single_map"]))
        self.run_follow_cyan.append(bool(kwargs.get("follow_cyan")))
        self._requested = profile.slug in self.completed_slugs

    def consume_resource_yellow_rotation_handoff(self):
        requested = self._requested
        self._requested = False
        return requested


class NieoResourceChainTests(unittest.TestCase):
    def _worker(self, dar_runner):
        worker = BotWorker.__new__(BotWorker)
        worker.project_root = "test-root"
        worker.user_stop_requested = False
        worker.stop_current = False
        worker._stop_event = threading.Event()
        worker.dar_route_runner = dar_runner
        worker.emit_and_log = Mock()
        worker._run_dar_mode_with_restart = (
            lambda _label, run_once, prepare_after_restart=None: run_once()
        )
        return worker

    def test_runs_five_resources_in_order_then_completes(self):
        profiles = _profiles()
        dar_runner = _FakeDarRunner()
        worker = self._worker(dar_runner)

        with (
            patch("core.nieo_mode_registry.load_all_nieo_modes") as load_all,
            patch(
                "core.nieo_mode_registry.get_profile",
                side_effect=lambda _root, slug: profiles.get(slug),
            ),
        ):
            completed = worker._run_nieo_resource_chain(
                use_foreground=False,
                single_map=False,
            )

        self.assertTrue(completed)
        load_all.assert_called_once_with("test-root", reload=True)
        self.assertEqual(dar_runner.runs, list(NIEO_RESOURCE_CHAIN_SLUGS))
        self.assertEqual(
            dar_runner.run_single_map,
            [False, False, True, False, False],
        )
        self.assertEqual(len(dar_runner.pre_entries), 5)
        self.assertEqual(
            [call[1] for call in dar_runner.configure_calls[:4]],
            ["切换到下一个资源模式"] * 4,
        )
        self.assertEqual(
            dar_runner.configure_calls[4],
            (True, "进入普通轮换重连模式"),
        )
        self.assertEqual(dar_runner.configure_calls[-1][0], False)

    def test_follow_cyan_is_forwarded_to_every_resource_entry_and_mode(self):
        profiles = _profiles()
        dar_runner = _FakeDarRunner()
        worker = self._worker(dar_runner)

        with (
            patch("core.nieo_mode_registry.load_all_nieo_modes"),
            patch(
                "core.nieo_mode_registry.get_profile",
                side_effect=lambda _root, slug: profiles.get(slug),
            ),
        ):
            completed = worker._run_nieo_resource_chain(
                use_foreground=False,
                single_map=False,
                follow_cyan=True,
            )

        self.assertTrue(completed)
        self.assertEqual(dar_runner.pre_follow_cyan, [True] * 5)
        self.assertEqual(dar_runner.run_follow_cyan, [True] * 5)

    def test_rotation_nieo_resource_forwards_single_map_and_follow_cyan(self):
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(
            stop_current=False,
            project_root="test-root",
            daily_runner=SimpleNamespace(run_single_script=Mock(return_value=True)),
        )
        runner._emit = Mock()
        runner._special_essence_gate_after_to = Mock(return_value=None)
        runner._sleep_abortable = Mock()
        runner._check_last_map_and_newnpc = Mock(return_value=(11, True))
        runner.run_nieo_mode = Mock()
        runner._rotation_nieo_single_map_escape = False
        runner._rotation_nieo_follow_cyan = True

        with patch("core.dar_route_runner.kernel_cursor", return_value=0):
            ok = runner._rotation_step5_execute_to_script_and_start_mode(
                "resource:nieo_resource",
                False,
                threading.Event(),
                datetime(2099, 1, 1, tzinfo=timezone.utc),
            )

        self.assertTrue(ok)
        kwargs = runner.run_nieo_mode.call_args.kwargs
        self.assertTrue(kwargs["nieo_resource_defeat"])
        self.assertFalse(kwargs["nieo_single_map_escape"])
        self.assertTrue(kwargs["follow_cyan"])

    def test_rotation_follow_cyan_is_scoped_to_nieo_family_modes(self):
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner._rotation_nieo_follow_cyan = True

        for mode in (
            "nieo",
            "resource:nieo_resource",
            "resource:pure_energy",
            "resource:晶化气泡",
        ):
            with self.subTest(mode=mode):
                self.assertTrue(runner._rotation_follow_cyan_for_mode(mode))
        for mode in ("rare", "rare:乌索", "resource:rare:乌索", "eit", "mantis"):
            with self.subTest(mode=mode):
                self.assertFalse(runner._rotation_follow_cyan_for_mode(mode))

        runner._rotation_nieo_follow_cyan = False
        self.assertFalse(
            runner._rotation_follow_cyan_for_mode("resource:晶化气泡")
        )

    def test_rotation_initial_follow_stays_purple_for_nieo_and_water_grass(self):
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner._rotation_nieo_follow_cyan = True

        self.assertFalse(runner._rotation_initial_follow_cyan_for_mode("nieo"))
        self.assertFalse(
            runner._rotation_initial_follow_cyan_for_mode("resource:水生海草")
        )
        self.assertTrue(
            runner._rotation_initial_follow_cyan_for_mode("resource:pure_energy")
        )
        self.assertTrue(
            runner._rotation_initial_follow_cyan_for_mode("resource:晶化气泡")
        )

    def test_pre_entry_defers_cyan_only_for_nieo_and_water_grass(self):
        self.assertTrue(
            DarRouteRunner._pre_nieo_should_defer_cyan_until_a(
                pem_route=False,
                to_script=None,
            )
        )
        self.assertTrue(
            DarRouteRunner._pre_nieo_should_defer_cyan_until_a(
                pem_route=False,
                to_script="to水生海草1.json",
            )
        )
        self.assertFalse(
            DarRouteRunner._pre_nieo_should_defer_cyan_until_a(
                pem_route=True,
                to_script=None,
            )
        )
        self.assertFalse(
            DarRouteRunner._pre_nieo_should_defer_cyan_until_a(
                pem_route=False,
                to_script="to晶化气泡",
            )
        )

    def test_rotation_nieo_and_pure_energy_forward_follow_cyan(self):
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(
            stop_current=False,
            project_root="test-root",
            daily_runner=SimpleNamespace(run_single_script=Mock(return_value=True)),
        )
        runner._emit = Mock()
        runner._special_essence_gate_after_to = Mock(return_value=None)
        runner._sleep_abortable = Mock()
        runner._check_last_map_and_newnpc = Mock(
            side_effect=lambda expected, timeout_s: (expected, True)
        )
        runner.run_nieo_mode = Mock()
        runner.run_pure_energy_resource_mode = Mock()
        runner._rotation_nieo_single_map_escape = False
        runner._rotation_nieo_follow_cyan = True

        with patch("core.dar_route_runner.kernel_cursor", return_value=0):
            nieo_ok = runner._rotation_step5_execute_to_script_and_start_mode(
                "nieo",
                False,
                threading.Event(),
                datetime(2099, 1, 1, tzinfo=timezone.utc),
            )
            pure_ok = runner._rotation_step5_execute_to_script_and_start_mode(
                "resource:pure_energy",
                False,
                threading.Event(),
                datetime(2099, 1, 1, tzinfo=timezone.utc),
            )

        self.assertTrue(nieo_ok)
        self.assertTrue(pure_ok)
        self.assertTrue(runner.run_nieo_mode.call_args.kwargs["follow_cyan"])
        self.assertTrue(
            runner.run_pure_energy_resource_mode.call_args.kwargs["follow_cyan"]
        )

    def test_rotation_custom_resource_forwards_follow_cyan(self):
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(
            stop_current=False,
            project_root="test-root",
            daily_runner=SimpleNamespace(run_single_script=Mock(return_value=True)),
        )
        runner._emit = Mock()
        runner._special_essence_gate_after_to = Mock(return_value=None)
        runner._sleep_abortable = Mock()
        runner._check_last_map_and_newnpc = Mock(return_value=(54, True))
        runner.run_configured_nieo_mode = Mock()
        runner._rotation_nieo_single_map_escape = True
        runner._rotation_nieo_follow_cyan = True
        profile = SimpleNamespace(
            name="晶化气泡",
            map_a_id=54,
            to_script="to晶化气泡",
        )

        with (
            patch("core.dar_route_runner.kernel_cursor", return_value=0),
            patch(
                "core.nieo_mode_registry.get_profile",
                return_value=profile,
            ),
        ):
            ok = runner._rotation_step5_execute_to_script_and_start_mode(
                "resource:晶化气泡",
                False,
                threading.Event(),
                datetime(2099, 1, 1, tzinfo=timezone.utc),
            )

        self.assertTrue(ok)
        kwargs = runner.run_configured_nieo_mode.call_args.kwargs
        self.assertTrue(kwargs["single_map"])
        self.assertTrue(kwargs["follow_cyan"])

    def test_stops_chain_when_one_mode_does_not_reach_target(self):
        profiles = _profiles()
        dar_runner = _FakeDarRunner(
            completed_slugs={"晶化气泡"},
        )
        worker = self._worker(dar_runner)

        with (
            patch("core.nieo_mode_registry.load_all_nieo_modes"),
            patch(
                "core.nieo_mode_registry.get_profile",
                side_effect=lambda _root, slug: profiles.get(slug),
            ),
        ):
            completed = worker._run_nieo_resource_chain(
                use_foreground=True,
                single_map=True,
            )

        self.assertFalse(completed)
        self.assertEqual(dar_runner.runs, ["晶化气泡", "露西之核"])
        self.assertEqual(dar_runner.configure_calls[-1][0], False)

    def test_yellow_victory_handoff_only_requests_at_sixty(self):
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner._emit = Mock()
        runner.configure_resource_yellow_rotation_handoff(
            True,
            completion_action="切换到下一个资源模式",
        )

        for _ in range(59):
            self.assertFalse(runner._note_resource_yellow_victory())
        self.assertFalse(runner.consume_resource_yellow_rotation_handoff())

        self.assertTrue(runner._note_resource_yellow_victory())
        self.assertTrue(runner.consume_resource_yellow_rotation_handoff())
        self.assertFalse(runner.consume_resource_yellow_rotation_handoff())
        self.assertIn(
            "切换到下一个资源模式",
            runner._emit.call_args_list[-1].args[0],
        )


if __name__ == "__main__":
    unittest.main()
