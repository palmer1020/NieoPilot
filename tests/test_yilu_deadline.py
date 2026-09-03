import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from core.bot_thread import BotWorker
from core.daily_runner import DailyRunner


BEIJING = timezone(timedelta(hours=8))


class YiluDeadlineTests(unittest.TestCase):
    def test_lanlan_only_restores_npc_swf_before_preparing_mode_swf(self):
        calls = []
        worker = BotWorker.__new__(BotWorker)
        worker.stop_current = False
        worker._stop_event = threading.Event()
        worker.emit_and_log = lambda *_args, **_kwargs: None
        worker.daily_runner = SimpleNamespace()
        worker.dar_route_runner = SimpleNamespace()
        worker._lanlan_cyan_pet_for_current_time = lambda: 347

        def restore_npc(tasks, log_tag=""):
            calls.append(("restore", dict(tasks), log_tag))
            return True

        def stop_after_prepare(*_args, **_kwargs):
            calls.append(("prepare",))
            return False

        worker._ensure_newnpc_multi_90000_for_task = restore_npc
        worker._prepare_hourly_daily_swf = stop_after_prepare

        ok = worker.run_hourly_yilu_lanlan_maintenance(
            use_foreground=False,
            run_yilu=False,
            run_lanlan=True,
        )

        self.assertFalse(ok)
        self.assertEqual(calls[0][0], "restore")
        self.assertEqual(calls[0][1], {"lanlan_mode": True})
        self.assertEqual(calls[1][0], "prepare")

    def test_lanlan_stops_before_prepare_when_npc_restore_fails(self):
        calls = []
        worker = BotWorker.__new__(BotWorker)
        worker.stop_current = False
        worker._stop_event = threading.Event()
        worker.emit_and_log = lambda *_args, **_kwargs: None
        worker.daily_runner = SimpleNamespace()
        worker.dar_route_runner = SimpleNamespace()
        worker._lanlan_cyan_pet_for_current_time = lambda: 347
        worker._ensure_newnpc_multi_90000_for_task = (
            lambda *_args, **_kwargs: False
        )
        worker._prepare_hourly_daily_swf = (
            lambda *_args, **_kwargs: calls.append("prepare") or True
        )

        ok = worker.run_hourly_yilu_lanlan_maintenance(
            use_foreground=False,
            run_yilu=False,
            run_lanlan=True,
        )

        self.assertFalse(ok)
        self.assertEqual(calls, [])

    def test_orange_scan_stops_immediately_at_window_deadline(self):
        emitted = []
        deadline = datetime(2026, 8, 14, 9, 10, tzinfo=BEIJING)
        runner = DailyRunner.__new__(DailyRunner)
        runner._emit = lambda text, level="INFO": emitted.append((level, text))
        runner._should_abort = lambda: False
        runner._beijing_now = lambda: deadline

        result = runner._wait_yilu_orange_point(
            object(),
            log_tag="依卢截止测试",
            timeout_s=45.0,
            window_deadline=deadline,
        )

        self.assertIsNone(result)
        self.assertTrue(any("窗口已于 09:10:00 结束" in text for _, text in emitted))
        self.assertFalse(any("等待依卢橙色点超时" in text for _, text in emitted))

    def test_petitem_wait_also_obeys_deadline(self):
        emitted = []
        deadline = datetime(2026, 8, 14, 9, 10, tzinfo=BEIJING)
        framework = SimpleNamespace()

        def stage2(**kwargs):
            self.assertTrue(kwargs["config"].abort_check())
            return False, None

        framework.stage2_calibration_and_petitem = stage2
        runner = DailyRunner.__new__(DailyRunner)
        runner.bot = SimpleNamespace(
            _stop_event=threading.Event(),
            stop_current=False,
            dar_route_runner=SimpleNamespace(
                _switch_pet_for_rare_mode=lambda *_args, **_kwargs: True
            ),
        )
        runner._emit = lambda text, level="INFO": emitted.append((level, text))
        runner._should_abort = lambda: False
        runner._beijing_now = lambda: deadline
        runner._ensure_unified_framework = lambda _regions: True
        runner._unified_framework = framework

        ok = runner._run_yilu_rare_battle(
            object(),
            False,
            log_tag="依卢截止测试",
            initial_cursor=0,
            window_deadline=deadline,
        )

        self.assertFalse(ok)
        self.assertTrue(any("窗口已于 09:10:00 结束" in text for _, text in emitted))

    def test_hourly_worker_rejects_stale_yilu_before_swf_or_reconnect(self):
        emitted = []
        now = datetime(2026, 8, 14, 9, 10, tzinfo=BEIJING)
        worker = BotWorker.__new__(BotWorker)
        worker.stop_current = False
        worker._stop_event = threading.Event()
        worker.emit_and_log = lambda text, level="INFO": emitted.append((level, text))
        worker.daily_runner = SimpleNamespace(_beijing_now=lambda: now)
        worker.dar_route_runner = SimpleNamespace()
        worker._prepare_hourly_daily_swf = lambda *_args, **_kwargs: self.fail(
            "过期依卢不应准备SWF"
        )

        ok = worker.run_hourly_yilu_lanlan_maintenance(
            use_foreground=False,
            run_yilu=True,
            run_lanlan=False,
            yilu_deadline=now,
        )

        self.assertTrue(ok)
        self.assertTrue(any("窗口已于 09:10:00 结束" in text for _, text in emitted))


if __name__ == "__main__":
    unittest.main()
