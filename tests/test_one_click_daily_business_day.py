import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.daily_runner import DailyRunner


BEIJING_TZ = timezone(timedelta(hours=8))


class OneClickDailyBusinessDayTests(unittest.TestCase):
    def _runner(self, temp_dir: str) -> DailyRunner:
        root = Path(temp_dir)
        runner = DailyRunner.__new__(DailyRunner)
        runner._emit = lambda *_args, **_kwargs: None
        runner._one_click_daily_progress = None
        runner._one_click_daily_record_path = lambda: str(
            root / "one_click_daily_status.json"
        )
        runner._one_click_daily_completion_csv_path = lambda: str(
            root / "one_click_daily_completed.csv"
        )
        return runner

    def test_completed_at_repairs_stale_start_business_day(self) -> None:
        now = datetime(2026, 8, 11, 11, 0, tzinfo=BEIJING_TZ)
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = self._runner(temp_dir)
            Path(runner._one_click_daily_record_path()).write_text(
                json.dumps(
                    {
                        "business_day": "2026-08-10",
                        "status": "complete",
                        "completed_at": "2026-08-11T06:22:58+08:00",
                    }
                ),
                encoding="utf-8",
            )

            self.assertTrue(runner.has_one_click_daily_complete_today(now))

    def test_completion_ledger_survives_running_progress_overwrite(self) -> None:
        now = datetime(2026, 8, 11, 23, 0, tzinfo=BEIJING_TZ)
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = self._runner(temp_dir)
            Path(runner._one_click_daily_record_path()).write_text(
                json.dumps(
                    {
                        "business_day": "2026-08-11",
                        "status": "running",
                        "completed_at": "",
                    }
                ),
                encoding="utf-8",
            )
            Path(runner._one_click_daily_completion_csv_path()).write_text(
                "time,business_day,phase,note\n"
                "2026-08-11T06:22:58.580+08:00,2026-08-11,complete,test\n",
                encoding="utf-8",
            )

            self.assertTrue(runner.has_one_click_daily_complete_today(now))

    def test_finish_complete_uses_completion_business_day_and_appends_ledger(self) -> None:
        completed_at = datetime(2026, 8, 11, 6, 22, 58, tzinfo=BEIJING_TZ)
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = self._runner(temp_dir)
            runner._beijing_now = lambda: completed_at
            runner._one_click_daily_progress = {
                "business_day": "2026-08-10",
                "status": "running",
                "started_at": "2026-08-10T14:06:23+08:00",
            }

            runner.finish_one_click_daily_progress("complete", "all variants complete")

            state = json.loads(
                Path(runner._one_click_daily_record_path()).read_text(encoding="utf-8")
            )
            self.assertEqual(state["business_day"], "2026-08-11")
            self.assertEqual(state["status"], "complete")
            with Path(runner._one_click_daily_completion_csv_path()).open(
                "r", encoding="utf-8-sig", newline=""
            ) as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["business_day"], "2026-08-11")

    def test_pre_six_completion_belongs_to_previous_business_day(self) -> None:
        now = datetime(2026, 8, 11, 5, 59, tzinfo=BEIJING_TZ)
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = self._runner(temp_dir)
            Path(runner._one_click_daily_completion_csv_path()).write_text(
                "time,business_day,phase,note\n"
                "2026-08-11T03:21:37.829+08:00,2026-08-10,complete,test\n",
                encoding="utf-8",
            )

            self.assertTrue(runner.has_one_click_daily_complete_today(now))
            self.assertFalse(
                runner.has_one_click_daily_complete_today(
                    datetime(2026, 8, 11, 6, 0, tzinfo=BEIJING_TZ)
                )
            )


if __name__ == "__main__":
    unittest.main()
