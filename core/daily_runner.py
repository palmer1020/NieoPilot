# core/daily_runner.py
import json
import os
import random
import time
from typing import Optional, List, Dict, Any, Tuple

from core.logger import logger
from core.utils import window_manager
from core.post_battle_cleaner import PostBattleCleaner
from core.unified_battle_framework import UnifiedBattleFramework, BattleConfig, BattleMode
from core.fixed_mode_adapter import FixedModeAdapter
from core.kernel_log_match import (
    line_matches,
    first_map_id_in_line,
    RE_PETITEM,
    RE_NEWNPC_MULTI,
    RE_MAP_PATH_LOOSE,
)

# 勇者之塔：独立按钮与一键日常后续的默认对战场数（原 10）。
DEFAULT_HERO_TOWER_BATTLES = 2

# 优先用 config 里的 BASE_PATH / DAILY_SEQUENCE（如果没有也能兜底）
try:
    from config import BASE_PATH, DAILY_SEQUENCE
except Exception:
    BASE_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DAILY_SEQUENCE = []


def _kernel_line_has_any_map(line: str) -> bool:
    s = str(line)
    return bool(first_map_id_in_line(s)) or line_matches(RE_MAP_PATH_LOOSE, s)


def _kernel_line_has_map_id(line: str, map_id: int) -> bool:
    return first_map_id_in_line(str(line)) == map_id


class DailyRunner:
    """
    ✅ 兼容两种脚本格式：
    1) 新录制器（tools/script_recorder.py）导出：
       step: {"action":"click","x":..,"y":..,"delay":..}
    2) 老格式：
       step: {"pos":[x,y],"delay":..,"bg": true/false}

    ✅ 给 BotWorker 使用的 API：
    - run_all(background_mode=True, include_hero_tower_after_daily=False)
    - run_single_script(name, bg_mode=True)
    - run_script(script_path, bg_override=None)
    """

    SCRIPT_FOLDER_NAME = "fix_script"

    def __init__(self, bot):
        self.bot = bot
        self.script_dir = os.path.join(BASE_PATH, self.SCRIPT_FOLDER_NAME)
        self._unified_framework = None
        self._fixed_adapter = None

    # ----------------------------
    # BotWorker 会调用的两个方法
    # ----------------------------
    def run_all(
        self,
        background_mode: bool = True,
        sequence: Optional[List[str]] = None,
        include_hero_tower_after_daily: bool = False,
    ) -> bool:
        """
        执行 config.DAILY_SEQUENCE（或外部传入 sequence）中的脚本（不带 .json 也行）
        background_mode=True => 全部按后台执行（除非脚本里显式写 bg=false 且你不覆盖）
        include_hero_tower_after_daily：勾选时执行 DAILY_SEQUENCE 含脚本「6」，并在日常后打勇者之塔两场再接 1v1×2；
            不勾选（默认）只跑 1–5（从队列中去掉「6」），跳过勇者之塔，直接进入 1v1×2。
        """
        if sequence is None:
            sequence = list(DAILY_SEQUENCE or [])
            if include_hero_tower_after_daily and not any(
                str(s).strip() == "6" for s in sequence
            ):
                sequence = list(sequence) + ["6"]
        else:
            sequence = list(sequence)

        if not include_hero_tower_after_daily:
            sequence = [s for s in sequence if str(s).strip() != "6"]

        if not sequence:
            self._emit("⚠ DAILY_SEQUENCE 为空：没有可执行的日常脚本", "WARN")
            return False

        ok_all = True
        for idx, name in enumerate(sequence):
            if self._should_abort():
                self._emit("⛔ 日常任务中止（stop_current）", "SYSTEM")
                return False

            self._emit(f"📜 日常脚本队列：开始执行 {name}", "SYSTEM")
            ok = self.run_single_script(name, bg_mode=background_mode)
            ok_all = ok_all and ok

        # ✅ 日常脚本完成后：可选勇者之塔两回合，再接 1v1x2 + 大乱斗x2
        if not self._should_abort() and ok_all:
            try:
                use_foreground = (not background_mode)
                regions = getattr(self.bot, "regions", None)
                ok_tower = True

                if include_hero_tower_after_daily:
                    self._emit("⏱ 日常任务完成：1s 后开始【勇者之塔】循环…", "SYSTEM")
                    time.sleep(1.0)
                    ok_tower = self.run_hero_tower(
                        times=DEFAULT_HERO_TOWER_BATTLES,
                        background_mode=background_mode,
                        use_unified_framework=False,
                    )
                    ok_all = ok_all and ok_tower

                    # ✅ 勇者之塔完成后，先点击"勇者之塔.离开"，等待7秒，再执行后续
                    if not self._should_abort() and ok_tower and regions:
                        try:
                            self._emit("🖱 点击：勇者之塔.离开", "INFO")
                            if self._click_region_safe(regions, "勇者之塔.离开", use_foreground):
                                self._emit("⏳ 等待7秒...", "INFO")
                                time.sleep(7.0)
                            else:
                                self._emit("⚠️ 点击勇者之塔.离开失败，但继续执行", "WARN")
                        except Exception as e:
                            self._emit(f"⚠️ 点击勇者之塔.离开异常: {e}，但继续执行", "WARN")

                    tail_ready = bool(ok_tower and ok_all and not self._should_abort())
                    tail_intro = "⏱ 勇者之塔完成：3s 后开始【1v1x2】…"
                else:
                    tail_ready = bool(ok_all and not self._should_abort())
                    tail_intro = "⏱ 日常完成：跳过勇者之塔，3s 后开始【1v1x2】…"

                if tail_ready:
                    try:
                        self._emit(tail_intro, "SYSTEM")
                        time.sleep(3.0)
                        if not self._should_abort():
                            ok_1v1 = self.run_1v1_x2(use_foreground=use_foreground)
                            ok_all = ok_all and ok_1v1
                    except Exception as e:
                        self._emit(f"💥 1v1x2异常: {e}", "ERROR")
                        ok_all = False

                    if not self._should_abort() and ok_all:
                        try:
                            self._emit("⏱ 1v1x2完成：3s 后开始【大乱斗x2】…", "SYSTEM")
                            time.sleep(3.0)
                            if not self._should_abort():
                                ok_chaos = self.run_chaos_battle_x2(use_foreground=use_foreground)
                                ok_all = ok_all and ok_chaos
                        except Exception as e:
                            self._emit(f"💥 大乱斗x2异常: {e}", "ERROR")
                            ok_all = False
            except Exception as e:
                self._emit(f"💥 日常后续流程异常: {e}", "ERROR")
                ok_all = False

        return ok_all

    # ----------------------------
    # 勇者之塔：循环对战 + 胜利清理
    # ----------------------------
    def run_hero_tower(self, times: int = DEFAULT_HERO_TOWER_BATTLES, background_mode: bool = True, use_unified_framework: bool = False) -> bool:
        """日常后续：勇者之塔循环 times 次。"""
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口：无法执行勇者之塔", "ERROR")
            return False

        # 需要 bot 里有 regions 和 battle_runner（你现在的 BotWorker 有）
        regions = getattr(self.bot, "regions", None)
        battle_runner = getattr(self.bot, "battle_runner", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions，无法执行勇者之塔", "ERROR")
            return False

        use_foreground = (not background_mode)
        from config import TEMPLATES_PATH
        template_root = TEMPLATES_PATH

        # 触发对战的 region（对应：assets/regions/勇者之塔/点击对战.json）
        battle_key = "勇者之塔.点击对战"
        if not regions.get(battle_key):
            self._emit(
                f"❌ 找不到区域：{battle_key}（请确认 assets/regions/勇者之塔/点击对战.json 已存在且 RegionStore 已加载）",
                "ERROR",
            )
            return False

        self._emit(f"🗼 勇者之塔：开始循环 {times} 次（前台={use_foreground}，使用旧实现）", "SYSTEM")

        # ✅ 直接使用旧实现（不尝试统一框架）
        if battle_runner is None:
            self._emit("❌ DailyRunner 缺少 bot.battle_runner，无法执行勇者之塔", "ERROR")
            return False
        
        cleaner = PostBattleCleaner(self.bot, regions, template_root)

        for i in range(int(times)):
            if self._should_abort():
                self._emit("⛔ 勇者之塔中止（stop_current）", "SYSTEM")
                return False

            self._emit(f"🗼 勇者之塔：第 {i+1}/{times} 场 → 点击对战", "SYSTEM")

            # 1) 点击"勇者之塔.点击对战"进入战斗
            try:
                r = regions.get(battle_key)
                gx, gy = r.sample_click_point()
                if use_foreground:
                    window_manager.click(gx, gy)
                else:
                    window_manager.click_background(gx, gy)
            except Exception as e:
                self._emit(f"❌ 点击 {battle_key} 失败: {e}", "ERROR")
                return False

            # 2) 自动击败（单场）
            try:
                battle_runner.run_defeat_mode(use_foreground=use_foreground)
            except TypeError:
                # 兼容旧签名
                battle_runner.run_defeat_mode()

            # 3) 胜利清理：等黄色探针 -> 点胜利确认 -> 点普通确认 4 次
            cleaner.run_stage3_training_room(
                use_foreground=use_foreground,
                click_plan=cleaner.CLICK_PLAN_HERO_TOWER,
            )

            # 4) 稍微缓冲一下 UI
            time.sleep(0.15)

        self._emit(f"✅ 勇者之塔：{times} 次对战完成", "SUCCESS")
        return True
    
    # ----------------------------
    # 大乱斗x2：特殊战斗模式
    # ----------------------------
    def run_chaos_battle_x2(self, use_foreground: bool = True) -> bool:
        """执行大乱斗x2：两次特殊战斗循环"""
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口：无法执行大乱斗x2", "ERROR")
            return False
        
        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions，无法执行大乱斗x2", "ERROR")
            return False
        
        from config import TEMPLATES_PATH
        cleaner = PostBattleCleaner(self.bot, regions, TEMPLATES_PATH)
        
        # 初始化统一框架（用于探针检测和战斗逻辑）
        if self._unified_framework is None:
            self._unified_framework = UnifiedBattleFramework(self.bot, regions, TEMPLATES_PATH)
        
        # 执行两次战斗
        for battle_num in range(2):
            if self._should_abort():
                self._emit("⛔ 大乱斗x2中止（stop_current）", "SYSTEM")
                return False
            
            self._emit(f"⚔ 大乱斗x2：第 {battle_num + 1}/2 场", "SYSTEM")
            
            # 第一场：需要先移动大乱斗；第二场：直接点击大乱斗
            if battle_num == 0:
                # 1. 点击"勇者之塔.移动大乱斗"
                self._emit("🖱 点击：勇者之塔.移动大乱斗", "INFO")
                if not self._click_region_safe(regions, "勇者之塔.移动大乱斗", use_foreground):
                    return False
                
                # 2. 等待2.5秒
                time.sleep(2.5)
            
            # 3. 点击"勇者之塔.精灵大乱斗"
            self._emit("🖱 点击：勇者之塔.精灵大乱斗", "INFO")
            if not self._click_region_safe(regions, "勇者之塔.精灵大乱斗", use_foreground):
                return False
            
            # 4. 等待时间（统一改为1秒）
            time.sleep(1.0)
            
            # 5. 点击"勇者之塔.开始大乱斗"
            self._emit("🖱 点击：勇者之塔.开始大乱斗", "INFO")
            if not self._click_region_safe(regions, "勇者之塔.开始大乱斗", use_foreground):
                return False
            
            # 6. 等待PetItem并执行第一回合技能（大乱斗模式：无超时限制）
            self._emit("⏳ 等待PetItem进入对战（无超时限制）...", "INFO")
            if not self._wait_for_petitem_and_first_skill(regions, use_foreground, timeout_s=None):
                self._emit("❌ 等待PetItem或第一回合失败（可能被中止）", "ERROR")
                return False
            
            # 7. 执行战斗循环（大乱斗模式）
            if not self._run_chaos_battle_loop(regions, use_foreground, cleaner, is_chaos=True):
                self._emit("❌ 战斗循环失败", "ERROR")
                return False
            
            # 8. 检测胜利探针（黄色或白色）并点击确认，然后1AND1清理（参考训练室/勇者之塔）
            # 先等待UI稳定（与训练室保持一致，延迟2.5秒）
            self._emit("⏳ 等待UI稳定（2.5秒）...", "INFO")
            time.sleep(2.5)
            self._emit("🟡 检测胜利探针（黄色或白色）...", "INFO")
            victory_detected = self._detect_victory_probe_yellow_or_white(cleaner, use_foreground, timeout_s=8.0)
            if not victory_detected:
                self._emit("❌ 未检测到胜利探针（超时）", "ERROR")
                return False
            
            # 9. 点击"对话框.对战胜利确认"（参考stage4_post_battle的逻辑）
            self._emit("🖱 点击：对话框.对战胜利确认", "INFO")
            if not self._click_region_safe(regions, "对话框.对战胜利确认", use_foreground):
                return False
            
            # 10. 1AND1清理对话框（使用统一框架的方法，参考训练室/勇者之塔）
            self._emit("⏳ 清理对话框（1 AND 1，10秒超时）...", "INFO")
            from core.unified_battle_framework import BattleConfig, BattleMode
            if self._unified_framework is None:
                self._emit("❌ 缺少unified_framework，无法执行1AND1清理", "ERROR")
                return False
            
            config = BattleConfig(
                mode=BattleMode.FIXED,  # 大乱斗和1v1使用固定模式
                use_foreground=use_foreground,
                abort_check=lambda: self._should_abort()
            )
            try:
                # 使用10秒超时（大乱斗模式）
                self._unified_framework._wait_for_confirm_probes(config, timeout_s=10.0)
            except Exception as e:
                self._emit(f"⚠️ 1AND1清理异常: {e}", "WARN")
        
        self._emit("✅ 大乱斗x2：2场对战全部完成", "SUCCESS")
        return True

    # ----------------------------
    # 小号对战（刷经验）：无限循环
    # ----------------------------
    def run_exp_minor_battle(self, use_foreground: bool = True) -> bool:
        """执行小号对战（刷经验）：无限循环，首次进入恢复精灵一和精灵二，每场战斗结束后恢复，直到用户点击停止"""
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口：无法执行小号对战", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions", "ERROR")
            return False

        from config import TEMPLATES_PATH
        cleaner = PostBattleCleaner(self.bot, regions, TEMPLATES_PATH)

        if self._unified_framework is None:
            self._unified_framework = UnifiedBattleFramework(self.bot, regions, TEMPLATES_PATH)

        battle_count = 0
        while not self._should_abort():
            battle_count += 1
            self._emit(f"📚 小号对战：第 {battle_count} 场", "SYSTEM")

            # 第一步：第零次战斗恢复（首次进入 + 每场战斗结束都恢复）
            if not self._recover_pet_one_and_two(regions, use_foreground):
                self._emit("❌ 恢复精灵一和精灵二失败", "ERROR")
                return False

            if self._should_abort():
                return False

            # 第二步：扫描 经验.1 区域，纯黑时点击并发起对战
            self._emit("🔍 等待 经验.1 区域变黑（RGB均<30）...", "INFO")
            poll_count = 0
            while not self._should_abort():
                poll_count += 1
                log_rgb = (poll_count % 100 == 1)  # 每100次输出一次RGB调试（约10秒）
                if self._check_exp_probe_1_black(regions, log_rgb=log_rgb):
                    self._emit("✅ 经验.1 已变黑，点击区域1", "SUCCESS")
                    if not self._click_region_safe(regions, "经验.1", use_foreground):
                        break
                    time.sleep(0.5)
                    self._emit("🖱 双击 经验.接受对战", "INFO")
                    self._click_region_safe(regions, "经验.接受对战", use_foreground)
                    time.sleep(0.1)
                    self._click_region_safe(regions, "经验.接受对战", use_foreground)
                    break
                time.sleep(0.1)

            if self._should_abort():
                return False

            # 等待 PetItem（20秒超时）
            self._emit("⏳ 等待 PetItem 进入对战（20秒超时）...", "INFO")
            if not self._wait_for_petitem_and_first_skill(regions, use_foreground, timeout_s=20.0):
                self._emit("❌ 等待 PetItem 或第一回合失败", "ERROR")
                continue

            if self._should_abort():
                return False

            # 第三步：战斗循环（技能一 + 精灵二/精灵一切换），检测到 map 信号即结束
            if not self._run_exp_minor_battle_loop(regions, use_foreground):
                self._emit("❌ 战斗循环失败", "ERROR")
                continue

            if self._should_abort():
                return False

            # 第四步：检测胜利探针（黄或白），点击确定（无1AND1）
            self._emit("⏳ 等待UI稳定（2.5秒）...", "INFO")
            time.sleep(2.5)
            self._emit("🟡 检测胜利探针（黄色或白色）...", "INFO")
            if not self._detect_victory_probe_yellow_or_white(cleaner, use_foreground, timeout_s=8.0):
                self._emit("❌ 未检测到胜利探针（超时）", "ERROR")
                continue

            self._emit("🖱 点击：对话框.对战胜利确认", "INFO")
            if not self._click_region_safe(regions, "对话框.对战胜利确认", use_foreground):
                continue

            # 无1AND1，直接回到第一步恢复
            time.sleep(1.0)

        self._emit("✅ 小号对战已停止", "SUCCESS")
        return True

    def _recover_pet_one_and_two(self, regions, use_foreground: bool) -> bool:
        """恢复精灵一和精灵二：打开背包 -> 双击精灵一恢复 -> 等待 -> 双击精灵二恢复 -> 关闭背包"""
        bag_open_key = "精灵背包.打开精灵背包"
        pet_one_key = "精灵背包.精灵一"
        pet_two_key = "精灵背包.精灵二"
        recover_key = "精灵背包.精灵恢复"

        try:
            self._emit("💼 打开精灵背包", "INFO")
            if not self._click_region_safe(regions, bag_open_key, use_foreground):
                return False
            time.sleep(2.5)

            for pet_name, pet_key in [("精灵一", pet_one_key), ("精灵二", pet_two_key)]:
                self._emit(f"🐾 双击{pet_name}（准备恢复）", "INFO")
                if not self._click_region_safe(regions, pet_key, use_foreground):
                    return False
                time.sleep(0.1)
                if not self._click_region_safe(regions, pet_key, use_foreground):
                    return False
                time.sleep(0.5)
                self._emit("💊 点击精灵恢复", "INFO")
                if not self._click_region_safe(regions, recover_key, use_foreground):
                    return False
                time.sleep(1.0)
                self._emit("⏳ 使用1AND1确认", "INFO")
                from core.unified_battle_framework import BattleConfig, BattleMode
                config = BattleConfig(
                    mode=BattleMode.FIXED,
                    use_foreground=use_foreground,
                    abort_check=lambda: self._should_abort()
                )
                self._unified_framework._wait_for_confirm_probes(config, timeout_s=2.0)
                time.sleep(0.5)

            self._emit("💼 关闭精灵背包", "INFO")
            if not self._click_region_safe(regions, bag_open_key, use_foreground):
                return False
            time.sleep(0.5)
            return True
        except Exception as e:
            self._emit(f"❌ 恢复精灵一和精灵二异常: {e}", "ERROR")
            return False

    def _check_exp_probe_1_black(self, regions, log_rgb: bool = False) -> bool:
        """检查 经验.1 区域是否近乎纯黑（RGB均<30）

        Args:
            regions: 区域存储
            log_rgb: 是否输出RGB调试信息（每10次检查输出一次，避免日志过多）
        """
        import numpy as np
        r = regions.get("经验.1")
        if not r:
            if log_rgb:
                self._emit("⚠️ [经验.1] 区域不存在", "DEBUG")
            return False
        x1, y1, x2, y2 = r.outer_bbox()
        img = window_manager.grab_game_bbox(x1, y1, x2, y2, min_size_px=2)
        if img is None:
            if log_rgb:
                self._emit("⚠️ [经验.1] grab_game_bbox 返回 None", "DEBUG")
            return False
        arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
        mean_rgb = arr.mean(axis=(0, 1)).astype(int)
        r_val, g_val, b_val = int(mean_rgb[0]), int(mean_rgb[1]), int(mean_rgb[2])
        is_black = r_val < 30 and g_val < 30 and b_val < 30
        if log_rgb or is_black:
            self._emit(f"🔍 [经验.1] RGB=({r_val},{g_val},{b_val}) 纯黑={is_black}", "INFO")
        return is_black

    def _run_exp_minor_battle_loop(self, regions, use_foreground: bool) -> bool:
        """小号对战战斗循环：蓝时技能一，灰时 精灵二/出战/精灵一/出战

        战斗结束：检测到 map 信号即可（小号对战只需 map）
        """
        from core.logger import fetch_kernel_since, kernel_cursor

        battle_runner = getattr(self.bot, "battle_runner", None)
        if battle_runner is None:
            self._emit("❌ 缺少battle_runner", "ERROR")
            return False

        probe_model = battle_runner._load_probe_templates()
        cursor = kernel_cursor()

        switch_sequence = [
            "对战.切换精灵.切换精灵二",
            "对战.切换精灵.出战",
            "对战.切换精灵.切换精灵一",
            "对战.切换精灵.出战"
        ]
        switch_index = 0
        last_switch_time = 0.0
        switch_interval = 0.5
        last_probe_state = "UNKNOWN"

        self._emit("⚔️ 开始小号对战战斗循环...", "INFO")

        while True:
            if self._should_abort():
                return False
            self._wait_if_paused()

            # 检查 map 信号（战斗结束，小号对战只需要 map）
            try:
                lines = fetch_kernel_since(cursor)
                if isinstance(lines, list):
                    for line in lines:
                        line_str = str(line)
                        if _kernel_line_has_any_map(line_str):
                            self._emit("🏁 战斗结束（检测到 map 信号）", "SUCCESS")
                            return True
                cursor = kernel_cursor()
            except Exception:
                pass

            # 回合探针：蓝时技能一，灰时切换精灵
            state, _, _ = self._unified_framework._detect_round_probe(probe_model)
            if last_probe_state == "GRAY" and state == "BLUE":
                self._emit("🎯 检测到灰变蓝：使用技能一", "INFO")
                if not self._click_region_safe(regions, "对战.使用技能一", use_foreground):
                    return False
                time.sleep(0.1)
            elif state == "GRAY":
                now = time.time()
                if now - last_switch_time >= switch_interval:
                    switch_key = switch_sequence[switch_index]
                    if not self._click_region_safe(regions, switch_key, use_foreground):
                        return False
                    switch_index = (switch_index + 1) % len(switch_sequence)
                    last_switch_time = now

            last_probe_state = state
            time.sleep(0.05)
    
    def _click_region_safe(self, regions, key: str, use_foreground: bool) -> bool:
        """安全点击区域"""
        try:
            r = regions.get(key)
            if not r:
                self._emit(f"❌ 找不到区域：{key}", "ERROR")
                return False
            gx, gy = r.sample_click_point()
            if use_foreground:
                window_manager.click(gx, gy)
            else:
                window_manager.click_background(gx, gy)
            return True
        except Exception as e:
            self._emit(f"❌ 点击 {key} 失败: {e}", "ERROR")
            return False
    
    def _wait_for_map_and_npc(self, map_id: int, timeout_s: float = 30.0) -> bool:
        """等待进入指定地图（map信号 + newNPC信号）"""
        from core.logger import fetch_kernel_since, kernel_cursor

        start_time = time.time()
        cursor = kernel_cursor()
        map_seen = False
        npc_seen = False
        
        while (time.time() - start_time) < timeout_s:
            if self._should_abort():
                return False
            
            try:
                lines = fetch_kernel_since(cursor)
                if isinstance(lines, list):
                    for line in lines:
                        line_str = str(line)
                        if _kernel_line_has_map_id(line_str, map_id):
                            map_seen = True
                            self._emit(f"🗺 检测到map信号：map_id={map_id}", "INFO")
                        if line_matches(RE_NEWNPC_MULTI, line_str):
                            npc_seen = True
                            self._emit(f"📡 检测到newNPC信号", "INFO")
                        
                        if map_seen and npc_seen:
                            return True
                
                cursor = kernel_cursor()
            except Exception:
                pass
            
            time.sleep(0.05)
        
        return False
    
    def _wait_for_map_and_npc_any(self, timeout_s: float = 30.0) -> bool:
        """等待进入任意地图（map信号 + newNPC信号）"""
        from core.logger import fetch_kernel_since, kernel_cursor

        start_time = time.time()
        cursor = kernel_cursor()
        map_seen = False
        npc_seen = False
        
        while (time.time() - start_time) < timeout_s:
            if self._should_abort():
                return False
            
            try:
                lines = fetch_kernel_since(cursor)
                if isinstance(lines, list):
                    for line in lines:
                        line_str = str(line)
                        if _kernel_line_has_any_map(line_str):
                            map_seen = True
                            self._emit("🗺 检测到map信号", "INFO")
                        if line_matches(RE_NEWNPC_MULTI, line_str):
                            npc_seen = True
                            self._emit("📡 检测到newNPC信号", "INFO")
                        
                        if map_seen and npc_seen:
                            return True
                
                cursor = kernel_cursor()
            except Exception:
                pass
            
            time.sleep(0.05)
        
        return False
    
    def _wait_for_petitem_unlimited(self) -> bool:
        """等待PetItem信号（无超时限制，但会响应中止）"""
        from core.logger import fetch_kernel_since, kernel_cursor

        cursor = kernel_cursor()
        
        while True:
            if self._should_abort():
                return False
            
            try:
                lines = fetch_kernel_since(cursor)
                if isinstance(lines, list):
                    for line in lines:
                        if line_matches(RE_PETITEM, str(line)):
                            self._emit("✅ 检测到PetItem信号", "SUCCESS")
                            return True
                
                cursor = kernel_cursor()
            except Exception:
                pass
            
            time.sleep(0.02)
    
    def _wait_for_petitem_and_first_skill(self, regions, use_foreground: bool, timeout_s: Optional[float] = None) -> bool:
        """等待PetItem或第一次灰变蓝，然后使用第一回合技能
        
        Args:
            regions: 区域存储
            use_foreground: 是否前台运行
            timeout_s: 超时时间（秒），如果为None表示无超时限制
        """
        from core.logger import fetch_kernel_since, kernel_cursor
        
        battle_runner = getattr(self.bot, "battle_runner", None)
        if battle_runner is None:
            self._emit("❌ 缺少battle_runner，无法执行第一回合技能", "ERROR")
            return False

        cursor = kernel_cursor()
        probe_model = battle_runner._load_probe_templates()
        last_probe_state = "UNKNOWN"
        petitem_detected = False
        
        start_time = time.time()
        
        # 如果timeout_s为None，表示无超时限制（使用一个很长的超时时间）
        effective_timeout = timeout_s if timeout_s is not None else 3600.0  # 默认1小时
        
        while (time.time() - start_time) < effective_timeout:
            if self._should_abort():
                return False
            
            self._wait_if_paused()
            
            # 1. 检测PetItem信号
            if not petitem_detected:
                try:
                    lines = fetch_kernel_since(cursor)
                    if isinstance(lines, list):
                        for line in lines:
                            if line_matches(RE_PETITEM, str(line)):
                                self._emit("✅ 检测到PetItem信号，使用第一回合技能", "SUCCESS")
                                petitem_detected = True
                                skill_key = "对战.使用技能一"
                                if not self._click_region_safe(regions, skill_key, use_foreground):
                                    return False
                                time.sleep(0.1)
                                return True
                    
                    cursor = kernel_cursor()
                except Exception:
                    pass
            
            # 2. 检测第一次灰变蓝
            state, s_blue, s_gray = self._unified_framework._detect_round_probe(probe_model)
            if last_probe_state == "GRAY" and state == "BLUE":
                self._emit("✅ 检测到第一次灰变蓝，使用第一回合技能", "SUCCESS")
                skill_key = "对战.使用技能一"
                if not self._click_region_safe(regions, skill_key, use_foreground):
                    return False
                time.sleep(0.1)
                return True
            
            last_probe_state = state
            time.sleep(0.05)
        
        # 超时处理
        if timeout_s is not None:
            self._emit(f"⏱️ 等待PetItem或灰变蓝超时（{timeout_s}秒），放弃检测继续下一步", "WARN")
        else:
            self._emit("⏱️ 等待PetItem或灰变蓝超时（未知原因），放弃检测继续下一步", "WARN")
        return True
    
    def _run_chaos_battle_loop(self, regions, use_foreground: bool, cleaner, is_chaos: bool = True) -> bool:
        """执行战斗循环
        
        Args:
            regions: 区域存储
            use_foreground: 是否前台运行
            cleaner: PostBattleCleaner实例
            is_chaos: 是否为大乱斗模式（True=大乱斗，False=1v1）
        """
        from core.logger import fetch_kernel_since, kernel_cursor
        
        battle_runner = getattr(self.bot, "battle_runner", None)
        if battle_runner is None:
            self._emit("❌ 缺少battle_runner，无法执行战斗循环", "ERROR")
            return False
        
        # 加载探针模板（使用battle_runner的方法）
        probe_model = battle_runner._load_probe_templates()
        
        # 检测Map+NewNPC信号（战斗结束）
        cursor = kernel_cursor()
        map_seen = False
        npc_seen = False
        
        # 大乱斗模式：灰色期间切换精灵的逻辑
        if is_chaos:
            switch_sequence = [
                "对战.切换精灵.切换精灵二",
                "对战.切换精灵.出战",
                "对战.切换精灵.切换精灵三",
                "对战.切换精灵.出战"
            ]
            switch_index = 0
            last_switch_time = 0.0
            switch_interval = 0.5  # 每0.5秒点击一次
            # ✅ 大乱斗模式：记录上一回合第一个点击的技能（None表示第一次）
            last_first_skill = None
        
        last_probe_state = "UNKNOWN"
        
        self._emit("⚔️ 开始战斗循环...", "INFO")
        
        while True:
            if self._should_abort():
                return False
            
            self._wait_if_paused()
            
            # 检查Map+NewNPC（战斗结束）
            try:
                lines = fetch_kernel_since(cursor)
                if isinstance(lines, list):
                    for line in lines:
                        line_str = str(line)
                        if _kernel_line_has_any_map(line_str):
                            map_seen = True
                            self._emit("🗺 检测到map信号", "INFO")
                        if line_matches(RE_NEWNPC_MULTI, line_str):
                            npc_seen = True
                            self._emit("📡 检测到newNPC信号", "INFO")
                        
                        if map_seen and npc_seen:
                            self._emit("🏁 战斗结束（map + newNPC）", "SUCCESS")
                            return True
                
                cursor = kernel_cursor()
            except Exception:
                pass
            
            # 检测回合探针（使用unified_framework的方法）
            state, s_blue, s_gray = self._unified_framework._detect_round_probe(probe_model)
            
            # 检测灰变蓝：使用技能（后续回合的灰变蓝）
            if last_probe_state == "GRAY" and state == "BLUE":
                if is_chaos:
                    # ✅ 大乱斗模式：四个技能都要点一遍，按随机顺序
                    # 规则：
                    # 1. 第一个技能不能和上一回合第一个技能相同
                    # 2. 第一次（last_first_skill为None）必须把技能一放在第一位
                    # 3. 如果技能一不是第一位，必须放在第二位
                    
                    skill_names = ['一', '二', '三', '四']
                    all_skills = [1, 2, 3, 4]
                    
                    if last_first_skill is None:
                        # 第一次：技能一必须在第一位
                        skill_order = [1, 2, 3, 4]
                        random.shuffle(skill_order[1:])  # 随机排列后三个技能
                        first_skill = 1
                    else:
                        # 后续回合：
                        # 1. 第一个技能不能和上一回合第一个技能相同
                        # 2. 如果技能一不是第一位，必须放在第二位
                        available_first_skills = [s for s in all_skills if s != last_first_skill]
                        first_skill = random.choice(available_first_skills)
                        
                        if first_skill == 1:
                            # 如果第一个技能是一，其他三个随机排列
                            remaining = [s for s in all_skills if s != 1]
                            random.shuffle(remaining)
                            skill_order = [1] + remaining
                        else:
                            # 如果第一个技能不是一，技能一必须在第二位
                            remaining = [s for s in all_skills if s not in [first_skill, 1]]
                            random.shuffle(remaining)
                            skill_order = [first_skill, 1] + remaining
                    
                    # 记录本回合第一个技能
                    last_first_skill = first_skill
                    
                    # 按顺序点击四个技能
                    self._emit(f"🎯 检测到灰变蓝：大乱斗模式，按顺序点击技能{skill_order[0]}、{skill_order[1]}、{skill_order[2]}、{skill_order[3]}", "INFO")
                    for skill_num in skill_order:
                        skill_key = f"对战.使用技能{skill_names[skill_num - 1]}"
                        if not self._click_region_safe(regions, skill_key, use_foreground):
                            return False
                        time.sleep(0.1)  # 每个技能点击后等待0.1秒
                else:
                    # 1v1模式：使用一技能
                    skill_key = "对战.使用技能一"
                    self._emit("🎯 检测到灰变蓝：使用一技能", "INFO")
                    if not self._click_region_safe(regions, skill_key, use_foreground):
                        return False
                    time.sleep(0.1)  # 短暂等待
            elif state == "GRAY" and is_chaos:
                # 大乱斗模式：灰色期间按顺序点击切换精灵二/出战/切换精灵三/出战
                now = time.time()
                if now - last_switch_time >= switch_interval:
                    switch_key = switch_sequence[switch_index]
                    # ✅ 移除频繁的灰色探针日志输出（减少日志噪音）
                    if not self._click_region_safe(regions, switch_key, use_foreground):
                        return False
                    switch_index = (switch_index + 1) % len(switch_sequence)  # 循环
                    last_switch_time = now
            
            last_probe_state = state
            time.sleep(0.05)
    
    def _detect_victory_probe_yellow_or_white(self, cleaner, use_foreground: bool, timeout_s: float = 8.0) -> bool:
        """检测胜利探针（黄色或白色FFFFFF）
        
        注意：此方法仅用于大乱斗x2和1v1x2模式
        训练室和勇者之塔仍使用只检测黄色的detect_victory_probe_yellow方法
        
        支持的探针颜色：
        - 黄色（通过cleaner.detect_victory_probe_yellow检测）
        - 白色（FFFFFF，RGB值都>=245）
        """
        result = self._detect_victory_probe_result(cleaner, use_foreground, timeout_s)
        return result in ("yellow", "white")

    def _detect_victory_probe_result(
        self, cleaner, use_foreground: bool, timeout_s: float = 8.0
    ) -> Optional[str]:
        """检测胜利探针颜色，返回 "yellow" | "white" | None
        
        用于雷伊特训：黄色=胜利，白色=失败
        """
        import numpy as np

        key_victory = "对战.胜利探针"

        try:
            start_time = time.time()
            while (time.time() - start_time) < timeout_s:
                if self._should_abort():
                    return None

                try:
                    got_yellow, score, rgb = cleaner.detect_victory_probe_yellow(
                        use_foreground=use_foreground,
                        tol=10,
                        ratio_th=0.75
                    )

                    if got_yellow:
                        self._emit(f"✅ 检测到胜利黄色探针 (score={score:.3f}, rgb={rgb})", "SUCCESS")
                        return "yellow"

                    img = cleaner._grab_region_img(key_victory)
                    if img is None:
                        time.sleep(0.08)
                        continue

                    arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
                    h, w = arr.shape[:2]
                    cy, cx = h // 2, w // 2
                    y1_patch = max(cy - 1, 0)
                    y2_patch = min(cy + 2, h)
                    x1_patch = max(cx - 1, 0)
                    x2_patch = min(cx + 2, w)
                    patch = arr[y1_patch:y2_patch, x1_patch:x2_patch, :]

                    white_mask = (
                        (patch[..., 0].astype(np.int16) >= 245) &
                        (patch[..., 1].astype(np.int16) >= 245) &
                        (patch[..., 2].astype(np.int16) >= 245)
                    )

                    if white_mask.any():
                        white_pixels = patch[white_mask]
                        avg_rgb = white_pixels.mean(axis=0).astype(int)
                        self._emit(f"✅ 检测到胜利白色探针（FFFFFF，rgb={tuple(avg_rgb)}）", "SUCCESS")
                        return "white"

                except Exception as e:
                    self._emit(f"⚠️ 检测胜利探针异常: {e}", "WARN")

                time.sleep(0.08)

            return None
        except Exception as e:
            self._emit(f"❌ 检测胜利探针失败: {e}", "ERROR")
            return None
    
    def _wait_for_1and1_cleanup(self, use_foreground: bool, timeout_s: float = 0.0) -> bool:
        """等待并点击1AND1直到消失
        
        Args:
            use_foreground: 是否前台运行
            timeout_s: 超时时间，0.0表示不超时直到消失，>0表示超时时间（秒）
        """
        from core.unified_battle_framework import BattleConfig, BattleMode
        
        if self._unified_framework is None:
            return False
        
        # 创建临时config用于1AND1检测
        config = BattleConfig(
            mode=BattleMode.FIXED,
            use_foreground=use_foreground,
            abort_check=lambda: self._should_abort()
        )
        
        try:
            # 调用统一框架的1AND1清理方法
            self._unified_framework._wait_for_confirm_probes(config, timeout_s=timeout_s)
            return True
        except Exception as e:
            self._emit(f"⚠️ 1AND1清理异常: {e}", "WARN")
            return False
    
    # ----------------------------
    # 1v1x2：特殊战斗模式
    # ----------------------------
    def run_1v1_x2(self, use_foreground: bool = True) -> bool:
        """执行1v1x2：两次特殊战斗循环（包含恢复）"""
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口：无法执行1v1x2", "ERROR")
            return False
        
        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions，无法执行1v1x2", "ERROR")
            return False
        
        from config import TEMPLATES_PATH
        cleaner = PostBattleCleaner(self.bot, regions, TEMPLATES_PATH)
        
        # 初始化统一框架（用于探针检测和战斗逻辑）
        if self._unified_framework is None:
            self._unified_framework = UnifiedBattleFramework(self.bot, regions, TEMPLATES_PATH)
        
        # 执行两次战斗
        for battle_num in range(2):
            if self._should_abort():
                self._emit("⛔ 1v1x2中止（stop_current）", "SYSTEM")
                return False
            
            self._emit(f"⚔ 1v1x2：第 {battle_num + 1}/2 场", "SYSTEM")
            
            # 第一场：需要先切图然后移动1v1；第二场：直接点击精灵王之战
            if battle_num == 0:
                # 1. 点击"勇者之塔.切图"
                self._emit("🖱 点击：勇者之塔.切图", "INFO")
                if not self._click_region_safe(regions, "勇者之塔.切图", use_foreground):
                    return False
                
                # 2. 等待map+NPC出现（检测到任意map和newNPC即可）
                self._emit("⏳ 等待进入地图（map + NPC）...", "INFO")
                if not self._wait_for_map_and_npc_any(timeout_s=30.0):
                    self._emit("❌ 等待地图超时", "ERROR")
                    return False
                
                # ✅ 检测到map+NPC后等待1秒再执行切换到1v1
                self._emit("⏳ 检测到map+NPC，等待1秒后切换到1v1...", "INFO")
                time.sleep(1.0)
                
                # 3. 点击"勇者之塔.移动1v1"
                self._emit("🖱 点击：勇者之塔.移动1v1", "INFO")
                if not self._click_region_safe(regions, "勇者之塔.移动1v1", use_foreground):
                    return False
                
                # 4. 等待3.5秒（延长1秒）
                time.sleep(3.5)
            
            # 5. 点击"勇者之塔.精灵王之战"
            self._emit("🖱 点击：勇者之塔.精灵王之战", "INFO")
            if not self._click_region_safe(regions, "勇者之塔.精灵王之战", use_foreground):
                return False
            
            # 6. 等待1秒
            time.sleep(1.0)
            
            # 7. 点击"勇者之塔.1v1"
            self._emit("🖱 点击：勇者之塔.1v1", "INFO")
            if not self._click_region_safe(regions, "勇者之塔.1v1", use_foreground):
                return False
            
            # 8. 等待PetItem并执行第一回合技能（1v1模式：无超时限制）
            self._emit("⏳ 等待PetItem进入对战（无超时限制）...", "INFO")
            if not self._wait_for_petitem_and_first_skill(regions, use_foreground, timeout_s=None):
                self._emit("❌ 等待PetItem或第一回合失败（可能被中止）", "ERROR")
                return False
            
            # 9. 执行战斗循环（1v1模式，不需要灰色期间点击）
            if not self._run_chaos_battle_loop(regions, use_foreground, cleaner, is_chaos=False):
                self._emit("❌ 战斗循环失败", "ERROR")
                return False
            
            # 10. 检测胜利探针（黄色或白色）并点击确认，然后1AND1清理（参考训练室/勇者之塔）
            # 先等待UI稳定（与训练室保持一致，延迟2.5秒）
            self._emit("⏳ 等待UI稳定（2.5秒）...", "INFO")
            time.sleep(2.5)
            self._emit("🟡 检测胜利探针（黄色或白色）...", "INFO")
            victory_detected = self._detect_victory_probe_yellow_or_white(cleaner, use_foreground, timeout_s=8.0)
            if not victory_detected:
                self._emit("❌ 未检测到胜利探针（超时）", "ERROR")
                return False
            
            # 11. 点击"对话框.对战胜利确认"（参考stage4_post_battle的逻辑）
            self._emit("🖱 点击：对话框.对战胜利确认", "INFO")
            if not self._click_region_safe(regions, "对话框.对战胜利确认", use_foreground):
                return False
            
            # 12. 1AND1清理对话框（使用统一框架的方法，参考训练室/勇者之塔）
            self._emit("⏳ 清理对话框（1 AND 1，10秒超时）...", "INFO")
            from core.unified_battle_framework import BattleConfig, BattleMode
            if self._unified_framework is None:
                self._emit("❌ 缺少unified_framework，无法执行1AND1清理", "ERROR")
                return False
            
            config = BattleConfig(
                mode=BattleMode.FIXED,  # 大乱斗和1v1使用固定模式
                use_foreground=use_foreground,
                abort_check=lambda: self._should_abort()
            )
            try:
                # 使用10秒超时（1v1模式）
                self._unified_framework._wait_for_confirm_probes(config, timeout_s=10.0)
            except Exception as e:
                self._emit(f"⚠️ 1AND1清理异常: {e}", "WARN")
            
            # 13. 恢复精灵一（只有第一场战斗后才执行，且等待2.5s后执行）
            if battle_num == 0:
                # ✅ 第一场战斗后等待2.5s再执行恢复流程
                self._emit("⏳ 第一场战斗完成，等待2.5秒后执行恢复流程...", "INFO")
                time.sleep(2.5)
                self._emit("🩹 恢复精灵一（第一场战斗后）...", "INFO")
                if not self._recover_pet_one(regions, use_foreground):
                    self._emit("⚠️ 恢复精灵一失败，但继续执行", "WARN")
        
        # 第二场战斗后再次恢复精灵一
        self._emit("🩹 恢复精灵一（任务结束前）...", "INFO")
        if not self._recover_pet_one(regions, use_foreground):
            self._emit("⚠️ 恢复精灵一失败", "WARN")
        
        self._emit("✅ 1v1x2：2场对战全部完成", "SUCCESS")
        return True
    
    def _recover_pet_one(self, regions, use_foreground: bool) -> bool:
        """恢复精灵一（参考野外稀有精灵模式）：点击精灵背包 -> 双击精灵一 -> 点击恢复 -> 1AND1确认 -> 点击打开背包关闭"""
        # ✅ 使用正确的region keys（和野外稀有精灵模式一致）
        bag_open_key = "精灵背包.打开精灵背包"
        pet_one_key = "精灵背包.精灵一"
        recover_key = "精灵背包.精灵恢复"
        
        # 检查统一框架是否可用（用于1AND1确认）
        if self._unified_framework is None:
            self._emit("❌ 恢复精灵一：缺少unified_framework，无法执行1AND1确认", "ERROR")
            return False
        
        try:
            # 1. 打开精灵背包（参考野外稀有精灵模式）
            self._emit("💼 打开精灵背包", "INFO")
            if not self._click_region_safe(regions, bag_open_key, use_foreground):
                return False
            # ✅ 打开精灵背包后等待2.5s，确保背包完全打开（参考野外模式）
            time.sleep(2.5)
            
            # 2. 双击精灵一（参考野外稀有精灵模式）
            self._emit("🐾 双击精灵一（准备恢复）", "INFO")
            if not self._click_region_safe(regions, pet_one_key, use_foreground):
                return False
            time.sleep(0.1)  # 短暂间隔
            if not self._click_region_safe(regions, pet_one_key, use_foreground):
                return False
            # ✅ 双击后等待0.5s（参考野外模式）
            time.sleep(0.5)
            
            # 3. 点击恢复（参考野外稀有精灵模式）
            self._emit("💊 点击精灵恢复", "INFO")
            if not self._click_region_safe(regions, recover_key, use_foreground):
                return False
            # ✅ 精灵恢复后等待1.0s，确保恢复操作完成（参考野外模式）
            time.sleep(1.0)
            
            # 4. 使用1AND1确认残留的恢复后的确认（参考野外稀有精灵模式）
            self._emit("⏳ 使用1AND1确认残留的恢复后的确认", "INFO")
            from core.unified_battle_framework import BattleConfig, BattleMode
            temp_config = BattleConfig(
                mode=BattleMode.FIXED,
                use_foreground=use_foreground,
                abort_check=lambda: self._should_abort()
            )
            # ✅ 使用2秒超时（参考野外模式）
            self._unified_framework._wait_for_confirm_probes(temp_config, timeout_s=2.0)
            
            # 5. 点击打开精灵背包关闭它（参考野外稀有精灵模式）
            self._emit("💼 扫描完成后，点击打开精灵背包关闭它", "INFO")
            if not self._click_region_safe(regions, bag_open_key, use_foreground):
                return False
            # ✅ 等待0.5s（参考野外模式）
            time.sleep(0.5)
            
            self._emit("✅ 恢复精灵一完成", "SUCCESS")
            return True
        except Exception as e:
            self._emit(f"❌ 恢复精灵一异常: {e}", "ERROR")
            return False

    def _recover_teixun_pets(self, regions, use_foreground: bool) -> bool:
        """特训恢复：恢复精灵一 + 扫描血条二/三/四，蓝色则一并恢复"""
        bag_open_key = "精灵背包.打开精灵背包"
        recover_key = "精灵背包.精灵恢复"
        pet_keys = {"一": "精灵背包.精灵一", "二": "精灵背包.精灵二", "三": "精灵背包.精灵三", "四": "精灵背包.精灵四"}
        hp_bar_keys = {"二": "精灵背包.血条二", "三": "精灵背包.血条三", "四": "精灵背包.血条四"}
        # 蓝色血条 = 需恢复（参考 dar_route_runner）
        COLOR_BLUE_HP = (24, 73, 146)
        COLOR_TOLERANCE = 25  # 距离容差

        if self._unified_framework is None:
            self._emit("❌ 恢复精灵：缺少unified_framework", "ERROR")
            return False

        try:
            self._emit("💼 打开精灵背包", "INFO")
            if not self._click_region_safe(regions, bag_open_key, use_foreground):
                return False
            time.sleep(2.5)

            def _recover_one_pet(pos: str) -> bool:
                pet_key = pet_keys.get(pos)
                if not pet_key:
                    return False
                self._emit(f"🐾 双击精灵{pos}（准备恢复）", "INFO")
                if not self._click_region_safe(regions, pet_key, use_foreground):
                    return False
                time.sleep(0.1)
                if not self._click_region_safe(regions, pet_key, use_foreground):
                    return False
                time.sleep(0.5)
                self._emit("💊 点击精灵恢复", "INFO")
                if not self._click_region_safe(regions, recover_key, use_foreground):
                    return False
                time.sleep(1.0)
                self._emit("⏳ 使用1AND1确认（10秒超时）", "INFO")
                from core.unified_battle_framework import BattleConfig, BattleMode
                cfg = BattleConfig(mode=BattleMode.FIXED, use_foreground=use_foreground, abort_check=lambda: self._should_abort())
                self._unified_framework._wait_for_confirm_probes(cfg, timeout_s=10.0)
                time.sleep(0.5)
                return True

            def _is_hp_bar_blue(hp_key: str) -> bool:
                rgb = self._unified_framework._mean_rgb(hp_key) if self._unified_framework else None
                if rgb is None:
                    return False
                dist = ((rgb[0] - COLOR_BLUE_HP[0]) ** 2 + (rgb[1] - COLOR_BLUE_HP[1]) ** 2 + (rgb[2] - COLOR_BLUE_HP[2]) ** 2) ** 0.5
                return dist <= COLOR_TOLERANCE

            # 1. 恢复精灵一
            if not _recover_one_pet("一"):
                return False

            # 2. 扫描血条二、三、四，蓝色则恢复
            for pos in ["二", "三", "四"]:
                hp_key = hp_bar_keys.get(pos)
                if not hp_key or not regions.get(hp_key):
                    continue
                if _is_hp_bar_blue(hp_key):
                    self._emit(f"🔵 血条{pos}为蓝色，恢复精灵{pos}", "INFO")
                    if not _recover_one_pet(pos):
                        self._emit(f"⚠️ 恢复精灵{pos}失败，继续", "WARN")

            self._emit("💼 关闭精灵背包", "INFO")
            if not self._click_region_safe(regions, bag_open_key, use_foreground):
                pass
            time.sleep(0.5)
            self._emit("✅ 特训恢复完成", "SUCCESS")
            return True
        except Exception as e:
            self._emit(f"❌ 特训恢复异常: {e}", "ERROR")
            return False

    # ----------------------------
    # 雷伊特训
    # ----------------------------
    def run_leiyi_training(
        self,
        loop_count: int = 10,
        use_foreground: bool = True,
        *,
        training_battle_mode: str = "leiyi",
    ) -> bool:
        """雷伊特训 / 嘟嘟卡拉。
        training_battle_mode:
          - \"leiyi\": loop_count 由输入框（1–999）；特训.1/2；战斗 4→2→1→3；白失败点特训.3 再恢复；黄探针胜利结束。
          - \"dudukala\": 无限循环直至黄探针胜利或 stop；嘟嘟卡拉1/2 入战；战斗每回合仅技能一；
            退场以「最近一次出手之后」kernel 的 map+newNpc 为准；白失败不点特训.3，直接恢复；loop_count 忽略。"""
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口：无法执行雷伊特训", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions，无法执行雷伊特训", "ERROR")
            return False

        from config import TEMPLATES_PATH

        cleaner = PostBattleCleaner(self.bot, regions, TEMPLATES_PATH)
        if self._unified_framework is None:
            self._unified_framework = UnifiedBattleFramework(self.bot, regions, TEMPLATES_PATH)

        mode = (training_battle_mode or "leiyi").strip().lower()
        if mode not in ("leiyi", "dudukala"):
            mode = "leiyi"

        if mode == "dudukala":
            self._emit(
                "🎪 嘟嘟卡拉：无限循环，仅在黄色胜利探针时结束（或点停止）；"
                "每场每回合技能一；每次出手后重认 kernel map+newNpc；白探针则恢复再继续；无特训.3",
                "SYSTEM",
            )
            loop_count = 0  # unused
        else:
            loop_count = max(1, min(999, loop_count))
            self._emit(f"⚡ 雷伊特训：最多 {loop_count} 次循环（黄=胜利退出，白=失败恢复）", "SYSTEM")

        entry_key = "特训.嘟嘟卡拉1" if mode == "dudukala" else "特训.1"
        trigger_key = "特训.嘟嘟卡拉2" if mode == "dudukala" else "特训.2"
        label = "嘟嘟卡拉" if mode == "dudukala" else "雷伊特训"

        def _do_single_training_round(loop_display: str) -> str:
            """返回 \"yellow_win\" | \"white_retry\" | \"fatal\" | \"aborted\""""
            self._emit(loop_display, "SYSTEM")

            # 1. 单击入口区域
            self._emit(f"🖱 点击：{entry_key}", "INFO")
            if not self._click_region_safe(regions, entry_key, use_foreground):
                self._emit(f"❌ 点击 {entry_key} 失败", "ERROR")
                return "fatal"

            time.sleep(0.5)

            # 2. 训练室校准：校准成功时自动重触发第二个点
            def _trigger_second():
                r = regions.get(trigger_key)
                if not r:
                    raise KeyError(f"找不到区域：{trigger_key}")
                x1, y1, x2, y2 = r.inner_bbox()
                return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

            self._emit(
                f"⏳ 等待校准直到 PetItem 出现（校准成功后自动重点 {trigger_key}）...",
                "INFO",
            )
            success, _ = self._unified_framework.stage2_calibration_and_petitem(
                trigger_callback=_trigger_second,
                use_foreground=use_foreground,
                timeout_s=60.0,
                skip_stage1=False,
            )
            if not success:
                self._emit("❌ 等待 PetItem 或校准失败", "ERROR")
                return "fatal"

            rs = 1 if mode == "dudukala" else None
            round_at_end = self._run_leiyi_battle_loop(
                regions, use_foreground, repeat_skill=rs
            )
            if round_at_end is None:
                if self._should_abort():
                    return "aborted"
                self._emit("❌ 对战特训战斗循环失败", "ERROR")
                return "fatal"

            self._emit("⏳ 等待 UI 稳定（2.5秒）...", "INFO")
            time.sleep(2.5)

            self._emit("🟡 检测胜利探针...", "INFO")
            probe_result = self._detect_victory_probe_result(cleaner, use_foreground, timeout_s=8.0)
            if probe_result is None:
                self._emit("❌ 未检测到胜利探针（超时）", "ERROR")
                return "fatal"

            self._emit("🖱 点击：对话框.对战胜利确认", "INFO")
            if not self._click_region_safe(regions, "对话框.对战胜利确认", use_foreground):
                self._emit("❌ 点击胜利确认失败", "ERROR")
                return "fatal"

            if probe_result == "yellow":
                self._emit(f"🏆 {label}：胜利（黄色探针），结束任务", "SUCCESS")
                return "yellow_win"

            self._emit("❌ 本局失败（白色探针），恢复精灵", "INFO")
            time.sleep(1.0)
            if mode == "leiyi":
                self._emit("🖱 点击：特训.3（清理失败弹窗）", "INFO")
                if not self._click_region_safe(regions, "特训.3", use_foreground):
                    self._emit("⚠️ 点击特训.3失败，继续尝试恢复", "WARN")
                time.sleep(0.5)
            self._emit("🩹 恢复精灵一...", "INFO")
            if not self._recover_pet_one(regions, use_foreground):
                self._emit("⚠️ 恢复精灵一失败，继续下一轮", "WARN")
            time.sleep(0.5)
            return "white_retry"

        if mode == "dudukala":
            rnd = 0
            while not self._should_abort():
                rnd += 1
                sub = _do_single_training_round(
                    f"🎪 {label}：第 {rnd} 轮（无限直至黄探针胜利或停止）"
                )
                if sub == "yellow_win":
                    return True
                if sub in ("fatal", "aborted"):
                    if sub == "aborted":
                        self._emit("⛔ 嘟嘟卡拉中止（stop_current）", "SYSTEM")
                    return False
                # white_retry → continue

            self._emit("⛔ 嘟嘟卡拉中止（stop_current）", "SYSTEM")
            return False

        # 雷伊：固定次数循环
        for idx in range(loop_count):
            if self._should_abort():
                self._emit("⛔ 对战特训中止（stop_current）", "SYSTEM")
                return False
            sub = _do_single_training_round(f"⚡ {label}：第 {idx + 1}/{loop_count} 轮")
            if sub == "yellow_win":
                return True
            if sub == "fatal":
                return False
            if sub == "aborted":
                self._emit("⛔ 对战特训中止（stop_current）", "SYSTEM")
                return False
            # white_retry → continue

        self._emit(f"✅ 雷伊特训：已完成 {loop_count} 次循环", "SUCCESS")
        return True

    # ----------------------------
    # 特训循环（特训.A + 特训.B，输赢都继续）
    # ----------------------------
    def run_teixun_loop(self, use_foreground: bool = True) -> bool:
        """特训循环：恢复精灵一 → A→B 入战(含校准) → 战斗(2/3/4技能) → map+黄白探针 → 确认
        黄=1AND1后恢复；白=等待后跳过1AND1直接恢复。仅停止按钮退出。"""
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口：无法执行特训循环", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions，无法执行特训循环", "ERROR")
            return False

        from config import TEMPLATES_PATH

        cleaner = PostBattleCleaner(self.bot, regions, TEMPLATES_PATH)
        if self._unified_framework is None:
            self._unified_framework = UnifiedBattleFramework(self.bot, regions, TEMPLATES_PATH)

        self._emit("🔄 特训循环：无限循环（黄=1AND1恢复，白=等待后直接恢复，仅停止按钮退出）", "SYSTEM")

        teixun_win_count = 0
        teixun_lose_count = 0
        teixun_durations: List[float] = []  # 每次对战时长（秒）

        while not self._should_abort():
            self._emit("🔄 特训循环：新一轮", "SYSTEM")

            # 1. 恢复精灵一，并扫描血条二/三/四，蓝色则一并恢复
            self._emit("🩹 恢复精灵（一+血条二/三/四蓝色者）...", "INFO")
            if not self._recover_teixun_pets(regions, use_foreground):
                self._emit("❌ 恢复精灵失败", "ERROR")
                return False
            time.sleep(0.5)

            # 2. 恢复完点击A之前，先点击一次 登录.隐藏
            self._emit("🖱 点击：登录.隐藏", "INFO")
            self._click_region_safe(regions, "登录.隐藏", use_foreground)
            time.sleep(0.3)

            # 3. 定义 trigger：先点 A，返回 B 坐标（校准 success 时会重新执行 A→B）
            def _trigger_teixun_ab():
                self._emit("🖱 点击：特训.A", "INFO")
                if not self._click_region_safe(regions, "特训.A", use_foreground):
                    raise KeyError("点击特训.A失败")
                time.sleep(0.8)  # 点击A后稍长间隔再点B
                r = regions.get("特训.B")
                if not r:
                    raise KeyError("找不到特训.B")
                x1, y1, x2, y2 = r.inner_bbox()
                return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

            self._emit("⏳ 等待校准直到 PetItem 出现（A→B，校准后重试 A→B）...", "INFO")
            teixun_config = BattleConfig(
                mode=BattleMode.FIXED,
                use_foreground=use_foreground,
                skill_key="对战.使用技能二",  # 第一回合用技能二
                abort_check=lambda: self._should_abort(),
            )
            success, _ = self._unified_framework.stage2_calibration_and_petitem(
                trigger_callback=_trigger_teixun_ab,
                use_foreground=use_foreground,
                timeout_s=60.0,
                skip_stage1=False,
                config=teixun_config,
            )
            if not success:
                self._emit("⚠️ 等待 PetItem 或校准失败，重试入战...", "WARN")
                continue

            # 4. 战斗循环：2-4技能二×3，5-9技能三×5，10技能四×1后续四（第10回合后加切换）
            battle_start_time = time.time()
            round_at_end = self._run_teixun_battle_loop(regions, use_foreground)
            if round_at_end is None:
                self._emit("❌ 特训战斗循环失败或被中止", "ERROR")
                return False

            # 5. 等待 UI 稳定
            self._emit("⏳ 等待 UI 稳定（2.5秒）...", "INFO")
            time.sleep(2.5)

            # 6. 检测黄白探针
            self._emit("🟡 检测胜利探针...", "INFO")
            probe_result = self._detect_victory_probe_result(cleaner, use_foreground, timeout_s=8.0)
            if probe_result is None:
                self._emit("❌ 未检测到胜利探针（超时）", "ERROR")
                return False

            # 7. 点击确认
            self._emit("🖱 点击：对话框.对战胜利确认", "INFO")
            if not self._click_region_safe(regions, "对话框.对战胜利确认", use_foreground):
                self._emit("❌ 点击胜利确认失败", "ERROR")
                return False

            # 8. 战后统计 + 处理：黄=胜利，白=失败
            battle_duration = time.time() - battle_start_time
            teixun_durations.append(battle_duration)
            avg_dur = sum(teixun_durations) / len(teixun_durations)
            if probe_result == "yellow":
                teixun_win_count += 1
                self._emit(f"🏆 黄色探针：胜利（累计胜 {teixun_win_count} 败 {teixun_lose_count}，本次 {battle_duration:.1f}s，平均 {avg_dur:.1f}s）", "INFO")
                self._wait_for_1and1_cleanup(use_foreground, timeout_s=10.0)
            else:
                teixun_lose_count += 1
                self._emit(f"❌ 白色探针：失败（累计胜 {teixun_win_count} 败 {teixun_lose_count}，本次 {battle_duration:.1f}s，平均 {avg_dur:.1f}s）", "INFO")
                time.sleep(2.0)  # 等待一小段时间

        # 8. 停止时输出统计
        total = teixun_win_count + teixun_lose_count
        avg_dur = sum(teixun_durations) / len(teixun_durations) if teixun_durations else 0.0
        self._emit("✅ 特训循环已停止", "SUCCESS")
        self._emit(f"📊 统计：胜 {teixun_win_count} 败 {teixun_lose_count} 共 {total} 场，平均每场 {avg_dur:.1f} 秒", "SYSTEM")
        return True

    def _run_teixun_battle_loop(
        self, regions, use_foreground: bool
    ) -> Optional[int]:
        """特训战斗循环：2-4回合技能二×3，5-9回合技能三×5，10回合技能四×1，后续四
        第十回合使用技能四后灰期：双击精灵一→出战→精灵二→出战→精灵三→出战，直到下次蓝"""
        from core.logger import fetch_kernel_since, kernel_cursor

        battle_runner = getattr(self.bot, "battle_runner", None)
        if battle_runner is None:
            self._emit("❌ 缺少 battle_runner，无法执行战斗循环", "ERROR")
            return None

        probe_model = battle_runner._load_probe_templates()
        skill_names = ["一", "二", "三", "四"]

        # 第十回合起灰期：按 2341→3412→4123→1234 循环（每回合一循环）
        _chu = "对战.切换精灵.出战"
        _s = lambda n: f"对战.切换精灵.切换精灵{['一','二','三','四'][n-1]}"
        teixun_switch_cycles = [
            [_s(2), _chu, _s(3), _chu, _s(4), _chu, _s(1), _chu],  # 2341
            [_s(3), _chu, _s(4), _chu, _s(1), _chu, _s(2), _chu],  # 3412
            [_s(4), _chu, _s(1), _chu, _s(2), _chu, _s(3), _chu],  # 4123
            [_s(1), _chu, _s(2), _chu, _s(3), _chu, _s(4), _chu],  # 1234
        ]
        teixun_switch_index = 0
        teixun_switch_cycle_idx = 0  # 当前使用的循环：0=2341, 1=3412, 2=4123, 3=1234
        last_teixun_switch_time = 0.0
        teixun_switch_interval = 0.5
        skill_four_after_round10 = False  # 第十回合使用技能四后为True，灰期做切换

        cursor = kernel_cursor()
        map_seen = False

        last_probe_state = "UNKNOWN"
        round_idx = 1  # stage2 已执行第1回合，此处从第2回合起

        self._emit("⚔️ 特训战斗循环：2-4技能二×3，5-9技能三×5，10技能四×1后续四；第10回合起灰期 2341→3412→4123→1234 循环", "INFO")

        def _double_click_region(key: str) -> bool:
            if not self._click_region_safe(regions, key, use_foreground):
                return False
            time.sleep(0.1)
            if not self._click_region_safe(regions, key, use_foreground):
                return False
            time.sleep(0.1)
            return True

        while True:
            if self._should_abort():
                return None

            self._wait_if_paused()

            # 检测 map 日志（战斗结束，只需 map 即可）
            try:
                lines = fetch_kernel_since(cursor)
                if isinstance(lines, list):
                    for line in lines:
                        line_str = str(line)
                        if _kernel_line_has_any_map(line_str):
                            map_seen = True
                            self._emit(f"🏁 战斗结束（检测到 map，第 {round_idx} 回合后）", "SUCCESS")
                            return round_idx
                cursor = kernel_cursor()
            except Exception:
                pass

            state, _, _ = self._unified_framework._detect_round_probe(probe_model)

            def _skill_for_round(r: int) -> int:
                if r <= 4:
                    return 2  # 2-4回合技能二×3
                if r <= 9:
                    return 3  # 5-9回合技能三×5
                return 4  # 10回合技能四×1，后续四

            if round_idx == 1 and state == "BLUE":
                skill_num = _skill_for_round(2)
                skill_key = f"对战.使用技能{skill_names[skill_num - 1]}"
                self._emit(f"🎯 第 2 回合：使用技能{skill_num}", "INFO")
                if not self._click_region_safe(regions, skill_key, use_foreground):
                    return None
                if round_idx + 1 >= 10 and skill_num == 4:
                    skill_four_after_round10 = True
                    teixun_switch_cycle_idx = (round_idx + 1 - 10) % 4
                    teixun_switch_index = 0
                time.sleep(0.1)
                round_idx += 1
            elif last_probe_state == "GRAY" and state == "BLUE":
                skill_num = _skill_for_round(round_idx + 1)
                skill_key = f"对战.使用技能{skill_names[skill_num - 1]}"
                self._emit(f"🎯 第 {round_idx + 1} 回合：使用技能{skill_num}", "INFO")
                if not self._click_region_safe(regions, skill_key, use_foreground):
                    return None
                if round_idx + 1 >= 10 and skill_num == 4:
                    skill_four_after_round10 = True
                    teixun_switch_cycle_idx = (round_idx + 1 - 10) % 4
                    teixun_switch_index = 0
                time.sleep(0.1)
                round_idx += 1
            elif state == "GRAY" and skill_four_after_round10:
                # 灰期：按当前回合对应循环 2341/3412/4123/1234 双击
                now = time.time()
                if now - last_teixun_switch_time >= teixun_switch_interval:
                    seq = teixun_switch_cycles[teixun_switch_cycle_idx]
                    switch_key = seq[teixun_switch_index % len(seq)]
                    if not _double_click_region(switch_key):
                        return None
                    teixun_switch_index += 1
                    last_teixun_switch_time = now

            last_probe_state = state
            time.sleep(0.05)

    def _run_leiyi_battle_loop(
        self,
        regions,
        use_foreground: bool,
        *,
        repeat_skill: Optional[int] = None,
        max_skill_uses: Optional[int] = None,
    ) -> Optional[int]:
        """雷伊特训战斗循环：默认技能顺序 4→2→1→3。
        repeat_skill 若指定（如 1）：每检测到可出手则点该技能；出手后以带数字 id 的 map swf（如 path=resource\\map\\429.swf）
        判定退场即可，无需 newNpc。雷伊特训（无 repeat_skill）：仍为 map + newNpc。
        返回战斗结束时的回合计（内部计数），None 表示失败/中止。"""
        from core.logger import fetch_kernel_since, kernel_cursor

        battle_runner = getattr(self.bot, "battle_runner", None)
        if battle_runner is None:
            self._emit("❌ 缺少 battle_runner，无法执行战斗循环", "ERROR")
            return None

        probe_model = battle_runner._load_probe_templates()
        skill_order = [4, 2, 1, 3]
        skill_names = ["一", "二", "三", "四"]

        cursor = kernel_cursor()
        map_seen = False
        npc_seen = False
        # 特训/嘟嘟卡拉：退场常只有 path=resource\map\{id}.swf，没有 newNpc 行。
        reload_map_swf_seen = False
        reload_map_mid: Optional[int] = None

        last_probe_state = "UNKNOWN"
        round_idx = 0

        skill_uses_done = 0
        skill_cap = max_skill_uses if max_skill_uses is not None and max_skill_uses > 0 else None

        def _mark_battle_exit_window_after_skill() -> None:
            """清掉入局前残留的 map/newNpc，并从当前 kernel 末尾只认「出过招之后」的行。"""
            nonlocal cursor, map_seen, npc_seen, reload_map_swf_seen, reload_map_mid
            map_seen = False
            npc_seen = False
            reload_map_swf_seen = False
            reload_map_mid = None
            cursor = kernel_cursor()

        if repeat_skill is not None:
            if repeat_skill not in (1, 2, 3, 4):
                repeat_skill = 1
            cap_txt = "" if skill_cap is None else f"（最多出手{skill_cap}次）"
            self._emit(f"⚔️ 战斗循环：每回合技能{repeat_skill}{cap_txt}", "INFO")
        else:
            self._emit("⚔️ 雷伊特训战斗循环：技能顺序 4→2→1→3", "INFO")

        while True:
            if self._should_abort():
                return None

            self._wait_if_paused()

            # 检测退场：repeat_skill（嘟嘟卡拉等）常以单条 resource\map\{id}.swf 收场；雷伊特训仍要 map+newNpc。
            try:
                lines = fetch_kernel_since(cursor)
                if isinstance(lines, list):
                    ever_acted = skill_uses_done > 0 or round_idx > 0
                    for line in lines:
                        line_str = str(line)
                        if ever_acted:
                            pid = first_map_id_in_line(line_str)
                            if pid is not None:
                                reload_map_swf_seen = True
                                reload_map_mid = pid
                        if _kernel_line_has_any_map(line_str):
                            map_seen = True
                        if line_matches(RE_NEWNPC_MULTI, line_str):
                            npc_seen = True
                    if ever_acted:
                        if repeat_skill is not None and reload_map_swf_seen:
                            extra = f" id={reload_map_mid}" if reload_map_mid is not None else ""
                            self._emit(
                                f"🏁 战斗结束（退场 resource/map/*.swf{extra}，第 {round_idx} 回合后）",
                                "SUCCESS",
                            )
                            return round_idx
                        if repeat_skill is None and map_seen and npc_seen:
                            self._emit(
                                f"🏁 战斗结束（map+newNpc，第 {round_idx} 回合后）",
                                "SUCCESS",
                            )
                            return round_idx
                cursor = kernel_cursor()
            except Exception:
                pass

            state, _, _ = self._unified_framework._detect_round_probe(probe_model)

            def _exec_skill(skill_num: int) -> bool:
                sk = f"对战.使用技能{skill_names[skill_num - 1]}"
                return self._click_region_safe(regions, sk, use_foreground)

            if repeat_skill is not None:
                can_cast = skill_cap is None or skill_uses_done < skill_cap
                if can_cast and round_idx == 0 and state == "BLUE":
                    self._emit(
                        f"🎯 第 1 回合：使用技能{repeat_skill}（探针已蓝，立即执行）",
                        "INFO",
                    )
                    if not _exec_skill(repeat_skill):
                        return None
                    _mark_battle_exit_window_after_skill()
                    time.sleep(0.1)
                    skill_uses_done += 1
                    round_idx += 1
                elif can_cast and last_probe_state == "GRAY" and state == "BLUE":
                    self._emit(f"🎯 第 {round_idx + 1} 回合：使用技能{repeat_skill}", "INFO")
                    if not _exec_skill(repeat_skill):
                        return None
                    _mark_battle_exit_window_after_skill()
                    time.sleep(0.1)
                    skill_uses_done += 1
                    round_idx += 1
            else:
                # ✅ 首次进入时探针可能已是 BLUE（第一回合已就绪），需立即执行技能 4
                if round_idx == 0 and state == "BLUE":
                    skill_num = skill_order[0]
                    self._emit(
                        f"🎯 第 1 回合：使用技能{skill_num}（探针已蓝，立即执行）",
                        "INFO",
                    )
                    if not _exec_skill(skill_num):
                        return None
                    _mark_battle_exit_window_after_skill()
                    time.sleep(0.1)
                    round_idx += 1
                elif last_probe_state == "GRAY" and state == "BLUE":
                    if round_idx < len(skill_order):
                        skill_num = skill_order[round_idx]
                        self._emit(
                            f"🎯 第 {round_idx + 1} 回合：使用技能{skill_num}",
                            "INFO",
                        )
                        if not _exec_skill(skill_num):
                            return None
                        _mark_battle_exit_window_after_skill()
                        time.sleep(0.1)
                        round_idx += 1

            last_probe_state = state
            time.sleep(0.05)

    def run_single_script(self, script_name: str, bg_mode: bool = True) -> bool:
        script_path = self._resolve_script_path(script_name)
        if not script_path:
            self._emit(f"❌ 脚本不存在: {script_name}", "ERROR")
            return False
        return self.run_script(script_path, bg_override=bg_mode)

    # ----------------------------
    # 核心执行器
    # ----------------------------
    def run_script(self, script_path: str, bg_override: Optional[bool] = None) -> bool:
        if not os.path.exists(script_path):
            self._emit(f"❌ 脚本不存在: {script_path}", "ERROR")
            return False

        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口：请先在 Dashboard 点【启动游戏】", "ERROR")
            return False

        try:
            with open(script_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            steps = data.get("steps", [])
            if not steps:
                self._emit(f"⚠ 脚本为空（没有 steps）：{os.path.basename(script_path)}", "WARN")
                return False

            self._emit(f"📜 开始执行脚本: {os.path.basename(script_path)}", "SYSTEM")

            for idx, step in enumerate(steps, start=1):
                if self._should_abort():
                    self._emit("⛔ 脚本中止（stop_current）", "SYSTEM")
                    return False

                self._wait_if_paused()

                gx, gy = self._parse_step_xy(step)
                if gx is None or gy is None:
                    self._emit(f"⚠ [步骤 {idx}] 无法解析坐标，已跳过: {step}", "WARN")
                    continue

                delay = float(step.get("delay", 0.2))
                if delay < 0:
                    delay = 0.0

                if bg_override is None:
                    bg = bool(step.get("bg", True))
                else:
                    bg = bool(bg_override)

                mode_text = "后台" if bg else "前台"
                
                # 构建日志信息，显示区域名称（如果有）
                log_info = f"✅ [步骤 {idx}] 点击: ({int(gx)}, {int(gy)})"
                
                # 如果使用了区域名称，在日志中显示
                if "x" in step and "y" in step:
                    x_val = step["x"]
                    y_val = step["y"]
                    region_info = []
                    if isinstance(x_val, str):
                        region_info.append(f"x={x_val}")
                    if isinstance(y_val, str):
                        region_info.append(f"y={y_val}")
                    if region_info:
                        log_info += f" | 区域: {', '.join(region_info)}"
                
                log_info += f" | 延迟 {delay:.2f}s | 模式: {mode_text}"
                self._emit(log_info, "DEBUG")

                time.sleep(delay)

                if bg:
                    window_manager.click_background(gx, gy)
                else:
                    window_manager.click(gx, gy)

            self._emit(f"✅ 脚本完成: {os.path.basename(script_path)}", "SUCCESS")
            return True

        except Exception as e:
            logger.error(f"读取或执行脚本异常: {e}")
            self._emit(f"💥 脚本执行异常: {e}", "ERROR")
            return False

    # ----------------------------
    # helpers
    # ----------------------------
    def _resolve_script_path(self, script_name: str) -> Optional[str]:
        name = (script_name or "").strip()
        if not name:
            return None

        if os.path.exists(name) and name.lower().endswith(".json"):
            return os.path.abspath(name)

        if not name.lower().endswith(".json"):
            name2 = name + ".json"
        else:
            name2 = name

        p = os.path.join(self.script_dir, name2)
        if os.path.exists(p):
            return os.path.abspath(p)

        return None

    def _parse_step_xy(self, step: Dict[str, Any]):
        """解析步骤坐标，支持：
        1. 数字坐标：{"x": 100, "y": 200}
        2. 区域名称：{"x": "对战.胜利探针", "y": "对战.胜利探针"} 或 {"x": "对战.胜利探针", "y": 200}
        3. 数组格式：{"pos": [100, 200]}
        4. gx/gy格式：{"gx": 100, "gy": 200}
        """
        # 辅助函数：获取区域的中心点坐标
        def _get_region_center(region_name: str) -> Optional[Tuple[float, float]]:
            """从区域名称获取中心点坐标"""
            regions = getattr(self.bot, "regions", None)
            if not regions:
                return None
            region = regions.get(region_name)
            if not region:
                return None
            try:
                x1, y1, x2, y2 = region.outer_bbox()
                return (x1 + x2) / 2.0, (y1 + y2) / 2.0
            except Exception:
                return None

        # 1. 解析 x, y 格式
        if "x" in step and "y" in step:
            x_val = step["x"]
            y_val = step["y"]
            
            # 如果 x 是字符串（区域名称）
            if isinstance(x_val, str):
                center = _get_region_center(x_val)
                if center is None:
                    self._emit(f"⚠️ 找不到区域：{x_val}", "WARN")
                    return None, None
                gx = center[0]
            else:
                # x 是数字
                try:
                    gx = float(x_val)
                except (ValueError, TypeError):
                    return None, None
            
            # 如果 y 是字符串（区域名称）
            if isinstance(y_val, str):
                center = _get_region_center(y_val)
                if center is None:
                    self._emit(f"⚠️ 找不到区域：{y_val}", "WARN")
                    return None, None
                gy = center[1]
            else:
                # y 是数字
                try:
                    gy = float(y_val)
                except (ValueError, TypeError):
                    return None, None
            
            return gx, gy

        # 2. 解析 pos 数组格式（兼容旧格式）
        if "pos" in step and isinstance(step["pos"], (list, tuple)) and len(step["pos"]) >= 2:
            try:
                pos_x = step["pos"][0]
                pos_y = step["pos"][1]
                
                # 支持 pos 中也可以是区域名称
                if isinstance(pos_x, str):
                    center = _get_region_center(pos_x)
                    if center is None:
                        self._emit(f"⚠️ 找不到区域：{pos_x}", "WARN")
                        return None, None
                    gx = center[0]
                else:
                    gx = float(pos_x)
                
                if isinstance(pos_y, str):
                    center = _get_region_center(pos_y)
                    if center is None:
                        self._emit(f"⚠️ 找不到区域：{pos_y}", "WARN")
                        return None, None
                    gy = center[1]
                else:
                    gy = float(pos_y)
                
                return gx, gy
            except Exception:
                return None, None

        # 3. 解析 gx, gy 格式（兼容旧格式）
        if "gx" in step and "gy" in step:
            try:
                gx_val = step["gx"]
                gy_val = step["gy"]
                
                # 支持 gx/gy 中也可以是区域名称
                if isinstance(gx_val, str):
                    center = _get_region_center(gx_val)
                    if center is None:
                        self._emit(f"⚠️ 找不到区域：{gx_val}", "WARN")
                        return None, None
                    gx = center[0]
                else:
                    gx = float(gx_val)
                
                if isinstance(gy_val, str):
                    center = _get_region_center(gy_val)
                    if center is None:
                        self._emit(f"⚠️ 找不到区域：{gy_val}", "WARN")
                        return None, None
                    gy = center[1]
                else:
                    gy = float(gy_val)
                
                return gx, gy
            except Exception:
                return None, None

        return None, None

    def _should_abort(self) -> bool:
        return bool(getattr(self.bot, "stop_current", False))

    def _wait_if_paused(self):
        if hasattr(self.bot, "wait_if_paused") and callable(getattr(self.bot, "wait_if_paused")):
            self.bot.wait_if_paused()
            return

        while getattr(self.bot, "is_paused", False) and (not self._should_abort()):
            time.sleep(0.05)

    def _emit(self, text: str, level: str = "INFO"):
        if hasattr(self.bot, "emit_and_log") and callable(getattr(self.bot, "emit_and_log")):
            self.bot.emit_and_log(text, level)
        else:
            try:
                self.bot.log_signal.emit(text, level)
            except Exception:
                pass
