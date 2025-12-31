# core/fixed_mode_adapter.py
"""
固定模式适配器（训练室、勇者之塔等）

适配统一对战框架，实现固定模式的特定逻辑：
- Stage 1: 固定触发点
- Stage 3: 固定出招（技能四或自定义）
- Stage 4: 固定胜利检测流程
"""
import time
from typing import Optional, Callable, Tuple

from core.unified_battle_framework import (
    UnifiedBattleFramework, 
    BattleConfig, 
    BattleMode
)
from core.region_store import RegionStore


class FixedModeAdapter:
    """
    固定模式适配器
    
    适用于：
    - 训练室固定次数
    - 训练室直升100
    - DailyRunner勇者之塔
    """
    
    def __init__(self, framework: UnifiedBattleFramework):
        self.framework = framework
        self.regions = framework.regions
        self.bot = framework.bot
    
    def _emit(self, text: str, level: str = "INFO"):
        """日志输出"""
        if hasattr(self.bot, "emit_and_log"):
            self.bot.emit_and_log(text, level)
        else:
            print(f"[{level}] {text}")
    
    def _trigger_training_room(self) -> Tuple[float, float]:
        """训练室Stage 1: 固定触发对战"""
        # 点击训练室对战按钮
        keys = ["训练室.点击对战", "训练室.点击对战.按钮", "训练室.对战"]
        
        clicked = False
        trigger_xy = (0.0, 0.0)
        
        for key in keys:
            try:
                r = self.regions.get(key)
                if r:
                    x1, y1, x2, y2 = r.inner_bbox()
                    trigger_xy = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
                    clicked = True
                    break
            except Exception:
                continue
        
        if not clicked:
            raise KeyError("找不到训练室对战触发区域")
        
        self._emit(f"🎯 Stage 1: 点击训练室对战 -> ({trigger_xy[0]:.0f}, {trigger_xy[1]:.0f})", "INFO")
        return trigger_xy
    
    def _trigger_hero_tower(self) -> Tuple[float, float]:
        """勇者之塔Stage 1: 固定触发对战"""
        key = "勇者之塔.点击对战"
        
        try:
            r = self.regions.get(key)
            if not r:
                raise KeyError(f"找不到区域: {key}")
            
            x1, y1, x2, y2 = r.inner_bbox()
            trigger_xy = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
            
            self._emit(f"🎯 Stage 1: 点击勇者之塔对战 -> ({trigger_xy[0]:.0f}, {trigger_xy[1]:.0f})", "INFO")
            return trigger_xy
            
        except Exception as e:
            raise KeyError(f"勇者之塔触发失败: {e}")
    
    def _action_skill_four(self, round_idx: int) -> str:
        """Stage 3: 永远执行技能四"""
        return "skill"
    
    def _action_custom_skill(self, skill_key: str):
        """Stage 3: 自定义技能"""
        def callback(round_idx: int) -> str:
            return "skill"
        
        # 修改framework的skill_key
        return callback
    
    def run_training_room(
        self,
        use_foreground: bool = False,
        skill_key: str = "对战.使用技能四",
        max_battles: int = 30,
        recover_every: int = 5,
        abort_check: Optional[Callable[[], bool]] = None,
        connect_script_callback: Optional[Callable[[], None]] = None,
    ) -> int:
        """
        运行训练室固定次数模式
        
        返回: 实际完成的对战次数
        """
        self._emit(f"🏫 训练室模式启动: max_battles={max_battles}, recover_every={recover_every}", "SYSTEM")
        
        config = BattleConfig(
            mode=BattleMode.FIXED,
            use_foreground=use_foreground,
            skill_key=skill_key,
            trigger_callback=self._trigger_training_room,
            action_callback=self._action_skill_four,
            abort_check=abort_check,
        )
        
        completed = 0
        battle_count = 0
        
        try:
            for i in range(max_battles):
                # 检查中止
                if abort_check and abort_check():
                    self._emit("⛔ 训练室模式中止", "WARN")
                    break
                
                # 每N场恢复一次（在"下一场开始前"做）
                if recover_every > 0 and battle_count > 0 and battle_count % recover_every == 0:
                    self._emit(f"🩹 第{battle_count}场后自动恢复", "INFO")
                    self._recover_training_room(use_foreground)
                
                # 执行对战
                battle_count += 1
                self._emit(f"⚔️ 训练室对战 {battle_count}/{max_battles}", "INFO")
                
                success = self.framework.run_battle(config, is_training_room=True)
                
                if success:
                    completed += 1
                else:
                    self._emit(f"⚠️ 第{battle_count}场对战失败或跳过", "WARN")
                
                # ✅ 仅当"完整打满30场"才执行连接脚本（并且是在清完弹窗之后）
                if (max_battles == 30) and (battle_count == 30) and connect_script_callback:
                    connect_script_callback()
                    self._emit("✅ 训练室单批次(30场)完成", "SUCCESS")
                    return completed
                
                # 短暂延迟（等待3.5s由统一框架内部处理，这里不需要额外等待）
                time.sleep(0.5)
        
        except Exception as e:
            self._emit(f"💥 训练室模式异常: {e}", "ERROR")
        
        self._emit(f"✅ 训练室模式完成: {completed}/{battle_count}场成功", "SUCCESS")
        return completed
    
    def run_training_room_until_level(
        self,
        target_level: int = 100,
        use_foreground: bool = False,
        skill_key: str = "对战.使用技能四",
        battles_per_batch: int = 30,
        recover_every: int = 5,
        debug_stop_level: Optional[int] = None,
        abort_check: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """
        运行训练室直升100模式
        
        返回: True=达到目标等级，False=未达到或被中止
        """
        self._emit(
            f"⬆️ 训练室直升模式启动: target_level={target_level}, "
            f"battles_per_batch={battles_per_batch}, recover_every={recover_every}",
            "SYSTEM"
        )
        
        config = BattleConfig(
            mode=BattleMode.FIXED,
            use_foreground=use_foreground,
            skill_key=skill_key,
            trigger_callback=self._trigger_training_room,
            action_callback=self._action_skill_four,
            abort_check=abort_check,
        )
        
        battle_count = 0
        current_batch = 0
        
        try:
            while True:
                # 检查中止
                if abort_check and abort_check():
                    self._emit("⛔ 训练室直升模式中止", "WARN")
                    return False
                
                # 每批次前恢复
                if recover_every > 0 and battle_count > 0 and battle_count % recover_every == 0:
                    self._emit(f"🩹 第{battle_count}场后自动恢复", "INFO")
                    self._recover_training_room(use_foreground)
                
                # 执行对战
                battle_count += 1
                current_batch += 1
                self._emit(f"⚔️ 训练室对战 {battle_count} (批次{current_batch}/{battles_per_batch})", "INFO")
                
                success = self.framework.run_battle(config, is_training_room=True)
                
                if not success:
                    self._emit(f"⚠️ 第{battle_count}场对战失败或跳过", "WARN")
                
                # 检查等级（在第一回合后OCR）
                # TODO: 实现OCR等级检测
                # level = self._ocr_level_after_first_round()
                # if level and level >= target_level:
                #     self._emit(f"✅ 达到目标等级 {level} >= {target_level}", "SUCCESS")
                #     break
                # if debug_stop_level and level and level >= debug_stop_level:
                #     self._emit(f"🛑 调试停等级 {level} >= {debug_stop_level}", "WARN")
                #     break
                
                # 批次完成后执行连接脚本（完整打满30场后执行）
                if current_batch >= battles_per_batch:
                    current_batch = 0
                    self._emit(f"✅ 完成一个批次 ({battles_per_batch}场)", "INFO")
                    # 执行连接脚本
                    if connect_script_callback:
                        connect_script_callback()
                
                time.sleep(0.5)
        
        except Exception as e:
            self._emit(f"💥 训练室直升模式异常: {e}", "ERROR")
            return False
        
        return False  # 暂时返回False，等OCR实现后修正
    
    def run_hero_tower(
        self,
        times: int = 10,
        use_foreground: bool = False,
        skill_key: str = "对战.使用技能四",
        abort_check: Optional[Callable[[], bool]] = None,
    ) -> int:
        """
        运行勇者之塔模式
        
        返回: 实际完成的对战次数
        """
        self._emit(f"🗼 勇者之塔模式启动: times={times}", "SYSTEM")
        
        config = BattleConfig(
            mode=BattleMode.FIXED,
            use_foreground=use_foreground,
            skill_key=skill_key,
            trigger_callback=self._trigger_hero_tower,
            action_callback=self._action_skill_four,
            abort_check=abort_check,
        )
        
        completed = 0
        
        try:
            for i in range(times):
                # 检查中止
                if abort_check and abort_check():
                    self._emit("⛔ 勇者之塔模式中止", "WARN")
                    break
                
                # 执行对战（勇者之塔模式，使用is_hero_tower=True）
                self._emit(f"⚔️ 勇者之塔对战 {i+1}/{times}", "INFO")
                success = self.framework.run_battle(config, is_training_room=True, is_hero_tower=True)
                
                if success:
                    completed += 1
                else:
                    self._emit(f"⚠️ 第{i+1}场对战失败或跳过", "WARN")
                
                time.sleep(0.5)
            
            # 执行十次后点击离开区域
            if completed > 0:
                self._emit("🚪 勇者之塔完成，点击离开", "INFO")
                try:
                    leave_key = "勇者之塔.离开"
                    r = self.regions.get(leave_key)
                    if r:
                        x1, y1, x2, y2 = r.inner_bbox()
                        leave_xy = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
                        if use_foreground:
                            from core.utils import window_manager
                            window_manager.click(leave_xy[0], leave_xy[1])
                        else:
                            from core.utils import window_manager
                            window_manager.click_background(leave_xy[0], leave_xy[1])
                        self._emit(f"✅ 已点击离开区域: ({leave_xy[0]:.0f}, {leave_xy[1]:.0f})", "SUCCESS")
                        time.sleep(0.5)
                    else:
                        self._emit(f"⚠️ 找不到离开区域: {leave_key}", "WARN")
                except Exception as e:
                    self._emit(f"❌ 点击离开区域失败: {e}", "ERROR")
        
        except Exception as e:
            self._emit(f"💥 勇者之塔模式异常: {e}", "ERROR")
        
        self._emit(f"✅ 勇者之塔模式完成: {completed}/{times}场成功", "SUCCESS")
        return completed
    
    def _recover_training_room(self, use_foreground: bool):
        """训练室恢复逻辑"""
        keys = {
            "open_bag": ["训练室.精灵背包", "精灵背包.打开精灵背包"],
            "recover": ["训练室.精灵恢复"],
            "confirm": ["对话框.普通确认"],
            "close_bag": ["精灵背包.关闭精灵背包", "训练室.关闭精灵背包"],
        }
        
        # 查找存在的key
        actual_keys = {}
        for k, candidates in keys.items():
            for cand in candidates:
                if self.regions.get(cand):
                    actual_keys[k] = cand
                    break
        
        if not all(actual_keys.values()):
            self._emit("⚠️ 训练室恢复：缺少必要region", "WARN")
            return
        
        try:
            # 打开背包
            r = self.regions.get(actual_keys["open_bag"])
            x1, y1, x2, y2 = r.inner_bbox()
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            if use_foreground:
                from core.utils import window_manager
                window_manager.click(cx, cy)
            else:
                from core.utils import window_manager
                window_manager.click_background(cx, cy)
            time.sleep(1.5)
            
            # 恢复
            r = self.regions.get(actual_keys["recover"])
            x1, y1, x2, y2 = r.inner_bbox()
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            if use_foreground:
                window_manager.click(cx, cy)
            else:
                window_manager.click_background(cx, cy)
            time.sleep(0.5)
            
            # 确认
            r = self.regions.get(actual_keys["confirm"])
            x1, y1, x2, y2 = r.inner_bbox()
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            if use_foreground:
                window_manager.click(cx, cy)
            else:
                window_manager.click_background(cx, cy)
            time.sleep(0.2)
            
            # 关闭背包
            r = self.regions.get(actual_keys["close_bag"])
            x1, y1, x2, y2 = r.inner_bbox()
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            if use_foreground:
                window_manager.click(cx, cy)
            else:
                window_manager.click_background(cx, cy)
            time.sleep(0.2)
            
            self._emit("✅ 训练室恢复完成", "SUCCESS")
        
        except Exception as e:
            self._emit(f"❌ 训练室恢复失败: {e}", "ERROR")


