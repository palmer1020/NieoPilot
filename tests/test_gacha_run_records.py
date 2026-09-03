import csv
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from core.daily_runner import DailyRunner


class _GachaRegions:
    def get(self, key):
        if key in {"扭蛋.1", "扭蛋.2", "扭蛋.3", "扭蛋.4"}:
            return object()
        return None


class GachaRunRecordTests(unittest.TestCase):
    @staticmethod
    def _runner() -> DailyRunner:
        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(regions=_GachaRegions())
        runner._emit = lambda *_args, **_kwargs: None
        runner._should_abort = lambda: False
        runner._click_region_safe = lambda *_args, **_kwargs: True
        runner._wait_gacha_test_probe_pair = lambda *_args, **_kwargs: True
        return runner

    def test_each_cycle_has_its_own_id_and_records_after_1and1_clear(self) -> None:
        runner = self._runner()
        events = []
        run_ids = iter(("GACHA-first", "GACHA-second"))
        runner._new_gacha_run_id = lambda: next(run_ids)
        runner._beijing_now = lambda: datetime(
            2026, 7, 25, 12, 0, tzinfo=timezone(timedelta(hours=8))
        )

        def clear_1and1(*_args, on_first_detected=None, **_kwargs):
            events.append("clear")
            if on_first_detected is not None:
                on_first_detected()
            return True

        def record(run_id, *, completed_at, **_kwargs):
            events.append(("record", run_id, completed_at))
            return True

        runner._wait_1and1_clear = clear_1and1
        runner._append_gacha_completion_record = record

        with (
            patch("core.daily_runner.window_manager.find_window", return_value=True),
            patch(
                "core.daily_runner.mean_rgb_for_region_key",
                return_value=(0, 0, 0),
            ),
            patch("core.daily_runner.time.sleep", return_value=None),
        ):
            ok = runner.run_gacha_probe_test(times=2)

        self.assertTrue(ok)
        self.assertEqual(
            events,
            [
                "clear",
                (
                    "record",
                    "GACHA-first",
                    datetime(
                        2026,
                        7,
                        25,
                        12,
                        0,
                        tzinfo=timezone(timedelta(hours=8)),
                    ),
                ),
                "clear",
                (
                    "record",
                    "GACHA-second",
                    datetime(
                        2026,
                        7,
                        25,
                        12,
                        0,
                        tzinfo=timezone(timedelta(hours=8)),
                    ),
                ),
            ],
        )

    def test_failed_1and1_clear_does_not_write_completion_record(self) -> None:
        runner = self._runner()
        recorded = []
        runner._new_gacha_run_id = lambda: "GACHA-not-complete"
        runner._wait_1and1_clear = lambda *_args, **_kwargs: False
        runner._append_gacha_completion_record = (
            lambda run_id, **_kwargs: recorded.append(run_id) or True
        )

        with patch(
            "core.daily_runner.window_manager.find_window",
            return_value=True,
        ):
            ok = runner.run_gacha_probe_test()

        self.assertFalse(ok)
        self.assertEqual(recorded, [])

    def test_obsolete_red_probe_is_not_rechecked_after_1and1(self) -> None:
        runner = self._runner()
        recorded = []
        runner._new_gacha_run_id = lambda: "GACHA-red"
        runner._beijing_now = lambda: datetime(
            2026, 7, 25, 12, 0, tzinfo=timezone(timedelta(hours=8))
        )

        def clear_1and1(*_args, on_first_detected=None, **_kwargs):
            if on_first_detected is not None:
                on_first_detected()
            return True

        runner._wait_1and1_clear = clear_1and1
        runner._append_gacha_completion_record = (
            lambda run_id, **_kwargs: recorded.append(run_id) or True
        )

        with (
            patch("core.daily_runner.window_manager.find_window", return_value=True),
            patch(
                "core.daily_runner.mean_rgb_for_region_key",
                return_value=(240, 30, 6),
            ),
            patch("core.daily_runner.time.sleep", return_value=None),
        ):
            ok = runner.run_gacha_probe_test()

        self.assertTrue(ok)
        self.assertEqual(recorded, ["GACHA-red"])

    def test_clicks_wait_for_the_new_two_probe_states(self) -> None:
        runner = self._runner()
        events = []
        runner._new_gacha_run_id = lambda: "GACHA-pair"
        runner._beijing_now = lambda: datetime(
            2026, 7, 26, 8, 20, tzinfo=timezone(timedelta(hours=8))
        )
        runner._click_region_safe = (
            lambda _regions, key, _foreground: events.append(("click", key)) or True
        )
        runner._wait_gacha_test_probe_pair = (
            lambda _regions, key1, rgb1, key2, rgb2, **_kwargs: (
                events.append(("wait", key1, rgb1, key2, rgb2)) or True
            )
        )
        runner._wait_1and1_clear = lambda *_args, **_kwargs: True
        runner._append_gacha_completion_record = lambda *_args, **_kwargs: True

        with (
            patch("core.daily_runner.window_manager.find_window", return_value=True),
            patch("core.daily_runner.time.sleep", return_value=None),
        ):
            ok = runner.run_gacha_probe_test()

        self.assertTrue(ok)
        self.assertEqual(
            events,
            [
                ("click", "扭蛋.1"),
                ("wait", "扭蛋.3", (14, 99, 133), "扭蛋.4", (255, 204, 0)),
                ("click", "扭蛋.2"),
                ("wait", "扭蛋.3", (25, 167, 190), "扭蛋.4", (152, 142, 41)),
                ("click", "扭蛋.3"),
            ],
        )

    def test_more_than_ten_gacha_failure_requests_reconnect(self) -> None:
        runner = self._runner()
        handoffs = []
        initial_reconnects = []
        runner.run_gacha_reconnect_to_ready = (
            lambda use_foreground, *, reconnect_round: (
                initial_reconnects.append((use_foreground, reconnect_round)) or True
            )
        )
        runner.bot.request_gacha_recovery_after_failure = (
            lambda **kwargs: handoffs.append(kwargs) or False
        )
        runner._click_region_safe = lambda *_args, **_kwargs: False

        with patch(
            "core.daily_runner.window_manager.find_window",
            return_value=True,
        ):
            ok = runner.run_gacha_probe_test(times=11, background_mode=True)

        self.assertFalse(ok)
        self.assertEqual(initial_reconnects, [(False, 1)])
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(handoffs[0]["total"], 11)
        self.assertEqual(handoffs[0]["completed_cycles"], 0)
        self.assertTrue(handoffs[0]["session_after_reconnect"])
        self.assertFalse(handoffs[0]["use_foreground"])
        self.assertIn("点击 扭蛋.1 失败", handoffs[0]["reason"])

    def test_ten_gacha_failure_also_requests_reconnect(self) -> None:
        runner = self._runner()
        handoffs = []
        runner.run_gacha_reconnect_to_ready = (
            lambda *_args, **_kwargs: self.fail(
                "计划次数不大于10时，第1次前不应主动重连"
            )
        )
        runner.bot.request_gacha_recovery_after_failure = (
            lambda **kwargs: handoffs.append(kwargs) or False
        )
        runner._click_region_safe = lambda *_args, **_kwargs: False

        with patch(
            "core.daily_runner.window_manager.find_window",
            return_value=True,
        ):
            ok = runner.run_gacha_probe_test(times=10)

        self.assertFalse(ok)
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(handoffs[0]["total"], 10)

    def test_internal_post_failure_gacha_skips_duplicate_initial_reconnect(
        self,
    ) -> None:
        runner = self._runner()
        runner.run_gacha_reconnect_to_ready = (
            lambda *_args, **_kwargs: self.fail(
                "失败恢复已经完成重连，内部剩余扭蛋不应再次首轮重连"
            )
        )
        runner._click_region_safe = lambda *_args, **_kwargs: False

        with patch(
            "core.daily_runner.window_manager.find_window",
            return_value=True,
        ):
            ok = runner.run_gacha_probe_test(
                times=99,
                failure_handoff=False,
                initial_reconnect=False,
            )

        self.assertFalse(ok)

    def test_csv_persists_session_1and1_timestamp_and_duration(self) -> None:
        runner = self._runner()
        emitted = []
        runner._emit = lambda message, level="INFO": emitted.append((message, level))
        beijing_tz = timezone(timedelta(hours=8))
        session_started = datetime(
            2026, 7, 25, 11, 59, 50, tzinfo=beijing_tz
        )
        first = datetime(2026, 7, 25, 12, 0, 0, tzinfo=beijing_tz)
        second = first + timedelta(seconds=65.25)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "gacha_runs.csv")
            runner._gacha_run_record_csv_path = lambda: path

            self.assertTrue(
                runner._append_gacha_completion_record(
                    "GACHA-first",
                    session_id="GACHA-SESSION-test",
                    session_cycle=1,
                    session_total=2,
                    session_started_at=session_started,
                    completed_at=first,
                )
            )
            self.assertTrue(
                runner._append_gacha_completion_record(
                    "GACHA-second",
                    session_id="GACHA-SESSION-test",
                    session_cycle=2,
                    session_total=2,
                    session_started_at=session_started,
                    completed_at=second,
                    previous_completed_at=first,
                    duration_seconds=65.25,
                    rolling_average_seconds=65.25,
                    trend_delta_seconds=None,
                )
            )

            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["session_id"], "GACHA-SESSION-test")
        self.assertEqual(rows[0]["session_cycle"], "1")
        self.assertEqual(rows[0]["session_total"], "2")
        self.assertEqual(rows[0]["run_id"], "GACHA-first")
        self.assertEqual(rows[0]["timestamp"], first.isoformat(timespec="milliseconds"))
        self.assertEqual(
            rows[0]["one_and_one_cleared_at"],
            first.isoformat(timespec="milliseconds"),
        )
        self.assertEqual(rows[0]["previous_timestamp"], "")
        self.assertEqual(rows[0]["duration_seconds"], "")
        self.assertEqual(rows[0]["duration"], "")
        self.assertEqual(rows[1]["run_id"], "GACHA-second")
        self.assertEqual(
            rows[1]["previous_timestamp"],
            first.isoformat(timespec="milliseconds"),
        )
        self.assertEqual(rows[1]["duration_seconds"], "65.250")
        self.assertEqual(rows[1]["duration"], "00:01:05.250")
        self.assertEqual(rows[1]["rolling_average_seconds"], "65.250")
        self.assertEqual(emitted, [])

    def test_generated_run_ids_are_unique(self) -> None:
        runner = self._runner()
        run_ids = {runner._new_gacha_run_id() for _ in range(100)}

        self.assertEqual(len(run_ids), 100)
        self.assertTrue(all(run_id.startswith("GACHA-") for run_id in run_ids))

    def test_legacy_gacha_csv_is_migrated_without_losing_rows(self) -> None:
        runner = self._runner()
        beijing_tz = timezone(timedelta(hours=8))
        current = datetime(2026, 7, 26, 9, 0, tzinfo=beijing_tz)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "gacha_runs.csv")
            runner._gacha_run_record_csv_path = lambda: path
            with open(path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=(
                        "run_id",
                        "timestamp",
                        "previous_timestamp",
                        "duration_seconds",
                        "duration",
                    ),
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "run_id": "GACHA-legacy",
                        "timestamp": "2026-07-26T08:00:00.000+08:00",
                        "previous_timestamp": "",
                        "duration_seconds": "",
                        "duration": "",
                    }
                )

            self.assertTrue(
                runner._append_gacha_completion_record(
                    "GACHA-new",
                    session_id="GACHA-SESSION-new",
                    session_cycle=1,
                    session_total=1,
                    session_started_at=current,
                    completed_at=current,
                )
            )

            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["run_id"], "GACHA-legacy")
        self.assertEqual(rows[0]["session_id"], "")
        self.assertEqual(rows[1]["run_id"], "GACHA-new")
        self.assertEqual(rows[1]["session_id"], "GACHA-SESSION-new")
        self.assertEqual(
            rows[1]["one_and_one_cleared_at"],
            current.isoformat(timespec="milliseconds"),
        )

    def test_generated_session_ids_are_unique(self) -> None:
        runner = self._runner()
        session_ids = {runner._new_gacha_session_id() for _ in range(100)}

        self.assertEqual(len(session_ids), 100)
        self.assertTrue(
            all(session_id.startswith("GACHA-SESSION-") for session_id in session_ids)
        )

    def test_each_new_gacha_segment_gets_a_new_session(self) -> None:
        runner = self._runner()
        session_ids = iter(("GACHA-SESSION-before", "GACHA-SESSION-after-refresh"))
        recorded_sessions = []
        runner._new_gacha_session_id = lambda: next(session_ids)
        runner._wait_1and1_clear = lambda *_args, **_kwargs: True
        runner._append_gacha_completion_record = (
            lambda _run_id, **kwargs: (
                recorded_sessions.append(kwargs["session_id"]) or True
            )
        )

        with (
            patch("core.daily_runner.window_manager.find_window", return_value=True),
            patch("core.daily_runner.time.sleep", return_value=None),
        ):
            self.assertTrue(
                runner.run_gacha_probe_test(
                    times=1,
                    failure_handoff=False,
                    initial_reconnect=False,
                )
            )
            self.assertTrue(
                runner.run_gacha_probe_test(
                    times=1,
                    failure_handoff=False,
                    initial_reconnect=False,
                )
            )

        self.assertEqual(
            recorded_sessions,
            ["GACHA-SESSION-before", "GACHA-SESSION-after-refresh"],
        )

    def test_normal_gacha_logs_each_cycle_and_1and1_timestamp(self) -> None:
        runner = self._runner()
        emitted = []
        runner._emit = lambda message, level="INFO": emitted.append((message, level))
        runner._wait_1and1_clear = lambda *_args, **_kwargs: True
        runner._append_gacha_completion_record = lambda *_args, **_kwargs: True

        with (
            patch("core.daily_runner.window_manager.find_window", return_value=True),
            patch("core.daily_runner.time.sleep", return_value=None),
        ):
            ok = runner.run_gacha_probe_test(times=5)

        self.assertTrue(ok)
        cycle_starts = [message for message, _level in emitted if "[扭蛋轮次]" in message]
        completions = [message for message, _level in emitted if "[扭蛋完成]" in message]
        self.assertEqual(len(cycle_starts), 5)
        self.assertEqual(len(completions), 5)
        self.assertTrue(all("1AND1清空时间=" in message for message in completions))
        self.assertTrue(all("距上次完成=" in message for message in completions))
        self.assertFalse(any("点击 扭蛋." in message for message, _level in emitted))
        self.assertFalse(any("双探针命中" in message for message, _level in emitted))


if __name__ == "__main__":
    unittest.main()
