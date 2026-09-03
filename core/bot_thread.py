# core/bot_thread.py
import math
import os
import shutil
import time
import threading
from datetime import datetime, time as dt_time
from typing import Callable, Dict, Optional

from PyQt6.QtCore import QThread, pyqtSignal

from core.utils import window_manager
from core.region_store import RegionStore
from core.daily_runner import (
    DailyRunner,
    DEFAULT_HERO_TOWER_BATTLES,
    GACHA_RECONNECT_EARLY_FAILURE_CYCLES,
    MASTER_CUP_568_TYPES,
    NEW_DAILY_SEQ9_SCRIPT_91_NAME,
)
from core.battle_runner import BattleRunner
from core.training_level_runner import TrainingLevelRunner

# 野外捕捉（螳螂/稀有精灵）
from core.dar_route_runner import (
    DarRouteRunner,
    DEFAULT_PROFILE_MANTIS,
    DEFAULT_PROFILE_DUGULU,
    DEFAULT_PROFILE_SHUANGTA,
    DEFAULT_PROFILE_XIAODOUYA,
    DEFAULT_PROFILE_FLASH_PIPI,
    EYEBALL_PROFILE,
    WildCaptureProfile,
)
from core.wild_mode_registry import BUILTIN_WILD_PROFILE_KEYS, resolve_wild_capture_profile

_BUILTIN_WILD_KEYS = BUILTIN_WILD_PROFILE_KEYS
NIEO_RESOURCE_CHAIN_SLUGS = (
    "晶化气泡",
    "露西之核",
    "水生海草",
    "贝壳精华",
    "水之精华",
)


class _DeadlineStopEvent(threading.Event):
    """Event that also becomes set when a task deadline or parent stop is reached."""

    def __init__(self, parent: threading.Event, deadline: datetime, now_fn: Callable[[], datetime]):
        super().__init__()
        self._parent = parent
        self._deadline = deadline
        self._now_fn = now_fn

    def deadline_reached(self) -> bool:
        return self._now_fn() >= self._deadline

    def is_set(self) -> bool:
        return super().is_set() or self._parent.is_set() or self.deadline_reached()

    def wait(self, timeout: Optional[float] = None) -> bool:
        end_at = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not self.is_set():
            if end_at is not None and time.monotonic() >= end_at:
                return False
            time.sleep(0.05)
        return True

# 已移除 CalibrationTestRunner（438测试功能已删除）


class BotWorker(QThread):
    log_signal = pyqtSignal(str, str)      # (text, level)
    state_signal = pyqtSignal(str)         # "IDLE" / "RUNNING"
    task_done_signal = pyqtSignal()        # Dashboard 解锁用
    def __init__(self, project_root: str):
        super().__init__()
        self.project_root = project_root

        self._engine_alive = True

        # Set by DailyRunner when the daily-chain chaos battle times out.
        self.rotation_handoff_after_chaos_timeout = False
        self._gacha_rotation_handoff_started = False
        self.new_daily_chain_completed = False

        # Runtime state.
        self.is_running = False
        self.is_paused = False
        self.stop_current = False
        self.user_stop_requested = False
        self._task_swf_full_base_done = False

        self._task_lock = threading.Lock()
        self.active_tasks: Dict = {}

        self._stop_event = threading.Event()

        # Paths.
        from config import REGIONS_PATH, TEMPLATES_PATH
        region_root = REGIONS_PATH
        template_root = TEMPLATES_PATH

        self.regions = self._init_region_store(region_root)

        # Runners.
        self.daily_runner = DailyRunner(self)
        self.battle_runner = BattleRunner(self, self.regions, template_root)
        self.training_level_runner = TrainingLevelRunner(
            self, self.regions, template_root, battle_runner=self.battle_runner
        )

        self.dar_route_runner = self._init_dar_route_runner(template_root)

        self.listener = None
        self._pynput_ok = False

    # --------------------
    # init helpers
    # --------------------
    def _init_region_store(self, region_root: str):
        try:
            return RegionStore(region_root=region_root)
        except TypeError:
            pass
        try:
            return RegionStore(project_root=self.project_root)
        except TypeError:
            pass
        return RegionStore(region_root)

    def _init_dar_route_runner(self, template_root: str):
        """
        创建 DarRouteRunner，优先使用新版构造参数。

        template_root 仅保留用于兼容旧调用，不再传入 DarRouteRunner。
        """
        ctors = [
            lambda: DarRouteRunner(self, self.regions, self.battle_runner, map_swf_id=11),
            lambda: DarRouteRunner(self, self.regions, self.battle_runner),
            lambda: DarRouteRunner(self, self.regions),
        ]

        last_err = None
        for fn in ctors:
            try:
                obj = fn()
            except TypeError as e:
                last_err = e
                continue

            br = getattr(obj, "battle_runner", None)

            # ✅ 没 battle_runner 字段也放行（极老版本）
            if br is None:
                return obj

            # ✅ 必须是 BattleRunner 或至少有这个方法
            if hasattr(br, "run_mantis_capture_mode"):
                return obj

            last_err = TypeError(
                f"DarRouteRunner init failed: unexpected battle_runner type {type(br)}"
            )

        raise last_err


    # --------------------
    # task control
    # --------------------
    def set_tasks(self, tasks: dict):
        with self._task_lock:
            self.active_tasks = dict(tasks or {})

        self.stop_current = False
        self.is_paused = False
        self._stop_event.clear()

    _LOG_TEXT_REPLACEMENTS = (
        ("[pynput]", "[键盘监听]"),
        ("[keyboard]", "[键盘]"),
        ("[stop]", "[停止]"),
        ("[daily-chain]", "[一键日常]"),
        ("[new-daily-chain]", "[一键新日常]"),
        ("[rotation-mode]", "[轮换模式]"),
        ("[rotation-test]", "[轮换测试]"),
        ("[rotation-chain-test]", "[轮换链测试]"),
        ("[battle-chain]", "[对战链]"),
        ("[gacha-test]", "[抽奖测试]"),
        ("[hero-tower]", "[勇者之塔]"),
        ("[shanni-energy-drain]", "[闪尼吸能]"),
        ("[honor-exchange]", "[荣誉兑换]"),
        ("[chaos-battle-x2]", "[大乱斗x2]"),
        ("[1v1-x2]", "[1v1x2]"),
        ("[exp-minor-battle]", "[小号刷经验]"),
        ("[nono-soul-fusion-check]", "[nono孵化检测]"),
        ("[psychic-exp]", "[超能经验]"),
        ("[training-battle]", "[训练对战]"),
        ("[training-until-level]", "[训练升级到目标]"),
        ("[training-level]", "[训练升级]"),
        ("[teixun-loop]", "[特训循环]"),
        ("[dar-route-test]", "[稀有路线测试]"),
        ("[wild-capture]", "[野外捕捉]"),
        ("[wild capture]", "[野外捕捉]"),
        ("[smart-tracking-test]", "[智能追踪测试]"),
        ("[custom-nieo]", "[自定义尼奥]"),
        ("[pure-energy]", "[纯净能量]"),
        ("[pure-energy-resource]", "[纯净能量资源]"),
        ("[nieo-mode]", "[尼奥模式]"),
        ("[nieo-resource]", "[尼奥资源]"),
        ("[nieo-pre]", "[尼奥前置]"),
        ("[afk-battle]", "[挂机对战]"),
        ("[event-pet]", "[活动精灵]"),
        ("[pinnacle]", "[巅峰对战]"),
        ("[nie-family-test]", "[尼尔家族测试]"),
        ("[map]", "[地图]"),
        ("[route]", "[路线]"),
        ("[run]", "[运行]"),
        ("[time]", "[时间]"),
        ("[flash-pipi]", "[闪光皮皮]"),
        ("[eyeball]", "[眼球]"),
        ("[wusuo]", "[乌索]"),
        ("[shuangta]", "[双塔]"),
        ("[dugulu]", "[嘟咕噜]"),
        ("[map-mismatch]", "[地图不匹配]"),
        ("[no-steady-reconnect]", "[未稳定重连]"),
        ("[reconnect-restart]", "[重连重启]"),
        ("[reconnect-check]", "[重连检测]"),
        ("[reconnect-check-after-escape]", "[逃跑后重连检测]"),
        ("[calibration-fallback]", "[校准兜底]"),
        ("[post-calibration-map]", "[校准后地图]"),
        ("[post-calibration-reconnect]", "[校准后重连]"),
        ("[post-battle-recovery]", "[战后恢复]"),
        ("[normal-1and1]", "[普通1AND1]"),
        ("[enemy monitor]", "[敌方监控]"),
        ("[enemy-info]", "[敌方信息]"),
        ("[enemy-info-monitor]", "[敌方信息监控]"),
        ("[rare switch]", "[稀有切换]"),
        ("[rare switch probe]", "[稀有切换探针]"),
        ("[rotation]", "[轮换]"),
        ("[release]", "[放生]"),
        ("[stats]", "[统计]"),
        ("[select-four]", "[选择四技能]"),
        ("[unexpected-pet]", "[异常精灵]"),
        ("[post-calibration-map]", "[校准后地图]"),
        ("[WARN]", "[警告]"),
        ("[OK]", "[成功]"),
        ("[ERR]", "[错误]"),
        ("ESC stop requested", "ESC 请求停止"),
        ("current task stop requested", "已请求停止当前任务"),
        ("Space resume", "空格恢复"),
        ("F1 paused", "F1 已暂停"),
        ("F1 resumed", "F1 已恢复"),
        ("start login pipeline", "启动登录流程"),
        ("login pipeline complete", "登录流程完成"),
        ("login pipeline failed", "登录流程失败"),
        ("run pre-daily handoff", "执行日常前置交接"),
        ("before variant", "在方案前"),
        ("complete; start", "完成，启动"),
        ("pre reconnect failed", "前置重连失败"),
        ("retry full flow", "重试完整流程"),
        ("retry after refresh", "刷新后重试"),
        ("wait timed out", "等待超时"),
        ("timed out", "超时"),
        ("timeout", "超时"),
        ("stopped", "已停止"),
        ("incomplete", "未完成"),
        ("complete", "完成"),
        ("finished", "完成"),
        ("failed", "失败"),
        ("started", "已启动"),
        ("reconnect", "重连"),
        ("restart", "重启"),
        ("start", "启动"),
        ("waiting", "等待"),
        ("wait", "等待"),
        ("game window not found", "未找到游戏窗口"),
        ("missing region", "缺少区域"),
        ("missing/click failed", "缺少区域或点击失败"),
        ("exception", "异常"),
        ("traceback", "异常堆栈"),
        ("启动ed", "已启动"),
        ("完成d", "已完成"),
        (" foreground=", " 前台点击="),
        (" background=", " 后台="),
        (" test=", " 测试模式="),
        (" rare=", " 稀有="),
        (" profile=", " 方案="),
        (" rare_slot=", " 稀有槽="),
        (" resource=", " 资源="),
        (" variant=", " 方案="),
        (" step=", " 步骤="),
        (" skip_tower=", " 跳过勇者之塔="),
        (" current=", " 当前="),
        (" next=", " 下次="),
        (" target=", " 目标="),
        (" batch=", " 批次="),
        (" recover_every=", " 恢复间隔="),
        (" debug_stop=", " 调试停止="),
    )

    @classmethod
    def _localize_log_text(cls, text: str) -> str:
        if not isinstance(text, str):
            text = str(text)
        for old, new in cls._LOG_TEXT_REPLACEMENTS:
            text = text.replace(old, new)
        return text

    def emit_and_log(self, text: str, level: str = "INFO"):
        """Send runtime logs to the Dashboard without blocking automation.

        Do not mirror these messages through ``logging`` here.  On Windows a
        console in QuickEdit/selection mode can block ``WriteConsoleW``
        indefinitely.  Because this method runs on the automation QThread,
        such a synchronous write also stops battle scanning and its timeout
        checks.  The Dashboard owns runtime-log display and export.
        """
        text = self._localize_log_text(text)
        try:
            self.log_signal.emit(text, level)
        except Exception:
            pass

    # --------------------
    # keyboard listener
    # --------------------
    def _start_keyboard_listener(self):
        if self.listener:
            return

        try:
            from pynput import keyboard
            self._pynput_ok = True
        except Exception as e:
            self._pynput_ok = False
            self.emit_and_log(f"[pynput] 不可用，ESC/空格监听关闭: {e}", "WARN")
            return

        def on_key_press(key):
            try:
                if not self.is_running:
                    return

                if key == keyboard.Key.esc:
                    self.stop()
                    self.emit_and_log("[keyboard] ESC stop requested", "SYSTEM")
                    return

                if key == keyboard.Key.space:
                    if self.is_paused:
                        self.is_paused = False
                        self.emit_and_log("[keyboard] Space resume", "SYSTEM")
                    return

                if key == keyboard.Key.f1:
                    self.is_paused = not self.is_paused
                    state = "paused" if self.is_paused else "resumed"
                    self.emit_and_log(f"[keyboard] F1 {state}", "SYSTEM")
                    return

            except Exception as e:
                self.emit_and_log(f"[keyboard] listener error: {e}", "ERROR")

        self.listener = keyboard.Listener(on_press=on_key_press)
        self.listener.daemon = True
        self.listener.start()

    def _stop_keyboard_listener(self):
        if self.listener:
            try:
                self.listener.stop()
            except Exception:
                pass
        self.listener = None

    def stop(self):
        self.user_stop_requested = True
        self.stop_current = True
        self._stop_event.set()
        self.is_paused = False
        self.emit_and_log("[停止] 已请求停止当前任务", "SYSTEM")

    def shutdown(self):
        self._engine_alive = False
        self.user_stop_requested = True
        self.stop_current = True
        self._stop_event.set()
        self.is_paused = False
        self._stop_keyboard_listener()

    def wait_if_paused(self):
        while self.is_paused and self.is_running and (not self.stop_current):
            time.sleep(0.05)

    def _run_dar_mode_with_restart(
        self,
        label: str,
        run_once: Callable[[], object],
        prepare_after_restart: Optional[Callable[[], bool]] = None,
    ) -> None:
        """Run a Dar mode repeatedly from the task boundary, never recursively."""
        while not self.user_stop_requested and not self.stop_current:
            run_once()
            restart_reason = self.dar_route_runner.consume_mode_restart_request(
                self._stop_event
            )
            if restart_reason is None:
                return
            self.emit_and_log(
                f"🔄 [{label}] 外层重启模式：{restart_reason}",
                "SYSTEM",
            )
            if prepare_after_restart is not None:
                while not self.user_stop_requested and not self.stop_current:
                    if prepare_after_restart():
                        break
                    if self._stop_event.is_set():
                        return
                    self.emit_and_log(
                        f"⚠️ [{label}] 重启前置未完成，继续重试",
                        "WARN",
                    )
                    time.sleep(1.0)
                if self.user_stop_requested or self.stop_current:
                    return
            time.sleep(0.2)

    def _prepare_daily_mode_after_restart(
        self,
        label: str,
        use_foreground: bool,
    ) -> bool:
        """Reconnect to a known base/map state before rerunning a DailyRunner mode."""
        self._clear_game_tmp_cache(log_tag=f"{label}重启")
        return bool(
            self.dar_route_runner.run_refresh_login_until_map(
                use_foreground,
                self._stop_event,
                include_base_and_map_gate=True,
            )
        )

    def _run_daily_mode_with_restart(
        self,
        label: str,
        run_once: Callable[[], object],
        use_foreground: bool,
    ) -> None:
        """Enable DailyRunner restart signaling only inside a guarded task boundary."""
        def guarded_run_once():
            previous = bool(
                getattr(self.daily_runner, "_outer_mode_restart_enabled", False)
            )
            self.daily_runner._outer_mode_restart_enabled = True
            try:
                return run_once()
            finally:
                self.daily_runner._outer_mode_restart_enabled = previous

        self._run_dar_mode_with_restart(
            label,
            guarded_run_once,
            prepare_after_restart=lambda: self._prepare_daily_mode_after_restart(
                label,
                use_foreground,
            ),
        )

    def _prepare_swf_fill_union(self) -> None:
        """Run the Pet254 preparation once per task."""
        self.dar_route_runner.ensure_swf_union_filled(log_tag="启动")

    def _prepare_swf_fill_union_nieo(self) -> None:
        """Run the Nieo Pet254 preparation once per task."""
        self.dar_route_runner.ensure_swf_union_filled(log_tag="启动", nieo_pet254=True)

    def _prepare_swf_wild(self, profile) -> None:
        """Run wild SWF preparation once per task/profile."""
        self.dar_route_runner.ensure_wild_profile_swf(profile, log_tag="启动")

    def _run_nieo_resource_chain(
        self,
        use_foreground: bool,
        *,
        single_map: bool,
        follow_cyan: bool = False,
    ) -> bool:
        """Run the fixed five-resource chain, requiring 60 yellow wins per mode."""
        from core.nieo_mode_registry import get_profile, load_all_nieo_modes

        load_all_nieo_modes(self.project_root, reload=True)
        profiles = [
            get_profile(self.project_root, slug)
            for slug in NIEO_RESOURCE_CHAIN_SLUGS
        ]
        missing = [
            slug
            for slug, profile in zip(NIEO_RESOURCE_CHAIN_SLUGS, profiles)
            if profile is None
        ]
        if missing:
            self.emit_and_log(
                f"❌ [资源五连] 缺少模式配置：{', '.join(missing)}",
                "ERROR",
            )
            return False

        try:
            for index, profile in enumerate(profiles, start=1):
                if (
                    self.user_stop_requested
                    or self.stop_current
                    or self._stop_event.is_set()
                ):
                    return False

                is_last = index == len(profiles)
                completion_action = (
                    "进入普通轮换重连模式"
                    if is_last
                    else "切换到下一个资源模式"
                )
                self.dar_route_runner.configure_resource_yellow_rotation_handoff(
                    True,
                    completion_action=completion_action,
                )
                self.emit_and_log(
                    f"🌊 [资源五连 {index}/{len(profiles)}] "
                    f"启动 {profile.name}，黄色胜利目标 60 次",
                    "SYSTEM",
                )

                while (
                    not self.user_stop_requested
                    and not self.stop_current
                    and not self._stop_event.is_set()
                ):
                    if self.dar_route_runner._execute_nieo_pre_rotation_reconnect(
                        use_foreground=use_foreground,
                        stop_event=self._stop_event,
                        pem_route=False,
                        to_script=profile.to_script,
                        expected_map_id=profile.map_a_id,
                        reason=f"资源五连-{profile.name}-前置",
                        follow_cyan=follow_cyan,
                    ):
                        break
                    self.emit_and_log(
                        f"⚠️ [资源五连] {profile.name} 前置重连失败，继续重试",
                        "WARN",
                    )

                if (
                    self.user_stop_requested
                    or self.stop_current
                    or self._stop_event.is_set()
                ):
                    return False

                mode_single_map = bool(single_map or profile.stay_on_b_map)
                self._run_dar_mode_with_restart(
                    f"资源五连-{profile.name}",
                    lambda profile=profile, mode_single_map=mode_single_map: (
                        self.dar_route_runner.run_configured_nieo_mode(
                            profile,
                            stop_event=self._stop_event,
                            use_foreground=use_foreground,
                            skip_nie_77=False,
                            single_map=mode_single_map,
                            follow_cyan=follow_cyan,
                        )
                    ),
                    prepare_after_restart=(
                        lambda profile=profile: (
                            self.dar_route_runner._execute_nieo_pre_rotation_reconnect(
                                use_foreground=use_foreground,
                                stop_event=self._stop_event,
                                pem_route=False,
                                to_script=profile.to_script,
                                expected_map_id=profile.map_a_id,
                                reason=f"资源五连-{profile.name}-模式重启",
                                follow_cyan=follow_cyan,
                            )
                        )
                    ),
                )

                if not self.dar_route_runner.consume_resource_yellow_rotation_handoff():
                    if (
                        not self.user_stop_requested
                        and not self.stop_current
                        and not self._stop_event.is_set()
                    ):
                        self.emit_and_log(
                            f"❌ [资源五连] {profile.name} 未达到黄色胜利 60 次，"
                            "终止后续模式和轮换交接",
                            "ERROR",
                        )
                    return False

                self.emit_and_log(
                    f"✅ [资源五连 {index}/{len(profiles)}] "
                    f"{profile.name} 黄色胜利 60 次已完成",
                    "SUCCESS",
                )
        finally:
            self.dar_route_runner.configure_resource_yellow_rotation_handoff(False)

        return True

    def _prepare_fusion_special_pet254(self) -> bool:
        try:
            self._clear_game_tmp_cache(log_tag="融合SWF")
            from core.swf_resource_ops import sync_fusion_pet_254_set

            ok, msg = sync_fusion_pet_254_set(
                runtime_subset=self._task_swf_should_use_union()
            )
            if ok:
                self._mark_task_swf_base_done()
            self.emit_and_log(
                f"{'✅' if ok else '❌'} [融合SWF] {msg}",
                "SUCCESS" if ok else "ERROR",
            )
            return bool(ok)
        except Exception as e:
            self.emit_and_log(f"❌ [融合SWF] 准备异常：{e}", "ERROR")
            return False

    def _run_nono_fusion_pre_mode_check(self, use_foreground: bool, mode_name: str):
        """Run a due Nono fusion before a rare/Nieo/resource mode starts."""
        retry_round = 0
        while not self.stop_current and not self._stop_event.is_set():
            try:
                ok, handled = self.daily_runner.run_nono_soul_fusion_pre_mode_check(
                    use_foreground=use_foreground,
                    mode_name=mode_name,
                )
                if ok:
                    return True, handled
            except Exception as e:
                self.emit_and_log(f"❌ [{mode_name}-融合前检查] 执行异常：{e}", "ERROR")
            retry_round += 1
            if self.stop_current or self._stop_event.is_set():
                break
            self.emit_and_log(
                f"⚠️ [{mode_name}-融合前检查] 未完成，不退出模式；完整重连后继续重试（第 {retry_round} 次）",
                "WARN",
            )
            time.sleep(1.0)
        return False, False

    def _resolve_wild_capture_profile(self, profile_name: str) -> WildCaptureProfile:
        return resolve_wild_capture_profile(self.project_root, profile_name)

    @staticmethod
    def _wild_profile_supports_pre_reconnect(profile_name: str, profile: WildCaptureProfile) -> bool:
        if profile_name in _BUILTIN_WILD_KEYS:
            return True
        if getattr(profile, "slug", None):
            map_zero = getattr(profile, "map_zero_id", None)
            if map_zero is not None:
                try:
                    return int(map_zero) != int(profile.map_swf_id)
                except (TypeError, ValueError):
                    pass
            return bool(getattr(profile, "to_script", None))
        return False

    @staticmethod
    def _wild_uses_unified_pre(profile_name: str, profile: WildCaptureProfile) -> bool:
        if profile_name in ("dugulu", "shuangta", "xiaodouya", "eyeball"):
            return True
        if getattr(profile, "slug", None):
            map_zero = getattr(profile, "map_zero_id", None)
            if map_zero is not None:
                try:
                    return int(map_zero) != int(profile.map_swf_id)
                except (TypeError, ValueError):
                    pass
        return False

    @staticmethod
    def _parse_int(v, default=None):
        if v is None:
            return default
        try:
            return int(v)
        except Exception:
            return default

    def request_gacha_recovery_after_failure(
        self,
        *,
        total: int,
        completed_cycles: int,
        session_after_reconnect: bool = False,
        use_foreground: bool,
        reason: str,
    ) -> bool:
        """扭蛋失败先重连恢复；重连后前三次内再失败才升级到轮换。"""
        if self.user_stop_requested or self.stop_current or self._stop_event.is_set():
            return False

        original_total = max(1, int(total))
        completed_before_reconnect = max(0, int(completed_cycles))
        remaining = max(1, original_total - completed_before_reconnect)
        reconnect_round = 0
        self.emit_and_log(
            f"🔄 [扭蛋重连] 首次失败前已完成{completed_before_reconnect}次，"
            f"剩余{remaining}次；失败原因：{reason}",
            "SYSTEM",
        )

        if session_after_reconnect:
            failed_attempt = completed_before_reconnect + 1
            if failed_attempt <= GACHA_RECONNECT_EARLY_FAILURE_CYCLES:
                self.emit_and_log(
                    f"🔄 [扭蛋转轮换] 首次重连后第{failed_attempt}次内即失败，"
                    f"执行轮换重连：{reason}",
                    "SYSTEM",
                )
                self._run_gacha_rotation_handoff(
                    total=original_total,
                    use_foreground=use_foreground,
                    reason=f"首次重连后第{failed_attempt}次失败：{reason}",
                )
                return False
            self.emit_and_log(
                f"⚠️ [扭蛋重连] 首次重连后已连续完成"
                f"{completed_before_reconnect}次，第{failed_attempt}次才失败；"
                f"不直接轮换，继续扭蛋重连",
                "WARN",
            )

        while not self.user_stop_requested:
            reconnect_round += 1
            self.emit_and_log(
                f"🔄 [扭蛋重连] 第{reconnect_round}轮：重连屏蔽→瞭望露台→"
                "跟随紫色→荣誉兑换.to扭蛋",
                "SYSTEM",
            )
            reconnect_ok = bool(
                self.daily_runner.run_gacha_reconnect_to_ready(
                    use_foreground,
                    reconnect_round=reconnect_round,
                )
            )
            if not reconnect_ok:
                if self.user_stop_requested:
                    return False
                self.stop_current = False
                self._stop_event.clear()
                self.emit_and_log(
                    f"⚠️ [扭蛋重连] 第{reconnect_round}轮恢复入口失败，继续重连",
                    "WARN",
                )
                time.sleep(1.0)
                continue

            segment_ok = bool(
                self.daily_runner.run_gacha_probe_test(
                    times=remaining,
                    background_mode=(not use_foreground),
                    failure_handoff=False,
                    initial_reconnect=False,
                )
            )
            if segment_ok:
                self.emit_and_log(
                    f"✅ [扭蛋重连] 第{reconnect_round}轮已完成剩余{remaining}次扭蛋",
                    "SUCCESS",
                )
                return True
            if self.user_stop_requested:
                return False

            segment_completed = max(
                0,
                int(
                    getattr(
                        self.daily_runner,
                        "_last_gacha_completed_cycles",
                        0,
                    )
                    or 0
                ),
            )
            segment_reason = str(
                getattr(self.daily_runner, "_last_gacha_failure_reason", "")
                or "未知失败"
            )
            failed_attempt = segment_completed + 1
            remaining = max(1, remaining - segment_completed)
            if failed_attempt <= GACHA_RECONNECT_EARLY_FAILURE_CYCLES:
                self.emit_and_log(
                    f"🔄 [扭蛋转轮换] 第{reconnect_round}轮重连后第"
                    f"{failed_attempt}次内再次失败，执行轮换重连：{segment_reason}",
                    "SYSTEM",
                )
                self._run_gacha_rotation_handoff(
                    total=original_total,
                    use_foreground=use_foreground,
                    reason=(
                        f"扭蛋重连第{reconnect_round}轮在第{failed_attempt}次失败："
                        f"{segment_reason}"
                    ),
                )
                return False

            self.emit_and_log(
                f"⚠️ [扭蛋重连] 第{reconnect_round}轮连续完成"
                f"{segment_completed}次后才失败，不升级轮换；剩余{remaining}次，"
                f"再次执行扭蛋重连：{segment_reason}",
                "WARN",
            )
        return False

    def _run_gacha_rotation_handoff(
        self,
        *,
        total: int,
        use_foreground: bool,
        reason: str,
    ) -> bool:
        if self.user_stop_requested or self.stop_current or self._stop_event.is_set():
            return False
        if bool(getattr(self, "_gacha_rotation_handoff_started", False)):
            return True
        self._gacha_rotation_handoff_started = True
        with self._task_lock:
            tasks = dict(self.active_tasks)

        self.emit_and_log(
            f"🔄 [扭蛋转轮换] 扭蛋共{int(total)}次，{reason}，开始轮换模式",
            "SYSTEM",
        )
        self._ensure_newnpc_multi_4_hidden(log_tag="gacha-to-rotation")
        is_test_mode = bool(tasks.get("rotation_test_mode", False))
        interval_minutes_nieo = float(
            tasks.get("rotation_interval_minutes_nieo", 60.0) or 60.0
        )
        interval_minutes_shuangta = float(
            tasks.get("rotation_interval_minutes_shuangta", 60.0) or 60.0
        )
        hard_limit_sec = float(tasks.get("petswf_hard_limit_sec", 8.5) or 8.5)
        if is_test_mode:
            self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_NIEO = (
                interval_minutes_nieo
            )
            self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_SHUANGTA = (
                interval_minutes_shuangta
            )
            self.dar_route_runner.PETSWF_TO_PETITEM_HARD_LIMIT_SEC = hard_limit_sec
        else:
            self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_NIEO = 60.0
            self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_SHUANGTA = 60.0
            self.dar_route_runner.PETSWF_TO_PETITEM_HARD_LIMIT_SEC = 8.5

        return bool(
            self.dar_route_runner.run_rotation_mode(
                stop_event=self._stop_event,
                use_foreground=use_foreground,
                is_test_mode=is_test_mode,
                rotation_rare_slot=str(
                    tasks.get("rotation_rare_slot") or "shuangta"
                ).strip().lower(),
                rotation_resource_enabled=bool(
                    tasks.get("rotation_resource_enabled")
                ),
                rotation_resource_slug=str(
                    tasks.get("rotation_resource_slug") or "rare:乌索"
                ),
                rotation_mantis_enabled=bool(tasks.get("rotation_mantis_enabled")),
                rotation_eit_enabled=bool(tasks.get("rotation_eit_enabled", False)),
                rotation_nieo_single_map_escape=bool(
                    tasks.get("rotation_nieo_single_map_escape", False)
                ),
                rotation_nieo_follow_cyan=bool(
                    tasks.get("rotation_nieo_follow_cyan", False)
                ),
                rotation_full_daily_maintenance=False,
                initial_swf_full=False,
            )
        )

    @staticmethod
    def _tasks_need_rare_nieo_asset_prep(tasks: dict) -> bool:
        """Return whether rare/Nieo startup asset prep is needed."""
        if tasks.get("light_mantis_mode"):
            return True
        if tasks.get("event_pet_mode") or tasks.get("eit_mode"):
            return True
        if tasks.get("afk_battle_mode"):
            sub = str(tasks.get("afk_sub_mode") or "").strip().lower()
            if sub in ("rare", "nieo"):
                return True
        return False

    @staticmethod
    def _tasks_need_daily_newnpc_multi_prep(tasks: dict) -> bool:
        """Return whether daily newNpc/multi prep is needed."""
        return bool(
            tasks.get("new_daily_mode")
            or tasks.get("new_daily_chain_mode")
            or tasks.get("pre_daily_mode")
            or tasks.get("daily_chain")
            or tasks.get("chip_gold_honor_mode")
            or tasks.get("lanlan_mode")
            or tasks.get("yilu_mode")
            or tasks.get("light_mantis_mode")
            or tasks.get("happy_valley_daily")
            or tasks.get("collection_daily_mode")
            or tasks.get("daily_start_mode")
            or tasks.get("gacha_test")
        )

    @staticmethod
    def _lanlan_cyan_pet_for_current_time(now: Optional[datetime] = None) -> Optional[int]:
        """Return Lanlan cyan pet id for the current allowed time window."""
        now = now or datetime.now()
        tod = now.time()
        end = dt_time(23, 45)
        weekday = now.weekday()  # Monday=0
        if weekday in (1, 3) and dt_time(0, 0) <= tod <= end:
            return 347
        if weekday == 6 and dt_time(6, 0) <= tod <= end:
            return 1459
        if weekday == 5 and dt_time(0, 0) <= tod <= end:
            return 683
        return None

    def _sync_all_pet_254_base_or_stop(
        self,
        log_tag: str,
        *,
        runtime_subset: bool = False,
    ) -> bool:
        try:
            from core.swf_resource_ops import (
                sync_all_pet_254_base,
                sync_runtime_pet_254_base,
            )

            use_union = self._task_swf_should_use_union()
            base_sync = (
                sync_runtime_pet_254_base
                if use_union
                else sync_all_pet_254_base
            )
            ok, msg = base_sync()
            if ok:
                self._mark_task_swf_base_done()
            base_label = (
                "潜在影响并集橙色254"
                if use_union
                else "首次全量橙色254"
            )
            self.emit_and_log(
                f"{'✅' if ok else '❌'} [{log_tag}] {base_label}：{msg}",
                "SUCCESS" if ok else "ERROR",
            )
            if not ok:
                self.stop_current = True
                self._stop_event.set()
            return bool(ok)
        except Exception as e:
            self.emit_and_log(f"❌ [{log_tag}] 全量橙色254异常：{e}", "ERROR")
            self.stop_current = True
            self._stop_event.set()
            return False

    def _task_swf_should_use_union(self) -> bool:
        """Use the small impact union after this task completes one full base."""
        return bool(getattr(self, "_task_swf_full_base_done", False))

    def _mark_task_swf_base_done(self) -> None:
        self._task_swf_full_base_done = True

    def _prepare_yilu_swf_or_stop(self) -> bool:
        try:
            from core.swf_resource_ops import sync_pet_254

            ok, msg = sync_pet_254(
                runtime_subset=self._task_swf_should_use_union()
            )
            if ok:
                self._mark_task_swf_base_done()
            self.emit_and_log(
                f"{'✅' if ok else '❌'} [依卢SWF] 1337→252紫色，197→519青色；{msg}",
                "SUCCESS" if ok else "ERROR",
            )
            if not ok:
                self.stop_current = True
                self._stop_event.set()
            return bool(ok)
        except Exception as e:
            self.emit_and_log(f"❌ [依卢SWF] 准备异常：{e}", "ERROR")
            self.stop_current = True
            self._stop_event.set()
            return False

    def _prepare_light_mantis_swf_or_stop(self) -> bool:
        try:
            from core.swf_resource_ops import sync_pet_254

            ok, msg = sync_pet_254(
                runtime_subset=self._task_swf_should_use_union()
            )
            if ok:
                self._mark_task_swf_base_done()
            self.emit_and_log(
                f"{'✅' if ok else '❌'} [光螳螂SWF] 1337→252紫色，197→519青色；{msg}",
                "SUCCESS" if ok else "ERROR",
            )
            if not ok:
                self.stop_current = True
                self._stop_event.set()
            return bool(ok)
        except Exception as e:
            self.emit_and_log(f"❌ [光螳螂SWF] 准备异常：{e}", "ERROR")
            self.stop_current = True
            self._stop_event.set()
            return False

    def _prepare_lanlan_swf_or_stop(self) -> bool:
        cyan_pet_id = self._lanlan_cyan_pet_for_current_time()
        if cyan_pet_id is None:
            self.emit_and_log(
                "[岚岚SWF] 当前时间不在允许窗口内，停止执行",
                "WARN",
            )
            self.stop_current = True
            self._stop_event.set()
            return False
        if not self._sync_all_pet_254_base_or_stop("岚岚SWF基线"):
            return False
        try:
            from core.swf_resource_ops import sync_lanlan_pet_254_set

            ok, msg = sync_lanlan_pet_254_set(cyan_pet_id)
            cyan_text = f"{cyan_pet_id}" if int(cyan_pet_id) == 1459 else f"67+{cyan_pet_id}"
            self.emit_and_log(
                f"{'✅' if ok else '❌'} [岚岚SWF] 1337→252紫色，{cyan_text}→519青色；{msg}",
                "SUCCESS" if ok else "ERROR",
            )
            if not ok:
                self.stop_current = True
                self._stop_event.set()
            return bool(ok)
        except Exception as e:
            self.emit_and_log(f"❌ [岚岚SWF] 准备异常：{e}", "ERROR")
            self.stop_current = True
            self._stop_event.set()
            return False

    def _prepare_fly_pet_1337_or_stop(self) -> bool:
        """所有模式启动时幂等备份 flyPet 483/1337，并用 50 覆盖两个本体。"""
        try:
            from core.swf_resource_ops import ensure_fly_pet_483_and_1337_from_50

            ok, msg = ensure_fly_pet_483_and_1337_from_50()
            self.emit_and_log(
                f"{'✅' if ok else '❌'} [flyPet-483/1337] {msg}",
                "SUCCESS" if ok else "ERROR",
            )
            if not ok:
                self.stop_current = True
                self._stop_event.set()
            return bool(ok)
        except Exception as e:
            self.emit_and_log(f"❌ [flyPet-483/1337] 准备异常：{e}", "ERROR")
            self.stop_current = True
            self._stop_event.set()
            return False

    def _prepare_one_click_release_swf_or_stop(self) -> bool:
        if not self._sync_all_pet_254_base_or_stop("一键放生SWF基线"):
            return False
        try:
            from core.swf_resource_ops import sync_one_click_release_pet_254_set

            ok, msg = sync_one_click_release_pet_254_set()
            self.emit_and_log(
                f"{'✅' if ok else '❌'} [一键放生SWF] 1337→252紫色；"
                f"65/95/102/143/416/528/604/607/650/667→519青色；{msg}",
                "SUCCESS" if ok else "ERROR",
            )
            if not ok:
                self.stop_current = True
                self._stop_event.set()
            return bool(ok)
        except Exception as e:
            self.emit_and_log(f"❌ [一键放生SWF] 准备异常：{e}", "ERROR")
            self.stop_current = True
            self._stop_event.set()
            return False

    def _prepare_weekly_purple_follow_swf_or_stop(self) -> bool:
        if not self._sync_all_pet_254_base_or_stop("一键周常SWF基线"):
            return False
        try:
            from core.swf_resource_ops import sync_weekly_purple_follow_pet_254_set

            ok, msg = sync_weekly_purple_follow_pet_254_set()
            self.emit_and_log(
                f"{'✅' if ok else '❌'} [一键周常SWF] 1337→252紫色跟随；{msg}",
                "SUCCESS" if ok else "ERROR",
            )
            if not ok:
                self.stop_current = True
                self._stop_event.set()
            return bool(ok)
        except Exception as e:
            self.emit_and_log(f"❌ [一键周常SWF] 准备异常：{e}", "ERROR")
            self.stop_current = True
            self._stop_event.set()
            return False

    def _prepare_hourly_daily_swf(
        self,
        task_name: str,
        cyan_pet_id: Optional[int] = None,
        *,
        runtime_subset: bool = True,
    ) -> bool:
        """Prepare one daily task without stopping the active rare/Nieo loop on failure."""
        try:
            from core import swf_resource_ops

            use_union = self._task_swf_should_use_union()
            base_sync = (
                swf_resource_ops.sync_runtime_pet_254_base
                if use_union
                else swf_resource_ops.sync_all_pet_254_base
            )
            ok_base, msg_base = base_sync()
            if ok_base:
                self._mark_task_swf_base_done()
            base_label = (
                "潜在影响并集橙色254"
                if use_union
                else "首次全量橙色254"
            )
            self.emit_and_log(
                f"{'✅' if ok_base else '❌'} [整点补跑-{task_name}] {base_label}：{msg_base}",
                "SUCCESS" if ok_base else "ERROR",
            )
            if not ok_base:
                return False

            if task_name == "依卢":
                ok, msg = swf_resource_ops.sync_yilu_pet_254_set()
            elif task_name == "岚岚" and cyan_pet_id is not None:
                ok, msg = swf_resource_ops.sync_lanlan_pet_254_set(int(cyan_pet_id))
            elif task_name == "光螳螂":
                ok, msg = swf_resource_ops.sync_light_mantis_pet_254_set()
            elif task_name == "一键放生":
                ok, msg = swf_resource_ops.sync_one_click_release_pet_254_set()
            else:
                self.emit_and_log(f"❌ [整点补跑] 无效的 SWF 任务：{task_name}", "ERROR")
                return False
            self.emit_and_log(
                f"{'✅' if ok else '❌'} [整点补跑-{task_name}SWF] {msg}",
                "SUCCESS" if ok else "ERROR",
            )
            return bool(ok)
        except Exception as e:
            self.emit_and_log(f"❌ [整点补跑-{task_name}SWF] 准备异常：{e}", "ERROR")
            return False

    def run_hourly_yilu_lanlan_maintenance(
        self,
        *,
        use_foreground: bool,
        run_yilu: bool,
        run_lanlan: bool,
        run_weekend_release: bool = False,
        runtime_subset: bool = True,
        yilu_deadline: Optional[datetime] = None,
    ) -> bool:
        """Run missing hourly dailies, then the lower-priority weekend release."""
        if not run_yilu and not run_lanlan and not run_weekend_release:
            return True
        if self.stop_current or self._stop_event.is_set():
            return False

        drr = self.dar_route_runner
        maintenance_stop_event = self._stop_event
        if run_yilu:
            now = self.daily_runner._beijing_now()
            if yilu_deadline is None:
                yilu_deadline = now.replace(minute=10, second=0, microsecond=0)
            if now >= yilu_deadline:
                self.emit_and_log(
                    f"⏭️ [整点补跑] 依卢窗口已于 {yilu_deadline.strftime('%H:%M:%S')} 结束；"
                    "本次取消依卢且不写完成记录",
                    "WARN",
                )
                run_yilu = False
            else:
                maintenance_stop_event = _DeadlineStopEvent(
                    self._stop_event,
                    yilu_deadline,
                    self.daily_runner._beijing_now,
                )
        lanlan_pet_id = self._lanlan_cyan_pet_for_current_time() if run_lanlan else None
        if run_lanlan and lanlan_pet_id is None:
            self.emit_and_log("[整点补跑] 岚岚不在预定时间内，本次只执行依卢", "INFO")
            run_lanlan = False
        if not run_yilu and not run_lanlan and not run_weekend_release:
            return True

        task_labels = "+".join(
            label
            for enabled, label in (
                (run_yilu, "依卢"),
                (run_lanlan, "岚岚"),
                (run_weekend_release, "周末放生"),
            )
            if enabled
        )
        self.emit_and_log(f"⏰ [整点补跑] 开始：{task_labels}", "SYSTEM")

        if run_yilu:
            if not self._prepare_hourly_daily_swf(
                "依卢", runtime_subset=runtime_subset
            ):
                return False
            if not self._ensure_newnpc_multi_90000_for_task(
                {"yilu_mode": True}, log_tag="整点补跑-依卢前置"
            ):
                return False
            if maintenance_stop_event.is_set():
                self.emit_and_log(
                    "⏭️ [整点补跑] 依卢在SWF前置阶段到达10分截止，停止本小时依卢",
                    "WARN",
                )
                return False
        elif run_lanlan:
            # A paired Yilu/Lanlan request may become Lanlan-only when the
            # Yilu window expires. Restore the NPC resource before preparing
            # Lanlan or reconnecting; otherwise map 108 loads without its NPC.
            if not self._ensure_newnpc_multi_90000_for_task(
                {"lanlan_mode": True}, log_tag="整点补跑-岚岚前置"
            ):
                return False
            if not self._prepare_hourly_daily_swf(
                "岚岚", lanlan_pet_id, runtime_subset=runtime_subset
            ):
                return False

        initial_task = "依卢" if run_yilu else "岚岚"
        self._clear_game_tmp_cache(log_tag=f"整点补跑-{initial_task}前置")
        if not drr.run_refresh_login_until_map(
            use_foreground,
            maintenance_stop_event,
            include_base_and_map_gate=True,
        ):
            if run_yilu and isinstance(maintenance_stop_event, _DeadlineStopEvent) and maintenance_stop_event.deadline_reached():
                self.emit_and_log("⏭️ [整点补跑] 依卢重连期间到达10分截止", "WARN")
                return False
            self.emit_and_log("❌ [整点补跑] 刷新重连/基地门控失败", "ERROR")
            return False
        if not drr._run_nono_fusion_after_mode_reconnect(
            use_foreground,
            reconnect_reason="整点依卢岚岚补跑",
        ):
            self.emit_and_log("❌ [整点补跑] Nono 融合重连检查失败", "ERROR")
            return False
        if run_yilu and maintenance_stop_event.is_set():
            self.emit_and_log("⏭️ [整点补跑] 依卢重连后已到10分截止", "WARN")
            return False

        if run_yilu:
            party_ok = drr.ensure_pick_party_from_bag_warehouse_or_skip(
                use_foreground,
                maintenance_stop_event,
                log_tag="整点补跑-依卢前置-新六宠",
                verify_primary_166=True,
            )
            follow_ok = party_ok and drr.recover_pick_party_color_slots_from_closed_bag(
                use_foreground,
                maintenance_stop_event,
                log_tag="整点补跑-依卢前置",
                recover_purple=True,
                set_follow_purple=True,
                bag_already_open=True,
            )
            if maintenance_stop_event.is_set():
                if isinstance(maintenance_stop_event, _DeadlineStopEvent) and maintenance_stop_event.deadline_reached():
                    self.emit_and_log(
                        "⏭️ [整点补跑] 依卢六宠/恢复阶段到达10分截止，停止本小时依卢",
                        "WARN",
                    )
                return False
            if not follow_ok or not self.daily_runner.run_yilu_mode(
                use_foreground=use_foreground,
                window_deadline=yilu_deadline,
            ):
                self.emit_and_log("❌ [整点补跑] 依卢未完成", "ERROR")
                return False

        if run_lanlan:
            lanlan_primary = int(lanlan_pet_id) == 683
            if run_yilu:
                if not self._prepare_hourly_daily_swf(
                    "岚岚", lanlan_pet_id, runtime_subset=True
                ):
                    return False
                if not self._ensure_newnpc_multi_90000_for_task(
                    {"lanlan_mode": True}, log_tag="整点补跑-岚岚前置"
                ):
                    return False
                self.emit_and_log(
                    "🔄 [整点补跑] 依卢完成，岚岚为独立环节：重新连接并完成基地门控",
                    "SYSTEM",
                )
                self._clear_game_tmp_cache(log_tag="整点补跑-依卢到岚岚")
                if not drr.run_refresh_login_until_map(
                    use_foreground,
                    self._stop_event,
                    include_base_and_map_gate=True,
                ):
                    self.emit_and_log("❌ [整点补跑] 依卢→岚岚刷新重连/基地门控失败", "ERROR")
                    return False
                if not drr._run_nono_fusion_after_mode_reconnect(
                    use_foreground,
                    reconnect_reason="整点依卢到岚岚",
                ):
                    self.emit_and_log("❌ [整点补跑] 依卢→岚岚 Nono 融合重连检查失败", "ERROR")
                    return False
            party_ok = drr.ensure_target_cyan_pick_party_from_bag_warehouse_or_rebuild(
                int(lanlan_pet_id),
                use_foreground,
                self._stop_event,
                log_tag="整点补跑-岚岚前置-新六宠",
                set_cyan_primary=lanlan_primary,
                base_pet_id=67 if int(lanlan_pet_id) != 1459 else 197,
                verify_primary_166=int(lanlan_pet_id) != 683,
            )
            if not party_ok or not self.daily_runner.run_lanlan_mode(use_foreground=use_foreground):
                self.emit_and_log("❌ [整点补跑] 岚岚未完成", "ERROR")
                return False
            if int(lanlan_pet_id) != 1459:
                restore_ok = drr.replace_current_cyan_with_pick_pet_from_closed_bag(
                    67,
                    use_foreground,
                    self._stop_event,
                    log_tag=f"整点补跑-岚岚完成-放回{int(lanlan_pet_id)}取67",
                    require_putback_confirmation=True,
                    set_follow_purple=False,
                    recover_target_after_take=False,
                )
                if not restore_ok:
                    self.emit_and_log("❌ [整点补跑] 岚岚完成后收回精灵失败", "ERROR")
                    return False
            else:
                self.emit_and_log(
                    "✅ [整点补跑-岚岚周日] 保持普通六宠，不执行仓库换宠收尾",
                    "SUCCESS",
                )

        if run_weekend_release:
            if not self._run_weekend_release_for_rotation_precheck(
                use_foreground,
                runtime_subset=True,
            ):
                self.emit_and_log("❌ [整点补跑] 周末放生未完成", "ERROR")
                return False

        self.emit_and_log(
            f"✅ [整点补跑] 已完成：{task_labels}，必要收尾已完成",
            "SUCCESS",
        )
        return True

    def _weekend_release_due(self, now: Optional[datetime] = None) -> bool:
        now = now or self.daily_runner._beijing_now()
        if int(now.weekday()) not in (5, 6):
            return False
        has_record = getattr(
            self.daily_runner, "has_one_click_release_weekly_record", None
        )
        return callable(has_record) and not bool(has_record(now))

    def _run_weekend_release_for_rotation_precheck(
        self,
        use_foreground: bool,
        *,
        runtime_subset: bool = True,
    ) -> bool:
        """Run the weekend-only release after Yilu/Lanlan and record success."""
        now = self.daily_runner._beijing_now()
        if int(now.weekday()) not in (5, 6):
            return True
        if not self._weekend_release_due(now):
            self.emit_and_log("✅ [周末放生] 本周已有完成记录，跳过", "SUCCESS")
            return True
        self.emit_and_log("📋 [周末放生] 本周未完成，开始低优先度放生", "SYSTEM")
        if not self._prepare_hourly_daily_swf(
            "一键放生", runtime_subset=runtime_subset
        ):
            return False
        self._clear_game_tmp_cache(log_tag="周末放生前置")
        if not self.dar_route_runner.run_refresh_login_until_map(
            use_foreground,
            self._stop_event,
            include_base_and_map_gate=False,
        ):
            self.emit_and_log("❌ [周末放生] 刷新登录/屏蔽失败", "ERROR")
            return False
        if not self.daily_runner.run_one_click_release_mode(
            use_foreground=use_foreground
        ):
            self.emit_and_log("❌ [周末放生] 一键放生未完成", "ERROR")
            return False
        if not self.daily_runner.append_one_click_release_weekly_record(
            note="轮换周末低优先度检查"
        ):
            self.emit_and_log("❌ [周末放生] 放生完成但周记录写入失败", "ERROR")
            return False
        self.emit_and_log("✅ [周末放生] 已完成并写入本周记录", "SUCCESS")
        return True

    def _run_light_mantis_for_rotation_precheck(
        self,
        use_foreground: bool,
        *,
        runtime_subset: bool = True,
        log_context: str = "轮换前置",
    ) -> bool:
        if not self._prepare_hourly_daily_swf(
            "光螳螂", runtime_subset=runtime_subset
        ):
            return False
        self._clear_game_tmp_cache(log_tag=f"{log_context}-光螳螂")
        if not self.dar_route_runner.run_refresh_login_until_map(
            use_foreground, self._stop_event
        ):
            self.emit_and_log(f"❌ [{log_context}-光螳螂] 刷新登录失败", "ERROR")
            return False
        party_ok = self.dar_route_runner.ensure_target_cyan_pick_party_from_bag_warehouse_or_rebuild(
            197,
            use_foreground,
            self._stop_event,
            log_tag=f"{log_context}-光螳螂-新六宠",
            skip_cyan_recovery_when_primary=True,
        )
        if not party_ok:
            self.emit_and_log(f"❌ [{log_context}-光螳螂] 组队/恢复/跟随失败", "ERROR")
            return False
        if not self.daily_runner.run_light_mantis_mode(use_foreground=use_foreground):
            self.emit_and_log(f"❌ [{log_context}-光螳螂] 未完成", "ERROR")
            return False
        return True

    def _run_light_mantis_before_weekly_if_due(self, use_foreground: bool) -> bool:
        """一键周常开始前检查本周记录，仅在缺失时补跑光螳螂。"""
        now = self.daily_runner._beijing_now()
        if self.daily_runner.has_light_mantis_weekly_record(now):
            self.emit_and_log(
                "✅ [一键周常前置] 本周已有光螳螂完成记录，跳过补跑",
                "SUCCESS",
            )
            return True
        self.emit_and_log(
            "📋 [一键周常前置] 本周尚无光螳螂记录，先执行光螳螂",
            "SYSTEM",
        )
        ok = self._run_light_mantis_for_rotation_precheck(
            use_foreground,
            log_context="一键周常前置",
        )
        if ok:
            self.emit_and_log("✅ [一键周常前置] 光螳螂完成，继续执行周常", "SUCCESS")
            return True
        if self.user_stop_requested:
            return False
        self.stop_current = False
        self._stop_event.clear()
        self.emit_and_log(
            "⚠️ [一键周常前置] 光螳螂未完成，仍继续执行周常",
            "WARN",
        )
        return False

    def _master_cup_settings_from_tasks(
        self,
        tasks: dict,
        *,
        allow_zero: bool = False,
    ) -> tuple[str, int, bool, bool]:
        cup_type = str(tasks.get("master_cup_type") or "水系").strip() or "水系"
        yellow_target = self._parse_int(
            tasks.get("master_cup_yellow_target", 36), 36
        )
        if yellow_target < 0 or (yellow_target == 0 and not allow_zero):
            yellow_target = 36
        pre_setup = bool(tasks.get("master_cup_pre_setup"))
        norm_mode = cup_type == "诺姆"
        if norm_mode:
            yellow_target = 10
        return cup_type, yellow_target, pre_setup, norm_mode

    def _run_configured_master_cup(
        self,
        tasks: dict,
        use_foreground: bool,
        *,
        log_context: str = "大师杯",
    ) -> bool:
        cup_type, yellow_target, pre_setup, norm_mode = (
            self._master_cup_settings_from_tasks(tasks)
        )
        self.emit_and_log(
            f"🏆 [{log_context}] 启动：系别={cup_type}，"
            f"前台点击={use_foreground}，目标黄胜={yellow_target}，"
            f"前置={pre_setup}，诺姆模式={norm_mode}",
            "SYSTEM",
        )
        if not self._prepare_master_cup_swf_or_stop(cup_type, norm_mode):
            return False
        ok = bool(
            self.daily_runner.run_master_cup_mode(
                cup_type=cup_type,
                use_foreground=use_foreground,
                yellow_target_count=yellow_target,
                pre_setup=pre_setup,
                norm_mode=norm_mode,
            )
        )
        if ok:
            self.daily_runner.append_master_cup_weekly_record(
                cup_type=cup_type,
                norm_ran=norm_mode,
                yellow_target=yellow_target,
                pre_setup=pre_setup,
                note=log_context,
            )
        return ok

    def _run_master_cup_before_weekly_if_due(
        self, tasks: dict, use_foreground: bool
    ) -> bool:
        """光螳螂检查后按本周记录决定是否执行已配置的大师杯。"""
        now = self.daily_runner._beijing_now()
        record = self.daily_runner.get_master_cup_weekly_record(now)
        if record is not None:
            self.emit_and_log(
                "✅ [一键周常前置-大师杯] 本周已有完成记录，跳过补跑："
                f"时间={record.get('time') or '-'}，"
                f"系别={record.get('cup_type') or '-'}，"
                f"诺姆={'是' if str(record.get('norm_ran')).lower() == 'true' else '否'}",
                "SUCCESS",
            )
            return True
        cup_type, yellow_target, pre_setup, norm_mode = (
            self._master_cup_settings_from_tasks(tasks, allow_zero=True)
        )
        if yellow_target == 0:
            self.emit_and_log(
                "⏭️ [一键周常前置-大师杯] 场次=0，按配置跳过大师杯；"
                "不准备SWF、不进入大师杯、不写完成记录，继续执行周常",
                "INFO",
            )
            return True
        self.emit_and_log(
            "📋 [一键周常前置-大师杯] 本周尚无完成记录，按当前设置执行："
            f"系别={cup_type}，目标黄胜={yellow_target}，"
            f"前置={pre_setup}，诺姆={'是' if norm_mode else '否'}",
            "SYSTEM",
        )
        ok = self._run_configured_master_cup(
            tasks,
            use_foreground,
            log_context="一键周常前置-大师杯",
        )
        if ok:
            self.emit_and_log(
                "✅ [一键周常前置-大师杯] 本周大师杯完成，继续执行周常",
                "SUCCESS",
            )
            return True
        if self.user_stop_requested:
            return False
        self.stop_current = False
        self._stop_event.clear()
        self.emit_and_log(
            "⚠️ [一键周常前置-大师杯] 本周大师杯未完成，仍继续执行周常",
            "WARN",
        )
        return False

    def _one_click_weekly_stop_requested(self) -> bool:
        return bool(
            self.user_stop_requested
            or self.stop_current
            or self._stop_event.is_set()
        )

    def _run_one_click_weekly_task(
        self,
        tasks: dict,
        use_foreground: bool,
    ) -> bool:
        """执行一次周常；停止信号只向外返回，不跳过主循环的任务清理。"""
        self._run_light_mantis_before_weekly_if_due(use_foreground)
        if self._one_click_weekly_stop_requested():
            self.emit_and_log(
                "🛑 [一键周常] 光螳螂阶段收到停止信号，结束本次周常并进入任务清理",
                "SYSTEM",
            )
            return False

        self._run_master_cup_before_weekly_if_due(tasks, use_foreground)
        if self._one_click_weekly_stop_requested():
            self.emit_and_log(
                "🛑 [一键周常] 大师杯阶段收到停止信号，结束本次周常并进入任务清理",
                "SYSTEM",
            )
            return False

        if not self._prepare_weekly_purple_follow_swf_or_stop():
            return False
        if self._one_click_weekly_stop_requested():
            return False

        refresh_ok = bool(
            self.dar_route_runner.run_refresh_login_until_map(
                use_foreground,
                self._stop_event,
                include_base_and_map_gate=False,
            )
        )
        if not refresh_ok:
            if self._one_click_weekly_stop_requested():
                self.emit_and_log("🛑 [一键周常] 刷新阶段已停止", "SYSTEM")
            else:
                self.emit_and_log("❌ [一键周常] 刷新重连/屏蔽失败", "ERROR")
                self.stop_current = True
                self._stop_event.set()
            return False

        gacha_filled_times = max(
            1,
            self._parse_int(tasks.get("weekly_gacha_filled_times", 1), 1),
        )
        if not self.daily_runner.run_chip_gold_honor_mode(
            use_foreground=use_foreground,
            gacha_filled_times=gacha_filled_times,
        ):
            if self._one_click_weekly_stop_requested():
                self.emit_and_log("🛑 [一键周常] 周常主体已停止", "SYSTEM")
            else:
                self.emit_and_log("❌ [一键周常] 未完成", "ERROR")
                self.stop_current = True
                self._stop_event.set()
            return False
        return True

    def _prepare_wild_mode_after_restart(
        self,
        profile_name: str,
        profile: WildCaptureProfile,
        use_foreground: bool,
    ) -> bool:
        """Re-enter a normal wild mode through its full pre-mode boundary."""
        if profile_name == "flash_pipi":
            return bool(
                self.dar_route_runner._execute_flash_pipi_pre_rotation_reconnect(
                    use_foreground=use_foreground,
                    stop_event=self._stop_event,
                )
            )
        if profile_name == "mantis":
            return bool(
                self.dar_route_runner._execute_mantis_pre_rotation_reconnect(
                    use_foreground=use_foreground,
                    stop_event=self._stop_event,
                )
            )
        if self._wild_uses_unified_pre(profile_name, profile):
            return bool(
                self.dar_route_runner._execute_unified_wild_rare_pre_reconnect(
                    profile,
                    use_foreground=use_foreground,
                    stop_event=self._stop_event,
                )
            )
        return True

    def _run_one_click_daily_for_rotation_precheck(
        self,
        use_foreground: bool,
        *,
        runtime_subset: bool = True,
        skip_exp_input: bool = True,
    ) -> bool:
        self._clear_game_tmp_cache(log_tag="轮换前置-一键日常")
        self._ensure_newnpc_multi_90000_for_task(
            {"new_daily_chain_mode": True}, log_tag="轮换前置-一键日常"
        )
        self._ensure_newnpc_multi_4_hidden(log_tag="轮换前置-一键日常")
        self.daily_runner.begin_one_click_daily_progress("1", 1)
        self.daily_runner.mark_one_click_daily_progress("孵化", variant="1", step=1)
        self.emit_and_log(
            "📋 [轮换前置-一键日常] 从孵化开始：孵化 → 预选/签到 → 日常六宠 → 欢乐谷 → to日常 → 一键日常全链路",
            "SYSTEM",
        )
        if not self._prepare_hatch_swf_or_stop(runtime_subset=runtime_subset):
            self.daily_runner.finish_one_click_daily_progress(
                "failed", "rotation precheck hatch SWF preparation failed"
            )
            return False
        if not self._prepare_one_click_daily_attempt(1):
            self.daily_runner.finish_one_click_daily_progress(
                "failed", "rotation precheck daily SWF preparation failed"
            )
            return False
        if not self.daily_runner.run_hatch_start(use_foreground=use_foreground):
            self.daily_runner.finish_one_click_daily_progress(
                "failed", "rotation precheck hatch failed"
            )
            return False
        self.daily_runner.mark_one_click_daily_progress(
            "孵化", variant="1", step=1, completed=True
        )
        self.daily_runner.mark_one_click_daily_progress("预选至一键日常", variant="1", step=1)
        if not self._prepare_hatch_exp_swf_or_stop(runtime_subset=True):
            self.daily_runner.finish_one_click_daily_progress(
                "failed", "rotation precheck collection SWF preparation failed"
            )
            return False
        ok = self.daily_runner.run_collection_daily_mode(
            use_foreground=use_foreground,
            skip_refresh_login=True,
            skip_exp_input=skip_exp_input,
            before_attempt=self._prepare_one_click_daily_attempt,
        )
        if self.stop_current or self._stop_event.is_set():
            self.daily_runner.finish_one_click_daily_progress("stopped", "manual stop")
            return False
        if not ok:
            self.daily_runner.finish_one_click_daily_progress(
                "failed", "rotation precheck full daily chain did not complete"
            )
            return False
        self.daily_runner.finish_one_click_daily_progress(
            "complete", "hatch through full daily chain complete"
        )
        self.new_daily_chain_completed = True
        return True

    def run_rotation_button_daily_precheck(
        self,
        use_foreground: bool,
        *,
        skip_exp_input: bool = True,
    ) -> bool:
        """Require today's one-click daily before a manual rotation-button start."""
        now = self.daily_runner._beijing_now()
        if self.daily_runner.has_one_click_daily_complete_today(now):
            self.emit_and_log(
                "✅ [轮换按钮前置] 当前06:00业务日的一键日常已完成，直接进入轮换",
                "SUCCESS",
            )
            return True

        self.emit_and_log(
            "📋 [轮换按钮前置] 当前06:00业务日的一键日常尚未完成，先执行完整一键日常",
            "SYSTEM",
        )
        if not self._run_one_click_daily_for_rotation_precheck(
            use_foreground,
            runtime_subset=False,
            skip_exp_input=skip_exp_input,
        ):
            if self.user_stop_requested:
                return False
            self.emit_and_log(
                "⚠️ [轮换按钮前置] 一键日常未完整完成，不再自重连整套日常；按设定继续启动轮换",
                "WARN",
            )
            return True

        self.emit_and_log(
            "✅ [轮换按钮前置] 一键日常已完成，开始轮换",
            "SUCCESS",
        )
        return True

    def run_rotation_time_due_maintenance(self, use_foreground: bool) -> bool:
        """Run due Nono fusion during rotation time checks.

        The full daily chain is scheduled separately at the rotation checkpoints
        and designated hourly full-chain scans.
        """
        # Temporary policy: rotation must never launch Nono fusion from its
        # periodic time-check hook.  This opt-in flag has no UI and defaults
        # to False, so the caller continues normal rotation scheduling.
        if not bool(getattr(self, "_rotation_nono_fusion_enabled", False)):
            return False

        if bool(getattr(self, "_rotation_time_due_maintenance_running", False)):
            return False
        self._rotation_time_due_maintenance_running = True
        handled = False
        try:
            fusion_ok, fusion_handled = self._run_nono_fusion_pre_mode_check(
                use_foreground,
                "轮换时间检查",
            )
            if not fusion_ok:
                self.emit_and_log("⚠️ [轮换时间检查] Nono融合检查失败，继续轮换时间判断", "WARN")
            if fusion_handled:
                handled = True
            return handled
        finally:
            self._rotation_time_due_maintenance_running = False

    def _continue_rotation_precheck_after_failure(self, task_name: str) -> bool:
        if bool(getattr(self, "user_stop_requested", False)):
            return False
        self.stop_current = False
        self._stop_event.clear()
        self.emit_and_log(f"⚠️ [轮换前置] {task_name}失败，继续下一项", "WARN")
        return True

    def run_rotation_startup_daily_precheck(
        self,
        use_foreground: bool,
        *,
        runtime_switch: bool = False,
    ) -> bool:
        """Run only the missing scheduled dailies before entering rotation mode."""
        self._rotation_full_chain_scan_handled = False
        self._rotation_full_chain_scan_failed_labels = []
        now = self.daily_runner._beijing_now()
        yilu_done = self.daily_runner.has_yilu_daily_record_today(now)
        lanlan_done = self.daily_runner.has_lanlan_daily_record_today(now)
        mantis_done = self.daily_runner.has_light_mantis_weekly_record(now)
        daily_done = self.daily_runner.has_one_click_daily_complete_today(now)
        hatch_due_state = self.daily_runner.hatch_one_click_daily_due_state(now)
        hatch_daily_due = bool(hatch_due_state.get("due"))
        lanlan_target = self._lanlan_cyan_pet_for_current_time(now)

        yilu_window_open = 0 <= int(now.minute) < 10
        run_yilu = (not yilu_done) and yilu_window_open
        run_lanlan = (not lanlan_done) and lanlan_target is not None
        run_mantis = not mantis_done
        run_daily = (not daily_done) and hatch_daily_due
        run_weekend_release = self._weekend_release_due(now)
        labels = [
            label
            for enabled, label in (
                (run_yilu, "依卢"),
                (run_lanlan, "岚岚"),
                (run_daily, "一键日常"),
                (run_mantis, "光螳螂"),
                (run_weekend_release, "周末放生"),
            )
            if enabled
        ]
        self.emit_and_log(
            "📋 [轮换前置] 状态："
            f"依卢={'已完成' if yilu_done else ('待执行' if yilu_window_open else '等待下个整点前10分钟')}，"
            f"岚岚={'已完成' if lanlan_done else ('待执行' if lanlan_target is not None else '不在预定时间')}，"
            f"光螳螂={'本周已完成' if mantis_done else '本周待执行'}，"
            f"一键日常={'已完成' if daily_done else ('待执行' if hatch_daily_due else '跳过')}，"
            f"周末放生={'本周待执行' if run_weekend_release else ('本周已完成' if int(now.weekday()) in (5, 6) else '非周末跳过')}，"
            f"孵化判断={hatch_due_state.get('reason')}",
            "SYSTEM",
        )
        if not labels:
            return True

        self._rotation_full_chain_scan_handled = True

        failed_labels = []
        runtime_subset = bool(runtime_switch)
        if run_yilu or run_lanlan:
            maintenance_ok = self.run_hourly_yilu_lanlan_maintenance(
                use_foreground=use_foreground,
                run_yilu=run_yilu,
                run_lanlan=run_lanlan,
                runtime_subset=runtime_subset,
            )
            runtime_subset = True
            if not maintenance_ok:
                failed_labels.append("依卢/岚岚")
                if not self._continue_rotation_precheck_after_failure("依卢/岚岚"):
                    return False
        if run_weekend_release:
            release_ok = self._run_weekend_release_for_rotation_precheck(
                use_foreground,
                runtime_subset=runtime_subset,
            )
            runtime_subset = True
            if not release_ok:
                failed_labels.append("周末放生")
                if not self._continue_rotation_precheck_after_failure("周末放生"):
                    return False
        if run_daily:
            daily_ok = self._run_one_click_daily_for_rotation_precheck(
                use_foreground,
                runtime_subset=runtime_subset,
            )
            runtime_subset = True
            if not daily_ok:
                failed_labels.append("一键日常")
                if not self._continue_rotation_precheck_after_failure("一键日常"):
                    return False
        if run_mantis:
            mantis_ok = self._run_light_mantis_for_rotation_precheck(
                use_foreground,
                runtime_subset=runtime_subset,
            )
            if not mantis_ok:
                failed_labels.append("光螳螂")
                if not self._continue_rotation_precheck_after_failure("光螳螂"):
                    return False
        if failed_labels:
            self._rotation_full_chain_scan_failed_labels = list(failed_labels)
            self.emit_and_log(
                f"⚠️ [轮换前置] 未完成：{'、'.join(failed_labels)}；继续启动轮换",
                "WARN",
            )
        else:
            self.emit_and_log(f"✅ [轮换前置] 已完成：{'、'.join(labels)}", "SUCCESS")
        return True

    @staticmethod
    def _master_cup_cyan_pet_for_cup(cup_type: str) -> Optional[int]:
        cup = str(cup_type or "").strip()
        if cup == "飞行系":
            return 268
        if cup == "诺姆":
            return 40
        if cup in MASTER_CUP_568_TYPES:
            return 568
        return None

    def _prepare_master_cup_swf_or_stop(self, cup_type: str, norm_mode: bool) -> bool:
        cyan_ids = set()
        cup_cyan = self._master_cup_cyan_pet_for_cup(cup_type)
        if cup_cyan is not None:
            cyan_ids.add(int(cup_cyan))
        if norm_mode:
            cyan_ids.add(40)
        if not self._sync_all_pet_254_base_or_stop("大师杯SWF基线"):
            return False
        try:
            from core.swf_resource_ops import sync_master_cup_pet_254_set

            ok, msg = sync_master_cup_pet_254_set(sorted(cyan_ids))
            master_cyan_display = set(cyan_ids)
            if 1459 not in master_cyan_display:
                master_cyan_display.add(67)
            self.emit_and_log(
                f"{'✅' if ok else '❌'} [大师杯SWF] 1337→252紫色，青色={sorted(master_cyan_display)}；{msg}",
                "SUCCESS" if ok else "ERROR",
            )
            if not ok:
                self.stop_current = True
                self._stop_event.set()
            return bool(ok)
        except Exception as e:
            self.emit_and_log(f"❌ [大师杯SWF] 准备异常：{e}", "ERROR")
            self.stop_current = True
            self._stop_event.set()
            return False

    def _prepare_happy_valley_swf_or_stop(self) -> bool:
        if not self._sync_all_pet_254_base_or_stop("欢乐谷SWF基线"):
            return False
        try:
            from core.swf_resource_ops import sync_hatch_pet_254_set

            ok, msg = sync_hatch_pet_254_set()
            self.emit_and_log(
                f"{'✅' if ok else '❌'} [欢乐谷SWF] 303→519青色，309→252紫色；{msg}",
                "SUCCESS" if ok else "ERROR",
            )
            if not ok:
                self.stop_current = True
                self._stop_event.set()
            return bool(ok)
        except Exception as e:
            self.emit_and_log(f"❌ [欢乐谷SWF] 准备异常：{e}", "ERROR")
            self.stop_current = True
            self._stop_event.set()
            return False

    def _prepare_hatch_swf_or_stop(self, *, runtime_subset: bool = False) -> bool:
        if not self._sync_all_pet_254_base_or_stop(
            "孵化SWF基线", runtime_subset=runtime_subset
        ):
            return False
        try:
            from core.swf_resource_ops import sync_hatch_pet_254_set

            ok, msg = sync_hatch_pet_254_set()
            self.emit_and_log(
                f"{'✅' if ok else '❌'} [孵化SWF] 606/67/309→252紫色，303→519青色；{msg}",
                "SUCCESS" if ok else "ERROR",
            )
            if not ok:
                self.stop_current = True
                self._stop_event.set()
            return bool(ok)
        except Exception as e:
            self.emit_and_log(f"❌ [孵化SWF] 准备异常：{e}", "ERROR")
            self.stop_current = True
            self._stop_event.set()
            return False

    def _prepare_hatch_exp_swf_or_stop(self, *, runtime_subset: bool = False) -> bool:
        if not self._sync_all_pet_254_base_or_stop(
            "孵化经验SWF基线", runtime_subset=runtime_subset
        ):
            return False
        try:
            from core.swf_resource_ops import sync_hatch_pet_254_set

            ok, msg = sync_hatch_pet_254_set()
            self.emit_and_log(
                f"{'✅' if ok else '❌'} [孵化经验SWF] 606/67/309→252紫色，303→519青色；{msg}",
                "SUCCESS" if ok else "ERROR",
            )
            if not ok:
                self.stop_current = True
                self._stop_event.set()
            return bool(ok)
        except Exception as e:
            self.emit_and_log(f"❌ [孵化经验SWF] 准备异常：{e}", "ERROR")
            self.stop_current = True
            self._stop_event.set()
            return False

    def _prepare_one_click_daily_attempt(self, attempt: int) -> bool:
        """Apply SWF state owned by the outer full-daily attempt boundary."""
        try:
            from core.swf_resource_ops import delete_pet_swf_ids

            ok, msg = delete_pet_swf_ids((198,))
            self.emit_and_log(
                f"{'✅' if ok else '❌'} [一键日常外层·第{attempt}轮] 删除198：{msg}",
                "SUCCESS" if ok else "ERROR",
            )
            if not ok:
                self.stop_current = True
                self._stop_event.set()
            return bool(ok)
        except Exception as exc:
            self.emit_and_log(
                f"❌ [一键日常外层·第{attempt}轮] 删除198异常：{exc}",
                "ERROR",
            )
            self.stop_current = True
            self._stop_event.set()
            return False

    def _clear_game_tmp_cache(self, *, log_tag: str = "日常前置") -> None:
        """Remove the game tmp directory."""
        try:
            from config import GAME_PATH

            game_root = os.path.dirname(os.path.abspath(GAME_PATH))
            tmp_dir = os.path.join(game_root, "tmp")
            if os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir)
                self.emit_and_log(
                    f"✅ [{log_tag}] 已清理 tmp：{tmp_dir}",
                    "SUCCESS",
                )
            else:
                self.emit_and_log(
                    f"ℹ️ [{log_tag}] tmp 不存在，跳过：{tmp_dir}",
                    "INFO",
                )
        except OSError as e:
            self.emit_and_log(
                f"❌ [{log_tag}] 清理 tmp 失败：{e}",
                "ERROR",
            )
        except Exception as e:
            self.emit_and_log(
                f"❌ [{log_tag}] 清理 tmp 异常：{e}",
                "ERROR",
            )

    def _ensure_newnpc_multi_4_hidden(self, log_tag: str = "任务前置") -> None:
        from core.swf_resource_ops import ensure_newnpc_multi_4_to_4_og

        ok, msg = ensure_newnpc_multi_4_to_4_og()
        self.emit_and_log(
            f"{'✅' if ok else '⚠️'} [{log_tag}] newNpc/multi：{msg}",
            "INFO" if ok else "WARN",
        )

    def _ensure_newnpc_multi_90000_for_task(self, tasks: dict, log_tag: str = "任务前置") -> bool:
        from core.swf_resource_ops import (
            ensure_newnpc_multi_90000_hidden_for_yilu,
            ensure_newnpc_multi_90000_restored_for_non_yilu,
            verify_newnpc_multi_90000_live,
        )

        if tasks.get("yilu_mode"):
            ok, msg = ensure_newnpc_multi_90000_hidden_for_yilu()
            action = "90000->90000_og"
        else:
            ok, msg = ensure_newnpc_multi_90000_restored_for_non_yilu()
            action = "90000_og->90000"
            if ok and tasks.get("lanlan_mode"):
                verified, verify_msg = verify_newnpc_multi_90000_live()
                ok = bool(verified)
                msg = f"{msg}；{verify_msg}"
        self.emit_and_log(
            f"{'✅' if ok else '⚠️'} [{log_tag}] newNpc/multi {action}：{msg}",
            "INFO" if ok else "WARN",
        )
        return bool(ok)

    def _run_rare_nieo_startup_maintenance(self) -> None:
        """Run rare/Nieo startup SWF and PetStorage maintenance."""
        self._clear_game_tmp_cache(log_tag="稀有/尼奥前置")

        try:
            from core import swf_resource_ops

            ok, msg = swf_resource_ops.sync_pet_254(
                runtime_subset=self._task_swf_should_use_union()
            )
            if ok:
                self._mark_task_swf_base_done()
            self.emit_and_log(
                f"{'✅' if ok else '❌'} [稀有/尼奥Pet254] {msg}",
                "SUCCESS" if ok else "ERROR",
            )
            ok2, msg2 = swf_resource_ops.sync_petstorage()
            self.emit_and_log(
                f"{'✅' if ok2 else '❌'} [稀有/尼奥PetStorage] {msg2}",
                "SUCCESS" if ok2 else "ERROR",
            )
        except Exception as e:
            self.emit_and_log(f"❌ [稀有/尼奥前置] 异常：{e}", "ERROR")

    # --------------------
    # main loop
    # --------------------
    def run(self):
        self.emit_and_log("[引擎] 自动化线程已启动", "ENGINE")
        self.state_signal.emit("IDLE")

        while self._engine_alive:
            with self._task_lock:
                tasks = dict(self.active_tasks)

            job_keys = (
                "daily_chain", "new_daily_chain_mode", "pre_daily_mode", "one_click_release_mode", "chip_gold_honor_mode",
                "lanlan_mode", "yilu_mode", "light_mantis_mode", "happy_valley_daily",
                "bag_putback_test_mode",
                "new_daily_mode",
                "run_script", "gacha_test", "hero_tower", "shanni_energy_drain",
                "master_cup_mode", "chaos_rotation_chain",
                "chaos_battle_x2", "1v1_x2", "exp_minor_battle",
                "nono_soul_fusion_check", "hatch_start_mode", "collection_daily_mode", "daily_start_mode", "psychic_exp_purple_mode", "fusion_mode",
                "training_level", "training_until_level", "leiyi_training", "teixun_loop",
                "dar_route_test", "wild_capture", "smart_tracking_test", "calibration_test",
                "nie_family_test", "nieo_mode", "nieo_resource_chain", "afk_battle_mode", "eit_mode",
                "event_pet_mode", "rotation_mode", "rotation_chain_test", "pinnacle_mode",
            )
            has_job = any(tasks.get(key) for key in job_keys)

            if not has_job:
                time.sleep(0.1)
                continue

            self.is_running = True
            self.user_stop_requested = False
            self.stop_current = False
            self.is_paused = False
            self._stop_event.clear()
            self.new_daily_chain_completed = False
            self._gacha_rotation_handoff_started = False

            self.state_signal.emit("RUNNING")
            self._start_keyboard_listener()
            fly_pet_ready = self._prepare_fly_pet_1337_or_stop()

            try:
                if not fly_pet_ready:
                    self.emit_and_log("❌ [任务前置] flyPet 1337 准备失败，停止当前模式", "ERROR")
                elif not window_manager.find_window():
                    self.emit_and_log("❌ 未找到游戏窗口，请先从 Dashboard 启动游戏", "ERROR")
                else:
                    use_foreground = bool(tasks.get("use_foreground", False))
                    use_background = (not use_foreground)

                    self.dar_route_runner.reset_swf_sync_state(
                        reset_task_baseline=True
                    )

                    if self._tasks_need_rare_nieo_asset_prep(tasks):
                        self.emit_and_log(
                            "[稀有/尼奥前置] 清理tmp → pet swf → PetStorage",
                            "SYSTEM",
                        )
                        self._run_rare_nieo_startup_maintenance()


                    try:
                        cap_tier = tasks.get("non_mantis_capsule_tier")
                        if cap_tier in ("cycle", "default", "super_super_special", "super", "high", "special"):
                            self.dar_route_runner.set_non_mantis_capsule_tier(str(cap_tier))
                        elif "non_mantis_use_super_capsule" in tasks:
                            self.dar_route_runner.set_non_mantis_use_super_capsule(
                                bool(tasks.get("non_mantis_use_super_capsule", False))
                            )
                        else:
                            self.dar_route_runner.set_non_mantis_capsule_tier("high")
                    except Exception:
                        pass

                    try:
                        self.dar_route_runner.set_pick_pet_mode(
                            bool(tasks.get("pick_pet_mode", True))
                        )
                        self.dar_route_runner.set_resist_drain_logic(
                            bool(tasks.get("resist_drain_logic", False))
                        )
                        self.dar_route_runner.set_enable_molecule_converter(
                            bool(tasks.get("enable_molecule_converter", False))
                        )
                    except Exception:
                        pass

                    try:
                        self.dar_route_runner._unified_reconnect_pipeline_always = True
                    except Exception:
                        pass

                    if tasks.get("lanlan_mode") and not self.stop_current:
                        self._prepare_lanlan_swf_or_stop()

                    if not self.stop_current:
                        if self._tasks_need_daily_newnpc_multi_prep(tasks):
                            self._clear_game_tmp_cache(log_tag="日常前置")
                        self._ensure_newnpc_multi_90000_for_task(
                            tasks,
                            log_tag="依卢前置" if tasks.get("yilu_mode") else "任务前置",
                        )
                        self._ensure_newnpc_multi_4_hidden(
                            log_tag="日常前置"
                            if self._tasks_need_daily_newnpc_multi_prep(tasks)
                            else "任务前置"
                        )

                    # ---- 鏂版棩甯?----
                    if tasks.get("new_daily_mode") and (not self.stop_current):
                        self.rotation_handoff_after_chaos_timeout = False
                        variant = str(tasks.get("new_daily_variant") or "1").strip()
                        try:
                            start_step = max(1, int(tasks.get("new_daily_start_step") or 1))
                        except (TypeError, ValueError):
                            start_step = 1
                        skip_tower = not bool(tasks.get("daily_include_hero_tower", False))
                        if start_step > 1:
                            self.emit_and_log(
                                f"[新日常] 启动：方案={variant}，起始步={start_step}，前台点击={use_foreground}",
                                "SYSTEM",
                            )
                        else:
                            self.emit_and_log(
                                f"[新日常] 启动：方案={variant}，前台点击={use_foreground}",
                                "SYSTEM",
                            )
                        try:
                            ok_daily = self.daily_runner.run_new_daily_mode(
                                use_foreground,
                                variant=variant,
                                start_step=start_step,
                                skip_hero_tower=skip_tower if variant == "9" else False,
                                from_daily_chain=False,
                            )
                            if ok_daily and variant == "9" and (not self.stop_current):
                                self.new_daily_chain_completed = True
                        except Exception as e:
                            self.emit_and_log(f"❌ [新日常] 异常：{e}", "ERROR")

                    # ---- 涓€閿柊日常锛堟柟妗?1鈥?锛?---
                    if tasks.get("new_daily_chain_mode") and (not self.stop_current):
                        self.rotation_handoff_after_chaos_timeout = False
                        start_variant = str(tasks.get("new_daily_variant") or "1").strip()
                        try:
                            start_step = max(1, int(tasks.get("new_daily_start_step") or 1))
                        except (TypeError, ValueError):
                            start_step = 1
                        skip_tower = not bool(tasks.get("daily_include_hero_tower", False))
                        self.emit_and_log(
                            f"[一键新日常] 启动：方案={start_variant}，起始步={start_step}，前台点击={use_foreground}，跳过勇者之塔={skip_tower}",
                            "SYSTEM",
                        )
                        daily_already_complete = (
                            self.daily_runner.has_one_click_daily_complete_today()
                        )
                        if daily_already_complete:
                            self.emit_and_log(
                                "✅ [一键新日常] 当前06:00业务日已完成，跳过整条一键日常",
                                "SUCCESS",
                            )
                            self.new_daily_chain_completed = True
                        elif self._prepare_one_click_daily_attempt(1):
                            try:
                                ok_chain = False
                                self.daily_runner.begin_one_click_daily_progress(
                                    start_variant, start_step
                                )
                                if start_variant == "1" and start_step == 1:
                                    self.daily_runner.mark_one_click_daily_progress(
                                        "前置 1/1", variant="1", step=1
                                    )
                                    self.emit_and_log(
                                        "[一键新日常] 方案1第1步前重连回基地门控后选择2-6中的橙色精灵跟随并执行to海洋能量",
                                        "SYSTEM",
                                    )
                                    ok_pre_daily = bool(
                                        self.daily_runner.run_new_daily_1_1_follow_to_ocean_energy(
                                            use_foreground=use_foreground,
                                        )
                                    )
                                    if ok_pre_daily and (not self.stop_current):
                                        self.daily_runner.mark_one_click_daily_progress(
                                            "前置 1/1", variant="1", step=1, completed=True
                                        )
                                        self.emit_and_log(
                                            "[一键新日常] 1/1前置完成，继续执行方案1",
                                            "SYSTEM",
                                        )
                                        ok_chain = bool(self.daily_runner.run_new_daily_chain_1_to_9(
                                            use_foreground,
                                            skip_hero_tower=skip_tower,
                                            from_daily_chain=True,
                                            start_variant=start_variant,
                                            start_step=start_step,
                                            track_progress=True,
                                        ))
                                    elif not self.stop_current:
                                        self.emit_and_log("❌ [新日常链] 1/1前置失败，停止链路", "ERROR")
                                else:
                                    ok_chain = bool(self.daily_runner.run_new_daily_chain_1_to_9(
                                        use_foreground,
                                        skip_hero_tower=skip_tower,
                                        from_daily_chain=True,
                                        start_variant=start_variant,
                                        start_step=start_step,
                                        track_progress=True,
                                    ))
                                if self.stop_current:
                                    self.daily_runner.finish_one_click_daily_progress(
                                        "stopped", "manual stop"
                                    )
                                elif not ok_chain:
                                    self.daily_runner.finish_one_click_daily_progress(
                                        "failed", "daily chain did not complete"
                                    )
                                if ok_chain and (not self.stop_current):
                                    self.new_daily_chain_completed = True
                            except Exception as e:
                                self.daily_runner.finish_one_click_daily_progress(
                                    "failed", f"exception: {e}"
                                )
                                self.emit_and_log(f"❌ [新日常链] 异常：{e}", "ERROR")

                    # ---- 棰勯€夐噸杩烇紙鍗曠嫭鎸夐挳锛氬彧璺戦閫夊墠缃紝涓嶈鎺ュ悗缁棩甯搁摼锛?---
                    if tasks.get("pre_daily_mode") and (not self.stop_current):
                        self.rotation_handoff_after_chaos_timeout = False
                        self.emit_and_log(
                            f"[预选前置] 启动，前台点击={use_foreground}",
                            "SYSTEM",
                        )
                        try:
                            self.dar_route_runner.run_pre_daily_mode(
                                use_foreground,
                                self._stop_event,
                            )
                        except Exception as e:
                            self.emit_and_log(f"❌ [预选日常] 异常：{e}", "ERROR")

                    if tasks.get("bag_putback_test_mode") and (not self.stop_current):
                        test_name = str(tasks.get("bag_putback_test_mode") or "").strip()
                        self.emit_and_log(
                            f"[测试模式] 启动背包探针测试：{test_name}，前台点击={use_foreground}",
                            "SYSTEM",
                        )
                        try:
                            test_ok = bool(
                                self.dar_route_runner.run_bag_putback_test(
                                    test_name,
                                    use_foreground,
                                    self._stop_event,
                                )
                            )
                            self.emit_and_log(
                                f"{'✅' if test_ok else '❌'} [测试模式] 背包探针测试{'完成' if test_ok else '失败'}",
                                "SUCCESS" if test_ok else "ERROR",
                            )
                        except Exception as e:
                            self.emit_and_log(f"❌ [测试模式] 背包探针测试异常：{e}", "ERROR")

                    # ---- 宀氬矚锛堥噸杩?鈫?灞忚斀 鈫?绱壊鏈哄璺熼殢 鈫?宀氬矚寰幆锛?---
                    if tasks.get("lanlan_mode") and (not self.stop_current):
                        self.rotation_handoff_after_chaos_timeout = False
                        self.emit_and_log(
                            f"[岚岚] 启动，前台点击={use_foreground}",
                            "SYSTEM",
                        )
                        try:
                            self._clear_game_tmp_cache(log_tag="岚岚前置")
                            refresh_ok = bool(
                                self.dar_route_runner.run_refresh_login_until_map(
                                    use_foreground,
                                    self._stop_event,
                                )
                            )
                            if not refresh_ok:
                                self.emit_and_log(
                                    "❌ [岚岚] 刷新登录失败",
                                    "ERROR",
                                )
                            elif not self.stop_current:
                                lanlan_target_pet_id = self._lanlan_cyan_pet_for_current_time()
                                if lanlan_target_pet_id is None:
                                    self.emit_and_log("❌ [岚岚] 当前时间不在允许窗口内", "ERROR")
                                    self.stop_current = True
                                    self._stop_event.set()
                                    continue
                                lanlan_primary = int(lanlan_target_pet_id) == 683
                                party_ok = bool(
                                    self.dar_route_runner.ensure_target_cyan_pick_party_from_bag_warehouse_or_rebuild(
                                        lanlan_target_pet_id,
                                        use_foreground,
                                        self._stop_event,
                                        log_tag="岚岚前置·新六宠",
                                        set_cyan_primary=lanlan_primary,
                                        base_pet_id=(
                                            67 if int(lanlan_target_pet_id) != 1459 else 197
                                        ),
                                        verify_primary_166=(
                                            int(lanlan_target_pet_id) != 683
                                        ),
                                    )
                                )
                                if not party_ok:
                                    self.emit_and_log(
                                        "❌ [岚岚] 新六宠校验/补取失败",
                                        "ERROR",
                                    )
                                    self.stop_current = True
                                    self._stop_event.set()
                                    continue
                                self.emit_and_log(
                                    "[岚岚] 前置完成，开始执行脚本",
                                    "SYSTEM",
                                )
                                script_ok = bool(
                                    self.daily_runner.run_lanlan_mode(
                                        use_foreground=use_foreground,
                                    )
                                )
                                if script_ok:
                                    if int(lanlan_target_pet_id) != 1459:
                                        restore_ok = bool(
                                            self.dar_route_runner.replace_current_cyan_with_pick_pet_from_closed_bag(
                                                67,
                                                use_foreground,
                                                self._stop_event,
                                                log_tag=f"岚岚完成·放回{int(lanlan_target_pet_id)}取67",
                                                require_putback_confirmation=True,
                                                set_follow_purple=False,
                                                recover_target_after_take=False,
                                            )
                                        )
                                        if restore_ok:
                                            self.emit_and_log(
                                                "✅ [岚岚] 已完成，战后精灵收回完成",
                                                "SUCCESS",
                                            )
                                        else:
                                            self.emit_and_log(
                                                "❌ [岚岚] 已完成但战后精灵收回失败",
                                                "ERROR",
                                            )
                                            self.stop_current = True
                                            self._stop_event.set()
                                    else:
                                        self.emit_and_log(
                                            "✅ [岚岚周日] 已完成，保持普通六宠，不执行仓库换宠收尾",
                                            "SUCCESS",
                                        )
                                else:
                                    self.emit_and_log(
                                        "⚠️ [岚岚] 未完成，停止任务",
                                        "WARN",
                                    )
                                    self.stop_current = True
                                    self._stop_event.set()
                        except Exception as e:
                            self.emit_and_log(
                                f"❌ [岚岚] 异常：{e}",
                                "ERROR",
                            )

                    # ---- 渚濆崲锛堥噸杩?鈫?108 鈫?依卢橙点 鈫?鏈哄涓夋浜屾妧鑳?鈫?瀹虫€曟崟鎹夛級----
                    if tasks.get("yilu_mode") and (not self.stop_current):
                        self.rotation_handoff_after_chaos_timeout = False
                        self.emit_and_log(
                            f"[依卢] 启动，前台点击={use_foreground}",
                            "SYSTEM",
                        )
                        try:
                            if not self._prepare_yilu_swf_or_stop():
                                continue
                            self._clear_game_tmp_cache(log_tag="依卢前置")
                            refresh_ok = bool(
                                self.dar_route_runner.run_refresh_login_until_map(
                                    use_foreground,
                                    self._stop_event,
                                )
                            )
                            if not refresh_ok:
                                self.emit_and_log(
                                    "❌ [依卢] 刷新登录失败",
                                    "ERROR",
                                )
                            elif not self.stop_current:
                                party_ok = bool(
                                    self.dar_route_runner.ensure_pick_party_from_bag_warehouse_or_skip(
                                        use_foreground,
                                        self._stop_event,
                                        log_tag="依卢前置·新六宠",
                                        verify_primary_166=True,
                                    )
                                )
                                if not party_ok:
                                    self.emit_and_log(
                                        "❌ [依卢] 新六宠校验/补取失败",
                                        "ERROR",
                                    )
                                    self.stop_current = True
                                    self._stop_event.set()
                                    continue
                                self.emit_and_log(
                                    "[依卢] 刷新完成，恢复精灵一/紫色并跟随紫色",
                                    "SYSTEM",
                                )
                                follow_ok = bool(
                                    self.dar_route_runner.recover_pick_party_color_slots_from_closed_bag(
                                        use_foreground,
                                        self._stop_event,
                                        log_tag="依卢前置",
                                        recover_purple=True,
                                        set_follow_purple=True,
                                        bag_already_open=True,
                                    )
                                )
                                if not follow_ok:
                                    self.emit_and_log(
                                        "❌ [依卢] 恢复/跟随失败",
                                        "ERROR",
                                    )
                                    self.stop_current = True
                                    self._stop_event.set()
                                if self.stop_current:
                                    continue
                                script_ok = bool(
                                    self.daily_runner.run_yilu_mode(
                                        use_foreground=use_foreground,
                                    )
                                )
                                if script_ok:
                                    self.emit_and_log(
                                        "✅ [依卢] 已完成",
                                        "SUCCESS",
                                    )
                                else:
                                    self.emit_and_log(
                                        "⚠️ [依卢] 未完成，停止任务",
                                        "WARN",
                                    )
                                    self.stop_current = True
                                    self._stop_event.set()
                        except Exception as e:
                            self.emit_and_log(
                                f"❌ [依卢] 异常：{e}",
                                "ERROR",
                            )

                    # ---- 光螳螂（重连 鈫?102 鈫?鍏夎灣铻傚叆鍙?鈫?涓撶敤鎴樻枟锛?---
                    if tasks.get("light_mantis_mode") and (not self.stop_current):
                        self.rotation_handoff_after_chaos_timeout = False
                        self.emit_and_log(
                            f"[光螳螂] 启动，前台点击={use_foreground}",
                            "SYSTEM",
                        )
                        try:
                            if not self._prepare_light_mantis_swf_or_stop():
                                continue
                            self._clear_game_tmp_cache(log_tag="光螳螂前置")
                            refresh_ok = bool(
                                self.dar_route_runner.run_refresh_login_until_map(
                                    use_foreground,
                                    self._stop_event,
                                )
                            )
                            if not refresh_ok:
                                self.emit_and_log(
                                    "❌ [光螳螂] 刷新登录失败",
                                    "ERROR",
                                )
                            elif not self.stop_current:
                                party_ok = bool(
                                    self.dar_route_runner.ensure_target_cyan_pick_party_from_bag_warehouse_or_rebuild(
                                        197,
                                        use_foreground,
                                    self._stop_event,
                                    log_tag="光螳螂前置·新六宠",
                                    skip_cyan_recovery_when_primary=True,
                                )
                                )
                                if not party_ok:
                                    self.emit_and_log(
                                        "❌ [光螳螂] 新六宠校验/补取失败",
                                        "ERROR",
                                    )
                                    self.stop_current = True
                                    self._stop_event.set()
                                    continue
                                self.emit_and_log(
                                    "[光螳螂] 组队、197恢复和紫色跟随已在目标青色前置完成",
                                    "SUCCESS",
                                )
                                script_ok = bool(
                                    self.daily_runner.run_light_mantis_mode(
                                        use_foreground=use_foreground,
                                    )
                                )
                                if script_ok:
                                    self.emit_and_log(
                                        "✅ [光螳螂] 已完成",
                                        "SUCCESS",
                                    )
                                else:
                                    self.emit_and_log(
                                        "⚠️ [光螳螂] 未完成，停止任务",
                                        "WARN",
                                    )
                                    self.stop_current = True
                                    self._stop_event.set()
                        except Exception as e:
                            self.emit_and_log(
                                f"❌ [光螳螂] 异常：{e}",
                                "ERROR",
                            )

                    # ---- 欢乐谷日常：首次取宠；路线入口失败时内部重连并重走 ----
                    if tasks.get("happy_valley_daily") and (not self.stop_current):
                        self.rotation_handoff_after_chaos_timeout = False
                        self.emit_and_log(
                            f"[欢乐谷日常] 启动：前台点击={use_foreground}",
                            "SYSTEM",
                        )
                        try:
                            if not self._prepare_happy_valley_swf_or_stop():
                                continue
                            script_ok = bool(
                                self.daily_runner.run_happy_valley_daily(
                                    use_foreground=use_foreground,
                                )
                            )
                            if script_ok:
                                self.emit_and_log("[欢乐谷日常] 已完成", "SUCCESS")
                            else:
                                self.emit_and_log(
                                    "[欢乐谷日常] 未完成，停止任务",
                                    "WARN",
                                )
                                self.stop_current = True
                                self._stop_event.set()
                        except Exception as e:
                            self.emit_and_log(f"❌ [欢乐谷日常] 异常：{e}", "ERROR")
                            self.stop_current = True
                            self._stop_event.set()

                    # ---- 日常 ----
                    if tasks.get("daily_chain") and (not self.stop_current):
                        self.rotation_handoff_after_chaos_timeout = False
                        self.emit_and_log(f"[一键日常] 启动：前台点击={use_foreground}", "SYSTEM")
                        self.daily_runner.run_all(
                            background_mode=use_background,
                            include_hero_tower_after_daily=bool(
                                tasks.get("daily_include_hero_tower", False)
                            ),
                        )

                    if tasks.get("one_click_release_mode") and (not self.stop_current):
                        self.rotation_handoff_after_chaos_timeout = False
                        self.emit_and_log(
                            f"[一键放生] 启动，前台点击={use_foreground}", "SYSTEM"
                        )
                        try:
                            if not self._prepare_one_click_release_swf_or_stop():
                                continue
                            refresh_ok = bool(
                                self.dar_route_runner.run_refresh_login_until_map(
                                    use_foreground,
                                    self._stop_event,
                                    include_base_and_map_gate=False,
                                )
                            )
                            if not refresh_ok:
                                self.emit_and_log("❌ [一键放生] 刷新重连/屏蔽失败", "ERROR")
                                self.stop_current = True
                                self._stop_event.set()
                            elif not self.daily_runner.run_one_click_release_mode(
                                use_foreground=use_foreground
                            ):
                                self.emit_and_log("❌ [一键放生] 未完成", "ERROR")
                                self.stop_current = True
                                self._stop_event.set()
                        except Exception as e:
                            self.emit_and_log(f"❌ [一键放生] 异常：{e}", "ERROR")
                            self.stop_current = True
                            self._stop_event.set()

                    # ---- 一键周常：露台放生金豆 → 苏克 → 刷新进实验室购买 → 露台荣誉兑换 ----
                    if tasks.get("chip_gold_honor_mode") and (not self.stop_current):
                        self.rotation_handoff_after_chaos_timeout = False
                        self.emit_and_log(
                            f"[一键周常] 启动，前台点击={use_foreground}", "SYSTEM"
                        )
                        try:
                            self._run_one_click_weekly_task(
                                tasks,
                                use_foreground,
                            )
                        except Exception as e:
                            self.emit_and_log(f"❌ [一键周常] 异常：{e}", "ERROR")
                            self.stop_current = True
                            self._stop_event.set()

                    # ---- 执行脚本 ----
                    if tasks.get("run_script") and (not self.stop_current):
                        script_name = tasks.get("run_script")
                        repeat = self._parse_int(tasks.get("run_repeat", 1), 1)
                        repeat = max(1, repeat)
                        self.emit_and_log(
                            f"[脚本执行] 启动：{script_name}.json，次数={repeat}，前台点击={use_foreground}",
                            "SYSTEM",
                        )
                        if script_name == '放生':
                            self.emit_and_log(
                                "[放生] 启动放生循环",
                                "SYSTEM",
                            )
                            release_ok = self.dar_route_runner.run_release_cycles(
                                repeat=repeat,
                                use_foreground=use_foreground,
                                stop_event=self._stop_event,
                            )
                            if not release_ok:
                                if self.stop_current or self._stop_event.is_set():
                                    self.emit_and_log("[放生] 已停止", "SYSTEM")
                                else:
                                    self.emit_and_log("[放生] 未完成，停止任务", "WARN")
                                self.stop_current = True
                                self._stop_event.set()
                        else:
                            for _ in range(repeat):
                                if self.stop_current:
                                    break
                                self.daily_runner.run_single_script(
                                    script_name, bg_mode=use_background
                                )

                    # ---- 扭蛋 ----
                    if tasks.get("gacha_test") and (not self.stop_current):
                        times = self._parse_int(tasks.get("gacha_test_times", 1), 1)
                        if times < 1:
                            times = 1
                        self.emit_and_log(
                            f"[抽奖测试] 启动：次数={times}，前台点击={use_foreground}",
                            "SYSTEM",
                        )
                        gacha_test_ok = False
                        try:
                            gacha_test_ok = bool(self.daily_runner.run_gacha_probe_test(
                                times=times,
                                background_mode=use_background,
                            ))
                        except Exception as e:
                            self.emit_and_log(f"❌ [抽奖测试] 异常：{e}", "ERROR")
                        if (
                            tasks.get("gacha_test_then_rotation")
                            and (not self.stop_current)
                            and not self._gacha_rotation_handoff_started
                        ):
                            if gacha_test_ok:
                                self.emit_and_log("[抽奖测试] 已完成，启动轮换模式", "SYSTEM")
                            else:
                                self.emit_and_log("⚠️ [抽奖测试] 中途失败，仍启动轮换模式", "WARN")
                            self._run_gacha_rotation_handoff(
                                total=times,
                                use_foreground=use_foreground,
                                reason=(
                                    "全部完成"
                                    if gacha_test_ok
                                    else "途中失败"
                                ),
                            )

                    # ---- 勇者之塔 ----
                    if tasks.get("hero_tower") and (not self.stop_current):
                        self.emit_and_log(
                            f"[勇者之塔] 启动：战斗次数={DEFAULT_HERO_TOWER_BATTLES}，前台点击={use_foreground}",
                            "SYSTEM",
                        )
                        self.daily_runner.run_hero_tower(
                            times=DEFAULT_HERO_TOWER_BATTLES,
                            background_mode=use_background,
                            use_unified_framework=True,
                        )

                    # ---- 闪尼吸能 ----
                    if tasks.get("shanni_energy_drain") and (not self.stop_current):
                        self.emit_and_log(
                            f"[闪尼吸能] 启动：前台点击={use_foreground}",
                            "SYSTEM",
                        )
                        self.daily_runner.run_shanni_energy_drain_loop(
                            use_foreground=use_foreground,
                        )

                    # ---- 澶у笀鏉?----
                    if tasks.get("master_cup_mode") and (not self.stop_current):
                        master_cup_ok = self._run_configured_master_cup(
                            tasks,
                            use_foreground,
                        )
                        if (
                            master_cup_ok
                            and tasks.get("master_cup_then_rotation")
                            and (not self.stop_current)
                        ):
                            self.emit_and_log(
                                "[大师杯] 目标达成，开始轮换模式",
                                "SYSTEM",
                            )
                            is_test_mode = bool(tasks.get("rotation_test_mode", False))
                            rare_slot = str(tasks.get("rotation_rare_slot") or "shuangta").strip().lower()
                            interval_minutes_nieo = float(tasks.get("rotation_interval_minutes_nieo", 60.0) or 60.0)
                            interval_minutes_shuangta = float(tasks.get("rotation_interval_minutes_shuangta", 60.0) or 60.0)
                            hard_limit_sec = float(tasks.get("petswf_hard_limit_sec", 8.5) or 8.5)
                            if is_test_mode:
                                self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_NIEO = interval_minutes_nieo
                                self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_SHUANGTA = interval_minutes_shuangta
                                self.dar_route_runner.PETSWF_TO_PETITEM_HARD_LIMIT_SEC = hard_limit_sec
                            else:
                                self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_NIEO = 60.0
                                self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_SHUANGTA = 60.0
                                self.dar_route_runner.PETSWF_TO_PETITEM_HARD_LIMIT_SEC = 8.5
                            self.dar_route_runner.run_rotation_mode(
                                stop_event=self._stop_event,
                                use_foreground=use_foreground,
                                is_test_mode=is_test_mode,
                                rotation_rare_slot=rare_slot,
                                rotation_resource_enabled=bool(tasks.get("rotation_resource_enabled")),
                                rotation_resource_slug=str(tasks.get("rotation_resource_slug") or "rare:乌索"),
                                rotation_mantis_enabled=bool(tasks.get("rotation_mantis_enabled")),
                                rotation_eit_enabled=bool(tasks.get("rotation_eit_enabled", False)),
                                rotation_nieo_single_map_escape=bool(tasks.get("rotation_nieo_single_map_escape", False)),
                                rotation_nieo_follow_cyan=bool(tasks.get("rotation_nieo_follow_cyan", False)),
                                rotation_full_daily_maintenance=False,
                                initial_swf_full=False,
                            )

                    # ---- 大乱斗 + 轮换链 ----
                    if tasks.get("chaos_rotation_chain") and (not self.stop_current):
                        use_foreground = bool(tasks.get("use_foreground", False))
                        is_test_mode = bool(tasks.get("rotation_test_mode", False))
                        rare_slot = str(tasks.get("rotation_rare_slot") or "shuangta").strip().lower()
                        interval_minutes_nieo = float(tasks.get("rotation_interval_minutes_nieo", 60.0) or 60.0)
                        interval_minutes_shuangta = float(tasks.get("rotation_interval_minutes_shuangta", 60.0) or 60.0)
                        hard_limit_sec = float(tasks.get("petswf_hard_limit_sec", 8.5) or 8.5)
                        if is_test_mode:
                            self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_NIEO = interval_minutes_nieo
                            self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_SHUANGTA = interval_minutes_shuangta
                            self.dar_route_runner.PETSWF_TO_PETITEM_HARD_LIMIT_SEC = hard_limit_sec

                        def _start_rotation():
                            if self.stop_current:
                                return
                            self.dar_route_runner.run_rotation_mode(
                                stop_event=self._stop_event,
                                use_foreground=use_foreground,
                                is_test_mode=is_test_mode,
                                rotation_rare_slot=rare_slot,
                                rotation_resource_enabled=bool(
                                    tasks.get("rotation_resource_enabled")
                                ),
                                rotation_resource_slug=str(
                                    tasks.get("rotation_resource_slug") or "rare:乌索"
                                ),
                                rotation_mantis_enabled=bool(
                                    tasks.get("rotation_mantis_enabled")
                                ),
                                rotation_eit_enabled=bool(
                                    tasks.get("rotation_eit_enabled", False)
                                ),
                                rotation_nieo_single_map_escape=bool(
                                    tasks.get("rotation_nieo_single_map_escape", False)
                                ),
                                rotation_nieo_follow_cyan=bool(
                                    tasks.get("rotation_nieo_follow_cyan", False)
                                ),
                                rotation_full_daily_maintenance=False,
                                initial_swf_full=False,
                            )

                        self.emit_and_log(
                            "[对战链] 启动：大乱斗x2 + 轮换/自定义链",
                            "SYSTEM",
                        )
                        self.daily_runner.run_chaos_then_rotation(
                            use_foreground=use_foreground,
                            rotation_runner=_start_rotation,
                        )

                    # ---- 澶т贡鏂梮2 ----
                    elif tasks.get("chaos_battle_x2") and (not self.stop_current):
                        self.emit_and_log(f"[大乱斗x2] 启动：前台点击={use_foreground}", "SYSTEM")
                        self.daily_runner.run_chaos_battle_x2(use_foreground=use_foreground)

                    # ---- 1v1x2 ----
                    elif tasks.get("1v1_x2") and (not self.stop_current):
                        self.emit_and_log(f"[1v1x2] 启动：前台点击={use_foreground}", "SYSTEM")
                        self._run_daily_mode_with_restart(
                            "1v1x2",
                            lambda: self.daily_runner.run_1v1_x2(
                                use_foreground=use_foreground
                            ),
                            use_foreground,
                        )

                    # ---- 小号对战（刷经验）----
                    if tasks.get("exp_minor_battle") and (not self.stop_current):
                        self.emit_and_log(f"[小号刷经验] 启动：前台点击={use_foreground}", "SYSTEM")
                        self._run_daily_mode_with_restart(
                            "小号刷经验",
                            lambda: self.daily_runner.run_exp_minor_battle(
                                use_foreground=use_foreground
                            ),
                            use_foreground,
                        )

                    if tasks.get("nono_soul_fusion_check") and (not self.stop_current):
                        self.emit_and_log(f"[nono孵化检测] 启动：前台点击={use_foreground}", "SYSTEM")
                        self.daily_runner.run_nono_soul_fusion_check(use_foreground=use_foreground)

                    if tasks.get("daily_start_mode") and (not self.stop_current):
                        daily_start = str(tasks.get("daily_start_mode") or "").strip().lower()
                        happy_phase = str(
                            tasks.get("happy_valley_start_phase") or "water"
                        ).strip().lower()
                        tracks_full_daily_progress = daily_start in {
                            "hatch",
                            "preselect",
                            "happy_valley",
                        }
                        if tracks_full_daily_progress and self.daily_runner.has_one_click_daily_complete_today():
                            self.emit_and_log(
                                "✅ [日常起点] 当前06:00业务日的一键日常已完成，跳过完整链路",
                                "SUCCESS",
                            )
                            self.new_daily_chain_completed = True
                            continue
                        full_daily_ok = False
                        if tracks_full_daily_progress:
                            self.daily_runner.begin_one_click_daily_progress("1", 1)
                            progress_label = {
                                "hatch": "孵化至一键日常",
                                "preselect": "预选至一键日常",
                                "happy_valley": f"欢乐谷{happy_phase}至一键日常",
                            }[daily_start]
                            self.daily_runner.mark_one_click_daily_progress(
                                progress_label,
                                variant="1",
                                step=1,
                            )
                        if daily_start == "hatch":
                            self.emit_and_log("[日常起点] 孵化→预选全链路", "SYSTEM")
                            if self._prepare_hatch_swf_or_stop():
                                hatch_ok = bool(
                                    self._prepare_one_click_daily_attempt(1)
                                    and self.daily_runner.run_hatch_start(
                                        use_foreground=use_foreground
                                    )
                                )
                                if hatch_ok and (not self.stop_current) and self._prepare_hatch_exp_swf_or_stop(
                                    runtime_subset=True
                                ):
                                    full_daily_ok = bool(self.daily_runner.run_collection_daily_mode(
                                        use_foreground=use_foreground,
                                        skip_refresh_login=True,
                                        skip_exp_input=bool(tasks.get("skip_daily_exp_input", False)),
                                        before_attempt=self._prepare_one_click_daily_attempt,
                                    ))
                        elif daily_start == "preselect":
                            self.emit_and_log("[日常起点] 预选：跳过孵化", "SYSTEM")
                            if self._prepare_hatch_exp_swf_or_stop():
                                full_daily_ok = bool(self.daily_runner.run_collection_daily_mode(
                                    use_foreground=use_foreground,
                                    skip_exp_input=bool(tasks.get("skip_daily_exp_input", False)),
                                    before_attempt=self._prepare_one_click_daily_attempt,
                                ))
                        elif daily_start == "happy_valley":
                            self.emit_and_log(
                                f"[日常起点] 欢乐谷直达：{happy_phase}",
                                "SYSTEM",
                            )
                            if self._prepare_happy_valley_swf_or_stop():
                                happy_ok = bool(
                                    self.daily_runner.run_happy_valley_daily(
                                        use_foreground=use_foreground,
                                        start_phase=happy_phase,
                                        skip_pet_preparation=True,
                                    )
                                )
                                if (
                                    happy_ok
                                    and (not self.stop_current)
                                    and self._prepare_one_click_daily_attempt(1)
                                ):
                                    full_daily_ok = bool(
                                        self.daily_runner.run_collection_daily_after_happy_valley(
                                            use_foreground=use_foreground,
                                            log_tag=f"日常起点·欢乐谷{happy_phase}后",
                                        )
                                    )
                        else:
                            self.emit_and_log(
                                f"❌ [日常起点] 未知起点：{daily_start!r}",
                                "ERROR",
                            )
                        if tracks_full_daily_progress:
                            if self.user_stop_requested or self.stop_current:
                                self.daily_runner.finish_one_click_daily_progress(
                                    "stopped", "manual stop"
                                )
                            elif full_daily_ok:
                                self.daily_runner.finish_one_click_daily_progress(
                                    "complete", f"{daily_start} full daily chain complete"
                                )
                                self.new_daily_chain_completed = True
                            else:
                                self.daily_runner.finish_one_click_daily_progress(
                                    "failed", f"{daily_start} full daily chain did not complete"
                                )

                    if tasks.get("hatch_start_mode") and (not self.stop_current):
                        self.emit_and_log(f"[孵化开始] 启动：前台点击={use_foreground}", "SYSTEM")
                        if self._prepare_hatch_swf_or_stop():
                            self.daily_runner.run_hatch_start(use_foreground=use_foreground)

                    if tasks.get("collection_daily_mode") and (not self.stop_current):
                        self.emit_and_log(f"[集合日常] 启动：前台点击={use_foreground}", "SYSTEM")
                        if self._prepare_hatch_exp_swf_or_stop():
                            self.daily_runner.run_collection_daily_mode(
                                use_foreground=use_foreground,
                                skip_exp_input=bool(tasks.get("skip_daily_exp_input", False)),
                                before_attempt=self._prepare_one_click_daily_attempt,
                            )

                    if tasks.get("psychic_exp_purple_mode") and (not self.stop_current):
                        exp_label = str(tasks.get("psychic_exp_label") or "超能经验")
                        exp_value = str(tasks.get("psychic_exp_value") or "5820")
                        exp_category_key = str(tasks.get("psychic_exp_category_key") or "精灵仓库.超能系")
                        self.emit_and_log(
                            f"[超能经验] 启动：标签={exp_label}，仓库从前到后续扫，数值={exp_value}，前台点击={use_foreground}",
                            "SYSTEM",
                        )
                        self.daily_runner.run_psychic_exp_purple_mode(
                            use_foreground=use_foreground,
                            warehouse_category_key=exp_category_key,
                            exp_value=exp_value,
                            mode_label=exp_label,
                        )

                    if tasks.get("fusion_mode") and (not self.stop_current):
                        fusion_label = str(tasks.get("fusion_label") or "卡鲁耶克")
                        raw_seq = tasks.get("fusion_sequence") or [3, 3, 3, 3]
                        if not isinstance(raw_seq, (list, tuple)):
                            raw_seq = [3, 3, 3, 3]
                        fusion_sequence = []
                        for value in list(raw_seq)[:4]:
                            try:
                                iv = int(value)
                            except (TypeError, ValueError):
                                iv = 3
                            fusion_sequence.append(max(1, min(24, iv)))
                        while len(fusion_sequence) < 4:
                            fusion_sequence.append(3)
                        fusion_pink_target = max(1, min(999, self._parse_int(tasks.get("fusion_pink_target", 4), 4)))
                        fusion_limit = max(0, min(999999, self._parse_int(tasks.get("fusion_limit", 0), 0)))
                        fusion_limit_text = str(fusion_limit) if fusion_limit else "不限"
                        self.emit_and_log(
                            f"[融合] 启动：方案={fusion_label}，序列={'-'.join(map(str, fusion_sequence))}，仓库自动翻到底，粉色目标={fusion_pink_target}，次数限制={fusion_limit_text}，前台点击={use_foreground}",
                            "SYSTEM",
                        )
                        fusion_success = False
                        fusion_attempt = 0
                        while not self.stop_current:
                            fusion_attempt += 1
                            if fusion_attempt > 1:
                                self.emit_and_log(
                                    f"[融合] 第 {fusion_attempt} 次重试",
                                    "WARN",
                                )
                            try:
                                if not self._prepare_fusion_special_pet254():
                                    fusion_success = False
                                    self.emit_and_log(
                                        "❌ [融合] 融合SWF准备失败",
                                        "ERROR",
                                    )
                                    break
                                fusion_success = bool(
                                    self.daily_runner.run_fusion_mode(
                                        use_foreground=use_foreground,
                                        sequence=tuple(fusion_sequence),
                                        scheme_label=fusion_label,
                                        primary_category_key=str(tasks.get("fusion_primary_category") or "精灵仓库.飞行系"),
                                        primary_color=str(tasks.get("fusion_primary_color") or "purple"),
                                        secondary_category_key=str(tasks.get("fusion_secondary_category") or "精灵仓库.超能系"),
                                        secondary_color=str(tasks.get("fusion_secondary_color") or "cyan"),
                                        normal_soulbead_id=str(tasks.get("fusion_normal_soulbead") or "1000008"),
                                        pink_target=fusion_pink_target,
                                        fusion_limit=fusion_limit,
                                    )
                                )
                            except Exception as e:
                                fusion_success = False
                                self.emit_and_log(f"❌ [融合] 内部异常，将重连后重试：{e}", "ERROR")
                                import traceback
                                self.emit_and_log(f"[融合] 异常堆栈：{traceback.format_exc()}", "ERROR")

                            if self.stop_current or getattr(self, "user_stop_requested", False):
                                self.emit_and_log("[融合] 检测到手动停止，退出融合任务", "WARN")
                                break
                            if fusion_success:
                                self.emit_and_log("✅ [融合] 已完成", "SUCCESS")
                                if tasks.get("fusion_then_rotation") and (not self.stop_current):
                                    is_test_mode = bool(tasks.get("rotation_test_mode", False))
                                    rare_slot = str(tasks.get("rotation_rare_slot") or "shuangta").strip().lower()
                                    interval_minutes_nieo = float(tasks.get("rotation_interval_minutes_nieo", 60.0) or 60.0)
                                    interval_minutes_shuangta = float(tasks.get("rotation_interval_minutes_shuangta", 60.0) or 60.0)
                                    hard_limit_sec = float(tasks.get("petswf_hard_limit_sec", 8.5) or 8.5)
                                    if is_test_mode:
                                        self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_NIEO = interval_minutes_nieo
                                        self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_SHUANGTA = interval_minutes_shuangta
                                        self.dar_route_runner.PETSWF_TO_PETITEM_HARD_LIMIT_SEC = hard_limit_sec
                                    else:
                                        self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_NIEO = 60.0
                                        self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_SHUANGTA = 60.0
                                        self.dar_route_runner.PETSWF_TO_PETITEM_HARD_LIMIT_SEC = 8.5
                                    self.dar_route_runner.run_rotation_mode(
                                        stop_event=self._stop_event,
                                        use_foreground=use_foreground,
                                        is_test_mode=is_test_mode,
                                        rotation_rare_slot=rare_slot,
                                        rotation_resource_enabled=bool(tasks.get("rotation_resource_enabled")),
                                        rotation_resource_slug=str(tasks.get("rotation_resource_slug") or "rare:乌索"),
                                        rotation_mantis_enabled=bool(tasks.get("rotation_mantis_enabled")),
                                        rotation_eit_enabled=bool(tasks.get("rotation_eit_enabled", False)),
                                        rotation_nieo_single_map_escape=bool(tasks.get("rotation_nieo_single_map_escape", False)),
                                        rotation_nieo_follow_cyan=bool(tasks.get("rotation_nieo_follow_cyan", False)),
                                        rotation_full_daily_maintenance=False,
                                        initial_swf_full=False,
                                    )
                                break

                            self.emit_and_log(
                                "[融合] 未完成，刷新后重试",
                                "WARN",
                            )
                            time.sleep(0.5)

                    # ---- 闆蜂紛鐗硅 / 嘟嘟卡拉对战 ----
                    if tasks.get("leiyi_training") and (not self.stop_current):
                        tb_mode = tasks.get("training_battle_mode") or "leiyi"
                        if tb_mode not in ("leiyi", "dudukala", "laokemengde"):
                            tb_mode = "leiyi"
                        if tb_mode == "dudukala":
                            self.emit_and_log(
                                f"[训练对战] 启动：模式={tb_mode}，前台点击={use_foreground}",
                                "SYSTEM",
                            )
                            loop_count_dummy = 1
                        else:
                            loop_count_dummy = self._parse_int(tasks.get("leiyi_loop_count", 10), 10)
                            loop_count_dummy = max(1, min(999, loop_count_dummy))
                            self.emit_and_log(
                                f"[训练对战] 启动：模式={tb_mode}，循环={loop_count_dummy}，前台点击={use_foreground}",
                                "SYSTEM",
                            )
                        self._prepare_swf_fill_union()
                        self._run_daily_mode_with_restart(
                            "训练对战",
                            lambda: self.daily_runner.run_leiyi_training(
                                loop_count=loop_count_dummy,
                                use_foreground=use_foreground,
                                training_battle_mode=tb_mode,
                            ),
                            use_foreground,
                        )

                    # ---- 鐗硅寰幆 ----
                    if tasks.get("teixun_loop") and (not self.stop_current):
                        self.emit_and_log(f"[特训循环] 启动：前台点击={use_foreground}", "SYSTEM")
                        self._run_daily_mode_with_restart(
                            "特训循环",
                            lambda: self.daily_runner.run_teixun_loop(
                                use_foreground=use_foreground
                            ),
                            use_foreground,
                        )

                    # ---- 训练室：升级直到目标（优先于单批次）----
                    if tasks.get("training_until_level") and (not self.stop_current):
                        battles_per_batch = self._parse_int(tasks.get("battles_per_batch", 30), 30)
                        recover_every = self._parse_int(tasks.get("recover_every", 5), 5)
                        target_level = self._parse_int(tasks.get("target_level", 100), 100)

                        debug_stop_level = self._parse_int(tasks.get("debug_stop_level", None), None)

                        # 训练室直升100模式：强制battles_per_batch=30
                        battles_per_batch = 30
                        recover_every = max(0, min(30, recover_every))
                        target_level = max(1, min(100, target_level))
                        if debug_stop_level is not None:
                            debug_stop_level = max(1, min(100, debug_stop_level))

                        self.emit_and_log(
                            f"[训练升级] 启动：目标等级={target_level}，批次={battles_per_batch}，恢复间隔={recover_every}，调试停止={debug_stop_level}，前台点击={use_foreground}",
                            "SYSTEM",
                        )
                        self._prepare_swf_fill_union()
                        self.training_level_runner.run_training_until_level(
                            target_level=target_level,
                            battles_per_batch=battles_per_batch,
                            recover_every=recover_every,
                            debug_stop_level=debug_stop_level,
                            use_foreground=use_foreground,
                        )

                    # ---- 训练室：单批次练级 ----
                    elif tasks.get("training_level") and (not self.stop_current):
                        max_battles = self._parse_int(tasks.get("max_battles", 30), 30)
                        recover_every = self._parse_int(tasks.get("recover_every", 5), 5)
                        debug_stop_level = self._parse_int(tasks.get("debug_stop_level", None), None)

                        # clamp
                        max_battles = max(1, min(30, max_battles))
                        recover_every = max(0, min(30, recover_every))
                        if debug_stop_level is not None:
                            debug_stop_level = max(1, min(100, debug_stop_level))

                        self.emit_and_log(
                            f"[训练升级] 启动：最大战斗={max_battles}，恢复间隔={recover_every}，调试停止={debug_stop_level}，前台点击={use_foreground}",
                            "SYSTEM",
                        )
                        self._prepare_swf_fill_union()
                        self.training_level_runner.run_training_level(
                            max_battles=max_battles,
                            recover_every=recover_every,
                            debug_stop_level=debug_stop_level,
                            use_foreground=use_foreground,
                        )

                    # ---- 螳螂捕捉(TEST) 按钮（你 dashboard 里叫 dar_route_test）----
                    if tasks.get("dar_route_test") and (not self.stop_current):
                        self.emit_and_log("[稀有路线测试] 启动默认光螳螂方案", "SYSTEM")
                        profile = DEFAULT_PROFILE_MANTIS
                        # 兼容 DarRouteRunner 可能叫 run_test / run
                        if hasattr(self.dar_route_runner, "run_test"):
                            self.dar_route_runner.run_test(
                                stop_event=self._stop_event,
                                use_foreground=use_foreground,
                                profile=profile,
                            )
                        else:
                            self._run_dar_mode_with_restart(
                                "稀有路线测试",
                                lambda: self.dar_route_runner.run(
                                    stop_event=self._stop_event,
                                    use_foreground=use_foreground,
                                    profile=profile,
                                ),
                            )

                    # ---- 野外捕捉（螳螂/稀有精灵）----
                    if tasks.get("wild_capture") and (not self.stop_current):
                        profile_name = (tasks.get("wild_capture_profile") or "mantis").lower().strip()
                        profile = self._resolve_wild_capture_profile(profile_name)

                        self._prepare_swf_wild(profile)
                        self.emit_and_log(
                            f"[野外捕捉] 启动：方案={profile_name}（{profile.name}），前台点击={use_foreground}",
                            "SYSTEM",
                        )

                        fusion_ok, fusion_handled = self._run_nono_fusion_pre_mode_check(
                            use_foreground, f"野外捕捉-{profile.name}"
                        )
                        if not fusion_ok:
                            self.stop_current = True
                            self._stop_event.set()

                        supports_rare_pre = self._wild_profile_supports_pre_reconnect(
                            profile_name, profile
                        )
                        do_rare_pre = supports_rare_pre and (
                            fusion_handled
                            or not bool(tasks.get("wild_skip_rotation_pre", False))
                        )
                        if do_rare_pre:
                            if profile_name == "flash_pipi":
                                while not self.stop_current and not self._stop_event.is_set():
                                    ok = self.dar_route_runner._execute_flash_pipi_pre_rotation_reconnect(
                                        use_foreground=use_foreground,
                                        stop_event=self._stop_event,
                                    )
                                    if ok:
                                        break
                                    self.emit_and_log(
                                        "[野外捕捉] 前置重连失败，重试完整流程",
                                        "WARN",
                                    )
                            elif profile_name == "mantis":
                                while not self.stop_current and not self._stop_event.is_set():
                                    ok = self.dar_route_runner._execute_mantis_pre_rotation_reconnect(
                                        use_foreground=use_foreground,
                                        stop_event=self._stop_event,
                                    )
                                    if ok:
                                        break
                                    self.emit_and_log(
                                        "[野外捕捉/光螳螂] 前置重连失败，重试完整流程",
                                        "WARN",
                                    )
                            elif self._wild_uses_unified_pre(profile_name, profile):
                                while not self.stop_current and not self._stop_event.is_set():
                                    ok = self.dar_route_runner._execute_unified_wild_rare_pre_reconnect(
                                        profile,
                                        use_foreground=use_foreground,
                                        stop_event=self._stop_event,
                                    )
                                    if ok:
                                        break
                                    self.emit_and_log(
                                        "[野外捕捉/统一前置] 前置重连失败，重试",
                                        "WARN",
                                    )

                        if not self.stop_current and not self._stop_event.is_set():
                            self._run_dar_mode_with_restart(
                                f"野外捕捉-{profile.name}",
                                lambda: self.dar_route_runner.run(
                                    stop_event=self._stop_event,
                                    use_foreground=use_foreground,
                                    profile=profile,
                                ),
                                prepare_after_restart=(
                                    lambda: self._prepare_wild_mode_after_restart(
                                        profile_name,
                                        profile,
                                        use_foreground,
                                    )
                                )
                                if supports_rare_pre
                                else None,
                            )

                    # ---- 鏅鸿兘杩借釜娴嬭瘯 ----
                    if tasks.get("smart_tracking_test") and (not self.stop_current):
                        profile_name = (tasks.get("wild_capture_profile") or "dugulu").lower().strip()
                        profile = self._resolve_wild_capture_profile(profile_name)

                        self._prepare_swf_wild(profile)
                        self.emit_and_log(f"[智能追踪测试] 启动：方案={profile_name}，前台点击={use_foreground}", "SYSTEM")
                        self._run_dar_mode_with_restart(
                            f"智能追踪-{profile.name}",
                            lambda: self.dar_route_runner.run(
                                stop_event=self._stop_event,
                                use_foreground=use_foreground,
                                profile=profile,
                                smart_tracking_mode=True,
                            ),
                            prepare_after_restart=lambda: self._prepare_wild_mode_after_restart(
                                profile_name,
                                profile,
                                use_foreground,
                            ),
                        )

                    # ---- 杞崲閾炬祴璇曪紙鐧藉ぉ妯″紡鈫掍紛鐗光啋铻宠瀭鈫掑凹濂モ啋绋€鏈夛級----
                    if tasks.get("rotation_chain_test") and (not self.stop_current):
                        use_foreground = bool(tasks.get("use_foreground", False))
                        resource_slug = str(tasks.get("rotation_resource_slug") or "rare:乌索")
                        rare_slot = str(tasks.get("rotation_rare_slot") or "shuangta").strip().lower()
                        self.emit_and_log(
                            f"[轮换链测试] 启动：资源={resource_slug}，稀有槽={rare_slot}",
                            "SYSTEM",
                        )
                        self.dar_route_runner.run_rotation_chain_test(
                            stop_event=self._stop_event,
                            use_foreground=use_foreground,
                            resource_slug=resource_slug,
                            rare_slot=rare_slot,
                            follow_cyan=bool(
                                tasks.get("rotation_nieo_follow_cyan", False)
                            ),
                        )

                    # ---- 双塔尼奥轮换模式 ----
                    if tasks.get("rotation_mode") and (not self.stop_current):
                        is_test_mode = bool(tasks.get("rotation_test_mode", False))
                        full_daily_maintenance = bool(
                            tasks.get("rotation_full_daily_maintenance", False)
                        )
                        mode_text = "测试模式（固定时间间隔切换）" if is_test_mode else "正式模式（根据北京时间自动切换）"
                        rare_slot = str(tasks.get("rotation_rare_slot") or "shuangta").strip().lower()
                        profile = resolve_wild_capture_profile(self.project_root, rare_slot)
                        rare_lbl = profile.name
                        entry_name = "测试完整轮换" if full_daily_maintenance else "轮换重连模式"
                        self.emit_and_log(
                            f"[{entry_name}] 启动：测试模式={is_test_mode}，非尼奥稀有={rare_lbl}",
                            "SYSTEM",
                        )
                        use_foreground = bool(tasks.get("use_foreground", False))
                        if full_daily_maintenance:
                            if not self.run_rotation_startup_daily_precheck(use_foreground):
                                self.emit_and_log("❌ [轮换前置] 日常未完成，取消启动轮换", "ERROR")
                                self.stop_current = True
                                self._stop_event.set()
                        elif bool(tasks.get("rotation_require_one_click_daily", False)):
                            if not self.run_rotation_button_daily_precheck(
                                use_foreground,
                                skip_exp_input=bool(
                                    tasks.get("skip_daily_exp_input", True)
                                ),
                            ):
                                self.stop_current = True
                                self._stop_event.set()
                        else:
                            self.emit_and_log(
                                "[轮换重连模式] 跳过一键日常/光螳螂完整检测；进入目标模式前仍检查到期依卢/岚岚；融合重连检查已暂时禁用",
                                "INFO",
                            )
                        # Temporary policy: do not start or resume Nono fusion from
                        # either rotation entry.  Reconnect-time checks are also
                        # disabled by DarRouteRunner while rotation is active.
                        self.emit_and_log(
                            "[轮换模式] 暂时禁用融合重连检查和 Nono 融合执行",
                            "INFO",
                        )
                        fusion_ok, _fusion_handled = True, False
                        if not fusion_ok:
                            self.stop_current = True
                            self._stop_event.set()

                        # 测试模式参数
                        interval_minutes_nieo = float(tasks.get("rotation_interval_minutes_nieo", 60.0) or 60.0)
                        interval_minutes_shuangta = float(tasks.get("rotation_interval_minutes_shuangta", 60.0) or 60.0)
                        hard_limit_sec = float(tasks.get("petswf_hard_limit_sec", 8.5) or 8.5)
                        if is_test_mode:
                            self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_NIEO = interval_minutes_nieo
                            self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_SHUANGTA = interval_minutes_shuangta
                            self.dar_route_runner.PETSWF_TO_PETITEM_HARD_LIMIT_SEC = hard_limit_sec
                            self.emit_and_log(
                                f"[轮换模式测试] 尼奥间隔={interval_minutes_nieo}分钟，非尼奥稀有间隔={interval_minutes_shuangta}分钟，硬限制={hard_limit_sec}s",
                                "INFO",
                            )
                        else:
                            self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_NIEO = 60.0
                            self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_SHUANGTA = 60.0
                            self.dar_route_runner.PETSWF_TO_PETITEM_HARD_LIMIT_SEC = 8.5

                        if not self.stop_current and not self._stop_event.is_set():
                            self.dar_route_runner.run_rotation_mode(
                                stop_event=self._stop_event,
                                use_foreground=use_foreground,
                                is_test_mode=is_test_mode,
                                rotation_rare_slot=rare_slot,
                                rotation_resource_enabled=bool(
                                    tasks.get("rotation_resource_enabled")
                                ),
                                rotation_resource_slug=str(
                                    tasks.get("rotation_resource_slug") or "rare:乌索"
                                ),
                                rotation_mantis_enabled=bool(
                                    tasks.get("rotation_mantis_enabled")
                                ),
                                rotation_eit_enabled=bool(
                                    tasks.get("rotation_eit_enabled", False)
                                ),
                                rotation_nieo_single_map_escape=bool(
                                    tasks.get("rotation_nieo_single_map_escape", False)
                                ),
                                rotation_nieo_follow_cyan=bool(
                                    tasks.get("rotation_nieo_follow_cyan", False)
                                ),
                                rotation_full_daily_maintenance=full_daily_maintenance,
                                initial_swf_full=True,
                            )

                    # ---- 🔗 尼奥资源五连（每项黄胜60次后进入普通轮换）----
                    if tasks.get("nieo_resource_chain") and (not self.stop_current):
                        use_foreground = bool(tasks.get("use_foreground", False))
                        fusion_ok, _fusion_handled = (
                            self._run_nono_fusion_pre_mode_check(
                                use_foreground,
                                "尼奥资源五连",
                            )
                        )
                        chain_completed = False
                        if not fusion_ok:
                            self.stop_current = True
                            self._stop_event.set()
                        else:
                            self._prepare_swf_fill_union()
                            chain_completed = self._run_nieo_resource_chain(
                                use_foreground,
                                single_map=bool(
                                    tasks.get("nieo_single_map_escape", True)
                                ),
                                follow_cyan=bool(
                                    tasks.get("nieo_follow_cyan", False)
                                ),
                            )

                        if (
                            chain_completed
                            and not self.stop_current
                            and not self._stop_event.is_set()
                        ):
                            self.emit_and_log(
                                "🔄 [资源五连] 五个模式均已完成，启动普通轮换重连模式",
                                "SYSTEM",
                            )
                            self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_NIEO = 60.0
                            self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_SHUANGTA = 60.0
                            self.dar_route_runner.PETSWF_TO_PETITEM_HARD_LIMIT_SEC = 8.5
                            self.dar_route_runner.run_rotation_mode(
                                stop_event=self._stop_event,
                                use_foreground=use_foreground,
                                is_test_mode=False,
                                rotation_rare_slot=str(
                                    tasks.get("rotation_rare_slot") or "shuangta"
                                ),
                                rotation_resource_enabled=bool(
                                    tasks.get("rotation_resource_enabled")
                                ),
                                rotation_resource_slug=str(
                                    tasks.get("rotation_resource_slug")
                                    or "rare:乌索"
                                ),
                                rotation_mantis_enabled=bool(
                                    tasks.get("rotation_mantis_enabled")
                                ),
                                rotation_eit_enabled=bool(
                                    tasks.get("rotation_eit_enabled", False)
                                ),
                                rotation_nieo_single_map_escape=bool(
                                    tasks.get(
                                        "rotation_nieo_single_map_escape",
                                        False,
                                    )
                                ),
                                rotation_nieo_follow_cyan=bool(
                                    tasks.get("rotation_nieo_follow_cyan", False)
                                ),
                                rotation_full_daily_maintenance=False,
                                initial_swf_full=False,
                            )

                    # ---- 🌊 尼奥模式（10/11地图循环）----
                    if tasks.get("nieo_mode") and (not self.stop_current):
                        test_nieo = tasks.get("test_nieo", False)
                        test_nie = tasks.get("test_nie", False)
                        skip_nie_77 = False
                        nieo_pre_rotation_first = not bool(
                            tasks.get("nieo_skip_pre_rotation", False)
                        )
                        nieo_test_force_switch = bool(tasks.get("nieo_test_force_switch", False))
                        nieo_single_map_escape = bool(tasks.get("nieo_single_map_escape", True))
                        nieo_follow_cyan = bool(tasks.get("nieo_follow_cyan", False))
                        test_msg = ""
                        if test_nieo:
                            test_msg += " [测试尼奥模式]"
                        if test_nie:
                            test_msg += " [测试尼尔模式]"
                        if nieo_pre_rotation_first:
                            test_msg += " [前置重连]"
                        else:
                            test_msg += " [跳过前置]"
                        if nieo_test_force_switch:
                            test_msg += " [强制map10到map11切换"
                        if nieo_single_map_escape:
                            test_msg += " [单图]"
                        if nieo_follow_cyan:
                            test_msg += " [跟随青色]"
                        sub_mode = str(tasks.get("nieo_sub_mode") or "nieo").strip().lower()
                        is_pure_energy = sub_mode == "pure_energy"
                        custom_slug = tasks.get("nieo_custom_slug") or (
                            sub_mode if sub_mode not in ("nieo", "pure_energy") else None
                        )
                        yellow60_handoff_enabled = bool(
                            tasks.get("nieo_yellow60_to_rotation", False)
                        ) and bool(is_pure_energy or custom_slug)
                        self.dar_route_runner.configure_resource_yellow_rotation_handoff(
                            yellow60_handoff_enabled
                        )

                        fusion_mode_name = "纯净能量" if is_pure_energy else (
                            f"自定义尼奥-{custom_slug}" if custom_slug else "尼奥模式"
                        )
                        fusion_ok, fusion_handled = self._run_nono_fusion_pre_mode_check(
                            use_foreground, fusion_mode_name
                        )
                        if not fusion_ok:
                            self.stop_current = True
                            self._stop_event.set()
                        elif fusion_handled:
                            # Fusion changes the party and location.  The normal pre-entry
                            # pipeline must restore the party and run the corresponding to-script.
                            nieo_pre_rotation_first = True

                        if (not custom_slug) and (not is_pure_energy):
                            self._prepare_swf_fill_union_nieo()
                        else:
                            self._prepare_swf_fill_union()

                        # 前置重连：尼奥走 to尼奥/map11；纯净能量走 to纯净能量/map26；自定义走 manifest.to_script
                        if nieo_pre_rotation_first:
                            custom_profile = None
                            if custom_slug:
                                from core.nieo_mode_registry import get_profile
                                custom_profile = get_profile(
                                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    custom_slug,
                                )
                            while not self.stop_current and not self._stop_event.is_set():
                                ok = self.dar_route_runner._execute_nieo_pre_rotation_reconnect(
                                    use_foreground=use_foreground,
                                    stop_event=self._stop_event,
                                    pem_route=is_pure_energy,
                                    to_script=(
                                        custom_profile.to_script if custom_profile else None
                                    ),
                                    expected_map_id=(
                                        custom_profile.map_a_id if custom_profile else None
                                    ),
                                    follow_cyan=nieo_follow_cyan,
                                )
                                if ok:
                                    break
                                tag = "纯净能量" if is_pure_energy else (
                                    f"自定义尼奥({custom_slug})" if custom_profile else '尼奥'
                                )
                                self.emit_and_log(f"[尼奥前置] 重连失败，重试：标记={tag}", "WARN")

                        if not self.stop_current and not self._stop_event.is_set():
                            if custom_slug:
                                from core.nieo_mode_registry import get_profile, load_all_nieo_modes
                                root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                                load_all_nieo_modes(root, reload=True)
                                profile = get_profile(root, custom_slug)
                                if profile is None:
                                    self.emit_and_log(
                                        f"❌ 未找到自定义尼奥方案：slug={custom_slug}",
                                        "ERROR",
                                    )
                                else:
                                    self.emit_and_log(
                                        f"[自定义尼奥] 启动：方案={profile.name} {test_msg}",
                                        "SYSTEM",
                                    )
                                    self._run_dar_mode_with_restart(
                                        f"自定义尼奥-{profile.name}",
                                        lambda: self.dar_route_runner.run_configured_nieo_mode(
                                            profile,
                                            stop_event=self._stop_event,
                                            use_foreground=use_foreground,
                                            skip_nie_77=skip_nie_77,
                                            single_map=nieo_single_map_escape,
                                            follow_cyan=nieo_follow_cyan,
                                        ),
                                        prepare_after_restart=lambda: self.dar_route_runner._execute_nieo_pre_rotation_reconnect(
                                            use_foreground=use_foreground,
                                            stop_event=self._stop_event,
                                            pem_route=False,
                                            to_script=profile.to_script,
                                            expected_map_id=profile.map_a_id,
                                            reason=f"{profile.name}-模式重启",
                                            follow_cyan=nieo_follow_cyan,
                                        ),
                                    )
                            elif is_pure_energy:
                                self.emit_and_log(f"[纯净能量] 启动 {test_msg}", "SYSTEM")
                                self._run_dar_mode_with_restart(
                                    "纯净能量",
                                    lambda: self.dar_route_runner.run_pure_energy_resource_mode(
                                        stop_event=self._stop_event,
                                        use_foreground=use_foreground,
                                        nieo_pre_rotation_first=bool(nieo_pre_rotation_first),
                                        skip_nie_77=skip_nie_77,
                                        single_map=nieo_single_map_escape,
                                        follow_cyan=nieo_follow_cyan,
                                    ),
                                    prepare_after_restart=lambda: self.dar_route_runner._execute_nieo_pre_rotation_reconnect(
                                        use_foreground=use_foreground,
                                        stop_event=self._stop_event,
                                        pem_route=True,
                                        reason="纯净能量-模式重启",
                                        follow_cyan=nieo_follow_cyan,
                                    ),
                                )
                            else:
                                self.emit_and_log(f"[尼奥模式] 启动 {test_msg}", "SYSTEM")
                                self._run_dar_mode_with_restart(
                                    "尼奥模式",
                                    lambda: self.dar_route_runner.run_nieo_mode(
                                        stop_event=self._stop_event,
                                        use_foreground=use_foreground,
                                        test_nieo=test_nieo,
                                        test_nie=test_nie,
                                        skip_nie_77=skip_nie_77,
                                        nieo_test_force_switch=nieo_test_force_switch,
                                        nieo_single_map_escape=nieo_single_map_escape,
                                        follow_cyan=nieo_follow_cyan,
                                    ),
                                    prepare_after_restart=lambda: self.dar_route_runner._execute_nieo_pre_rotation_reconnect(
                                        use_foreground=use_foreground,
                                        stop_event=self._stop_event,
                                        pem_route=False,
                                        reason="尼奥模式-重启",
                                        follow_cyan=nieo_follow_cyan,
                                    ),
                                )

                    # ---- 🎮 挂机对战模式 ----
                    if (
                        tasks.get("nieo_mode")
                        and yellow60_handoff_enabled
                        and self.dar_route_runner.consume_resource_yellow_rotation_handoff()
                        and (not self.stop_current)
                        and (not self._stop_event.is_set())
                    ):
                        self.emit_and_log(
                            "🔁 [资源黄胜] 已达 60 次，直接启动普通轮换重连模式",
                            "SYSTEM",
                        )
                        self.dar_route_runner.run_rotation_mode(
                            stop_event=self._stop_event,
                            use_foreground=use_foreground,
                            is_test_mode=False,
                            rotation_rare_slot=str(
                                tasks.get("rotation_rare_slot") or "shuangta"
                            ),
                            rotation_resource_enabled=bool(
                                tasks.get("rotation_resource_enabled")
                            ),
                            rotation_resource_slug=str(
                                tasks.get("rotation_resource_slug") or "pure_energy"
                            ),
                            rotation_mantis_enabled=bool(
                                tasks.get("rotation_mantis_enabled")
                            ),
                            rotation_eit_enabled=bool(
                                tasks.get("rotation_eit_enabled", False)
                            ),
                            rotation_nieo_single_map_escape=bool(
                                tasks.get("rotation_nieo_single_map_escape", False)
                            ),
                            rotation_nieo_follow_cyan=bool(
                                tasks.get("rotation_nieo_follow_cyan", False)
                            ),
                            rotation_full_daily_maintenance=False,
                            initial_swf_full=False,
                        )

                    if tasks.get("afk_battle_mode") and (not self.stop_current):
                        afk_sub = tasks.get("afk_sub_mode", "normal")
                        labels = {"normal": "normal", "defeat": "defeat", "rare": "rare", "nieo": "nieo"}
                        self.emit_and_log(f"[挂机对战] 启动：模式={labels.get(afk_sub, afk_sub)}", "SYSTEM")
                        if str(afk_sub).strip().lower() in ("rare", "nieo"):
                            fusion_ok, _fusion_handled = self._run_nono_fusion_pre_mode_check(
                                use_foreground, f"挂机-{afk_sub}"
                            )
                            if not fusion_ok:
                                self.stop_current = True
                                self._stop_event.set()
                        if not self.stop_current and not self._stop_event.is_set():
                            self._run_dar_mode_with_restart(
                                f"挂机对战-{labels.get(afk_sub, afk_sub)}",
                                lambda: self.dar_route_runner.run_afk_battle_mode(
                                    stop_event=self._stop_event,
                                    use_foreground=use_foreground,
                                    sub_mode=afk_sub,
                                ),
                            )

                    # ---- 🌿 娲诲姩绮剧伒妯″紡锛堝惈浼婄壒锛?---
                    if (tasks.get("event_pet_mode") or tasks.get("eit_mode")) and (
                        not self.stop_current
                    ):
                        from core.event_pet_mode_registry import get_profile

                        slug = str(
                            tasks.get("event_pet_slug") or "yite"
                        ).strip()
                        skip_pre = bool(
                            tasks.get("event_pet_skip_pre_rotation")
                            or tasks.get("eit_skip_pre_rotation")
                        )
                        root = os.path.dirname(
                            os.path.dirname(os.path.abspath(__file__))
                        )
                        profile = get_profile(root, slug)
                        if profile is None and tasks.get("eit_mode"):
                            self._run_dar_mode_with_restart(
                                "伊特",
                                lambda: self.dar_route_runner.run_eit_mode(
                                    stop_event=self._stop_event,
                                    use_foreground=use_foreground,
                                    skip_pre_rotation=skip_pre,
                                ),
                            )
                        elif profile is None:
                            self.emit_and_log(
                                f"❌ 未找到活动精灵方案：slug={slug}",
                                "ERROR",
                            )
                        else:
                            self.emit_and_log(
                                f"[活动精灵] 启动：方案={profile.name}，跳过前置={skip_pre}",
                                "SYSTEM",
                            )
                            self._run_dar_mode_with_restart(
                                f"活动精灵-{profile.name}",
                                lambda: self.dar_route_runner.run_event_pet_mode(
                                    profile,
                                    stop_event=self._stop_event,
                                    use_foreground=use_foreground,
                                    skip_pre_rotation=skip_pre,
                                ),
                                prepare_after_restart=lambda: self.dar_route_runner._execute_event_pet_pre_reconnect(
                                    profile,
                                    use_foreground,
                                    self._stop_event,
                                ),
                            )

                    # ---- 🏆 巅峰对战模式（排位/娱乐）----
                    if tasks.get("pinnacle_mode") and (not self.stop_current):
                        pinnacle_sub = tasks.get("pinnacle_mode_type", "rank")
                        pinnacle_small_account_mode = bool(
                            tasks.get("pinnacle_small_account_mode", False)
                        )
                        label = "排位" if pinnacle_sub == "rank" else "娱乐"
                        self.emit_and_log(f"[巅峰对战] 启动：模式={pinnacle_sub}", "SYSTEM")
                        if not self.stop_current and not self._stop_event.is_set():
                            self.dar_route_runner.run_pinnacle_mode(
                                stop_event=self._stop_event,
                                use_foreground=use_foreground,
                                mode=pinnacle_sub,
                                small_account_mode=pinnacle_small_account_mode,
                            )

                    # ---- 🧪 尼尔家族测试 ----
                    if tasks.get("nie_family_test") and (not self.stop_current):
                        try:
                            nie_family_test_type = tasks.get("nie_family_test_type", "nie")
                            if nie_family_test_type == "nie":
                                # 尼尔模式（77/310，第二回合切精灵三）
                                nie_family_id = 77  # 使用77作为代表
                                self.emit_and_log("[尼尔家族测试] 启动：尼尔", "SYSTEM")
                            elif nie_family_test_type == "ni":
                                # 尼奥模式（416，第二回合切精灵二）
                                nie_family_id = 416
                                self.emit_and_log("[尼尔家族测试] 启动：尼", "SYSTEM")
                            else:
                                nie_family_id = 77  # 榛樿
                                self.emit_and_log("[尼尔家族测试] 启动：默认", "SYSTEM")

                            self.emit_and_log(f"[尼尔家族测试] 调用 run_nie_family_test：id={nie_family_id}", "INFO")

                            # 添加调试输出
                            import sys
                            print(f"[DEBUG] About to call run_nie_family_test", file=sys.stderr, flush=True)

                            self.dar_route_runner.run_nie_family_test(
                            stop_event=self._stop_event,
                            use_foreground=use_foreground,
                                nie_family_id=nie_family_id,
                            )

                            print(f"[DEBUG] run_nie_family_test returned", file=sys.stderr, flush=True)
                            self.emit_and_log("✅ 尼尔家族测试已完成", "INFO")
                        except Exception as e:
                            self.emit_and_log(f"❌ [尼尔家族测试] 异常：{e}", "ERROR")
                            import traceback
                            self.emit_and_log(f"[尼尔家族测试] 异常堆栈：{traceback.format_exc()}", "ERROR")

                    # ---- 🔧 鏍″噯娴嬭瘯锛堢函灞忓箷妫€娴嬶級 ----
                    if tasks.get("calibration_test") and (not self.stop_current):
                        from core.unified_battle_framework import UnifiedBattleFramework
                        from config import TEMPLATES_PATH
                        framework = UnifiedBattleFramework(self, self.regions, TEMPLATES_PATH)
                        self.emit_and_log("🔧 启动校准测试（纯屏幕检测）", "SYSTEM")
                        success = framework.run_calibration_test(use_foreground=use_foreground)
                        if success:
                            self.emit_and_log("✅ 校准测试完成", "SUCCESS")
                        else:
                            self.emit_and_log("❌ 校准测试失败", "ERROR")

            except Exception as e:
                self.emit_and_log(f"❌ 任务执行异常：{e}", "ERROR")

            # 娓呯┖浠诲姟
            with self._task_lock:
                self.active_tasks = {}

            self.is_running = False
            self.stop_current = False
            self.is_paused = False
            self._stop_event.clear()
            self._stop_keyboard_listener()

            self.emit_and_log("[引擎] 当前任务结束，已空闲", "ENGINE")
            self.state_signal.emit("IDLE")
            self.task_done_signal.emit()

        self.emit_and_log("[引擎] 自动化线程已停止", "ENGINE")
