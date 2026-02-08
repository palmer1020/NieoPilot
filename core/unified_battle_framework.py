# core/unified_battle_framework.py
"""
统一对战流程框架

所有对战模式（固定、野外、玩家对战）都遵循以下4个Stage：
- Stage 1: 触发对战
- Stage 2: PetItem检测 + 校准（大探针小探针 + X1-X4扫描）
- Stage 3: 战斗循环（回合检测 + 出招）
- Stage 4: 战斗结束处理（胜利/捕捉/逃跑）
"""
import os
import re
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, Tuple, List, Dict, Any, Callable, Set
from dataclasses import dataclass
from enum import Enum
from collections import deque

import numpy as np
from PIL import Image

from core.logger import fetch_kernel_since, kernel_cursor, wait_kernel_contains
from core.logger import add_kernel_log_callback, remove_kernel_log_callback
from core.region_store import Region, RegionStore
from core.utils import window_manager


class BattleMode(Enum):
    """对战模式枚举"""
    FIXED = "fixed"        # 固定模式（训练室等）
    WILD = "wild"          # 野外模式
    PVP = "pvp"            # 玩家对战


class LastActionType(Enum):
    """上一回合动作类型"""
    SKILL = "skill"        # 技能
    CAPSULE = "capsule"    # 胶囊
    ESCAPE = "escape"      # 逃跑


@dataclass
class CalibrationResult:
    """校准结果"""
    success: bool
    clicked_group: Optional[int] = None  # 1-4
    x_values: Optional[List[int]] = None  # [X1, X2, X3, X4]
    distribution: Optional[str] = None  # "310", "301", etc.


@dataclass
class BattleConfig:
    """对战配置"""
    mode: BattleMode
    use_foreground: bool = False
    skill_key: Optional[str] = None
    trigger_callback: Optional[Callable[[], Tuple[float, float]]] = None
    action_callback: Optional[Callable[[int], str]] = None  # round_idx -> "skill"/"capsule"/"escape"
    abort_check: Optional[Callable[[], bool]] = None
    capture_callback: Optional[Callable[[], None]] = None  # 捕捉后回调
    escape_callback: Optional[Callable[[], None]] = None  # 逃跑后回调
    on_petitem_detected: Optional[Callable[[], None]] = None  # PetItem检测回调（检测到PetItem时立即调用）
    invincible_first_round: bool = False  # 第一回合是否使用无敌胶囊（仅野外模式）
    test_mode_capsule_only_mid: bool = False  # 测试模式：后续回合只使用中级胶囊（不交替高级）
    test_mode: bool = False  # 是否为测试模式


class UnifiedBattleFramework:
    """
    统一对战流程框架
    """
    
    # Region keys
    KEY_BIG_PROBE = "游戏.大探针"
    KEY_SMALL_PROBE = "游戏.小探针"
    KEY_GAME_CELLS = ["游戏.1a", "游戏.1b", "游戏.2a", "游戏.2b", 
                      "游戏.3a", "游戏.3b", "游戏.4a", "游戏.4b"]
    
    # 探针颜色（RGB）
    COLOR_BIG_PROBE = (47, 167, 238)  # 2FA7EE
    COLOR_SMALL_PROBE = (255, 255, 255)  # FFFFFF
    COLOR_ORANGE_CELL = (254, 103, 0)  # FE6700
    
    # 内核日志token
    TOKEN_PETITEM = "/resource/item/petItem/icon/"
    TOKEN_MAP = "/resource/map/"
    TOKEN_NEWNPC = "/resource/newNpc/multi/0.swf"
    TOKEN_FIGHT_SKILL = "/resource/fightResource/skill/swf/"  # 点击成功且未出现校准的信号
    TOKEN_FIGHT_PET = "/resource/fightResource/pet/swf/"  # pet swf信号（用于记录时间）
    
    # Stage 4 region keys
    KEY_VICTORY_PROBE = "对话框.对战胜利确认"  # 黄色探针
    KEY_VICTORY_CONFIRM = "对话框.对战胜利确认按钮"
    KEY_UPGRADE_CONFIRM = "对话框.升级确认"
    KEY_UPGRADE_CONFIRM_BTN = "对话框.升级确认按钮"
    KEY_SKILL_REPLACE_CANCEL = "对话框.技能替换取消"
    KEY_SKILL_REPLACE_CANCEL_BTN = "对话框.技能替换取消按钮"
    KEY_GENERAL_PROBE = "对话框.通用探针"  # 白色
    
    # 地图10白色探针（用于替代newNPC信号）
    KEY_WHITE_PROBE_MAP10 = "尼奥一.白色探针"
    KEY_NORMAL_CONFIRM_PROBE = "对话框.普通确认探针"  # 蓝色
    KEY_NORMAL_CONFIRM_BTN = "对话框.普通确认按钮"
    
    # 邮件配置
    EMAIL_ADDRESS = "1713518932qqcom@gmail.com"
    
    # 电池区域
    KEY_BATTERY = "系统.电池"
    COLOR_BATTERY_RED = (255, 0, 0)  # #FF0000
    COLOR_BATTERY_BLACK = (0, 0, 0)  # #000000
    
    def __init__(self, bot, regions: RegionStore, template_root: str):
        self.bot = bot
        self.regions = regions
        self.template_root = template_root
        
        self._kernel_q = deque(maxlen=6000)
        self._kernel_cb = None
        self._last_action: Optional[LastActionType] = None
        self._round_idx = 0
        
        # 日志节流：记录上次输出时间，避免高频日志刷屏
        self._log_throttle: Dict[str, float] = {}
        
        # ✅ petswf到PetItem的时间差记录（用于统计趋势）
        self._petswf_to_petitem_durations: List[float] = []
        
    # ================================
    # 工具方法
    # ================================
    
    def _emit(self, text: str, level: str = "INFO", throttle_key: Optional[str] = None, throttle_interval: float = 0.0):
        """
        日志输出，支持节流
        
        Args:
            text: 日志文本
            level: 日志级别
            throttle_key: 节流键（相同键的日志在throttle_interval秒内只输出一次）
            throttle_interval: 节流间隔（秒），0表示不节流
        """
        # 节流检查
        if throttle_key and throttle_interval > 0:
            now = time.time()
            last_time = self._log_throttle.get(throttle_key, 0)
            if now - last_time < throttle_interval:
                return  # 跳过本次日志
            self._log_throttle[throttle_key] = now
        
        if hasattr(self.bot, "emit_and_log"):
            try:
                self.bot.emit_and_log(text, level)
                return
            except Exception:
                pass
        print(f"[{level}] {text}")
    
    def _require_region(self, key: str) -> Region:
        """获取region，不存在则抛出异常"""
        r = self.regions.get(key)
        if not r:
            raise KeyError(f"Region not found: {key}")
        return r
    
    def _grab_region(self, key: str) -> Optional[Image.Image]:
        """截取region图像"""
        try:
            r = self._require_region(key)
            x1, y1, x2, y2 = r.outer_bbox()
            return window_manager.grab_game_bbox(x1, y1, x2, y2, min_size_px=2)
        except Exception:
            return None
    
    def _region_center(self, key: str) -> Tuple[float, float]:
        """获取region中心点坐标"""
        r = self._require_region(key)
        x1, y1, x2, y2 = r.outer_bbox()
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
    
    def _click_xy(self, x: float, y: float, use_foreground: bool):
        """点击坐标"""
        if use_foreground:
            window_manager.click(x, y)
        else:
            window_manager.click_background(x, y)
    
    def _click_region(self, key: str, use_foreground: bool):
        """点击region"""
        r = self._require_region(key)
        cx, cy = r.sample_click_point()
        self._click_xy(cx, cy, use_foreground)
        self._emit(f"🖱 点击 {key} -> ({cx:.0f},{cy:.0f})", "DEBUG", throttle_key=f"click_{key}", throttle_interval=1.0)
    
    def _click_region_twice(self, key: str, use_foreground: bool, gap: float = 0.06):
        """连续点击region两次（提升胶囊点击成功率）"""
        self._click_region(key, use_foreground)
        time.sleep(max(0.0, gap))
        self._click_region(key, use_foreground)
    
    def _rs_get(self, key: str) -> Optional[Region]:
        """获取region（不存在返回None）"""
        return self.regions.get(key)
    
    def _first_existing_key(self, candidates: List[str]) -> Optional[str]:
        """获取第一个存在的region键"""
        for k in candidates:
            try:
                if self._rs_get(k):
                    return k
            except Exception:
                continue
        return None
    
    def _check_color_strict(self, key: str, target_rgb: Tuple[int, int, int], tolerance: int = 0) -> bool:
        """检查region平均RGB是否严格等于目标颜色"""
        rgb = self._mean_rgb(key)
        if rgb is None:
            return False
        
        mr, mg, mb = rgb
        tr, tg, tb = target_rgb
        
        if tolerance == 0:
            return (mr, mg, mb) == (tr, tg, tb)
        else:
            # 允许tolerance误差
            return (abs(mr - tr) <= tolerance) and (abs(mg - tg) <= tolerance) and (abs(mb - tb) <= tolerance)
    
    def _mean_rgb(self, key: str) -> Optional[Tuple[int, int, int]]:
        """获取region平均RGB"""
        img = self._grab_region(key)
        if img is None:
            return None
        
        arr = np.asarray(img.convert("RGB"), dtype=np.float32)
        if arr.size == 0:
            return None
        
        mean = np.round(arr.mean(axis=(0, 1))).astype(int)
        return (int(mean[0]), int(mean[1]), int(mean[2]))
    
    def _count_orange_in_pair(self, key_a: str, key_b: str) -> int:
        """统计一对区域（a和b）中颜色严格为FE6700的数量，返回0/1/2"""
        count = 0
        
        # 检查a区域
        rgb_a = self._mean_rgb(key_a)
        if rgb_a == self.COLOR_ORANGE_CELL:
            count += 1
        
        # 检查b区域
        rgb_b = self._mean_rgb(key_b)
        if rgb_b == self.COLOR_ORANGE_CELL:
            count += 1
        
        return count
    
    def _check_white_probe_non_white(self) -> bool:
        """
        检查白色探针是否为非纯白色（表示newNPC已出现，可结束战斗）
        
        注意：此方法仅在地图10时有效（通过检查内核日志中的map信号来判断）
        如果不是地图10，直接返回False，不影响正常流程
        
        Returns:
            True=非纯白色（newNPC已出现），False=纯白色、探针不存在或不在地图10（newNPC未出现）
        """
        try:
            # ✅ 首先检查内核日志中最近的地图信号（只针对地图10的补丁）
            # 从队列末尾（最新）向队列开头（最旧）遍历，找到最近的一个地图信号
            # 如果最近的地图信号是地图11，说明不是地图10，直接返回False
            # 如果最近的地图信号是地图10，才继续检查白色探针
            recent_map = None
            # 反向遍历队列（从最新到最旧）
            for line in reversed(list(self._kernel_q)):
                line_str = str(line)
                if "/resource/map/10.swf" in line_str:
                    recent_map = 10
                    break
                elif "/resource/map/11.swf" in line_str:
                    recent_map = 11
                    break
            
            # 如果最近的地图信号是地图11，说明不是地图10，返回False
            if recent_map == 11:
                return False
            
            # 如果最近的地图信号不是地图10（包括没有找到任何地图信号），也返回False
            if recent_map != 10:
                return False
            
            # ✅ 只有在确认是地图10后，才检查白色探针
            # 尝试多个可能的白色探针键（闪光皮皮优先，然后是尼奥一）
            white_probe_key = None
            if self._rs_get("闪光皮皮.白色探针"):
                white_probe_key = "闪光皮皮.白色探针"
            elif self._rs_get(self.KEY_WHITE_PROBE_MAP10):
                white_probe_key = self.KEY_WHITE_PROBE_MAP10
            
            if not white_probe_key:
                # 白色探针不存在，返回False
                return False
            
            reg = self._rs_get(white_probe_key)
            if not reg:
                return False
            
            img = self._grab_region(white_probe_key)
            if img is None:
                return False
            
            pixels = list(img.getdata())
            if not pixels:
                return False
            
            # 检查是否所有像素都是纯白色（255,255,255），允许小的误差（tolerance=5）
            tolerance = 5
            white_count = 0
            for r, g, b in pixels:
                if abs(r - 255) <= tolerance and abs(g - 255) <= tolerance and abs(b - 255) <= tolerance:
                    white_count += 1
            
            # 如果超过80%的像素是纯白色，认为是纯白色（newNPC未出现）
            white_ratio = white_count / len(pixels)
            is_white = white_ratio >= 0.8
            
            # 返回非纯白色（如果is_white为False，表示非纯白色，newNPC已出现）
            return not is_white
        except Exception:
            # 任何异常都返回False，不影响正常流程
            return False
    
    def _send_email(self, subject: str, body: str):
        """发送邮件（异常情况）"""
        try:
            # 使用Gmail SMTP
            msg = MIMEMultipart()
            msg['From'] = self.EMAIL_ADDRESS
            msg['To'] = self.EMAIL_ADDRESS
            msg['Subject'] = subject
            
            msg.attach(MIMEText(body, 'plain', 'utf-8'))
            
            # 这里需要配置SMTP服务器，暂时只记录日志
            # 实际发送需要配置SMTP认证信息
            self._emit(f"📧 [邮件] {subject}\n{body}", "WARN")
            # TODO: 实现实际邮件发送（需要SMTP配置）
            
        except Exception as e:
            self._emit(f"❌ 邮件发送失败: {e}", "ERROR")
    
    # ================================
    # 内核日志监听
    # ================================
    
    def _start_kernel_listen(self):
        """启动内核日志监听"""
        self._kernel_q.clear()
        
        def _on_line(line: str):
            self._kernel_q.append(line)
        
        self._kernel_cb = _on_line
        add_kernel_log_callback(self._kernel_cb)
    
    def _stop_kernel_listen(self):
        """停止内核日志监听"""
        if self._kernel_cb:
            remove_kernel_log_callback(self._kernel_cb)
            self._kernel_cb = None
    
    def _has_petitem(self, line: str) -> bool:
        """检查是否包含PetItem信号"""
        return self.TOKEN_PETITEM in line
    
    def _has_map(self, line: str) -> bool:
        """检查是否包含map信号"""
        return self.TOKEN_MAP in line
    
    def _has_newnpc(self, line: str) -> bool:
        """检查是否包含newNpc信号（/resource/newNpc/multi/0.swf）"""
        return self.TOKEN_NEWNPC in line
    
    def _has_fight_skill(self, line: str) -> bool:
        """检查是否包含fightResource/skill/swf信号（点击成功且未出现校准）"""
        return self.TOKEN_FIGHT_SKILL in line
    
    def _has_fight_pet(self, line: str) -> bool:
        """检查是否包含fightResource/pet/swf信号"""
        return self.TOKEN_FIGHT_PET in line
    
    def _check_battle_end(self) -> Tuple[bool, bool]:
        """
        检查战斗是否结束（从日志检测Map + multi newnpc）
        
        返回: (map_seen, npc_seen)
        """
        map_seen = False
        npc_seen = False
        
        # ✅ 从内核队列检查（实时检测，不消费队列）
        temp_q = list(self._kernel_q)  # 使用list复制，不消费队列
        for line in temp_q:
            line_str = str(line)
            if self._has_map(line_str):
                map_seen = True
            if self._has_newnpc(line_str):
                npc_seen = True
        
        return map_seen, npc_seen
    
    # ================================
    # Stage 2: 校准逻辑
    # ================================
    
    def _check_calibration_probes(self) -> bool:
        """检查是否出现校准探针（大探针=2FA7EE AND 小探针=FFFFFF）"""
        big_ok = self._check_color_strict(self.KEY_BIG_PROBE, self.COLOR_BIG_PROBE, tolerance=5)
        small_ok = self._check_color_strict(self.KEY_SMALL_PROBE, self.COLOR_SMALL_PROBE, tolerance=5)
        return big_ok and small_ok
    
    def _calculate_x_values(self) -> Tuple[List[int], Dict[str, Region]]:
        """计算X1-X4值，返回(values, regions_dict)"""
        values = []
        regions_dict = {}
        
        for i in range(1, 5):
            key_a = f"游戏.{i}a"
            key_b = f"游戏.{i}b"
            
            try:
                count = self._count_orange_in_pair(key_a, key_b)
                values.append(count)
                regions_dict[f"{i}a"] = self._require_region(key_a)
                regions_dict[f"{i}b"] = self._require_region(key_b)
            except KeyError:
                values.append(0)
                regions_dict[f"{i}a"] = None
                regions_dict[f"{i}b"] = None
        
        return values, regions_dict
    
    def _analyze_distribution(self, x_values: List[int]) -> Tuple[str, Optional[int]]:
        """
        分析X值分布，返回(distribution, target_index)
        
        正常分布格式: "310", "301", "031", "130", "103", "013"
        这三个数字分别表示：count_1(值为1的个数), count_0(值为0的个数), count_2(值为2的个数)
        
        正常分布：count_1, count_0, count_2 这三个计数中，必定有一个是1
        - 如果count_2=1，说明有1个值为2的X，应该点击那个值为2的X
        - 如果count_1=1，说明有1个值为1的X，应该点击那个值为1的X
        - 如果count_0=1，说明有1个值为0的X，应该点击那个值为0的X（但这种情况不应该出现，因为正常分布应该点击值为1或2的）
        """
        # 统计每个值的出现次数
        count_0 = x_values.count(0)
        count_1 = x_values.count(1)
        count_2 = x_values.count(2)
        
        # 构建分布字符串（格式：count_1 + count_0 + count_2）
        simple_dist = f"{count_1}{count_0}{count_2}"
        
        # 正常分布：count_1, count_0, count_2 这三个计数中，必定有一个是1
        # 必须是以下6种分布才是正常pattern
        normal_patterns = ["013", "031", "130", "103", "301", "310"]
        
        # 验证：count_1 + count_0 + count_2 应该等于 4
        if (count_1 + count_0 + count_2) != 4:
            return simple_dist, None
        
        # 检查是否为正常分布（三个计数中有一个为1）
        if simple_dist in normal_patterns:
            # 根据用户描述："取对应为1的那个值对应的X变量"
            # 优先查找值为1的X（如果存在）
            if count_1 >= 1:
                # 找到第一个值为1的X
                for i, val in enumerate(x_values):
                    if val == 1:
                        return simple_dist, i + 1  # 1-4
            
            # 如果没有值为1的X，查找值为2的X（例如103分布）
            if count_2 == 1:
                for i, val in enumerate(x_values):
                    if val == 2:
                        return simple_dist, i + 1  # 1-4
            
            # 如果既没有值为1也没有值为2的X，查找值为0的X（理论上不应该出现）
            if count_0 == 1:
                for i, val in enumerate(x_values):
                    if val == 0:
                        return simple_dist, i + 1  # 1-4
            
            # 理论上不应该到这里
            return simple_dist, None
        
        # 异常分布
        return simple_dist, None
    
    def _calibrate_click_group(self, group_idx: int, use_foreground: bool) -> bool:
        """点击指定组（1-4）的a和b区域各自中心的中点"""
        try:
            key_a = f"游戏.{group_idx}a"
            key_b = f"游戏.{group_idx}b"
            
            # 获取中心点
            cx_a, cy_a = self._region_center(key_a)
            cx_b, cy_b = self._region_center(key_b)
            
            # 计算中点
            mid_x = (cx_a + cx_b) / 2.0
            mid_y = (cy_a + cy_b) / 2.0
            
            # 快速点击
            self._click_xy(mid_x, mid_y, use_foreground)
            # 校准点击日志不节流（重要操作，需要看到每次点击）
            self._emit(f"🧭 校准点击：组{group_idx} -> ({mid_x:.0f},{mid_y:.0f})", "DEBUG")
            return True
            
        except Exception as e:
            self._emit(f"❌ 校准点击失败: {e}", "ERROR")
            return False
    
    def stage2_calibration_and_petitem(
        self, 
        trigger_callback: Optional[Callable[[], Tuple[float, float]]] = None,
        use_foreground: bool = False,
        timeout_s: float = 10.0,
        skip_stage1: bool = False,  # 是否跳过Stage 1（野外模式已在外部完成Stage 1）
        config: Optional[BattleConfig] = None,  # 用于在检测到PetItem时立即执行第一回合动作
        initial_cursor: Optional[int] = None  # 当skip_stage1=True时，用于检查此cursor之后的新日志
    ) -> Tuple[bool, Optional[CalibrationResult]]:
        """
        Stage 2: 检测PetItem + 校准逻辑
        
        流程：
        1. 如果skip_stage1=False，获取触发坐标并持续点击触发点
        2. 如果skip_stage1=True（野外模式），直接等待检测信号（不持续点击）
        3. 检测以下信号：
           - fightResource/skill/swf（点击成功且未出现校准，停止点击但继续等待PetItem）
           - PetItem（进入Stage 3）
           - 校准探针（进入校准流程）
        4. 如果出现校准探针（大探针=2FA7EE AND 小探针=FFFFFF），进入校准：
           - 计算X1-X4，分析分布
           - 如果是正常分布（310/301等），点击对应组
           - 点击后继续检测：
             * 如果仍然1 AND 1或异常分布，发邮件并暂停
             * 如果不是1 AND 1，重新获取触发坐标并继续持续点击（仅当skip_stage1=False时）
        5. 如果检测到fightResource/skill/swf，停止点击，继续等待PetItem
        6. 如果检测到PetItem，进入Stage 3
        
        返回: (success, calibration_result)
        """
        t0 = time.time()
        calibration_attempts = 0
        max_calibration_attempts = 5
        
        # ✅ 补丁：在启动内核监听之前，先检查initial_cursor之前的日志中是否有fightResource/pet/swf/信号
        # 这样可以确保在野外稀有精灵模式和尼奥模式中都能正确记录时间
        # 注意：只在skip_stage1=True且提供了initial_cursor时执行，不影响其他场景
        # 时间测量是只读的，不影响任何OCR、精灵识别、入战等逻辑
        petswf_detected_time: Optional[float] = None
        if skip_stage1 and initial_cursor is not None:
            try:
                from core.logger import fetch_kernel_since
                # 检查initial_cursor之前最多200条日志（确保能检测到fightResource/pet/swf/信号）
                check_cursor = max(0, initial_cursor - 200)
                lines = fetch_kernel_since(check_cursor)
                if isinstance(lines, list):
                    # 从最新到最旧查找fightResource/pet/swf/信号（找到第一个就停止）
                    for line in reversed(lines):
                        line_str = str(line)
                        if self._has_fight_pet(line_str):
                            # 找到信号，使用当前时间作为近似值
                            # 注意：由于无法获取精确的检测时间，这里使用当前时间作为近似值
                            # 实际的时间差可能会稍微偏大，但足以用于时间测量和趋势分析
                            # ✅ 时间测量是只读的，不影响任何其他逻辑
                            petswf_detected_time = time.time()
                            self._emit(f"📝 [时间统计] 在Stage 2启动前检测到fightResource/pet/swf/信号（已记录时间，cursor范围: {check_cursor}->{initial_cursor}）", "INFO")
                            break
            except Exception as e:
                # 如果检查失败，不影响后续流程（petswf_detected_time保持为None，后续会继续检测）
                # ✅ 时间测量失败不影响任何其他逻辑
                pass
        
        # 启动内核监听（尽早开始监听，以便及时检测PetItem）
        self._start_kernel_listen()
        
        # 如果提供了initial_cursor，用于检查新日志（skip_stage1模式）
        check_cursor = initial_cursor
        if check_cursor is None and skip_stage1:
            # 如果没有提供cursor，则记录当前cursor（确保不丢失日志）
            check_cursor = kernel_cursor()
        
        try:
            if skip_stage1:
                self._emit("🔍 Stage 2: 等待PetItem或校准（野外模式，Stage 1已在外部完成）", "INFO")
            else:
                self._emit("🔍 Stage 2: 持续点击触发点直到检测到PetItem或校准", "INFO")
                # 首先获取触发坐标
                if not trigger_callback:
                    self._emit("❌ Stage 2: 未提供trigger_callback且skip_stage1=False", "ERROR")
                    return False, None
                trigger_xy = trigger_callback()
            
            last_click_time = 0.0
            click_interval = 0.1  # 每0.1秒点击一次
            saw_fight_skill = False  # 标志：是否已检测到fightResource/skill/swf（停止点击）
            # ✅ petswf_detected_time 已在上面初始化（如果提前检测到，已设置；否则为None）
            # 注意：时间测量是只读的，不影响任何OCR、精灵识别、入战等逻辑
            skill_detected_time: Optional[float] = None  # skill出现的时间（用于右下角检测判断）
            round_probe_was_gray = False  # 回合探针是否曾经是灰色（用于检测先灰后蓝）
            round_probe_model = None  # 回合探针模板模型（延迟加载）
            
            while (time.time() - t0) < timeout_s:
                # 检查中止
                if self.bot and hasattr(self.bot, "stop_current") and self.bot.stop_current:
                    return False, None
                
                # 检查暂停
                if self.bot and hasattr(self.bot, "is_paused"):
                    while self.bot.is_paused and not (hasattr(self.bot, "stop_current") and self.bot.stop_current):
                        time.sleep(0.05)
                
                # ✅ 0. 检测fightResource/pet/swf/信号（记录第一次出现的时间，必须在PetItem检测之前）
                if petswf_detected_time is None:
                    # 检查内核队列
                    if len(self._kernel_q) > 0:
                        temp_q = list(self._kernel_q)
                        for line in temp_q:
                            line_str = str(line)
                            if self._has_fight_pet(line_str):
                                petswf_detected_time = time.time()
                                self._emit(f"📝 [时间统计] 检测到fightResource/pet/swf/信号（开始计时）", "INFO")
                                break
                    
                    # 如果skip_stage1且提供了cursor，也检查fetch_kernel_since
                    if petswf_detected_time is None and skip_stage1 and check_cursor is not None:
                        try:
                            lines = fetch_kernel_since(check_cursor)
                            if isinstance(lines, list):
                                for line in lines:
                                    line_str = str(line)
                                    if self._has_fight_pet(line_str):
                                        petswf_detected_time = time.time()
                                        self._emit(f"📝 [时间统计] 检测到fightResource/pet/swf/信号（开始计时）", "INFO")
                                        break
                        except Exception:
                            pass
                
                # 优先检查PetItem（最重要的信号，优先检测）- 检测到PetItem立即执行第一回合动作
                # 1. 先检查内核队列（实时日志）
                petitem_found = False
                if len(self._kernel_q) > 0:
                    temp_q = list(self._kernel_q)  # 使用list复制，不消费队列
                    for line in temp_q:
                        line_str = str(line)
                        if self._has_petitem(line_str):
                            petitem_found = True
                            break
                
                # 2. 如果skip_stage1且提供了cursor，也检查fetch_kernel_since（避免遗漏日志）
                if not petitem_found and skip_stage1 and check_cursor is not None:
                    try:
                        lines = fetch_kernel_since(check_cursor)
                        if isinstance(lines, list):
                            for line in lines:
                                line_str = str(line)
                                if self._has_petitem(line_str):
                                    petitem_found = True
                                    break
                    except Exception:
                        pass
                
                # ✅ 3. 检查右下角探针（先灰后蓝）作为PetItem出现的条件
                if not petitem_found and (skill_detected_time is not None or petswf_detected_time is not None):
                    # 延迟加载回合探针模型
                    if round_probe_model is None:
                        round_probe_model = self._load_probe_templates()
                    
                    if round_probe_model:
                        probe_state, blue_score, gray_score = self._detect_round_probe(round_probe_model)
                        
                        # 如果探针曾经是灰色，现在变成蓝色，说明PetItem出现
                        if probe_state == "BLUE" and round_probe_was_gray:
                            petitem_found = True
                            self._emit("✅ 检测到右下角探针先灰后蓝，判定PetItem出现", "SUCCESS")
                        elif probe_state == "GRAY":
                            round_probe_was_gray = True  # 标记探针曾经是灰色
                
                if petitem_found:
                    self._emit("✅ 检测到PetItem信号，立即执行第一回合动作", "SUCCESS")
                    
                    # ✅ 记录petswf到PetItem的时间差
                    if petswf_detected_time is not None:
                        petitem_time = time.time()
                        duration = petitem_time - petswf_detected_time
                        self._petswf_to_petitem_durations.append(duration)
                        self._emit(f"📊 [时间统计] petswf到PetItem时间差: {duration:.3f}秒", "INFO")
                        
                        # ✅ 每5次输出一次统计趋势
                        if len(self._petswf_to_petitem_durations) % 5 == 0:
                            recent_5 = self._petswf_to_petitem_durations[-5:]
                            avg_duration = sum(recent_5) / len(recent_5)
                            min_duration = min(recent_5)
                            max_duration = max(recent_5)
                            total_count = len(self._petswf_to_petitem_durations)
                            
                            # 计算趋势（如果总数>=10，对比前5个和后5个）
                            trend_msg = ""
                            if total_count >= 10:
                                prev_5 = self._petswf_to_petitem_durations[-10:-5]
                                prev_avg = sum(prev_5) / len(prev_5)
                                if avg_duration > prev_avg * 1.1:
                                    trend_msg = f" ⚠️ 趋势：增加（前5次平均{prev_avg:.3f}s -> 当前{avg_duration:.3f}s）"
                                elif avg_duration < prev_avg * 0.9:
                                    trend_msg = f" ✅ 趋势：减少（前5次平均{prev_avg:.3f}s -> 当前{avg_duration:.3f}s）"
                                else:
                                    trend_msg = f" ➡️ 趋势：稳定（前5次平均{prev_avg:.3f}s -> 当前{avg_duration:.3f}s）"
                            
                            self._emit(f"📈 [时间统计] 最近5次petswf到PetItem时间差 - 平均: {avg_duration:.3f}s, 最小: {min_duration:.3f}s, 最大: {max_duration:.3f}s, 总计: {total_count}次{trend_msg}", "INFO")
                    
                    # ✅ 先调用PetItem检测回调（如果提供）
                    if config and config.on_petitem_detected:
                        try:
                            config.on_petitem_detected()
                        except Exception as e:
                            self._emit(f"⚠️ PetItem检测回调异常: {e}", "WARN")
                    
                    # ✅ 立即执行第一回合动作（不等到Stage 3，不输出"进入Stage 3"）
                    if config:
                        round_idx = 1
                        if config.action_callback:
                            action_type = config.action_callback(round_idx)
                            self._execute_action(action_type, config, round_idx=round_idx, invincible_first_round=config.invincible_first_round)
                        elif config.skill_key:
                            self._click_region_twice(config.skill_key, config.use_foreground, gap=0.06)
                            # ✅ 不在stage2中sleep，立即返回让stage3接管（_execute_action内部已有必要的sleep）
                            self._last_action = LastActionType.SKILL
                        self._emit("✅ 第一回合动作已执行", "SUCCESS")
                    
                    return True, None
                
                # 1. 检查fightResource/skill/swf（点击成功且未出现校准，停止点击但继续等待PetItem）
                if not saw_fight_skill:
                    temp_q = list(self._kernel_q)  # 使用list复制，不消费队列
                    for line in temp_q:
                        line_str = str(line)
                        if self._has_fight_skill(line_str):
                            saw_fight_skill = True
                            skill_detected_time = time.time()  # 记录skill出现的时间（用于右下角检测判断）
                            self._emit("✅ 检测到fightResource/skill/swf信号（点击成功，未出现校准），停止点击，继续等待PetItem", "SUCCESS")
                            last_click_time = float('inf')  # 设置为无限大，停止点击
                            break
                
                # 3. 检查校准探针
                if self._check_calibration_probes():
                    calibration_attempts += 1
                    if calibration_attempts > max_calibration_attempts:
                        self._emit("❌ 校准尝试次数过多，暂停并发送邮件", "ERROR")
                        self._send_email(
                            "校准失败 - 尝试次数过多",
                            f"校准尝试了{max_calibration_attempts}次仍无法完成。"
                        )
                        if self.bot:
                            self.bot.is_paused = True
                        return False, None
                    
                    self._emit(f"🧭 检测到校准探针（大=2FA7EE AND 小=FFFFFF），第{calibration_attempts}次校准", "WARN")
                    
                    # 计算X1-X4
                    x_values, regions_dict = self._calculate_x_values()
                    # X值和分布分析日志不节流（校准过程的重要信息）
                    self._emit(f"📊 X值: X1={x_values[0]}, X2={x_values[1]}, X3={x_values[2]}, X4={x_values[3]}", "INFO")
                    
                    # 分析分布
                    distribution, target_idx = self._analyze_distribution(x_values)
                    self._emit(f"📈 分布分析: {distribution}, 目标组: {target_idx}", "INFO")
                    
                    # 检查是否为异常分布
                    if target_idx is None:
                        # 异常分布：4+0或2+2
                        count_0 = x_values.count(0)
                        count_1 = x_values.count(1)
                        count_2 = x_values.count(2)
                        
                        self._emit(f"❌ 异常分布: {distribution}（count_1={count_1}, count_0={count_0}, count_2={count_2}）", "ERROR")
                        self._emit("⚠️ 正常分布应为: 013, 031, 130, 103, 301, 310", "WARN")
                        self._send_email(
                            "校准失败 - 异常分布",
                            f"检测到异常分布: {distribution}\nX值: X1={x_values[0]}, X2={x_values[1]}, X3={x_values[2]}, X4={x_values[3]}\n正常分布应为: 013, 031, 130, 103, 301, 310"
                        )
                        if self.bot:
                            self.bot.is_paused = True
                        return False, None
                    
                    # 点击目标组
                    self._emit(f"🎯 校准点击：组{target_idx}（分布={distribution}）", "INFO")
                    self._calibrate_click_group(target_idx, use_foreground)
                    time.sleep(0.1)  # 短暂等待点击生效
                    
                    # 点击后检查探针状态（必须1AND1消失才算校准成功）
                    time.sleep(0.2)  # 等待点击生效
                    if self._check_calibration_probes():
                        # 仍然1 AND 1，说明校准失败
                        self._emit(f"❌ 校准后探针仍为1 AND 1（点击组{target_idx}后），暂停并发送邮件", "ERROR")
                        self._send_email(
                            "校准失败 - 点击后仍为1 AND 1",
                            f"校准点击组{target_idx}后，探针仍为1 AND 1状态。分布: {distribution}\nX值: X1={x_values[0]}, X2={x_values[1]}, X3={x_values[2]}, X4={x_values[3]}"
                        )
                        if self.bot:
                            self.bot.is_paused = True
                        return False, None
                    
                    # 校准成功：大探针小探针不再是1 AND 1（1AND1已消失）
                    self._emit(f"✅ 校准成功（1AND1已消失，点击组{target_idx}有效），重新执行点击触发对战", "SUCCESS")
                    
                    if skip_stage1:
                        # 野外模式：校准成功后，需要重新执行Stage 1（ABABAB移动+扫描）
                        # 但这里无法执行，应该返回False让外部重新执行Stage 1
                        self._emit("⚠️ 野外模式校准成功，需要外部重新执行Stage 1（ABABAB移动+扫描）", "WARN")
                        return False, None
                    else:
                        # 固定模式：重新执行Stage 1：点击触发对战
                        if trigger_callback:
                            trigger_xy = trigger_callback()  # 重新获取触发坐标
                            self._click_xy(trigger_xy[0], trigger_xy[1], use_foreground)  # 立即点击一次触发对战
                            last_click_time = time.time()  # 重置点击时间，准备持续点击
                            # 重置超时计时器（给新的触发更多时间）
                            t0 = time.time()
                            continue
                
                # 4. 如果没有检测到fightResource/skill/swf或PetItem或校准探针
                if not skip_stage1:
                    # 固定模式：持续点击触发点
                    now = time.time()
                    if now - last_click_time >= click_interval:
                        self._click_xy(trigger_xy[0], trigger_xy[1], use_foreground)
                        last_click_time = now
                        # 节流：持续点击日志每2秒输出一次
                        self._emit(f"🖱 持续点击触发点 ({trigger_xy[0]:.0f}, {trigger_xy[1]:.0f})", "DEBUG", 
                                  throttle_key="continuous_trigger_click", throttle_interval=2.0)
                # 野外模式：不持续点击，只等待检测信号
                
                time.sleep(0.02)  # 减少等待时间，提高检测频率（从0.05s改为0.02s）
            
            # 超时前最后检查：即使没检测到PetItem，也检查是否已进入战斗
            if not petitem_found:
                # 最后检查：检测回合探针（右下角蓝色探针）
                if round_probe_model is None:
                    round_probe_model = self._load_probe_templates()
                
                if round_probe_model and (skill_detected_time is not None or petswf_detected_time is not None):
                    probe_state, blue_score, gray_score = self._detect_round_probe(round_probe_model)
                    if probe_state == "BLUE" and blue_score >= 0.90:
                        # 虽然没有PetItem日志，但回合探针变蓝，说明已经进入战斗
                        self._emit("✅ 超时前最后检查：检测到回合探针变蓝，判定已进入战斗", "SUCCESS")
                        petitem_found = True
                        
                        # 执行第一回合动作（如果还没有执行）
                        if config:
                            round_idx = 1
                            if config.action_callback:
                                action_type = config.action_callback(round_idx)
                                self._execute_action(action_type, config, round_idx=round_idx, invincible_first_round=config.invincible_first_round)
                            elif config.skill_key:
                                self._click_region_twice(config.skill_key, config.use_foreground, gap=0.06)
                                self._last_action = LastActionType.SKILL
                            self._emit("✅ 第一回合动作已执行（超时前最后检查）", "SUCCESS")
                        
                        return True, None
            
            # 超时
            self._emit(f"⏱️ Stage 2 超时（{timeout_s}s内未检测到PetItem）", "WARN")
            return False, None
        finally:
            # 确保停止内核监听
            self._stop_kernel_listen()
    
    # ================================
    # Stage 3: 战斗循环
    # ================================
    
    def _grab_probe_image(self) -> Optional[Image.Image]:
        """截取回合探针图像"""
        try:
            r = self._require_region("对战.回合探针")
            x1, y1, x2, y2 = r.outer_bbox()
            return window_manager.grab_game_bbox(x1, y1, x2, y2, min_size_px=2)
        except Exception:
            return None
    
    def _load_probe_templates(self):
        """加载回合探针模板（与BattleRunner相同的逻辑）"""
        import os
        from core.battle_runner import ProbeModel, _blue_strength, _ahash_bits
        
        PROBE_BLUE_REL = os.path.join("对战", "回合探针", "blue.png")
        PROBE_GRAY_REL = os.path.join("对战", "回合探针", "gray.png")
        
        blue_path = os.path.join(self.template_root, PROBE_BLUE_REL)
        gray_path = os.path.join(self.template_root, PROBE_GRAY_REL)
        
        if not os.path.exists(blue_path) or not os.path.exists(gray_path):
            self._emit(f"⚠️ 探针模板不存在：{blue_path} 或 {gray_path}", "WARN")
            return None
        
        blue_img = Image.open(blue_path).convert("RGB")
        gray_img = Image.open(gray_path).convert("RGB")
        
        blue_ref = _blue_strength(blue_img)
        gray_ref = _blue_strength(gray_img)
        span = max(10.0, abs(blue_ref - gray_ref))
        
        ahash_size = 10
        blue_bits = _ahash_bits(blue_img, size=ahash_size)
        gray_bits = _ahash_bits(gray_img, size=ahash_size)
        
        force = os.environ.get("NIEO_PROBE_MODE", "").strip().upper()
        color_gap = abs(blue_ref - gray_ref)
        if force in ("COLOR", "AHASH"):
            mode = force
        else:
            mode = "COLOR" if color_gap >= 6.0 else "AHASH"
        
        if mode == "COLOR":
            self._emit(f"🧪 探针模式=COLOR (blue_ref={blue_ref:.2f}, gray_ref={gray_ref:.2f}, gap={color_gap:.2f})", "DEBUG")
            return ProbeModel(mode="COLOR", blue_ref=blue_ref, gray_ref=gray_ref, span=span, tie_eps=0.05)
        
        self._emit(f"🧪 探针模式=AHASH (gap={color_gap:.2f}) size={ahash_size}", "DEBUG")
        return ProbeModel(mode="AHASH", blue_bits=blue_bits, gray_bits=gray_bits, ahash_size=ahash_size, tie_eps=0.03)
    
    def _detect_round_probe(self, probe_model: Optional[Any] = None) -> Tuple[str, float, float]:
        """
        检测回合探针状态
        返回: (state, blue_score, gray_score)
        state: "BLUE" / "GRAY" / "UNKNOWN"
        """
        if probe_model is None:
            return "UNKNOWN", 0.0, 0.0
        
        img = self._grab_probe_image()
        if img is None:
            return "UNKNOWN", 0.0, 0.0
        
        try:
            from core.battle_runner import _blue_strength, _ahash_bits, _sim_bits
            
            if probe_model.mode == "COLOR":
                v = _blue_strength(img)
                s_blue = max(0.0, 1.0 - abs(v - probe_model.blue_ref) / float(probe_model.span))
                s_gray = max(0.0, 1.0 - abs(v - probe_model.gray_ref) / float(probe_model.span))
                if abs(s_blue - s_gray) < probe_model.tie_eps:
                    return ("UNKNOWN", s_blue, s_gray)
                return ("BLUE", s_blue, s_gray) if s_blue > s_gray else ("GRAY", s_blue, s_gray)
            
            bits = _ahash_bits(img, size=probe_model.ahash_size)
            s_blue = _sim_bits(bits, probe_model.blue_bits)
            s_gray = _sim_bits(bits, probe_model.gray_bits)
            if abs(s_blue - s_gray) < probe_model.tie_eps:
                return ("UNKNOWN", s_blue, s_gray)
            return ("BLUE", s_blue, s_gray) if s_blue > s_gray else ("GRAY", s_blue, s_gray)
        
        except Exception as e:
            self._emit(f"⚠️ 探针检测异常: {e}", "WARN")
            return "UNKNOWN", 0.0, 0.0
    
    def _execute_action(self, action_type: str, config: BattleConfig, round_idx: int = 0, invincible_first_round: bool = False):
        """
        执行动作（技能/胶囊/逃跑）
        
        重要：所有技能/切换/捕捉/逃跑区域都要点击两次
        
        Args:
            action_type: "skill"/"capsule"/"escape"
            config: 对战配置
            round_idx: 当前回合数（用于决定使用中级/高级胶囊）
            invincible_first_round: 第一回合是否使用无敌胶囊（仅用于round_idx=1）
        """
        if action_type == "skill":
            if config.skill_key:
                # 所有技能区域点击两次
                self._click_region_twice(config.skill_key, config.use_foreground, gap=0.06)
                # 减少延迟，技能点击后只需要短暂等待即可（从0.55s减少到0.1s）
                time.sleep(0.1)
                self._last_action = LastActionType.SKILL
        elif action_type == "skill2":
            # 技能二：使用"对战.使用技能二"region
            skill2_key = "对战.使用技能二"
            if self._rs_get(skill2_key):
                self._click_region_twice(skill2_key, config.use_foreground, gap=0.06)
                time.sleep(0.1)
                self._last_action = LastActionType.SKILL
            else:
                self._emit("⚠️ 未找到技能二 region，回退为技能一", "WARN")
                if config.skill_key:
                    self._click_region_twice(config.skill_key, config.use_foreground, gap=0.06)
                    time.sleep(0.1)
                    self._last_action = LastActionType.SKILL
        elif action_type in ("capsule", "capsule_high"):
            # ✅ 所有捕捉逻辑前：先双击切换战斗面板，再双击切换捕捉面板
            battle_panel_key = "对战.切换战斗面板"
            if self._rs_get(battle_panel_key):
                self._emit("🔄 切换对战面板（双击）...", "INFO")
                self._click_region_twice(battle_panel_key, config.use_foreground, gap=0.06)
                time.sleep(0.3)  # 等待面板切换
            else:
                self._emit("⚠️ 未找到切换对战面板的 region，跳过", "WARN")
            
            # 实现胶囊逻辑（capsule_high表示尼尔家族模式：只使用高级胶囊）
            if round_idx == 1 and invincible_first_round:
                # 第一回合：无敌胶囊
                inv_key = "对战.捕捉.无敌精灵胶囊"
                inv_panel = self._first_existing_key(["对战.捕捉.切换捕捉面板"])
                if inv_panel and self._rs_get(inv_key):
                    # 切换捕捉面板点两次，然后等待约0.5s
                    self._emit("🔄 切换捕捉面板（双击）...", "INFO")
                    self._click_region_twice(inv_panel, config.use_foreground, gap=0.10)
                    time.sleep(0.50)
                    self._click_region_twice(inv_key, config.use_foreground, gap=0.08)
                    time.sleep(0.55)
                    self._emit(f"🛡 回合{round_idx}：无敌精灵胶囊(×2)", "INFO")
                    self._last_action = LastActionType.CAPSULE
                    return
                else:
                    self._emit("⚠ 无敌胶囊 region 缺失：回退为技能1", "WARN")
                    if config.skill_key:
                        self._click_region(config.skill_key, config.use_foreground)
                        time.sleep(0.55)
                        self._last_action = LastActionType.SKILL
                        return
            else:
                # 第2回合开始：中级/高级胶囊交替
                panel = self._first_existing_key([
                    "对战.捕捉.切换捕捉面板",
                    "对战.捕捉.切换捕捉面板+精灵胶囊",
                ])
                
                mid = self._first_existing_key([
                    "对战.捕捉.中级精灵胶囊",
                    "对战.捕捉.中级胶囊",
                ])
                
                high = self._first_existing_key([
                    "对战.捕捉.高级精灵胶囊",
                    "对战.捕捉.高级胶囊",
                ])
                
                combo_mid = self._first_existing_key([
                    "对战.捕捉.切换捕捉面板+中级精灵胶囊",
                ])
                
                combo_high = self._first_existing_key([
                    "对战.捕捉.切换捕捉面板+高级精灵胶囊",
                ])
                
                has_split = bool(panel and mid and high)
                has_combo = bool(combo_mid and combo_high)
                
                if has_split or has_combo:
                    # 胶囊节奏判断
                    if action_type == "capsule_high":
                        # 高级胶囊模式：只使用高级胶囊
                        use_mid = False
                    elif config.test_mode_capsule_only_mid:
                        # 测试模式：只使用中级胶囊
                        use_mid = True
                    elif action_type == "capsule" and round_idx == 1 and config.invincible_first_round:
                        # ✅ 第1回合使用胶囊且是螳螂无敌胶囊：使用中级胶囊（保留无敌胶囊逻辑）
                        use_mid = True
                    elif action_type == "capsule":
                        # ✅ 野外捕捉模式（除螳螂无敌胶囊外）：只使用高级胶囊
                        use_mid = False  # 所有野外捕捉模式都使用高级胶囊
                    else:
                        # 正常模式：只使用高级胶囊
                        use_mid = False  # 所有野外捕捉模式都使用高级胶囊
                    
                    if has_split:
                        # 分开录制：面板 + 胶囊（胶囊连点2次）
                        self._emit("🔄 切换捕捉面板（双击）...", "INFO")
                        self._click_region_twice(panel, config.use_foreground, gap=0.10)
                        time.sleep(0.50)
                        cap_key = mid if use_mid else high
                        self._click_region_twice(cap_key, config.use_foreground, gap=0.08)
                        self._emit(
                            f"🎯 回合{round_idx} 捕捉：面板 -> {'中级' if use_mid else '高级'}胶囊(×2)",
                            "INFO",
                        )
                    else:
                        # combo：直接点 combo 两次
                        ck = combo_mid if use_mid else combo_high
                        self._click_region_twice(ck, config.use_foreground, gap=0.50)
                        self._emit(
                            f"🎯 回合{round_idx} 捕捉：{'中级' if use_mid else '高级'}（combo×2）",
                            "INFO",
                        )
                    self._last_action = LastActionType.CAPSULE
                    return
                else:
                    self._emit("⚠ 胶囊 region 缺失：无法执行胶囊动作", "WARN")
                    self._last_action = LastActionType.CAPSULE
                    return
        elif action_type == "escape":
            # 逃跑逻辑：切换逃跑面板和确认逃跑都要点击两次
            escape_panel = self._first_existing_key(["对战.逃跑.切换逃跑面板"])
            escape_confirm = self._first_existing_key(["对战.逃跑.确认逃跑"])
            if escape_panel and escape_confirm:
                self._click_region_twice(escape_panel, config.use_foreground, gap=0.06)
                time.sleep(0.3)
                self._click_region_twice(escape_confirm, config.use_foreground, gap=0.06)
                self._emit(f"🏃 回合{round_idx}：逃跑（所有区域点击两次）", "INFO")
            self._last_action = LastActionType.ESCAPE
    
    def stage3_battle_loop(self, config: BattleConfig) -> bool:
        """
        Stage 3: 战斗循环
        
        检测回合变化，执行动作，直到战斗结束
        """
        self._emit("⚔️ Stage 3: 战斗循环", "INFO")
        
        # ✅ 启动内核监听（用于检测战斗结束信号：map + newNpc）
        self._start_kernel_listen()
        
        # ✅ 第一回合已在Stage 2检测到PetItem时执行，这里从round_idx=1开始
        # 注意：如果第一回合动作刚执行，给它一点时间生效（但不要sleep太久）
        round_idx = 1
        blue_streak = 0
        armed = False
        probe_model = self._load_probe_templates()
        last_probe_log = 0.0
        last_action_at = time.time()  # 第一回合已在Stage 2执行，记录执行时间
        
        # ✅ 不sleep，立即开始检测后续回合（第一回合动作已经在stage2执行）
        
        while True:
            # 检查中止
            if config.abort_check and config.abort_check():
                return False
            if self.bot and hasattr(self.bot, "stop_current") and self.bot.stop_current:
                return False
            
            # 检查暂停
            if self.bot and hasattr(self.bot, "is_paused"):
                while self.bot.is_paused and not (hasattr(self.bot, "stop_current") and self.bot.stop_current):
                    time.sleep(0.05)
            
            # ✅ 检查白色探针是否变白（地图10特殊处理，优先级高于map+newNPC检测）
            white_probe_ready = self._check_white_probe_non_white()
            if white_probe_ready:
                self._emit(f"🏁 检测到白色探针变白（newNPC已出现，回合{round_idx}），结束战斗", "SUCCESS")
                self._round_idx = round_idx
                
                # 停止内核监听
                self._stop_kernel_listen()
                
                # 战斗结束后，快速检测校准探针（1 AND 1）是否出现并消失
                self._wait_for_calibration_probes_to_disappear(use_foreground=config.use_foreground)
                
                return True
            
            # ✅ 检查战斗结束（只需要map信号就可以开始，不需要等待newNPC）
            map_seen, npc_seen = self._check_battle_end()
            if map_seen:
                self._emit(f"🏁 检测到战斗结束信号（Map，回合{round_idx}），开始战后处理", "SUCCESS")
                self._round_idx = round_idx
                
                # 停止内核监听
                self._stop_kernel_listen()
                
                # ✅ 不再在这里等待校准探针，直接返回让stage4处理
                return True
            
            # 检测回合探针
            state, s_blue, s_gray = self._detect_round_probe(probe_model)
            
            now = time.time()
            if now - last_probe_log >= 2.5:
                last_probe_log = now
                # 探针检测日志已节流（2.5秒间隔），不需要额外节流
                self._emit(f"🔎 探针={state} blue={s_blue:.3f} gray={s_gray:.3f} 回合={round_idx}", "DEBUG")
            
            # 回合检测逻辑：非蓝 -> 连续蓝触发
            if state == "BLUE":
                blue_streak += 1
            else:
                blue_streak = 0
                armed = True
            
            # 第一回合已在循环开始前执行（检测到PetItem后立即出招），这里只处理后续回合
            # 触发新回合（后续回合：非蓝->蓝触发）
            now = time.time()
            if armed and state == "BLUE" and blue_streak >= 1 and (now - last_action_at) >= 0.12:
                round_idx += 1
                self._emit(f"🎯 回合{round_idx}: 检测到可选技能", "INFO")
                
                # 执行动作
                if config.action_callback:
                    action_type = config.action_callback(round_idx)
                    # 如果返回"switch"，表示已经在action_callback中处理了切换，跳过执行
                    if action_type == "switch":
                        self._emit(f"🔄 回合{round_idx}: 切换精灵（已在action_callback中处理）", "INFO")
                    else:
                        self._execute_action(action_type, config, round_idx=round_idx, invincible_first_round=config.invincible_first_round)
                elif config.skill_key:
                    # 默认使用技能（所有技能区域点击两次）
                    self._click_region_twice(config.skill_key, config.use_foreground, gap=0.06)
                    time.sleep(0.55)
                    self._last_action = LastActionType.SKILL
                
                # ✅ 执行动作后，检查白色探针是否变白（地图10特殊处理）
                # 如果白色探针变为非纯白色，表示newNPC已出现，可以结束战斗
                time.sleep(0.1)  # 给动作一点时间生效
                white_probe_ready = self._check_white_probe_non_white()
                if white_probe_ready:
                    self._emit(f"🏁 检测到白色探针变白（newNPC已出现，回合{round_idx}），结束战斗", "SUCCESS")
                    self._round_idx = round_idx
                    
                    # 停止内核监听
                    self._stop_kernel_listen()
                    
                    # 战斗结束后，快速检测校准探针（1 AND 1）是否出现并消失
                    self._wait_for_calibration_probes_to_disappear(use_foreground=config.use_foreground)
                    
                    return True
                
                armed = False
                blue_streak = 0
                last_action_at = now
                time.sleep(0.05)
                continue
            
            time.sleep(0.03)
        
        return True
    
    # ================================
    # 战斗内恢复
    # ================================
    
    def _execute_battle_recovery(self, use_foreground: bool):
        """执行战斗内恢复：双击切换道具面板+超级体力药剂"""
        try:
            import os
            script_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fix_script", "战斗内恢复.json")
            if not os.path.exists(script_path):
                self._emit(f"⚠ 战斗内恢复脚本不存在: {script_path}", "WARN")
                return
            
            # 读取并执行脚本
            import json
            with open(script_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            steps = data.get("steps", [])
            if not steps:
                self._emit("⚠ 战斗内恢复脚本为空", "WARN")
                return
            
            self._emit("💊 开始执行战斗内恢复脚本", "INFO")
            for idx, step in enumerate(steps, start=1):
                if self.bot and hasattr(self.bot, "stop_current") and self.bot.stop_current:
                    return
                
                gx = step.get("x")
                gy = step.get("y")
                if gx is None or gy is None:
                    continue
                
                delay = float(step.get("delay", 0.2))
                if delay < 0:
                    delay = 0.0
                
                time.sleep(delay)
                
                if use_foreground:
                    window_manager.click(gx, gy)
                else:
                    window_manager.click_background(gx, gy)
            
            self._emit("✅ 战斗内恢复完成", "SUCCESS")
        except Exception as e:
            self._emit(f"💥 战斗内恢复异常: {e}", "ERROR")
    
    # ================================
    # 战斗结束后等待校准探针消失
    # ================================
    
    def _wait_for_calibration_probes_to_disappear(self, use_foreground: bool, timeout_s: float = 5.0):
        """
        战斗结束后，快速检测校准探针（1 AND 1）是否出现并消失
        如果检测到1 AND 1，等待它消失（变成00或其他状态）
        一旦1 AND 1消失，立即返回，进入下一阶段
        """
        t0 = time.time()
        saw_11 = False  # 是否曾经检测到1 AND 1
        
        self._emit("🔍 战斗结束后：检测校准探针状态", "DEBUG")
        
        while (time.time() - t0) < timeout_s:
            # 检查中止
            if self.bot and hasattr(self.bot, "stop_current") and self.bot.stop_current:
                return
            
            # 检查暂停
            if self.bot and hasattr(self.bot, "is_paused"):
                while self.bot.is_paused and not (hasattr(self.bot, "stop_current") and self.bot.stop_current):
                    time.sleep(0.05)
            
            # 检测校准探针（1 AND 1）
            has_11 = self._check_calibration_probes()
            
            if has_11:
                if not saw_11:
                    saw_11 = True
                    self._emit("🔍 检测到校准探针（1 AND 1），等待消失", "INFO")
            else:
                # 探针消失或从未出现
                if saw_11:
                    # 曾经检测到1 AND 1，现在已经消失
                    self._emit("✅ 校准探针（1 AND 1）已消失，进入下一阶段", "SUCCESS")
                    return
                else:
                    # 从未检测到1 AND 1，直接返回
                    self._emit("✅ 未检测到校准探针，直接进入下一阶段", "DEBUG")
                    return
            
            # 高频检测（每50ms检测一次，快速响应）
            time.sleep(0.05)
        
        # 超时
        if saw_11:
            self._emit(f"⚠️ 校准探针（1 AND 1）在{timeout_s}s内未消失，继续执行", "WARN")
    
    # ================================
    # Stage 4: 战斗结束处理
    # ================================
    
    def _wait_for_confirm_probes(self, config: BattleConfig, timeout_s: float = 2.0, is_training_room: bool = False):
        """等待并点击 通用探针白色 + 普通确认探针蓝色 1 AND 1，循环点击直到确认消失
        
        Args:
            config: 对战配置
            timeout_s: 超时时间（如果为0或负数，表示不超时，一直循环直到消失）
            is_training_room: 是否为训练室模式（已弃用）
        """
        COLOR_WHITE = (255, 255, 255)
        # 普通确认探针蓝色：根据常见UI设计，可能是浅蓝色或标准蓝色
        COLOR_BLUE = (47, 167, 238)  # 使用与回合探针类似的蓝色
        
        t0 = time.time()
        click_interval = 0.1  # 点击间隔（100ms，快速响应）
        has_clicked_at_least_once = False  # 是否至少点击过一次
        
        self._emit("⏳ 等待通用探针白色 + 普通确认探针蓝色 1 AND 1（必须出现过一次且直到消失）", "INFO")
        
        # 如果没有超时限制，使用一个很长的超时时间
        effective_timeout = timeout_s if timeout_s > 0 else 3600.0  # 默认1小时
        
        while (time.time() - t0) < effective_timeout:
            if config.abort_check and config.abort_check():
                return
            
            # 检测两个探针
            ok_white, ok_blue = self._check_probe_pair(
                self.KEY_GENERAL_PROBE, COLOR_WHITE,
                self.KEY_NORMAL_CONFIRM_PROBE, COLOR_BLUE,
                tolerance=5
            )
            
            # 如果都是1，点击确认按钮（循环点击直到消失）
            if ok_white and ok_blue:
                try:
                    self._click_region(self.KEY_NORMAL_CONFIRM_BTN, config.use_foreground)
                    if not has_clicked_at_least_once:
                        self._emit("✅ 检测到 1 AND 1，开始循环点击确认（必须等到消失）", "SUCCESS")
                        has_clicked_at_least_once = True
                    time.sleep(click_interval)  # 点击后等待一小段时间再检测
                    continue  # 继续循环检测
                except KeyError:
                    self._emit("⚠️ 确认按钮不存在", "WARN")
                    break
            else:
                # 如果不再是1 AND 1（探针消失），且已经点击过至少一次，结束等待
                if has_clicked_at_least_once:
                    self._emit("✅ 1 AND 1 已消失，结束循环点击", "SUCCESS")
                    return
                # ✅ 如果从未点击过，继续循环等待（直到超时或检测到1 AND 1）
                # 不需要立即break，让while循环的超时条件来处理
            
            time.sleep(0.05)  # 检测间隔（50ms）
        
        # 超时处理
        if has_clicked_at_least_once:
            self._emit("✅ 已循环点击确认（超时但已处理）", "SUCCESS")
        else:
            if timeout_s <= 0:
                self._emit("⚠️ 等待超时：未检测到 1 AND 1（无限等待模式）", "WARN")
            else:
                self._emit(f"⚠️ 超时：{timeout_s}s内未检测到 1 AND 1", "WARN")
    
    def _check_probe_pair(self, key1: str, color1: Tuple[int, int, int], 
                          key2: str, color2: Tuple[int, int, int], 
                          tolerance: int = 5) -> Tuple[bool, bool]:
        """检查一对探针"""
        ok1 = self._check_color_strict(key1, color1, tolerance)
        ok2 = self._check_color_strict(key2, color2, tolerance)
        return ok1, ok2
    
    def _detect_battery_status(self, use_foreground: bool = False) -> Optional[int]:
        """
        检测电池状态
        - 红色 #FF0000 -> 返回 1
        - 黑色 #000000 -> 返回 0
        - 其他或检测失败 -> 返回 None
        
        Returns:
            1: 红色（有电）
            0: 黑色（无电）
            None: 检测失败
        """
        try:
            battery_reg = self.regions.get(self.KEY_BATTERY)
            if not battery_reg:
                self._emit("⚠️ 电池区域不存在", "WARN")
                return None
            
            # 获取区域图像
            gx1, gy1, gx2, gy2 = battery_reg.outer_bbox()
            img = window_manager.grab_game_bbox(gx1, gy1, gx2, gy2)
            if img is None:
                return None
            
            # 计算平均RGB
            pixels = list(img.getdata())
            if not pixels:
                return None
            
            r = int(round(sum(p[0] for p in pixels) / len(pixels)))
            g = int(round(sum(p[1] for p in pixels) / len(pixels)))
            b = int(round(sum(p[2] for p in pixels) / len(pixels)))
            mean_rgb = (r, g, b)
            
            # 规则：R 通道 >= 64 判定为 1，否则判定为 0
            if r >= 64:
                self._emit(f"🔋 电池状态检测：R>=64 (RGB={mean_rgb}) -> 1", "INFO")
                return 1
            self._emit(f"🔋 电池状态检测：R<64 (RGB={mean_rgb}) -> 0", "INFO")
            return 0
        except Exception as e:
            self._emit(f"❌ 电池状态检测异常: {e}", "ERROR")
            return None
    
    def stage4_post_battle(self, config: BattleConfig, is_training_room: bool = False, is_hero_tower: bool = False) -> bool:
        """
        Stage 4: 战斗结束处理
        
        ✅ 新的对战结束逻辑（所有模式统一）：
        - 检测对战结束只需要 map 就可以开始
        - 胜利（SKILL）：黄色探针 -> 胜利确认 -> 1 AND 1直到消失
        - 逃跑或捕捉成功（ESCAPE/CAPSULE）：1 AND 1（必须出现过一次1 AND 1且直到消失）
        
        返回: True=成功处理，False=失败
        """
        self._emit("🎬 Stage 4: 战斗结束处理", "INFO")
        
        # ✅ 根据上一回合动作类型选择流程
        if self._last_action == LastActionType.SKILL:
            # 技能动作：需要检测黄色探针
            self._emit("🏆 检测到技能动作，进入胜利检测流程", "INFO")
            
            # 1. 等待黄色探针（胜利探针）
            victory_timeout = 8.0
            t0 = time.time()
            victory_detected = False
            
            self._emit("🟡 等待胜利黄色探针出现...", "INFO")
            
            while (time.time() - t0) < victory_timeout:
                if config.abort_check and config.abort_check():
                    return False
                if self.bot and hasattr(self.bot, "stop_current") and self.bot.stop_current:
                    return False
                
                # 检测黄色探针（从PostBattleCleaner复用逻辑）
                try:
                    from core.post_battle_cleaner import PostBattleCleaner
                    cleaner = PostBattleCleaner(self.bot, self.regions, self.template_root)
                    # 使用和训练室一致的参数
                    got_yellow, score, rgb = cleaner.detect_victory_probe_yellow(
                        use_foreground=config.use_foreground,
                        tol=10,
                        ratio_th=0.75
                    )
                    
                    if got_yellow:
                        victory_detected = True
                        self._emit(f"✅ 检测到胜利黄色探针 (score={score:.3f}, rgb={rgb})", "SUCCESS")
                        break
                except Exception as e:
                    self._emit(f"⚠️ 检测黄色探针失败: {e}", "WARN")
                
                time.sleep(0.08)
            
            # ✅ 必须检测到黄色探针后才能继续执行后续步骤
            if not victory_detected:
                self._emit("❌ 未检测到胜利黄色探针（超时），无法执行后续流程", "ERROR")
                return False
            
            # 第二步：持续点击胜利确认（0.3s频率），直到黄色探针消失
            last_click_time = 0.0
            click_interval = 0.3  # 0.3秒点击一次
            yellow_disappear_timeout = 10.0  # 黄色探针消失超时时间
            t1 = time.time()
            yellow_disappeared = False
            
            self._emit("🟡 持续点击胜利确认，等待黄色探针消失...", "INFO")
            
            try:
                from core.post_battle_cleaner import PostBattleCleaner
                cleaner = PostBattleCleaner(self.bot, self.regions, self.template_root)
                
                while (time.time() - t1) < yellow_disappear_timeout:
                    if config.abort_check and config.abort_check():
                        return False
                    if self.bot and hasattr(self.bot, "stop_current") and self.bot.stop_current:
                        return False
                    
                    # 高频检测黄色探针是否消失（每次循环都检测）
                    try:
                        got_yellow, score, rgb = cleaner.detect_victory_probe_yellow(
                            use_foreground=config.use_foreground,
                            tol=10,
                            ratio_th=0.75
                        )
                        
                        if not got_yellow:
                            yellow_disappeared = True
                            self._emit("✅ 黄色探针已消失，停止点击胜利确认", "SUCCESS")
                            break
                    except Exception as e:
                        pass  # 检测失败时继续点击
                    
                    # 每0.3秒点击一次胜利确认
                    now = time.time()
                    if now - last_click_time >= click_interval:
                        try:
                            self._click_region(self.KEY_VICTORY_CONFIRM, config.use_foreground)
                            last_click_time = now
                        except KeyError:
                            self._emit("⚠️ 胜利确认按钮不存在", "WARN")
                            break
                    
                    time.sleep(0.05)  # 高频检测循环（50ms）
                
                if not yellow_disappeared:
                    self._emit("⚠️ 黄色探针未消失（超时），继续后续流程", "WARN")
            except Exception as e:
                self._emit(f"⚠️ 点击胜利确认过程异常: {e}", "WARN")
            
            # 2. 训练室模式（非勇者之塔）：升级确认 + 技能替换取消（都双击）
            if is_training_room and not is_hero_tower:
                try:
                    # 升级确认：双击
                    self._click_region_twice(self.KEY_UPGRADE_CONFIRM_BTN, config.use_foreground, gap=0.1)
                    time.sleep(0.5)  # 点击后等待更长时间
                    # 技能替换取消：双击
                    self._click_region_twice(self.KEY_SKILL_REPLACE_CANCEL_BTN, config.use_foreground, gap=0.1)
                    time.sleep(0.3)
                    self._emit("✅ 训练室模式：已点击升级确认和技能替换取消", "SUCCESS")
                except KeyError:
                    self._emit("⚠️ 升级确认或技能替换取消按钮不存在", "WARN")
            
            # ✅ 3. 等待通用探针白色+普通确认探针蓝色 1 AND 1直到消失（所有模式统一：必须出现过一次1 AND 1且直到消失）
            # 使用0或负数表示不超时，一直循环直到消失
            self._wait_for_confirm_probes(config, timeout_s=0.0, is_training_room=is_training_room)
            
            # 4. 训练室模式：1AND1结束后点击关闭资料
            if is_training_room and not is_hero_tower:
                try:
                    self._click_region("训练室.关闭资料", config.use_foreground)
                    time.sleep(0.3)
                    self._emit("✅ 训练室模式：已点击关闭资料（1AND1结束后）", "INFO")
                except KeyError:
                    self._emit("⚠️ 训练室.关闭资料区域不存在", "WARN")
                
                # 训练室模式：1AND1+关闭资料结束后等待1.5s（如果不需要恢复，这个等待后直接开始下一轮）
                self._emit("⏳ 等待1.5s（1AND1+关闭资料后）", "INFO")
                time.sleep(1.5)
            
        elif self._last_action == LastActionType.CAPSULE:
            # ✅ 胶囊动作（捕捉）：1 AND 1（必须出现过一次1 AND 1且直到消失）
            self._emit("🎣 检测到胶囊动作（捕捉），等待1 AND 1出现并直到消失", "INFO")
            # 使用0或负数表示不超时，一直循环直到消失
            self._wait_for_confirm_probes(config, timeout_s=0.0)
            
        elif self._last_action == LastActionType.ESCAPE:
            # ✅ 逃跑动作：1 AND 1（必须出现过一次1 AND 1且直到消失）
            self._emit("🏃 检测到逃跑动作，等待1 AND 1出现并直到消失", "INFO")
            # 使用0或负数表示不超时，一直循环直到消失
            self._wait_for_confirm_probes(config, timeout_s=0.0)
        else:
            # 未知动作类型，默认进入1 AND 1检测
            self._emit("⚠️ 未知动作类型，默认进入1 AND 1检测", "WARN")
            self._wait_for_confirm_probes(config)
        
        # ✅ 新增：1AND1之后，检测电池状态（仅野外模式）
        if config.mode == BattleMode.WILD:
            battery_status_after = self._detect_battery_status(config.use_foreground)
            if battery_status_after is not None:
                self._emit(f"🔋 [战后检测] 电池状态: {battery_status_after}", "INFO")
                
                # 如果是在 DarRouteRunner 中调用，更新电池状态作为下一次的基准
                if hasattr(self.bot, 'dar_route_runner'):
                    dar_runner = self.bot.dar_route_runner
                    battery_before = getattr(dar_runner, '_battery_status_before_battle', None)
                    
                    # ✅ 无论是否捕捉成功，都更新电池状态作为下一次的基准
                    dar_runner._battery_status_before_battle = battery_status_after
                    
                    # ✅ 只有捕捉成功后，才判断是否触发刷新重连
                    if self._last_action == LastActionType.CAPSULE and battery_before is not None:
                        # 获取当前profile，判断是否为双塔或嘟咕噜模式
                        current_profile = getattr(dar_runner, '_current_profile', None)
                        profile_name_lower = current_profile.name.lower() if current_profile else ""
                        is_shuangta_or_dugulu = "双塔" in profile_name_lower or "嘟咕噜" in profile_name_lower
                        
                        # 双塔和嘟咕噜模式：使用1 to 0触发重启
                        # 其他模式：使用测试模式标志判断
                        if is_shuangta_or_dugulu:
                            # 双塔和嘟咕噜模式：1 to 0 触发
                            if battery_before == 1 and battery_status_after == 0:
                                self._emit(f"⚠️ [电池检测] 状态变化: {battery_before} to {battery_status_after} ({profile_name_lower}模式：1 to 0) -> 触发刷新重连", "WARN")
                                dar_runner._battery_status_after_battle = battery_status_after
                                dar_runner._should_refresh_reconnect = True
                        else:
                            # 其他模式：根据测试模式标志判断
                            test_mode = getattr(dar_runner, '_battery_test_mode', True)
                            
                            if test_mode:
                                # 测试模式：1 to 1
                                if battery_before == 1 and battery_status_after == 1:
                                    self._emit(f"⚠️ [电池检测] 状态变化: {battery_before} to {battery_status_after} (测试模式：1 to 1) -> 触发刷新重连", "WARN")
                                    dar_runner._battery_status_after_battle = battery_status_after
                                    dar_runner._should_refresh_reconnect = True
                            else:
                                # 正式模式：1 to 0
                                if battery_before == 1 and battery_status_after == 0:
                                    self._emit(f"⚠️ [电池检测] 状态变化: {battery_before} to {battery_status_after} (正式模式：1 to 0) -> 触发刷新重连", "WARN")
                                    dar_runner._battery_status_after_battle = battery_status_after
                                    dar_runner._should_refresh_reconnect = True
        
        return True
    
    # ================================
    # 校准测试（纯屏幕检测）
    # ================================
    
    def run_calibration_test(self, use_foreground: bool = False) -> bool:
        """
        校准测试：纯屏幕检测，不依赖日志
        
        执行流程：
        1. 检测校准探针（大探针=2FA7EE AND 小探针=FFFFFF）
        2. 如果检测到，计算X1-X4值
        3. 分析分布，找到目标组
        4. 点击对应组的中点
        
        返回: True=成功，False=失败
        """
        self._emit("🔧 开始校准测试（纯屏幕检测）", "INFO")
        
        # 1. 检查校准探针
        if not self._check_calibration_probes():
            self._emit("❌ 未检测到校准探针（大探针=2FA7EE AND 小探针=FFFFFF）", "WARN")
            self._emit("💡 提示：请确保已触发校准状态", "INFO")
            return False
        
        self._emit("✅ 检测到校准探针（大=2FA7EE AND 小=FFFFFF）", "SUCCESS")
        
        # 2. 计算X1-X4
        x_values, regions_dict = self._calculate_x_values()
        self._emit(f"📊 X值: X1={x_values[0]}, X2={x_values[1]}, X3={x_values[2]}, X4={x_values[3]}", "INFO")
        
        # 3. 分析分布
        distribution, target_idx = self._analyze_distribution(x_values)
        self._emit(f"📈 分布分析: {distribution}, 目标组: {target_idx}", "INFO")
        
        # 4. 检查是否为异常分布
        if target_idx is None:
            self._emit(f"❌ 异常分布: {distribution}，无法确定目标组", "ERROR")
            self._emit("⚠️ 正常分布应为: 013, 031, 130, 103, 301, 310", "WARN")
            # 异常分布，发送邮件
            self._send_email(
                "校准测试 - 异常分布",
                f"检测到异常分布: {distribution}\nX值: X1={x_values[0]}, X2={x_values[1]}, X3={x_values[2]}, X4={x_values[3]}"
            )
            return False
        
        # 5. 点击目标组
        self._emit(f"🎯 准备点击目标组: {target_idx}", "INFO")
        success = self._calibrate_click_group(target_idx, use_foreground)
        
        if success:
            self._emit(f"✅ 校准测试完成：已点击组{target_idx}", "SUCCESS")
            return True
        else:
            self._emit(f"❌ 校准测试失败：点击组{target_idx}失败", "ERROR")
            return False
    
    # ================================
    # 主流程入口
    # ================================
    
    def run_battle(self, config: BattleConfig, is_training_room: bool = False, is_hero_tower: bool = False) -> bool:
        """
        执行完整对战流程
        
        Args:
            config: 对战配置
            is_training_room: 是否为训练室模式（影响Stage 4的流程）
            is_hero_tower: 是否为勇者之塔模式（影响Stage 4的流程）
        
        返回: True=成功完成，False=失败或中止
        """
        try:
            self._start_kernel_listen()
            self._last_action = None
            self._round_idx = 0
            
            # Stage 1: 触发对战
            self._emit("🚀 Stage 1: 触发对战", "INFO")
            if config.trigger_callback:
                trigger_xy = config.trigger_callback()
                self._emit(f"✅ Stage 1: 触发坐标 ({trigger_xy[0]:.0f}, {trigger_xy[1]:.0f})", "SUCCESS")
            else:
                self._emit("❌ Stage 1: 未提供trigger_callback", "ERROR")
                return False
            
            # Stage 2: 校准和PetItem检测（传入trigger_callback以便重新触发，传入config以便检测到PetItem时执行第一回合）
            success, calib_result = self.stage2_calibration_and_petitem(
                config.trigger_callback,
                config.use_foreground,
                timeout_s=10.0,
                skip_stage1=False,  # 固定模式需要Stage 1
                config=config  # 传递config以便检测到PetItem时执行第一回合动作
            )
            
            if not success:
                self._emit("❌ Stage 2 失败，跳过本次对战", "WARN")
                return False
            
            # Stage 3: 战斗循环
            battle_success = self.stage3_battle_loop(config)
            if not battle_success:
                self._emit("❌ Stage 3 中止或失败", "WARN")
                return False
            
            # Stage 4: 战斗结束处理（内部已包含训练室模式的3s等待）
            post_success = self.stage4_post_battle(config, is_training_room=is_training_room, is_hero_tower=is_hero_tower)
            if not post_success:
                self._emit("❌ Stage 4 处理失败", "WARN")
                return False
            
            self._emit(f"✅ 对战完成（总回合数: {self._round_idx}）", "SUCCESS")
            return True
            
        except Exception as e:
            self._emit(f"💥 对战流程异常: {e}", "ERROR")
            import traceback
            self._emit(traceback.format_exc(), "ERROR")
            return False
        finally:
            self._stop_kernel_listen()
