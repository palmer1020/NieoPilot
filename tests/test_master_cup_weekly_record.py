import csv
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from core.daily_runner import DailyRunner


class MasterCupWeeklyRecordTests(unittest.TestCase):
    def test_close_bag_reports_success_for_rotation_handoff(self) -> None:
        calls = []

        class FakeDarRunner:
            def _close_pet_bag_with_verify(self, *args, **kwargs):
                calls.append((args, kwargs))

        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(dar_route_runner=FakeDarRunner())
        runner._emit = lambda *_args, **_kwargs: None

        ok = runner._master_cup_close_bag(
            False,
            threading.Event(),
            log_tag="大师杯完成·取宠后关闭背包",
        )

        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)

    def test_restore_67_success_reaches_master_cup_rotation_handoff(self) -> None:
        close_calls = []

        class FakeDarRunner:
            def open_pickmode_bag_warehouse_from_ready_bag(self, *_args, **_kwargs):
                return True

            def take_pickmode_pets_from_open_bag_warehouse(self, *_args, **_kwargs):
                return True

            def _close_pet_bag_with_verify(self, *args, **kwargs):
                close_calls.append((args, kwargs))

        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(
            dar_route_runner=FakeDarRunner(),
            _stop_event=threading.Event(),
        )
        runner._emit = lambda *_args, **_kwargs: None
        runner._master_cup_open_bag_ready = lambda *_args, **_kwargs: True
        runner._master_cup_put_back_cyan_from_open_bag = (
            lambda *_args, **_kwargs: True
        )
        runner._master_cup_close_warehouse_keep_bag_open = (
            lambda *_args, **_kwargs: True
        )

        ok = runner._master_cup_replace_cyan_from_current_bag(
            object(),
            {"pet_id": 67},
            False,
            log_tag="大师杯完成·青色换回67",
            set_cyan_primary=False,
            set_follow_purple=False,
            recover_target_after_take=False,
        )

        self.assertTrue(ok)
        self.assertEqual(len(close_calls), 1)

    def test_yellow_cleanup_skips_pet_recovery(self) -> None:
        runner = DailyRunner.__new__(DailyRunner)
        runner._emit = lambda *_args, **_kwargs: None
        runner._wait_for_1and1_cleanup = lambda *_args, **_kwargs: True
        runner._recover_pet_one = lambda *_args, **_kwargs: self.fail(
            "最终黄色成功后不应恢复精灵"
        )

        ok = runner._master_cup_recover_after_result(
            {},
            False,
            probe_result="yellow",
            tag="大师杯测试",
        )

        self.assertTrue(ok)

    def test_append_and_read_current_business_week_record(self) -> None:
        beijing_tz = timezone(timedelta(hours=8))
        completed_at = datetime(2026, 7, 24, 9, 49, 31, 225000, beijing_tz)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "master_cup_weekly.csv"
            runner = DailyRunner.__new__(DailyRunner)
            runner._emit = lambda *_args, **_kwargs: None
            runner._beijing_now = lambda: completed_at
            runner._master_cup_weekly_record_csv_path = lambda: str(path)

            self.assertTrue(
                runner.append_master_cup_weekly_record(
                    cup_type="电系",
                    norm_ran=False,
                    yellow_target=72,
                    pre_setup=True,
                    note="测试",
                )
            )
            record = runner.get_master_cup_weekly_record(completed_at)

            self.assertIsNotNone(record)
            self.assertEqual(record["cup_type"], "电系")
            self.assertEqual(record["norm_ran"], "false")
            self.assertEqual(record["business_week"], "2026-W30")
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["time"], "2026-07-24T09:49:31.225+08:00")

    def test_previous_business_week_is_not_current(self) -> None:
        beijing_tz = timezone(timedelta(hours=8))
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "master_cup_weekly.csv"
            path.write_text(
                "time,business_week,phase,cup_type,norm_ran,yellow_target,"
                "pre_setup,note\n"
                "2026-07-19T09:00:00.000+08:00,2026-W29,complete,草系,"
                "true,72,true,旧记录\n",
                encoding="utf-8",
            )
            runner = DailyRunner.__new__(DailyRunner)
            runner._emit = lambda *_args, **_kwargs: None
            runner._master_cup_weekly_record_csv_path = lambda: str(path)

            record = runner.get_master_cup_weekly_record(
                datetime(2026, 7, 24, 12, 0, tzinfo=beijing_tz)
            )

            self.assertIsNone(record)


if __name__ == "__main__":
    unittest.main()
