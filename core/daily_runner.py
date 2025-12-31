# core/daily_runner.py
import json
import os
import random
import time
from typing import Optional, List, Dict, Any

from core.logger import logger
from core.utils import window_manager
from core.post_battle_cleaner import PostBattleCleaner
from core.unified_battle_framework import UnifiedBattleFramework, BattleConfig, BattleMode
from core.fixed_mode_adapter import FixedModeAdapter

# 优先用 config 里的 BASE_PATH / DAILY_SEQUENCE（如果没有也能兜底）
try:
    from config import BASE_PATH, DAILY_SEQUENCE
except Exception:
    BASE_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DAILY_SEQUENCE = []


class DailyRunner:
    """
    ✅ 兼容两种脚本格式：
    1) 新录制器（tools/script_recorder.py）导出：
       step: {"action":"click","x":..,"y":..,"delay":..}
    2) 老格式：
       step: {"pos":[x,y],"delay":..,"bg": true/false}

    ✅ 给 BotWorker 使用的 API：
    - run_all(background_mode=True)
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
    def run_all(self, background_mode: bool = True, sequence: Optional[List[str]] = None) -> bool:
        """
        执行 config.DAILY_SEQUENCE（或外部传入 sequence）中的脚本（不带 .json 也行）
        background_mode=True => 全部按后台执行（除非脚本里显式写 bg=false 且你不覆盖）
        """
        if sequence is None:
            sequence = list(DAILY_SEQUENCE or [])

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

        # ✅ 完成AtoF后执行勇者之塔循环
        # 检查是否完成了AtoF（即序列包含A到F，且最后一个脚本是F）
        if not self._should_abort() and ok_all:
            # 检查序列是否包含A到F（按顺序）
            expected_atof = ["A", "B", "C", "D", "E", "F"]
            if len(sequence) >= len(expected_atof):
                # 检查前6个是否匹配AtoF
                if sequence[:len(expected_atof)] == expected_atof:
                    try:
                        self._emit("⏱ AtoF完成：1s 后开始【勇者之塔】循环…", "SYSTEM")
                        time.sleep(1.0)
                        ok_tower = self.run_hero_tower(times=10, background_mode=background_mode, use_unified_framework=False)
                        ok_all = ok_all and ok_tower
                        
                        # ✅ 勇者之塔完成后，先点击"勇者之塔.离开"，等待7秒，再执行1v1x2和大乱斗x2
                        if not self._should_abort() and ok_tower:
                            use_foreground = (not background_mode)
                            regions = getattr(self.bot, "regions", None)
                            
                            if regions:
                                # 点击"勇者之塔.离开"
                                try:
                                    self._emit("🖱 点击：勇者之塔.离开", "INFO")
                                    if self._click_region_safe(regions, "勇者之塔.离开", use_foreground):
                                        # 等待7秒
                                        self._emit("⏳ 等待7秒...", "INFO")
                                        time.sleep(7.0)
                                    else:
                                        self._emit("⚠️ 点击勇者之塔.离开失败，但继续执行", "WARN")
                                except Exception as e:
                                    self._emit(f"⚠️ 点击勇者之塔.离开异常: {e}，但继续执行", "WARN")
                            
                            # 等待3秒后执行1v1x2
                            try:
                                self._emit("⏱ 勇者之塔完成：3s 后开始【1v1x2】…", "SYSTEM")
                                time.sleep(3.0)
                                if not self._should_abort():
                                    ok_1v1 = self.run_1v1_x2(use_foreground=use_foreground)
                                    ok_all = ok_all and ok_1v1
                            except Exception as e:
                                self._emit(f"💥 1v1x2异常: {e}", "ERROR")
                                ok_all = False
                            
                            # 等待3秒后执行大乱斗x2
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
                        self._emit(f"💥 勇者之塔循环异常: {e}", "ERROR")
                        ok_all = False

        return ok_all

    # ----------------------------
    # 勇者之塔：循环对战 + 胜利清理
    # ----------------------------
    def run_hero_tower(self, times: int = 10, background_mode: bool = True, use_unified_framework: bool = False) -> bool:
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
            self._unified_framework = UnifiedBattleFramework(self.bot, regions, template_root)
        
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
        
        map_signal = f"/resource/map/{map_id}.swf"
        newnpc_signal = "/resource/newNpc/multi/0.swf"
        
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
                        if map_signal in line_str:
                            map_seen = True
                            self._emit(f"🗺 检测到map信号：{map_signal}", "INFO")
                        if newnpc_signal in line_str:
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
        
        map_signal = "/resource/map/"
        newnpc_signal = "/resource/newNpc/multi/0.swf"
        
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
                        if map_signal in line_str:
                            map_seen = True
                            self._emit("🗺 检测到map信号", "INFO")
                        if newnpc_signal in line_str:
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
        
        token = "/resource/item/petItem/icon/"
        cursor = kernel_cursor()
        
        while True:
            if self._should_abort():
                return False
            
            try:
                lines = fetch_kernel_since(cursor)
                if isinstance(lines, list):
                    for line in lines:
                        if token in str(line):
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
        
        token = "/resource/item/petItem/icon/"
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
                            if token in str(line):
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
        map_signal = "/resource/map/"
        newnpc_signal = "/resource/newNpc/multi/0.swf"
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
                        if map_signal in line_str:
                            map_seen = True
                            self._emit("🗺 检测到map信号", "INFO")
                        if newnpc_signal in line_str:
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
        import numpy as np
        
        # 使用cleaner的detect_victory_probe_yellow方法检测黄色
        # 同时检测白色（000000）
        # ✅ 修复：使用和训练室相同的region "对战.胜利探针"，而不是"对话框.对战胜利确认"
        key_victory = "对战.胜利探针"  # 与训练室保持一致
        
        try:
            start_time = time.time()
            while (time.time() - start_time) < timeout_s:
                if self._should_abort():
                    return False
                
                try:
                    # 检测黄色（使用cleaner的方法，它使用的是"对战.胜利探针"）
                    got_yellow, score, rgb = cleaner.detect_victory_probe_yellow(
                        use_foreground=use_foreground,
                        tol=10,
                        ratio_th=0.75
                    )
                    
                    if got_yellow:
                        self._emit(f"✅ 检测到胜利黄色探针 (score={score:.3f}, rgb={rgb})", "SUCCESS")
                        return True
                    
                    # 检测白色（FFFFFF）- 使用和训练室相同的region
                    img = cleaner._grab_region_img(key_victory)
                    if img is None:
                        time.sleep(0.08)
                        continue
                    
                    arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
                    h, w = arr.shape[:2]
                    cy, cx = h // 2, w // 2
                    
                    # 检查中心附近3x3区域
                    y1_patch = max(cy - 1, 0)
                    y2_patch = min(cy + 2, h)
                    x1_patch = max(cx - 1, 0)
                    x2_patch = min(cx + 2, w)
                    patch = arr[y1_patch:y2_patch, x1_patch:x2_patch, :]
                    
                    # 检测白色（FFFFFF，RGB值都接近255）
                    white_mask = (
                        (patch[..., 0].astype(np.int16) >= 245) &
                        (patch[..., 1].astype(np.int16) >= 245) &
                        (patch[..., 2].astype(np.int16) >= 245)
                    )
                    
                    if white_mask.any():
                        # 计算检测到的白色像素的平均RGB值用于日志
                        white_pixels = patch[white_mask]
                        avg_rgb = white_pixels.mean(axis=0).astype(int)
                        self._emit(f"✅ 检测到胜利白色探针（FFFFFF，rgb={tuple(avg_rgb)}）", "SUCCESS")
                        return True
                    
                except Exception as e:
                    self._emit(f"⚠️ 检测胜利探针异常: {e}", "WARN")
                
                time.sleep(0.08)
            
            return False
        except Exception as e:
            self._emit(f"❌ 检测胜利探针失败: {e}", "ERROR")
            return False
    
    def _wait_for_1and1_cleanup(self, use_foreground: bool, timeout_s: float = 0.0) -> bool:
        """等待并点击1AND1直到消失
        
        Args:
            use_foreground: 是否前台运行
            timeout_s: 超时时间，0.0表示不超时直到消失，>0表示超时时间（秒）
        """
        from core.unified_battle_framework import BattleConfig
        
        if self._unified_framework is None:
            return False
        
        # 创建临时config用于1AND1检测
        config = BattleConfig(
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
            self._unified_framework = UnifiedBattleFramework(self.bot, regions, template_root)
        
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
                self._emit(
                    f"✅ [步骤 {idx}] 点击: ({int(gx)}, {int(gy)}) | 延迟 {delay:.2f}s | 模式: {mode_text}",
                    "DEBUG",
                )

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
        if "x" in step and "y" in step:
            try:
                return float(step["x"]), float(step["y"])
            except Exception:
                return None, None

        if "pos" in step and isinstance(step["pos"], (list, tuple)) and len(step["pos"]) >= 2:
            try:
                return float(step["pos"][0]), float(step["pos"][1])
            except Exception:
                return None, None

        if "gx" in step and "gy" in step:
            try:
                return float(step["gx"]), float(step["gy"])
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
