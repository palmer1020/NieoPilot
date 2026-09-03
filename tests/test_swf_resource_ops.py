import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.bot_thread import BotWorker
from core.dar_route_runner import DarRouteRunner
from core.swf_resource_ops import (
    ensure_fly_pet_1337_from_483,
    ensure_fly_pet_483_and_1337_from_50,
    potential_pet_swf_union_ids,
    rotation_pet_swf_union_ids,
    sync_lanlan_pet_254_set,
    sync_master_cup_pet_254_set,
    sync_runtime_pet_254_base,
)


class FlyPetResourceMigrationTests(unittest.TestCase):
    def test_migrates_legacy_1337_from_483_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "50.swf").write_bytes(b"pet-50")
            (root / "483.swf").write_bytes(b"original-483")
            (root / "1337.swf").write_bytes(b"original-483")
            (root / "1337_og.swf").write_bytes(b"original-1337")

            ok, _msg = ensure_fly_pet_483_and_1337_from_50(root)

            self.assertTrue(ok)
            self.assertEqual((root / "483_og.swf").read_bytes(), b"original-483")
            self.assertEqual((root / "1337_og.swf").read_bytes(), b"original-1337")
            self.assertEqual((root / "483.swf").read_bytes(), b"pet-50")
            self.assertEqual((root / "1337.swf").read_bytes(), b"pet-50")

            ok_again, _msg_again = ensure_fly_pet_483_and_1337_from_50(root)

            self.assertTrue(ok_again)
            self.assertEqual((root / "483_og.swf").read_bytes(), b"original-483")
            self.assertEqual((root / "1337_og.swf").read_bytes(), b"original-1337")

    def test_old_function_name_uses_new_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "50.swf").write_bytes(b"pet-50")
            (root / "483.swf").write_bytes(b"original-483")
            (root / "1337.swf").write_bytes(b"original-1337")

            ok, _msg = ensure_fly_pet_1337_from_483(root)

            self.assertTrue(ok)
            self.assertEqual((root / "483.swf").read_bytes(), b"pet-50")
            self.assertEqual((root / "1337.swf").read_bytes(), b"pet-50")
            self.assertEqual((root / "483_og.swf").read_bytes(), b"original-483")
            self.assertEqual((root / "1337_og.swf").read_bytes(), b"original-1337")


class RotationPetSwfUnionTests(unittest.TestCase):
    def test_lanlan_weekday_uses_67_as_temporary_cyan_base(self) -> None:
        captured = {}

        def capture(name, mapping):
            captured["name"] = name
            captured["mapping"] = dict(mapping)
            return True, "ok"

        with patch(
            "core.swf_resource_ops._sync_named_pet_template_map",
            side_effect=capture,
        ):
            ok, _msg = sync_lanlan_pet_254_set(347)

        self.assertTrue(ok)
        self.assertEqual(
            captured["mapping"],
            {"1337": "252", "67": "519", "347": "519"},
        )

    def test_lanlan_sunday_only_marks_1459_cyan(self) -> None:
        captured = {}

        def capture(name, mapping):
            captured["mapping"] = dict(mapping)
            return True, "ok"

        with patch(
            "core.swf_resource_ops._sync_named_pet_template_map",
            side_effect=capture,
        ):
            ok, _msg = sync_lanlan_pet_254_set(1459)

        self.assertTrue(ok)
        self.assertEqual(
            captured["mapping"],
            {"1337": "252", "1459": "519"},
        )

    def test_master_cup_uses_67_as_temporary_cyan_base(self) -> None:
        captured = {}

        def capture(name, mapping):
            captured["mapping"] = dict(mapping)
            return True, "ok"

        with patch(
            "core.swf_resource_ops._sync_named_pet_template_map",
            side_effect=capture,
        ):
            ok, _msg = sync_master_cup_pet_254_set([568])

        self.assertTrue(ok)
        self.assertEqual(
            captured["mapping"],
            {"1337": "252", "67": "519", "568": "519"},
        )

    def test_target_party_can_replace_67_without_removing_197_or_1459(self) -> None:
        runner = DarRouteRunner.__new__(DarRouteRunner)

        party = runner._pickmode_target_party_id_list(631, base_pet_id=67)

        self.assertEqual(party, (166, 197, 1459, 606, 631, 1337))

    def test_union_includes_static_and_manifest_pet_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wild_dir = root / "assets" / "wild_modes"
            nieo_dir = root / "assets" / "nieo_modes"
            event_dir = root / "assets" / "event_pet_modes"
            wild_dir.mkdir(parents=True)
            nieo_dir.mkdir(parents=True)
            event_dir.mkdir(parents=True)
            (wild_dir / "custom.json").write_text(
                json.dumps(
                    {
                        "target_pet_ids": [9001],
                        "delete_swf_ids": [9002, 9003],
                    }
                ),
                encoding="utf-8",
            )
            (nieo_dir / "resource.json").write_text(
                json.dumps(
                    {
                        "battle_pet_ids_b": [9004],
                        "rare_capture_pets_c": 9005,
                    }
                ),
                encoding="utf-8",
            )
            (event_dir / "event.json").write_text(
                json.dumps(
                    {
                        "entry_pet_id": 9006,
                        "pick_flight_pet_id": 9007,
                    }
                ),
                encoding="utf-8",
            )

            union_ids = rotation_pet_swf_union_ids(root)

            self.assertTrue(
                {122, 166, 197, 269, 403, 491, 568, 1337, 9001, 9002, 9003,
                 9004, 9005, 9006, 9007}.issubset(union_ids)
            )
            self.assertLess(len(union_ids), 200)

    def test_runtime_base_only_maps_rotation_union(self) -> None:
        captured = {}

        def capture(name, mapping):
            captured["name"] = name
            captured["mapping"] = dict(mapping)
            return True, "ok"

        with patch(
            "core.swf_resource_ops.potential_pet_swf_union_ids",
            return_value=frozenset({197, 491, 1337}),
        ), patch(
            "core.swf_resource_ops._sync_named_pet_template_map",
            side_effect=capture,
        ):
            ok, _msg = sync_runtime_pet_254_base()

        self.assertTrue(ok)
        self.assertEqual(
            captured["mapping"],
            {"197": "254", "491": "254", "1337": "254"},
        )
        self.assertIn("潜在影响并集Pet254(3个ID)", captured["name"])

    def test_legacy_rotation_union_name_matches_shared_union(self) -> None:
        self.assertEqual(
            rotation_pet_swf_union_ids(),
            potential_pet_swf_union_ids(),
        )

    def test_bot_task_uses_full_once_then_union(self) -> None:
        worker = BotWorker.__new__(BotWorker)
        worker._task_swf_full_base_done = False
        worker.stop_current = False
        worker._stop_event = threading.Event()
        worker.emit_and_log = lambda *_args, **_kwargs: None
        calls = []

        with patch(
            "core.swf_resource_ops.sync_all_pet_254_base",
            side_effect=lambda: calls.append("full") or (True, "full"),
        ), patch(
            "core.swf_resource_ops.sync_runtime_pet_254_base",
            side_effect=lambda: calls.append("union") or (True, "union"),
        ):
            first_ok = worker._sync_all_pet_254_base_or_stop("first")
            second_ok = worker._sync_all_pet_254_base_or_stop("second")

        self.assertTrue(first_ok)
        self.assertTrue(second_ok)
        self.assertEqual(calls, ["full", "union"])
        self.assertTrue(worker._task_swf_full_base_done)

    def test_new_task_reset_requires_full_again(self) -> None:
        worker = BotWorker.__new__(BotWorker)
        worker._task_swf_full_base_done = True
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = worker

        runner.reset_swf_sync_state(reset_task_baseline=True)

        self.assertFalse(worker._task_swf_full_base_done)


if __name__ == "__main__":
    unittest.main()
