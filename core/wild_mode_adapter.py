# core/wild_mode_adapter.py
"""
野外模式适配器

将野外模式（稀有精灵捕捉、螳螂模式、资源刷取）适配到统一对战框架
"""
import time
from typing import Callable, Tuple, Optional

from core.unified_battle_framework import (
    UnifiedBattleFramework,
    BattleConfig,
    BattleMode,
    LastActionType
)
from core.region_store import RegionStore


class WildModeAdapter:
    """
    野外模式适配器
    
    适配以下模式：
    - 稀有精灵捕捉
    - 螳螂模式
    - 资源刷取
    - 稀有精灵抓捕
    """
    
    def __init__(self, framework: UnifiedBattleFramework):
        self.framework = framework
        self.battle_count = 0  # 战斗计数（用于每20次恢复）
    
    def _trigger_wild_battle(self, trigger_xy: Tuple[float, float]) -> Tuple[float, float]:
        """Stage 1: 触发野外对战（使用传入的触发坐标）"""
        return trigger_xy
    
    def _action_wild_capture(
        self, 
        round_idx: int,
        invincible_first_round: bool = False,
        skill1_key: str = "对战.使用技能一"
    ) -> str:
        """
        Stage 3: 野外捕捉动作逻辑
        
        - 第1回合：技能1（或无敌胶囊）
        - 第2回合开始：切换捕捉面板 -> 胶囊（默认「超超特」3 格循环，见 unified_battle_framework）
        """
        if round_idx == 1:
            if invincible_first_round:
                return "capsule"  # 无敌胶囊
            else:
                return "skill"  # 技能1
        
        # 第2回合开始：捕捉逻辑（胶囊档位见 unified_battle_framework 6 格循环）
        return "capsule"
    
    def run_wild_battle(
        self,
        trigger_xy: Tuple[float, float],
        use_foreground: bool = False,
        invincible_first_round: bool = False,
        skill1_key: str = "对战.使用技能一",
        capture_callback: Optional[Callable[[], None]] = None,
    ) -> bool:
        """
        运行野外对战
        
        Args:
            trigger_xy: 触发坐标
            use_foreground: 是否前台运行
            invincible_first_round: 第一回合是否使用无敌胶囊
            skill1_key: 技能1的region键
            capture_callback: 捕捉后回调
        
        Returns:
            True=成功，False=失败
        """
        # 创建触发回调（返回固定坐标）
        def trigger_callback() -> Tuple[float, float]:
            return trigger_xy
        
        # 创建动作回调
        def action_callback(round_idx: int) -> str:
            return self._action_wild_capture(round_idx, invincible_first_round, skill1_key)
        
        # 创建配置
        config = BattleConfig(
            mode=BattleMode.WILD,
            use_foreground=use_foreground,
            skill_key=skill1_key,
            trigger_callback=trigger_callback,
            action_callback=action_callback,
            capture_callback=capture_callback,
        )
        
        # 运行对战
        success = self.framework.run_battle(config, is_training_room=False)
        
        if success:
            self.battle_count += 1
        
        return success
    
    def should_recover(self) -> bool:
        """检查是否需要进行恢复（每20次战斗）"""
        return self.battle_count > 0 and self.battle_count % 20 == 0
    
    def reset_battle_count(self):
        """重置战斗计数"""
        self.battle_count = 0







