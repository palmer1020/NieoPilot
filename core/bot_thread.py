# core/bot_thread.py
import math
import os
import time
import threading
from typing import Dict, Optional

from PyQt6.QtCore import QThread, pyqtSignal

from core.logger import logger
from core.utils import window_manager
from core.region_store import RegionStore
from core.daily_runner import DailyRunner
from core.battle_runner import BattleRunner
from core.training_level_runner import TrainingLevelRunner

# 野外捕捉（螳螂/稀有精灵）
from core.dar_route_runner import DarRouteRunner, DEFAULT_PROFILE_MANTIS, DEFAULT_PROFILE_DUGULU, DEFAULT_PROFILE_SHUANGTA, DEFAULT_PROFILE_XIAODOUYA, DEFAULT_PROFILE_FLASH_PIPI, EYEBALL_PROFILE

# 已移除 CalibrationTestRunner（438测试功能已删除）


class BotWorker(QThread):
    log_signal = pyqtSignal(str, str)      # (text, level)
    state_signal = pyqtSignal(str)         # "IDLE" / "RUNNING"
    task_done_signal = pyqtSignal()        # Dashboard 解锁用

    def __init__(self, project_root: str):
        super().__init__()
        self.project_root = project_root

        self._engine_alive = True

        # 运行状态
        self.is_running = False
        self.is_paused = False
        self.stop_current = False  # 中止当前任务（不杀引擎）

        self._task_lock = threading.Lock()
        self.active_tasks: Dict = {}

        # ✅ 给 DarRouteRunner / 长循环用的 stop_event（和 stop_current 配合）
        self._stop_event = threading.Event()

        # ---- 路径 ----
        from config import REGIONS_PATH, TEMPLATES_PATH
        region_root = REGIONS_PATH
        template_root = TEMPLATES_PATH

        # ✅ 兼容不同 RegionStore 构造签名
        self.regions = self._init_region_store(region_root)

        # runners
        self.daily_runner = DailyRunner(self)
        self.battle_runner = BattleRunner(self, self.regions, template_root)
        self.training_level_runner = TrainingLevelRunner(
            self, self.regions, template_root, battle_runner=self.battle_runner
        )

        # ✅ DarRouteRunner 构造签名兼容（避免 TypeError: unexpected keyword argument）
        self.dar_route_runner = self._init_dar_route_runner(template_root)

        # 已移除 calibration_test_runner（438测试功能已删除）


        # 键盘监听器：只在任务跑时开启
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
        新版 DarRouteRunner 签名：
            DarRouteRunner(bot, regions, battle_runner, map_swf_id=11)

        template_root 仅保留参数以兼容旧调用，但不会再传进 DarRouteRunner（避免 battle_runner=字符串）。
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
                f"DarRouteRunner 初始化异常：battle_runner 类型不对：{type(br)}。"
                f"（很可能把 template_root 误传成 battle_runner 了）"
            )

        raise last_err


    # --------------------
    # task control
    # --------------------
    def set_tasks(self, tasks: dict):
        with self._task_lock:
            # 原定时任务（scheduled_task / scheduled_datetime）已移除，由轮换模式等替代
            self.active_tasks = dict(tasks or {})

        self.stop_current = False
        self.is_paused = False
        self._stop_event.clear()

    def emit_and_log(self, text: str, level: str = "INFO"):
        try:
            self.log_signal.emit(text, level)
        except Exception:
            pass

        if level == "ERROR":
            logger.error(text)
        elif level in ("WARN", "WARNING"):
            logger.warning(text)
        elif level == "DEBUG":
            logger.debug(text)
        else:
            logger.info(text)

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
            self.emit_and_log(f"⚠ pynput 不可用，ESC/空格监听关闭: {e}", "WARN")
            return

        def on_key_press(key):
            try:
                if not self.is_running:
                    return

                # ESC：停止当前任务
                if key == keyboard.Key.esc:
                    self.stop()
                    self.emit_and_log("⛔ ESC：已请求中止当前任务", "SYSTEM")
                    return

                # Space：继续（更贴近你“人机验证暂停后空格继续”的习惯）
                if key == keyboard.Key.space:
                    if self.is_paused:
                        self.is_paused = False
                        self.emit_and_log("▶ Space：继续", "SYSTEM")
                    return

                # F1：切换暂停/继续（保留一个 toggle 方便你调试）
                if key == keyboard.Key.f1:
                    self.is_paused = not self.is_paused
                    self.emit_and_log("⏸ 已暂停" if self.is_paused else "▶ 已继续", "SYSTEM")
                    return

            except Exception as e:
                logger.error(f"键盘监听异常: {e}")

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
        # ✅ 同时打 stop_current + stop_event，保证 dar_route_runner 这种长循环也能退出
        self.stop_current = True
        self._stop_event.set()
        self.is_paused = False
        self.emit_and_log("🛑 已请求停止当前任务", "SYSTEM")

    def shutdown(self):
        self._engine_alive = False
        self.stop_current = True
        self._stop_event.set()
        self.is_paused = False
        self._stop_keyboard_listener()

    def wait_if_paused(self):
        while self.is_paused and self.is_running and (not self.stop_current):
            time.sleep(0.05)

    @staticmethod
    def _parse_int(v, default=None):
        if v is None:
            return default
        try:
            return int(v)
        except Exception:
            return default

    # --------------------
    # main loop
    # --------------------
    def run(self):
        self.emit_and_log("🚀 自动化引擎已启动（待命）", "ENGINE")
        self.state_signal.emit("IDLE")

        while self._engine_alive:
            with self._task_lock:
                tasks = dict(self.active_tasks)

            has_job = bool(
                tasks.get("daily_chain")
                or tasks.get("run_script")  # ✅ 执行脚本
                or tasks.get("gacha")  # ✅ 扭蛋
                or tasks.get("hero_tower")  # ✅ 勇者之塔
                or tasks.get("chaos_battle_x2")  # ✅ 大乱斗x2
                or tasks.get("1v1_x2")
                or tasks.get("exp_minor_battle")  # ✅ 小号对战（刷经验）  # ✅ 1v1x2
                or tasks.get("training_level")
                or tasks.get("training_until_level")
                or tasks.get("leiyi_training")   # 雷伊特训
                or tasks.get("teixun_loop")     # 特训循环
                or tasks.get("dar_route_test")   # 你 Dashboard 里有这个按钮
                or tasks.get("wild_capture")     # ✅ 你新增的"螳螂/稀有精灵捕捉"
                or tasks.get("smart_tracking_test")  # 智能追踪测试
                or tasks.get("calibration_test")
                or tasks.get("nie_family_test")  # ✅ 尼尔家族测试
                or tasks.get("nieo_mode")  # ✅ 尼奥模式（10/11地图循环）
                or tasks.get("afk_battle_mode")  # ✅ 挂机对战模式
                or tasks.get("rotation_mode")  # ✅ 双塔尼奥轮换模式（已替换原定时任务）
                or tasks.get("pinnacle_mode")  # ✅ 巅峰对战模式（排位/娱乐）
            )

            if not has_job:
                time.sleep(0.1)
                continue

            self.is_running = True
            self.stop_current = False
            self.is_paused = False
            self._stop_event.clear()

            self.state_signal.emit("RUNNING")
            self._start_keyboard_listener()

            try:
                if not window_manager.find_window():
                    self.emit_and_log("❌ 未检测到游戏窗口：请先在 Dashboard 点【启动游戏】", "ERROR")
                else:
                    use_foreground = bool(tasks.get("use_foreground", False))
                    use_background = (not use_foreground)

                    try:
                        cap_tier = tasks.get("non_mantis_capsule_tier")
                        if cap_tier in ("super", "high", "special"):
                            self.dar_route_runner.set_non_mantis_capsule_tier(str(cap_tier))
                        elif "non_mantis_use_super_capsule" in tasks:
                            self.dar_route_runner.set_non_mantis_use_super_capsule(
                                bool(tasks.get("non_mantis_use_super_capsule", False))
                            )
                        else:
                            self.dar_route_runner.set_non_mantis_capsule_tier("super")
                    except Exception:
                        pass

                    # ---- 日常 ----
                    if tasks.get("daily_chain") and (not self.stop_current):
                        self.emit_and_log(f"▶ 开始一键日常（前台={use_foreground}）", "SYSTEM")
                        self.daily_runner.run_all(background_mode=use_background)

                    # ---- 执行脚本 ----
                    if tasks.get("run_script") and (not self.stop_current):
                        script_name = tasks.get("run_script")
                        repeat = self._parse_int(tasks.get("run_repeat", 1), 1)
                        repeat = max(1, repeat)
                        self.emit_and_log(
                            f"📜 开始执行脚本: {script_name}.json × {repeat}（前台={use_foreground}）",
                            "SYSTEM",
                        )
                        fangsheng_left_flips = 0
                        if script_name == "放生":
                            fangsheng_left_flips = math.ceil(repeat / 9) + 5
                            self.emit_and_log(
                                f"🐾 放生：每轮前先左翻页 {fangsheng_left_flips} 次"
                                f"（⌈{repeat}/9⌉+5）",
                                "INFO",
                            )
                        for _ in range(repeat):
                            if self.stop_current:
                                break
                            if script_name == "放生" and fangsheng_left_flips > 0:
                                flip_ok = True
                                for _flip in range(fangsheng_left_flips):
                                    if self.stop_current:
                                        flip_ok = False
                                        break
                                    if not self.daily_runner._click_region_safe(
                                        self.regions,
                                        "精灵仓库.左",
                                        use_foreground,
                                    ):
                                        self.emit_and_log(
                                            "❌ 放生前左翻页中止（缺少区域 精灵仓库.左 或点击失败）",
                                            "ERROR",
                                        )
                                        flip_ok = False
                                        break
                                    time.sleep(0.05)
                                if not flip_ok:
                                    break
                            self.daily_runner.run_single_script(
                                script_name, bg_mode=use_background
                            )

                    # ---- 扭蛋 ----
                    if tasks.get("gacha") and (not self.stop_current):
                        times = self._parse_int(tasks.get("gacha_times", 1), 1)
                        if times < 1:
                            times = 1
                        self.emit_and_log(f"🎲 扭蛋：循环 {times} 次（前台={use_foreground}）", "SYSTEM")
                        for _ in range(times):
                            if self.stop_current:
                                break
                            self.daily_runner.run_single_script("nd", bg_mode=use_background)

                    # ---- 勇者之塔 ----
                    if tasks.get("hero_tower") and (not self.stop_current):
                        self.emit_and_log(f"🗼 勇者之塔：10回合（前台={use_foreground}）", "SYSTEM")
                        self.daily_runner.run_hero_tower(times=10, background_mode=use_background, use_unified_framework=True)
                    
                    # ---- 大乱斗x2 ----
                    if tasks.get("chaos_battle_x2") and (not self.stop_current):
                        self.emit_and_log(f"⚔ 开始大乱斗x2（前台={use_foreground}）", "SYSTEM")
                        self.daily_runner.run_chaos_battle_x2(use_foreground=use_foreground)
                    
                    # ---- 1v1x2 ----
                    if tasks.get("1v1_x2") and (not self.stop_current):
                        self.emit_and_log(f"⚔ 开始1v1x2（前台={use_foreground}）", "SYSTEM")
                        self.daily_runner.run_1v1_x2(use_foreground=use_foreground)

                    # ---- 小号对战（刷经验）----
                    if tasks.get("exp_minor_battle") and (not self.stop_current):
                        self.emit_and_log(f"📚 开始小号对战（刷经验，前台={use_foreground}）", "SYSTEM")
                        self.daily_runner.run_exp_minor_battle(use_foreground=use_foreground)

                    # ---- 雷伊特训 ----
                    if tasks.get("leiyi_training") and (not self.stop_current):
                        loop_count = self._parse_int(tasks.get("leiyi_loop_count", 10), 10)
                        loop_count = max(1, min(999, loop_count))
                        self.emit_and_log(f"⚡ 开始雷伊特训（循环={loop_count} 前台={use_foreground}）", "SYSTEM")
                        self.dar_route_runner._check_and_fill_missing_swf_files()  # 像尼奥模式一样补齐swf
                        self.daily_runner.run_leiyi_training(loop_count=loop_count, use_foreground=use_foreground)

                    # ---- 特训循环 ----
                    if tasks.get("teixun_loop") and (not self.stop_current):
                        self.emit_and_log(f"🔄 开始特训循环（前台={use_foreground}）", "SYSTEM")
                        self.daily_runner.run_teixun_loop(use_foreground=use_foreground)

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
                            f"⬆ 升级直到 {target_level}（batch={battles_per_batch} recover_every={recover_every} debug_stop={debug_stop_level} 前台={use_foreground}）",
                            "SYSTEM",
                        )
                        self.dar_route_runner._check_and_fill_missing_swf_files()  # 像尼奥模式一样补齐swf
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
                            f"🏫 训练室练级：{max_battles} 场（recover_every={recover_every} debug_stop={debug_stop_level} 前台={use_foreground}）",
                            "SYSTEM",
                        )
                        self.dar_route_runner._check_and_fill_missing_swf_files()  # 像尼奥模式一样补齐swf
                        self.training_level_runner.run_training_level(
                            max_battles=max_battles,
                            recover_every=recover_every,
                            debug_stop_level=debug_stop_level,
                            use_foreground=use_foreground,
                        )

                    # ---- 螳螂捕捉(TEST) 按钮（你 dashboard 里叫 dar_route_test）----
                    if tasks.get("dar_route_test") and (not self.stop_current):
                        self.emit_and_log("🦂 螳螂捕捉(TEST)：按默认螳螂 profile 启动", "SYSTEM")
                        profile = DEFAULT_PROFILE_MANTIS
                        # 兼容 DarRouteRunner 可能叫 run_test / run
                        if hasattr(self.dar_route_runner, "run_test"):
                            self.dar_route_runner.run_test(
                                stop_event=self._stop_event,
                                use_foreground=use_foreground,
                                profile=profile,
                            )
                        else:
                            self.dar_route_runner.run(
                                stop_event=self._stop_event,
                                use_foreground=use_foreground,
                                profile=profile,
                            )

                    # ---- 野外捕捉（螳螂/稀有精灵）----
                    if tasks.get("wild_capture") and (not self.stop_current):
                        profile_name = (tasks.get("wild_capture_profile") or "mantis").lower().strip()
                        if profile_name == "mantis":
                            profile = DEFAULT_PROFILE_MANTIS
                        elif profile_name == "dugulu":
                            profile = DEFAULT_PROFILE_DUGULU
                        elif profile_name == "shuangta":
                            profile = DEFAULT_PROFILE_SHUANGTA
                        elif profile_name == "xiaodouya":
                            profile = DEFAULT_PROFILE_XIAODOUYA
                        elif profile_name == "flash_pipi":
                            profile = DEFAULT_PROFILE_FLASH_PIPI
                        elif profile_name == "eyeball":
                            from core.dar_route_runner import EYEBALL_PROFILE
                            profile = EYEBALL_PROFILE
                        else:
                            profile = DEFAULT_PROFILE_DUGULU

                        self.emit_and_log(f"🌲 野外捕捉启动：profile={profile_name} 前台={use_foreground}", "SYSTEM")

                        # 闪光皮皮专用：轮换重连前置（勾选时先执行双塔精灵版轮换重连，失败则重试完整流程直到成功）
                        if profile_name == "flash_pipi" and tasks.get("rare_rotation_reconnect_first"):
                            while not self.stop_current and not self._stop_event.is_set():
                                ok = self.dar_route_runner._execute_flash_pipi_pre_rotation_reconnect(
                                    use_foreground=use_foreground,
                                    stop_event=self._stop_event,
                                )
                                if ok:
                                    break
                                self.emit_and_log("⚠️ 轮换重连前置失败，重试完整流程（1-5）直到启动模式", "WARN")

                        if not self.stop_current and not self._stop_event.is_set():
                            self.dar_route_runner.run(
                                stop_event=self._stop_event,
                                use_foreground=use_foreground,
                                profile=profile,
                            )

                    # ---- 智能追踪测试 ----
                    if tasks.get("smart_tracking_test") and (not self.stop_current):
                        profile_name = (tasks.get("wild_capture_profile") or "dugulu").lower().strip()
                        if profile_name == "mantis":
                            profile = DEFAULT_PROFILE_MANTIS
                        elif profile_name == "dugulu":
                            profile = DEFAULT_PROFILE_DUGULU
                        elif profile_name == "shuangta":
                            profile = DEFAULT_PROFILE_SHUANGTA
                        elif profile_name == "xiaodouya":
                            profile = DEFAULT_PROFILE_XIAODOUYA
                        elif profile_name == "flash_pipi":
                            profile = DEFAULT_PROFILE_FLASH_PIPI
                        elif profile_name == "eyeball":
                            profile = EYEBALL_PROFILE
                        else:
                            profile = DEFAULT_PROFILE_DUGULU

                        self.emit_and_log(f"🧪 智能追踪测试启动：profile={profile_name} 前台={use_foreground}", "SYSTEM")
                        self.dar_route_runner.run(
                            stop_event=self._stop_event,
                            use_foreground=use_foreground,
                            profile=profile,
                            smart_tracking_mode=True,  # 启用智能追踪模式
                        )

                    # ---- 双塔尼奥轮换模式 ----
                    if tasks.get("rotation_mode") and (not self.stop_current):
                        is_test_mode = bool(tasks.get("rotation_test_mode", False))
                        mode_text = "测试模式（固定时间间隔切换）" if is_test_mode else "正式模式（根据北京时间自动切换）"
                        self.emit_and_log(f"🔄 启动双塔尼奥轮换模式（{mode_text}）", "SYSTEM")
                        use_foreground = bool(tasks.get("use_foreground", False))
                        
                        # 测试模式参数
                        interval_minutes_nieo = float(tasks.get("rotation_interval_minutes_nieo", 60.0) or 60.0)
                        interval_minutes_shuangta = float(tasks.get("rotation_interval_minutes_shuangta", 60.0) or 60.0)
                        hard_limit_sec = float(tasks.get("petswf_hard_limit_sec", 8.0) or 8.0)
                        if is_test_mode:
                            self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_NIEO = interval_minutes_nieo
                            self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_SHUANGTA = interval_minutes_shuangta
                            self.dar_route_runner.PETSWF_TO_PETITEM_HARD_LIMIT_SEC = hard_limit_sec
                            self.emit_and_log(
                                f"🧪 [轮换模式-测试] 参数：尼奥→双塔={interval_minutes_nieo}分钟，双塔→尼奥={interval_minutes_shuangta}分钟，硬线={hard_limit_sec}秒",
                                "INFO",
                            )
                        else:
                            # 非测试模式恢复默认值
                            self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_NIEO = 60.0
                            self.dar_route_runner.ROTATION_RECONNECT_INTERVAL_MINUTES_SHUANGTA = 60.0
                            self.dar_route_runner.PETSWF_TO_PETITEM_HARD_LIMIT_SEC = 8.0
                        
                        self.dar_route_runner.run_rotation_mode(
                            stop_event=self._stop_event,
                            use_foreground=use_foreground,
                            is_test_mode=is_test_mode,  # ✅ 传递测试模式标志
                        )

                    # ---- 🌊 尼奥模式（10/11地图循环）----
                    if tasks.get("nieo_mode") and (not self.stop_current):
                        test_nieo = tasks.get("test_nieo", False)
                        test_nie = tasks.get("test_nie", False)
                        skip_nie_77 = tasks.get("skip_nie_77", False)
                        nieo_pre_rotation_first = tasks.get("nieo_pre_rotation_first", False)
                        test_msg = ""
                        if test_nieo:
                            test_msg += " [测试尼奥模式]"
                        if test_nie:
                            test_msg += " [测试尼尔模式]"
                        if skip_nie_77:
                            test_msg += " [不捕捉尼尔]"
                        if nieo_pre_rotation_first:
                            test_msg += " [前置重连]"
                        self.emit_and_log(f"🌊 启动尼奥模式（10/11地图循环）{test_msg}", "SYSTEM")

                        # 尼奥模式专用：前置重连（使用尼奥模式的三个精灵）
                        if nieo_pre_rotation_first:
                            while not self.stop_current and not self._stop_event.is_set():
                                ok = self.dar_route_runner._execute_nieo_pre_rotation_reconnect(
                                    use_foreground=use_foreground,
                                    stop_event=self._stop_event,
                                )
                                if ok:
                                    break
                                self.emit_and_log("⚠️ 尼奥模式前置重连失败，重试完整流程（1-4）直到成功", "WARN")

                        if not self.stop_current and not self._stop_event.is_set():
                            self.dar_route_runner.run_nieo_mode(
                                stop_event=self._stop_event,
                                use_foreground=use_foreground,
                                test_nieo=test_nieo,
                                test_nie=test_nie,
                                skip_nie_77=skip_nie_77,
                            )
                    
                    # ---- 🎮 挂机对战模式 ----
                    if tasks.get("afk_battle_mode") and (not self.stop_current):
                        afk_sub = tasks.get("afk_sub_mode", "normal")
                        labels = {"normal": "普通", "defeat": "击败", "rare": "稀有", "nieo": "尼奥"}
                        self.emit_and_log(f"🎮 启动挂机{labels.get(afk_sub, afk_sub)}模式", "SYSTEM")
                        if not self.stop_current and not self._stop_event.is_set():
                            self.dar_route_runner.run_afk_battle_mode(
                                stop_event=self._stop_event,
                                use_foreground=use_foreground,
                                sub_mode=afk_sub,
                            )

                    # ---- 🏆 巅峰对战模式（排位/娱乐）----
                    if tasks.get("pinnacle_mode") and (not self.stop_current):
                        pinnacle_sub = tasks.get("pinnacle_mode_type", "rank")
                        pinnacle_small_account_mode = bool(
                            tasks.get("pinnacle_small_account_mode", False)
                        )
                        label = "排位" if pinnacle_sub == "rank" else "娱乐"
                        self.emit_and_log(f"🏆 启动巅峰对战模式（{label}）", "SYSTEM")
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
                                self.emit_and_log("🧪 启动尼尔测试（77/310，第二回合切精灵三）", "SYSTEM")
                            elif nie_family_test_type == "ni":
                                # 尼奥模式（416，第二回合切精灵二）
                                nie_family_id = 416
                                self.emit_and_log("🧪 启动尼奥测试（416，第二回合切精灵二）", "SYSTEM")
                            else:
                                nie_family_id = 77  # 默认
                                self.emit_and_log(f"🧪 启动尼尔家族测试（默认：77/310）", "SYSTEM")
                            
                            self.emit_and_log(f"🔍 准备调用run_nie_family_test，nie_family_id={nie_family_id}", "INFO")
                            
                            # 添加调试输出
                            import sys
                            print(f"[DEBUG] About to call run_nie_family_test", file=sys.stderr, flush=True)
                            
                            self.dar_route_runner.run_nie_family_test(
                            stop_event=self._stop_event,
                            use_foreground=use_foreground,
                                nie_family_id=nie_family_id,
                            )
                            
                            print(f"[DEBUG] run_nie_family_test returned", file=sys.stderr, flush=True)
                            self.emit_and_log("✅ run_nie_family_test执行完成", "INFO")
                        except Exception as e:
                            self.emit_and_log(f"❌ 尼尔家族测试异常: {e}", "ERROR")
                            import traceback
                            self.emit_and_log(f"📋 异常详情: {traceback.format_exc()}", "ERROR")

                    # ---- 🔧 校准测试（纯屏幕检测） ----
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
                self.emit_and_log(f"💥 任务执行异常: {e}", "ERROR")

            # 清空任务
            with self._task_lock:
                self.active_tasks = {}

            self.is_running = False
            self.stop_current = False
            self.is_paused = False
            self._stop_event.clear()
            self._stop_keyboard_listener()

            self.emit_and_log("🏁 当前任务结束，引擎待命中…", "ENGINE")
            self.state_signal.emit("IDLE")
            self.task_done_signal.emit()

        self.emit_and_log("😴 自动化引擎已停止", "ENGINE")
