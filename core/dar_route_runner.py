# core/dar_route_runner.py
import logging
import os
import re
import shutil
import threading
import time
import pickle
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple, Callable

from PIL import Image
import numpy as np

from core.logger import fetch_kernel_since, kernel_cursor, wait_kernel_contains
from core.region_store import Region, RegionStore
from core.utils import window_manager

# OCR support (optional)
try:
    import pytesseract
except ImportError:
    pytesseract = None

# Pet ID到名称的映射（用于OCR识别）
PET_ID_TO_NAME = {
    122: "达尔",  # 螳螂
    254: "嘟咕噜",
    102: "奇塔",
    143: "卡塔",
    27: "小豆芽",
    16: "布鲁",
    77: "尼尔",
    416: "尼奥",
    310: "闪光尼尔",
    164: "闪光皮皮",
    13: "格林",  # 假设ID
    14: "艾斯菲格",  # 假设ID（我方精灵）
    15: "闪光艾菲亚",  # 假设ID（我方精灵）
}

# 我方精灵名称列表（不需要识别这些）
MY_PET_NAMES = {"艾斯菲格","闪光艾菲亚"}

# 切换探针RGB参考值（用于验证切换的精灵是否正确）
# 测量自: assets/templates/对战/切换精灵/切换探针/
# 注意：根据实际测量和日志，图片文件名和实际颜色是反的
# 01_154137_a.png 实际RGB: R=111, G=106, B=48 (十六进制: #6F6A30) - 黄绿色调 -> 实际是闪光艾菲亚
# 01_154155_b.png 实际RGB: R=12, G=40, B=83 (十六进制: #0C2853) - 深蓝色调 -> 实际是艾斯菲格
# ✅ 已修正：根据实际检测结果，交换了RGB值
AISIFEIGE_PROBE_RGB = (12, 40, 83)  # 艾斯菲格（深蓝色调）- 从01_154155_b.png测量
FLASH_AIFEIA_PROBE_RGB = (111, 106, 48)  # 闪光艾菲亚（黄绿色调）- 从01_154137_a.png测量

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WildCaptureProfile:
    name: str
    route_hint: str
    map_swf_id: int
    target_mp3_id: int  # 单个mp3 ID（向后兼容）
    target_pet_id: int  # 单个pet ID（向后兼容）
    target_mp3_ids: Optional[Tuple[int, ...]] = None  # 多个mp3 ID（如果提供，优先使用）
    target_pet_ids: Optional[Tuple[int, ...]] = None  # 多个pet ID（如果提供，优先使用）
    excluded_pet_ids: Tuple[int, ...] = ()

    # AB 走位节流
    ab_cooldown_sec: float = 40.0

    # mp3 后的 burst 扫描窗口
    burst_window_sec: float = 1.0

    # 判定对战结束后等几秒再清 dialog
    post_battle_delay_sec: float = 3.0

    # 低强度扫描频率：每次扫描“全部 9 点”
    scan_step_interval_sec: float = 0.25

    # 低强度扫描“兜底触发”的灵敏度参数
    low_abs_min: float = 9.0               # 最小绝对差值门槛
    low_best_over_second: float = 2.0      # 最优点要比第二优点至少大多少

    # 你确认的规则：必须先听到 mp3，才允许触发“点击进战”
    require_mp3_before_trigger: bool = True
    mp3_trigger_window_sec: float = 2.0


DEFAULT_PROFILE_MANTIS = WildCaptureProfile(
    name="螳螂(122)",
    route_hint="达尔",
    map_swf_id=11,
    target_mp3_id=122,
    target_pet_id=122,
    excluded_pet_ids=(16, 27, 77, 310, 416),
    ab_cooldown_sec=40.0,
    scan_step_interval_sec=0.25,
)

DEFAULT_PROFILE_DUGULU = WildCaptureProfile(
    name="嘟咕噜(254)",
    route_hint="嘟咕噜",
    map_swf_id=323,
    target_mp3_id=254,
    target_pet_id=254,
    excluded_pet_ids=(),
    ab_cooldown_sec=40.0,
    scan_step_interval_sec=0.25,
)

DEFAULT_PROFILE_SHUANGTA = WildCaptureProfile(
    name="双塔(102/143)",
    route_hint="双塔",
    map_swf_id=320,
    target_mp3_id=102,  # 向后兼容字段（不使用）
    target_pet_id=102,  # 向后兼容字段（不使用）
    target_mp3_ids=(102, 143),  # 任意一只出现都要捕捉
    target_pet_ids=(102, 143),  # 任意一只出现都要捕捉
    excluded_pet_ids=(),
    ab_cooldown_sec=40.0,
    scan_step_interval_sec=0.25,
)

DEFAULT_PROFILE_XIAODOUYA = WildCaptureProfile(
    name="小豆芽(27)",
    route_hint="达尔",
    map_swf_id=11,
    target_mp3_id=27,
    target_pet_id=27,
    excluded_pet_ids=(),
    ab_cooldown_sec=40.0,
    scan_step_interval_sec=0.25,
)

DEFAULT_PROFILE_FLASH_PIPI = WildCaptureProfile(
    name="闪光皮皮(164)",
    route_hint="闪光皮皮",  # 使用闪光皮皮文件夹中的区域
    map_swf_id=10,  # 最终目标地图是10，但流程会先从11进入再切换到10
    target_mp3_id=164,
    target_pet_id=164,
    excluded_pet_ids=(),
    ab_cooldown_sec=40.0,
    scan_step_interval_sec=0.25,
)


class DarRouteRunner:
    """
    野外抓宠：低强度扫点（全9点） + mp3 触发 burst + 进入战斗后基于 fightResource/pet/swf 校验决定“无敌胶囊”或普通捕捉。

    重要规则（你确认）：
    - 只有听到目标 mp3 后的短窗口内，扫描命中才允许“点击进入对战”（burst/低扫一致）。
    - 地图不一致检测：仅野外模式启用（训练室不做）。
    - 点击触发对战后：调用 battle_runner.calibrate_after_trigger() 做屏幕校准与 PetItem 入战确认；
      若 10s 内没 PetItem -> 回 A，跳过本次对战进入下一轮稳态扫描。
    - 新增：命中目标准备入战时，先快速点“我不在的那个点”(A<->B)，再快速点目标点位。
    """

    # 对话框探针 & 普通确认
    KEY_PROBE_WHITE = "对话框.通用探针"
    KEY_PROBE_BLUE = "对话框.普通确认探针"
    KEY_NORMAL_CONFIRM = "对话框.普通确认"

    # 进入地图的通用 npc 探针
    KEY_NEWNPC_MULTI = "/resource/newNpc/multi/0.swf"
    
    # Login.swf token（用于检测断线重连）
    TOKEN_LOGIN_SWF = "/login/Login.swf"

    # fightResource pet swf
    _FIGHT_PET_SWF_RE = re.compile(r"/resource/fightResource/pet/swf/(\d+)\.swf")
    # pet swf and sound (for detecting target pet after fightresources)
    _PET_SWF_RE = re.compile(r"/resource/pet/swf/(\d+)\.swf")
    _PET_SOUND_RE = re.compile(r"/resource/pet/sound/(\d+)\.mp3")
    _MAP_SWF_RE = re.compile(r"/resource/map/(\d+)\.swf")

    # kernel poll 节流
    KERNEL_POLL_SEC = 0.05

    def __init__(self, bot, regions: RegionStore, battle_runner, map_swf_id: int = 11):
        self.bot = bot
        self.regions = regions
        self.battle_runner = battle_runner
        self.default_map_swf_id = map_swf_id

        self._baseline: Dict[str, List[Tuple[int, int, int]]] = {}
        self._jitter: Dict[str, float] = {}
        self._threshold: Dict[str, float] = {}
        
        # ❌ DISABLED: 长期像素数据记录（用于持续记录和保存九个区域的像素数据）
        # ❌ 已摒弃使用长期基线的想法，持续点击稀有精灵即可
        # 格式: {region_key: {'mean_sig': List[Tuple[int, int, int]], 'sample_count': int, 'last_updated': float}}
        # self._long_term_baseline: Dict[str, Dict] = {}
        # self._baseline_data_dir: str = os.path.join(getattr(self.bot, "project_root", os.getcwd()), "baseline_data")
        # os.makedirs(self._baseline_data_dir, exist_ok=True)

        self._current_pos: Optional[Tuple[float, float]] = None

        # ✅ 记录"当前锚点"是 A 还是 B（只在你点击 A/B 时更新）
        self._last_anchor: Optional[str] = None  # 'A' or 'B'
        
        # ✅ 统一框架集成
        self._battle_count = 0  # 战斗计数
        
        # ✅ 按任务类型分开的捕捉统计（使用profile.name作为key）
        # 格式: {task_name: {"total": int, "success": int}}
        self._task_stats: Dict[str, Dict[str, int]] = {}
        
        # ✅ 延迟触发机制：检测到突变但无MP3时，保留1秒，如果1秒内出现MP3则触发
        # 格式: (hit_key, hit_reg, diff, timestamp) 或 None
        self._pending_mutation: Optional[Tuple[str, Region, float, float]] = None
        
        # ✅ 尼奥模式校准记录：记录点击触发校准的点以及后续入站结果
        # 格式: List[{"point_key": str, "point_xy": (x, y), "calibration_success": bool, "entry_result": "success"/"timeout"/"failed"}]
        self._nieo_calibration_records: List[Dict[str, Any]] = []
        
        # ✅ 尼奥模式校准后放弃的点列表（校准成功后直接放弃，不继续点击）
        self._nieo_calibration_abort_points: List[str] = ["尼奥一.2", "尼奥二.2", "尼奥二.8"]
        
        # ✅ 尼奥模式时间测量：fightpetswf到PetItem的时间间隔
        self._nieo_swf_to_petitem_min_time: Optional[float] = None  # 最小值（秒）
        self._nieo_swf_to_petitem_current_time: Optional[float] = None  # 当前战斗的时间（秒）
        self._nieo_swf_to_petitem_swf_time: Optional[float] = None  # fightpetswf检测时间
        self._nieo_should_stop_after_battle: bool = False  # 是否需要在战斗结束后停止
        self._nieo_mode_start_time: Optional[float] = None  # 尼奥模式开始时间
        
        # 状态标记：用于定时任务打断
        self._is_scanning_steady_state = False  # 是否正在扫描稳态
        self._is_in_battle = False  # 是否在战斗中
        self._is_recovering = False  # 是否在恢复中
        self._stop_1and1_monitoring = False  # 是否停止1AND1监控（检测到MP3后停止）
        self._should_restart_after_reconnect = False  # 重连后是否应该重新启动任务（用于螳螂、小豆芽、嘟咕噜和双塔模式）
        self._reconnect_scripts_executing = False  # 是否正在执行重连脚本
        
        # 尼尔家族切换精灵计数器（用于测试模式的轮流切换）
        self._nie_switch_counter = 0  # 0表示下次切换精灵二，1表示下次切换精灵三
        self._xiaodouya_nie_test_mode = False  # 小豆芽尼尔测试模式标志
        
        # 记录最后一次战斗的尼尔家族信息（用于恢复逻辑）
        self._last_nie_family_id: Optional[int] = None  # 最后一次战斗的尼尔家族ID（None表示没有尼尔家族）
        
        # 立即收集到的pet IDs（在检测到fightResource/pet/swf/时收集）
        self._immediate_collected_pet_ids: Optional[set] = None
        
        # 当前战斗的尼尔家族ID（用于在stage2和stage3之间传递信息，不再使用pattern_valid）
        self._current_battle_nie_family_id: Optional[int] = None
        
        # 探针扫描结果（用于战斗中切换精灵）
        self._flash_aifeia_pos: Optional[str] = None  # 闪光艾菲亚的位置（"二"或"三"）
        
        # 电池状态相关
        self._battery_status_before_battle: Optional[int] = None  # 入站前最后一个电池状态
        self._battery_status_after_battle: Optional[int] = None  # 战后的电池状态
        self._battery_test_mode = True  # 测试模式：1 to 1 触发重连；正式模式改为 False：1 to 0 触发重连
        self._should_refresh_reconnect = False  # 是否需要刷新重连
        
        # 敌方信息监控：最后一次测得的等级和血量（用于双塔模式逃跑判断）
        self._last_enemy_level: Optional[int] = None
        self._last_enemy_hp: Optional[int] = None
        self._shuangta_should_escape: bool = False  # 双塔模式是否需要逃跑
        self._dugulu_should_escape: bool = False  # 嘟咕噜模式是否需要逃跑
        self._dugulu_ocr_failed: bool = False  # 嘟咕噜模式OCR识别是否失败
        self._aisifeige_pos: Optional[str] = None  # 艾斯菲格的位置（"二"或"三"）
        
        self._unified_framework = None
        self._wild_adapter = None
        self._init_unified_framework()
        
        # ❌ DISABLED: 持续记录基线数据的后台线程（已摒弃使用长期基线的想法）
        # self._baseline_recording_thread: Optional[threading.Thread] = None
        # self._baseline_recording_stop_event: Optional[threading.Event] = None
    
    def _init_unified_framework(self):
        """初始化统一框架"""
        try:
            from core.unified_battle_framework import UnifiedBattleFramework
            from core.wild_mode_adapter import WildModeAdapter
            
            from config import TEMPLATES_PATH
            self._unified_framework = UnifiedBattleFramework(self.bot, self.regions, TEMPLATES_PATH)
            self._wild_adapter = WildModeAdapter(self._unified_framework)
            self._emit("✅ 统一框架已初始化（野外模式）", "DEBUG")
        except Exception as e:
            self._emit(f"⚠️ 统一框架初始化失败，使用旧实现: {e}", "WARN")
            self._unified_framework = None
            self._wild_adapter = None

    # =========================
    # kernel helpers
    # =========================
    def _fetch_kernel(self, cursor: int) -> Tuple[int, List[str]]:
        """
        统一 fetch_kernel_since 的返回形态：
        - (new_cursor, lines)
        - lines
        - rows[(seq, ts, line)]
        """
        try:
            res: Any = fetch_kernel_since(cursor)
        except Exception:
            return kernel_cursor(), []

        if isinstance(res, tuple) and len(res) == 2 and isinstance(res[0], int):
            new_cursor, lines = res
            return int(new_cursor), self._coerce_lines(lines)

        return kernel_cursor(), self._coerce_lines(res)

    @staticmethod
    def _coerce_lines(obj: Any) -> List[str]:
        if obj is None:
            return []
        if isinstance(obj, str):
            return [obj]
        if isinstance(obj, list):
            if not obj:
                return []
            if isinstance(obj[0], tuple) and len(obj[0]) >= 3:
                out: List[str] = []
                for t in obj:
                    try:
                        out.append(str(t[-1]))
                    except Exception:
                        pass
                return out
            return [str(x) for x in obj]
        try:
            return [str(x) for x in obj]
        except Exception:
            return []

    def _handle_nieo_time_stop(self, nieo_stats: Dict[str, int]):
        """
        处理尼奥模式时间测量停止逻辑
        输出统计信息并停止切图
        """
        self._emit("⛔ [时间测量] 检测到异常时间间隔，停止切图", "SYSTEM")
        
        # 计算总运行时长
        if self._nieo_mode_start_time is not None:
            total_duration = time.time() - self._nieo_mode_start_time
            total_duration_str = f"{total_duration:.1f}秒"
        else:
            total_duration_str = "未知"
        
        # 输出统计信息
        self._emit(f"📊 [时间测量统计] fightpetswf到PetItem时间间隔：", "SYSTEM")
        if self._nieo_swf_to_petitem_min_time is not None:
            self._emit(f"   最小值: {self._nieo_swf_to_petitem_min_time:.3f}秒", "INFO")
        else:
            self._emit(f"   最小值: 未记录", "INFO")
        
        if self._nieo_swf_to_petitem_current_time is not None:
            self._emit(f"   当前值: {self._nieo_swf_to_petitem_current_time:.3f}秒", "INFO")
        else:
            self._emit(f"   当前值: 未记录", "INFO")
        
        self._emit(f"   尼奥模式运行总时长: {total_duration_str}", "INFO")
        
        # 输出战斗统计
        self._emit("📊 尼奥模式战斗统计：", "SYSTEM")
        self._emit(f"   普通逃跑：{nieo_stats['普通逃跑']}", "INFO")
        self._emit(f"   稀有捕捉：{nieo_stats['稀有捕捉']}（108布鲁：{nieo_stats['108捕捉']}）", "INFO")
        self._emit(f"   尼尔家族：{nieo_stats['尼尔家族']}（77：{nieo_stats['77捕捉']}，310：{nieo_stats['310捕捉']}，416：{nieo_stats['416捕捉']}）", "INFO")
        
        # TODO: 执行刷新操作（在TODO运转起来之前采取停止运行脚本行动）
        self._emit("⏸️ [TODO] 刷新操作（当前采取停止运行脚本行动）", "WARN")
        # 停止运行脚本（通过设置stop_current标志）
        if hasattr(self.bot, "stop_current"):
            self.bot.stop_current = True

    # ---------------------------
    # public
    # ---------------------------
    def run_nie_family_test(
        self,
        stop_event: threading.Event,
        use_foreground: bool,
        nie_family_id: int,  # 77/310 for 尼尔, 416 for 尼奥
    ) -> None:
        """
        尼尔家族测试模式：等待用户手动触发对战，然后执行一次完整对战流程
        
        Args:
            nie_family_id: 77/310 for 尼尔（第二回合切精灵三），416 for 尼奥（第二回合切精灵二）
        """
        # 强制输出，确保函数被调用
        import sys
        print(f"[DEBUG] run_nie_family_test called: nie_family_id={nie_family_id}", file=sys.stderr, flush=True)
        
        try:
            print(f"[DEBUG] About to call _emit", file=sys.stderr, flush=True)
            self._emit(f"🧪 尼尔家族测试模式启动（nie_family_id={nie_family_id}）", "SYSTEM")
            print(f"[DEBUG] First _emit called", file=sys.stderr, flush=True)
            self._emit("📝 等待用户手动发起对战...", "INFO")
            
            if not hasattr(self.battle_runner, "run_mantis_capture_mode"):
                self._emit(f"❌ battle_runner 类型错误：{type(self.battle_runner)}（应为 BattleRunner）", "ERROR")
                return

            self._emit("✅ battle_runner检查通过", "INFO")
            
            self._emit("🔍 正在检查游戏窗口...", "INFO")
            if not window_manager.ensure_game_hwnd():
                self._emit("❌ 未检测到游戏窗口：请先在 Dashboard 点【启动游戏】", "ERROR")
                return
            
            self._emit("✅ 游戏窗口检查通过", "INFO")
            
            # 使用一个虚拟的profile（用于恢复逻辑）
            self._emit("🔍 正在加载profile...", "INFO")
            from core.dar_route_runner import DEFAULT_PROFILE_DUGULU
            profile = DEFAULT_PROFILE_DUGULU
            self._emit("✅ profile加载完成", "INFO")
            
            # 在测试模式启动时扫描探针，识别闪光艾菲亚和艾斯菲格的位置（用于战斗中的切换逻辑）
            self._emit("🔍 开始扫描探针，识别闪光艾菲亚和艾斯菲格的位置（用于战斗中切换）", "INFO")
            flash_aifeia_pos, aisifeige_pos = self._scan_pet_probes_to_identify_pets(use_foreground)
            self._flash_aifeia_pos = flash_aifeia_pos
            self._aisifeige_pos = aisifeige_pos
            if flash_aifeia_pos and aisifeige_pos:
                self._emit(f"✅ 探针扫描完成：闪光艾菲亚=精灵{flash_aifeia_pos}，艾斯菲格=精灵{aisifeige_pos}（将在战斗中使用）", "SUCCESS")
        except Exception as e:
            self._emit(f"❌ 初始化阶段异常: {e}", "ERROR")
            import traceback
            self._emit(f"📋 异常详情: {traceback.format_exc()}", "ERROR")
            return
        
        try:
            # 1. 等待用户手动发起对战（等待检测到fightResource/pet/swf/或PetItem信号）
            self._emit("⏳ 等待检测到fightResource/pet/swf/或PetItem信号...", "INFO")
            
            from core.logger import fetch_kernel_since, kernel_cursor
            initial_cursor = kernel_cursor()
            self._emit(f"🔍 初始cursor: {initial_cursor}（开始监听内核日志）", "INFO")
            timeout_s = 60.0  # 最多等待60秒
            start_time = time.time()
            
            TOKEN_FIGHT_PET = "/resource/fightResource/pet/swf/"
            TOKEN_PETITEM = "/resource/item/petItem/icon/"
            battle_triggered = False
            petitem_already_detected = False  # 标记是否已经检测到PetItem
            
            last_log_time = time.time()  # 用于节流调试日志
            self._emit("🔍 进入等待循环...", "INFO")
            loop_count = 0  # 用于调试：统计循环次数
            while (time.time() - start_time) < timeout_s:
                # 优先检查停止信号（每个循环开始时检查）
                if stop_event.is_set():
                    self._emit("⛔ 测试已停止（stop_event）", "WARN")
                    return
                if getattr(self.bot, "stop_current", False):
                    self._emit("⛔ 测试已停止（stop_current）", "WARN")
                    return
                
                loop_count += 1
                if loop_count == 1:
                    self._emit("✅ 循环已启动，开始检测信号...", "INFO")
                elif loop_count % 100 == 0:  # 每100次循环输出一次
                    elapsed = time.time() - start_time
                    self._emit(f"🔍 循环运行中...（已循环{loop_count}次，已等待{elapsed:.1f}秒）", "INFO")
                
                try:
                    current_cursor = kernel_cursor()
                    lines = fetch_kernel_since(initial_cursor)
                    
                    if isinstance(lines, list) and len(lines) > 0:
                        # 节流：每5秒输出一次调试信息
                        now = time.time()
                        if now - last_log_time >= 5.0:
                            self._emit(f"🔍 检查到{len(lines)}条新日志（cursor={initial_cursor}->{current_cursor}）", "INFO")
                            # 输出前3条日志的前100个字符作为示例
                            for i, line in enumerate(lines[:3]):
                                line_preview = str(line)[:100] if len(str(line)) > 100 else str(line)
                                self._emit(f"   日志示例{i+1}: {line_preview}", "INFO")
                            last_log_time = now
                        
                        for idx, line in enumerate(lines):
                            line_str = str(line)
                            # 检查PetItem信号（最高优先级）
                            if TOKEN_PETITEM in line_str:
                                self._emit(f"✅ 检测到PetItem信号（已入战），开始执行测试流程\n日志行: {line_str[:200]}", "SUCCESS")
                                battle_triggered = True
                                petitem_already_detected = True  # 标记已检测到PetItem
                                # 立即收集pet IDs（从当前行开始向后搜索）
                                collected_pet_ids = self._collect_fight_pet_ids_immediate(stop_event, current_lines=lines, start_index=idx)
                                if collected_pet_ids:
                                    self._immediate_collected_pet_ids = collected_pet_ids
                                break
                            # 检查fightResource/pet/swf/信号
                            elif TOKEN_FIGHT_PET in line_str and not battle_triggered:
                                # 检测到fightResource/pet/swf/，说明对战已经开始，退出循环进入Stage 2等待PetItem
                                self._emit(f"✅ 检测到fightResource/pet/swf/信号（对战已开始），退出循环，将在Stage 2等待PetItem\n日志行: {line_str[:200]}", "INFO")
                                battle_triggered = True
                                # 立即收集pet IDs（从当前行开始向后搜索所有连续的pet/swf）
                                collected_pet_ids = self._collect_fight_pet_ids_immediate(stop_event, current_lines=lines, start_index=idx)
                                if collected_pet_ids:
                                    self._immediate_collected_pet_ids = collected_pet_ids
                                break
                        
                        # 如果处理了日志但没有检测到信号，更新cursor继续下次循环
                        if not battle_triggered:
                            initial_cursor = current_cursor
                    elif isinstance(lines, list) and len(lines) == 0:
                        # 没有新日志，但也要更新cursor（避免永远停留在旧的cursor）
                        # 节流：每5秒输出一次等待状态
                        now = time.time()
                        if now - last_log_time >= 5.0:
                            elapsed = now - start_time
                            self._emit(f"⏳ 仍在等待对战信号...（已等待{elapsed:.1f}秒，cursor={initial_cursor}->{current_cursor}）", "INFO")
                            last_log_time = now
                        initial_cursor = current_cursor  # 更新cursor，下次检查新日志
                    else:
                        # lines不是列表，也更新cursor
                        initial_cursor = current_cursor
                    
                    if battle_triggered:
                        # 检测到任何信号都退出循环（PetItem直接进入战斗，fightResource/pet/swf/进入Stage 2）
                        break
                except Exception as e:
                    # 输出异常信息以便调试
                    now = time.time()
                    if now - last_log_time >= 5.0:
                        self._emit(f"⚠️ 检测信号时发生异常: {e}", "WARN")
                        last_log_time = now
                    initial_cursor = kernel_cursor()  # 异常时也更新cursor
                
                time.sleep(0.1)
            
            if not battle_triggered:
                self._emit("⏱️ 等待对战信号超时（60秒）", "WARN")
                return
            
            # 2. 立即收集pet IDs并标注类型（如果之前已经收集过，直接使用）
            self._emit("📋 开始收集fight pet IDs...", "INFO")
            pet_ids = self._immediate_collected_pet_ids  # 使用之前已经收集到的pet IDs
            if not pet_ids:
                # 如果没有立即收集到，尝试再次收集（这种情况不应该发生，但保留作为后备）
                pet_ids = self._collect_fight_pet_ids(timeout=4.5, collect_window=0.55, stop_event=stop_event)
            
            if pet_ids:
                # 标注类型并输出
                self._classify_and_log_pet_ids(pet_ids, profile)
            else:
                self._emit(f"⚠️ 未收集到pet IDs，继续执行测试（测试模式下不依赖pattern检测）", "WARN")
                pet_ids = set()
            
            # 3. 等待校准和PetItem（使用统一框架的Stage 2）
            self._emit("🔍 等待校准和PetItem信号（如果已经入战则直接继续）...", "INFO")
            
            # 创建一个虚拟的reg_a用于恢复逻辑
            try:
                reg_a = self.regions.require("地图.11.A")  # 使用一个默认的A点
            except KeyError:
                try:
                    reg_a = self.regions.require("嘟咕噜.A")
                except KeyError:
                    self._emit("❌ 找不到A点区域，无法执行测试", "ERROR")
                    return
            
            route_points = []  # 测试模式下不需要route_points
            
            # 调用统一框架的Stage 2（等待校准和PetItem）
            if self._unified_framework:
                from core.unified_battle_framework import BattleConfig, BattleMode
                
                # 创建动作回调（尼尔家族逻辑）
                def action_callback(round_idx: int) -> str:
                    if round_idx == 1:
                        return "skill"  # 第一回合使用技能一
                    elif round_idx == 2:
                        # 第二回合切换精灵
                        pet_num = 3 if nie_family_id in (77, 310) else 2  # 77/310切精灵三，416切精灵二
                        self._switch_pet_for_nie_family(
                            nie_family_id, use_foreground, stop_event, test_mode=False
                        )
                        return "switch"
                    else:
                        # 第三回合后只使用高级胶囊
                        return "capsule_high"
                
                # 创建配置
                config = BattleConfig(
                    mode=BattleMode.WILD,
                    use_foreground=use_foreground,
                    skill_key="对战.使用技能一",
                    action_callback=action_callback,
                )
                
                # 3. 如果已经检测到PetItem，直接进入战斗循环；否则调用Stage 2等待PetItem
                if petitem_already_detected:
                    self._emit("✅ 已检测到PetItem，直接进入战斗循环", "INFO")
                    # 立即执行第一回合动作
                    if config and config.action_callback:
                        try:
                            action_type = config.action_callback(1)
                            self._unified_framework._execute_action(action_type, config, round_idx=1)
                            self._emit(f"✅ 第一回合动作已执行（{action_type}）", "SUCCESS")
                        except Exception as e:
                            self._emit(f"⚠️ 执行第一回合动作失败: {e}", "WARN")
                    success = True
                else:
                    # 调用Stage 2（等待校准和PetItem，检测到PetItem时立即执行第一回合）
                    self._emit("🔍 等待PetItem信号（使用Stage 2）...", "INFO")
                    # 记录当前cursor，以便stage2检查此cursor之后的所有新日志
                    stage2_cursor = kernel_cursor()
                    success, calib_result = self._unified_framework.stage2_calibration_and_petitem(
                        trigger_callback=None,  # 不需要trigger
                        use_foreground=use_foreground,
                        timeout_s=15.0,  # 增加超时时间到15秒
                        skip_stage1=True,  # 跳过Stage 1
                        config=config,
                        initial_cursor=stage2_cursor,  # 传入cursor，确保检查所有新日志
                    )
                    
                    if not success:
                        self._emit("❌ Stage 2失败（未检测到PetItem或校准失败）", "ERROR")
                        return
                
                # 4. 执行战斗循环（Stage 3）
                self._emit("⚔️ 开始战斗循环...", "INFO")
                self._is_in_battle = True
                
                battle_success = self._unified_framework.stage3_battle_loop(
                    config=config,
                )
                
                if not battle_success:
                    self._emit("❌ 战斗循环失败", "ERROR")
                    return
                
                # 5. 战斗结束处理（Stage 4）
                self._emit("🎬 战斗结束处理...", "INFO")
                post_success = self._unified_framework.stage4_post_battle(
                    config=config,
                    is_training_room=False,
                    is_hero_tower=False,
                )
                
                if not post_success:
                    self._emit("❌ 战斗结束处理失败", "ERROR")
                    return
                
                # 6. 等待战后延迟（与_post_battle_cleanup保持一致）
                self._emit("⏳ 等待战后延迟...", "INFO")
                self._sleep_abortable(stop_event, profile.post_battle_delay_sec)
                
                # 7. 清理对话框（使用1AND1检测）
                self._emit("🧹 战后清理对话框", "SYSTEM")
                self._clear_dialogs_by_probes(use_foreground, stop_event=stop_event)
                
                # 8. 执行战后恢复（放回仓库和恢复）
                self._emit("💊 执行战后恢复（放回仓库和恢复精灵）...", "INFO")
                self._is_recovering = True
                self._last_nie_family_id = nie_family_id
                self._recover_pets(use_foreground, stop_event, skip_return_storage=False, nie_family_id=nie_family_id, profile=profile)
                self._is_recovering = False
                self._last_nie_family_id = None
                
                self._emit("✅ 测试完成！", "SUCCESS")
            else:
                self._emit("❌ 统一框架未初始化，无法执行测试", "ERROR")
                
        except Exception as e:
            self._emit(f"❌ 测试异常: {e}", "ERROR")
            import traceback
            self._emit(f"📋 异常详情: {traceback.format_exc()}", "ERROR")
        finally:
            self._is_in_battle = False
            self._is_scanning_steady_state = False

    def _wait_for_map_id(self, map_id: int, stop_event: threading.Event, timeout_s: float = 30.0, white_probe_key: Optional[str] = None) -> bool:
        """
        等待进入指定地图ID（检测map信号和newNpc信号）
        
        Args:
            map_id: 目标地图ID（10特殊处理：使用白色探针代替newNPC）
            stop_event: 停止事件
            timeout_s: 超时时间（秒）
            white_probe_key: 白色探针的键名（仅用于地图10，如果为None会自动检测）
            
        Returns:
            True=成功进入地图，False=超时或停止
        """
        if stop_event.is_set() or getattr(self.bot, "stop_current", False):
            return False
        
        map_signal = f"/resource/map/{map_id}.swf"
        
        # 特殊处理：地图10不会输出newNPC信号，需要使用白色探针代替
        if map_id == 10:
            self._emit(f"⏳ 等待进入地图{map_id}：{map_signal} + 白色探针检测...", "SYSTEM")
        else:
            self._emit(f"⏳ 等待进入地图{map_id}：{map_signal} + newNpc...", "SYSTEM")
        
        # 等待map信号
        if not self._wait_kernel_contains_with_abort(
            map_signal,
            timeout_s=timeout_s,
            poll=0.05,
            stop_event=stop_event
        ):
            return False
        
        if stop_event.is_set() or getattr(self.bot, "stop_current", False):
            return False
        
        # ✅ 切换地图仍然需要 map+NPC（10号地图白色探针只在切地图时使用）
        # 地图10特殊处理：使用白色探针代替newNPC信号
        if map_id == 10:
            # 高频检测白色探针，直到不是纯白色（表示newNPC已出现）
            if not self._wait_for_white_probe_non_white(stop_event, timeout_s=timeout_s, white_probe_key=white_probe_key):
                return False
        else:
            # 等待newNpc信号
            if not self._wait_kernel_contains_with_abort(
                self.KEY_NEWNPC_MULTI,
                timeout_s=timeout_s,
                poll=0.05,
                stop_event=stop_event
            ):
                return False
        
        self._emit(f"✅ 已进入地图{map_id}", "SUCCESS")
        return True
    
    def _wait_for_white_probe_non_white(self, stop_event: threading.Event, timeout_s: float = 30.0, white_probe_key: Optional[str] = None) -> bool:
        """
        等待白色探针变为非纯白色（表示newNPC已出现）
        
        Args:
            stop_event: 停止事件
            timeout_s: 超时时间（秒）
            white_probe_key: 白色探针的键名（如果为None，会尝试多个可能的键）
            
        Returns:
            True=检测到非白色（newNPC已出现），False=超时或停止
        """
        # 如果没有指定白色探针键，尝试多个可能的键
        if white_probe_key is None:
            # 尝试闪光皮皮.白色探针（优先，因为闪光皮皮模式使用）
            if self.regions.get("闪光皮皮.白色探针"):
                white_probe_key = "闪光皮皮.白色探针"
            # 否则尝试尼奥一.白色探针（用于尼奥模式）
            elif self.regions.get("尼奥一.白色探针"):
                white_probe_key = "尼奥一.白色探针"
            else:
                self._emit("⚠️ 未找到白色探针区域（尝试了：闪光皮皮.白色探针、尼奥一.白色探针）", "WARN")
                return False
        start_time = time.time()
        poll_interval = 0.05  # 高频检测（50ms一次）
        
        self._emit(f"🔍 开始检测白色探针（等待非纯白色，表示newNPC已出现）...", "INFO")
        
        while (time.time() - start_time) < timeout_s:
            if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                return False
            
            # 检测白色探针是否为纯白色
            try:
                reg_white = self.regions.get(white_probe_key)
                if not reg_white:
                    self._emit(f"⚠️ 未找到白色探针区域：{white_probe_key}", "WARN")
                    # 如果找不到探针，等待一下后继续
                    time.sleep(poll_interval)
                    continue
                
                # 检查是否为纯白色（FFFFFF，RGB=(255,255,255)）
                is_white = self._check_white_probe_pure_white(reg_white)
                
                if not is_white:
                    # 不是纯白色，表示newNPC已出现
                    self._emit(f"✅ 白色探针已变为非纯白色（newNPC已出现）", "SUCCESS")
                    return True
                # 仍然是纯白色，继续等待
                
            except Exception as e:
                self._emit(f"⚠️ 检测白色探针异常: {e}", "WARN")
            
            time.sleep(poll_interval)
        
        self._emit(f"⏱️ 等待白色探针超时（{timeout_s}s）", "WARN")
        return False
    
    def _check_white_probe_pure_white(self, reg: Region) -> bool:
        """
        检测白色探针是否为纯白色（FFFFFF，RGB=(255,255,255)）
        
        Args:
            reg: 白色探针区域
            
        Returns:
            True=是纯白色（newNPC未出现），False=不是纯白色（newNPC已出现）
        """
        try:
            gx1, gy1, gx2, gy2 = reg.outer_bbox()
            img = window_manager.grab_game_bbox(gx1, gy1, gx2, gy2)
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
            
            # 如果超过80%的像素是纯白色，认为是纯白色
            white_ratio = white_count / len(pixels)
            return white_ratio >= 0.8
        except Exception:
            return False

    def run_nieo_mode(
        self,
        stop_event: threading.Event,
        use_foreground: bool,
        test_nieo: bool = False,  # 测试尼奥模式：普通精灵时第一回合切换，第二回合逃跑
        test_nie: bool = False,  # 测试尼尔模式：普通精灵时第一回合切换，第二回合逃跑
        skip_nie_77: bool = False,  # 不捕捉尼尔（77执行逃跑，310/416正常捕捉）
    ) -> None:
        """
        尼奥模式：10/11地图循环模式
        
        流程：
        1. 在11号地图执行恢复精灵一（首次恢复时会进行探针扫描：先恢复→1AND1→扫描探针→关闭背包）
        2. 点击地图/10.json进入10号地图，等待10号地图+newNPC信号
        3. 根据当前地图ID（10或11）使用对应的前缀（"尼奥一"或"尼奥二"）
        4. 点击Z点，开始burst扫描9个点
        5. 检测3个变化点，选择最合适的点进入战斗
        6. 根据战斗精灵数量执行不同策略（4只普通逃跑、4只稀有捕捉、5只尼尔家族战斗）
        7. 战后清理和恢复
        8. 切换地图（10<->11）
        9. 循环往复
        
        注意：
        - 所有10号地图相关的区域在"尼奥一"
        - 所有11号地图相关的区域在"尼奥二"
        - 10到11：尼奥一.to二
        - 11到10：尼奥二.to一
        """
        try:
            self._emit("🌊 尼奥模式启动（10/11地图循环）", "SYSTEM")
            
            # ✅ 清空校准记录列表（每次运行都是新的记录）
            self._nieo_calibration_records.clear()
            
            # ✅ 初始化时间测量变量
            self._nieo_swf_to_petitem_min_time = None
            self._nieo_swf_to_petitem_current_time = None
            self._nieo_swf_to_petitem_swf_time = None
            self._nieo_should_stop_after_battle = False
            self._nieo_mode_start_time = time.time()
            
            # 检查和补齐缺失的swf文件
            self._check_and_fill_missing_swf_files()
            
            if not window_manager.ensure_game_hwnd():
                self._emit("❌ 未检测到游戏窗口：请先在 Dashboard 点【启动游戏】", "ERROR")
                return
            
            # 注意：探针扫描将在第一次恢复流程中（1AND1结束后）进行，不在这里扫描
            # 1. 在11号地图执行恢复精灵一（首次恢复时会进行探针扫描）
            self._emit("💊 在11号地图执行恢复流程（恢复精灵一）", "SYSTEM")
            self._is_recovering = True
            # 使用一个虚拟的profile来调用恢复逻辑（只恢复精灵一，不涉及放回仓库）
            from core.dar_route_runner import DEFAULT_PROFILE_DUGULU
            temp_profile = DEFAULT_PROFILE_DUGULU
            self._recover_pets(use_foreground, stop_event, skip_return_storage=True, nie_family_id=None, profile=temp_profile)
            self._is_recovering = False
            
            # 2. 点击地图/10.json进入10号地图，等待10号地图+newNPC信号
            self._emit("🗺️ 点击地图/10.json进入10号地图", "SYSTEM")
            if not self._execute_map_entry_script(10, use_foreground, stop_event):
                self._emit("⚠️ 地图/10.json执行失败，尝试继续", "WARN")
            
            if self.bot.stop_current or stop_event.is_set():
                return
            
            # 等待10号地图+newNPC信号
            if not self._wait_for_map_id(10, stop_event, timeout_s=30.0):
                self._emit("⛔ 等待10号地图+newNPC信号超时/已停止", "WARN")
                return
            
            # 5. 初始化统一框架（如果还没有初始化）
            if not self._unified_framework:
                from core.unified_battle_framework import UnifiedBattleFramework
                from config import TEMPLATES_PATH
                self._unified_framework = UnifiedBattleFramework(self.bot, self.regions, TEMPLATES_PATH)
            
            if not self._wild_adapter:
                from core.wild_mode_adapter import WildModeAdapter
                self._wild_adapter = WildModeAdapter(self._unified_framework)
            
            # 统计信息
            nieo_stats = {
                "普通逃跑": 0,  # 4只：3只我方+33/198
                "稀有捕捉": 0,  # 4只：3只我方+108
                "尼尔家族": 0,  # 5只：3只我方+地图精灵+尼尔家族
                "108捕捉": 0,  # 108布鲁捕捉成功数
                "77捕捉": 0,   # 77尼尔捕捉成功数
                "310捕捉": 0,  # 310闪光尼尔捕捉成功数
                "416捕捉": 0,  # 416尼奥捕捉成功数
            }
            
            
            # 当前地图ID（从10开始，启动前在11，然后点击10.json进入10）
            current_map_id = 10
            current_prefix = "尼奥一"  # 10号地图使用"尼奥一"
            
            # 主循环：10/11地图循环
            while not stop_event.is_set() and not getattr(self.bot, "stop_current", False):
                self._wait_if_paused(stop_event)
                
                # 根据当前地图ID确定前缀
                if current_map_id == 10:
                    current_prefix = "尼奥一"
                else:  # 11
                    current_prefix = "尼奥二"
                
                self._emit(f"🗺️ 当前地图：{current_map_id}，使用前缀：{current_prefix}", "INFO")
                
                # 解析路线点（1-9）和Z点（尼奥模式不需要B点，使用Z点代替原来的A点）
                try:
                    route_points, reg_z, _ = self._resolve_route_regions(current_prefix, require_b=False, use_z=True)
                except KeyError as e:
                    self._emit(f"❌ 解析路线失败：{e}", "ERROR")
                    break
                
                # 计算并排序扫描顺序（基于距离），并排除最远的2个点，只保留7个点
                # 对于map10（尼奥一）：按到"尼奥一.to二"的距离排序，排除最远的2个点
                # 对于map11（尼奥二）：按到"尼奥二.to一"的距离排序，排除最远的2个点
                route_points_sorted = self._sort_route_points_by_distance(
                    route_points, current_prefix, current_map_id, reg_z
                )
                self._emit(f"📐 扫描顺序已优化（基于距离，排除最远2个点，只扫描7个点）", "INFO")
                
                # 点击Z点（离开刷新点）- 等待0.3s后双击
                self._emit(f"🧭 点击{current_prefix}.Z点（离开刷新点，等待0.3s后双击）", "SYSTEM")
                self._sleep_abortable(stop_event, 0.3)
                self._click_region_twice(reg_z, use_foreground, gap=0.06)
                self._current_pos = self._region_center(reg_z)
                self._sleep_abortable(stop_event, 1.0)
                
                # 标定基线（只标定7个点，排除最远的2个点）
                self._emit(f"📏 标定{current_prefix}的7点基线（排除最远2个点）", "SYSTEM")
                baseline_start_time = time.time()
                self._recalibrate_all(route_points_sorted, stop_event, map_id=current_map_id)
                baseline_duration = time.time() - baseline_start_time
                self._emit(f"✅ 基线标定完成（耗时{baseline_duration:.1f}s）", "SUCCESS")
                
                # 等待至少3秒后才开始burst扫描（确保基线稳定）
                min_wait_after_baseline = 3.0
                if baseline_duration < min_wait_after_baseline:
                    wait_time = min_wait_after_baseline - baseline_duration
                    self._emit(f"⏳ 等待{wait_time:.1f}s后开始burst扫描（确保基线稳定）", "INFO")
                    self._sleep_abortable(stop_event, wait_time)
                
                # 标记进入稳态扫描阶段
                self._is_scanning_steady_state = True
                
                # ✅ 新地图下的尼奥模式：不检测稀有精灵，不等待mp3，直接扫描变化点并触发战斗
                self._emit("🔍 开始扫描，寻找变化点（不等待mp3）...", "SYSTEM")
                max_scan_duration = 60.0  # 最多扫描60秒
                scan_start_time = time.time()
                
                # ✅ 优化策略：找到第一个变化点后，继续扫描剩余点，选择最近的变化点
                first_change_idx = None  # 第一个变化点的索引
                first_change_key = None
                first_change_reg = None
                first_change_diff = 0.0
                selected_key = None
                selected_reg = None
                change_found = False
                
                # 获取参考点（用于距离计算）
                if current_map_id == 10:  # 尼奥一
                    reference_key = f"{current_prefix}.to二"
                else:  # 尼奥二
                    reference_key = f"{current_prefix}.to一"
                reference_reg = self.regions.get(reference_key)
                if reference_reg:
                    reference_center = self._region_center(reference_reg)
                else:
                    # 如果找不到参考点，使用Z点作为参考
                    reference_center = self._region_center(reg_z)
                
                while not change_found:
                    if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                        break
                    
                    # 检查超时
                    if (time.time() - scan_start_time) > max_scan_duration:
                        self._emit(f"⏱️ 扫描超时（{max_scan_duration}s），未找到变化点", "WARN")
                        break
                    
                    # 快速遍历7个点，找到第一个变化点
                    for idx, (key, reg) in enumerate(route_points_sorted):
                        if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                            break
                        
                        base = self._baseline.get(key)
                        if base is None:
                            continue
                        
                        sig = self._grab_sig(reg, downsample=(8, 8))
                        diff = self._sig_diff(sig, base)
                        th = self._threshold.get(key, 9999.0)
                        
                        if diff >= th:
                            # 找到第一个变化点
                            if first_change_idx is None:
                                first_change_idx = idx
                                first_change_key = key
                                first_change_reg = reg
                                first_change_diff = diff
                                first_change_center = self._region_center(reg)
                                first_change_dist = self._dist2(first_change_center, reference_center)
                                self._emit(f"🎯 检测到第一个变化点：{key}（diff={diff:.2f}，索引{idx}），继续扫描剩余点寻找更近的点...", "SUCCESS")
                                
                                # ✅ 继续快速扫描前面的点（由近及远），寻找更近的变化点
                                # 注意：route_points_sorted是从近到远排序的，所以更近的点在索引0到idx-1之间
                                selected_key = key
                                selected_reg = reg
                                selected_dist = first_change_dist
                                
                                # 重新扫描前面的点（由近及远，寻找是否有更近的变化点）
                                # ✅ 优化：如果第一个变化点不在最近的3个点内（idx >= 3），重新扫描最近的3个点（索引0,1,2）
                                # 如果第一个变化点已经在最近的3个点内（idx < 3），不需要再扫描
                                # 如果前三个点没有刷新，则继续使用当前找到的点（selected_key保持不变）
                                if idx >= 3:
                                    for j in range(0, 3):  # 扫描索引0,1,2（最近的3个点）
                                        if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                                            break
                                    
                                        key2, reg2 = route_points_sorted[j]
                                        base2 = self._baseline.get(key2)
                                        if base2 is None:
                                            continue
                                        
                                        # 快速扫描
                                        sig2 = self._grab_sig(reg2, downsample=(8, 8))
                                        diff2 = self._sig_diff(sig2, base2)
                                        th2 = self._threshold.get(key2, 9999.0)
                                    
                                        if diff2 >= th2:
                                            # 找到另一个变化点，计算到参考点的距离
                                            change_center2 = self._region_center(reg2)
                                            change_dist2 = self._dist2(change_center2, reference_center)
                                            
                                            if change_dist2 < selected_dist:
                                                # 找到更近的变化点（相对于参考点），使用它并停止扫描
                                                selected_key = key2
                                                selected_reg = reg2
                                                selected_dist = change_dist2
                                                self._emit(f"🎯 发现更近的变化点：{key2}（diff={diff2:.2f}，索引{j}，距离更近），使用该点并停止扫描", "SUCCESS")
                                                break  # 找到更近的点后立即停止扫描
                                
                                change_found = True
                                break  # 退出外层循环
                    
                    if not change_found:
                        time.sleep(0.1)  # 短暂休眠，继续扫描（降低扫描频率，减少CPU占用）
                
                if not change_found or selected_key is None:
                    # 未找到任何变化点，继续外层循环
                    self._emit("🔕 未找到任何变化点，继续扫描", "DEBUG")
                    continue
                
                # 使用选中的变化点
                self._emit(f"✅ 最终选择变化点：{selected_key}（{'第一个变化点' if selected_key == first_change_key else '更近的变化点'}），准备触发对战", "SUCCESS")
                
                # 执行点击逻辑
                if selected_key and selected_reg:
                    # 点击选中的变化点，进入战斗（尼奥模式直接点击，不需要预点击）
                    self._emit(f"⚔️ 点击变化点{selected_key}，进入战斗", "SYSTEM")
                    
                    # 尼奥模式：直接点击目标点，然后持续点击直到检测到petswf或PetItem（不需要预点击）
                    tx, ty = self._region_center(selected_reg)
                    TOKEN_FIGHT_PET = "/resource/fightResource/pet/swf/"
                    TOKEN_PETITEM = "/resource/item/petItem/icon/"
                    timeout_s = 10.0  # ✅ 最多等待10秒
                    # ✅ 统一使用0.5秒点击间隔（每秒2次）
                    click_interval = 0.5  # 点击间隔（秒）
                    last_click_time = 0.0
                    start_time = time.time()
                    
                    from core.logger import fetch_kernel_since, kernel_cursor
                    initial_cursor = kernel_cursor()
                    current_cursor = initial_cursor  # 用于追踪已处理的日志位置
                    
                    self._emit(f"🖱️ 持续点击变化点，等待fightResource/pet/swf/或PetItem信号...", "INFO")
                    
                    result = None
                    collected_pet_ids = None
                    petitem_detected = False
                    
                    while (time.time() - start_time) < timeout_s and result is None:
                        # 检查停止信号
                        if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                            self._emit("⛔ 点击过程中被停止", "WARN")
                            result = None
                            break
                        
                        # 点击目标点（每次循环都点击一次）
                        now = time.time()
                        if now - last_click_time >= click_interval:
                            if use_foreground:
                                window_manager.click(tx, ty)
                            else:
                                window_manager.click_background(tx, ty)
                            last_click_time = now
                            # 点击后短暂等待，以便检测校准或pet/swf信号
                            time.sleep(0.05)
                        
                        # 点击后检查校准探针（如果需要）
                        if self._unified_framework._check_calibration_probes():
                            self._emit("🧭 检测到校准探针，执行校准", "WARN")
                            # ✅ 记录触发校准的点
                            calib_record = {
                                "point_key": selected_key,
                                "point_xy": (tx, ty),
                                "calibration_success": False,
                                "entry_result": None
                            }
                            # 执行校准
                            x_values, regions_dict = self._unified_framework._calculate_x_values()
                            distribution, target_idx = self._unified_framework._analyze_distribution(x_values)
                            if target_idx is not None:
                                self._unified_framework._calibrate_click_group(target_idx, use_foreground)
                                time.sleep(0.3)
                                calib_record["calibration_success"] = True
                                
                                # ✅ 将记录添加到列表
                                self._nieo_calibration_records.append(calib_record)
                                self._emit(f"📝 [校准记录] 点击点：{selected_key} ({tx:.0f},{ty:.0f})，校准成功", "INFO")
                                
                                # ✅ 检查是否需要放弃该点（校准后放弃列表）
                                if selected_key in self._nieo_calibration_abort_points:
                                    self._emit(f"⏸️ 点击点{selected_key}在校准后放弃列表中，放弃继续点击，切换地图", "INFO")
                                    # ✅ 更新校准记录的入站结果为"abort"（表示校准后放弃）
                                    if self._nieo_calibration_records:
                                        self._nieo_calibration_records[-1]["entry_result"] = "abort"
                                    result = None  # 设置为None，触发超时后的切换地图逻辑
                                    break  # 退出点击循环
                                
                                # ✅ 校准成功后，继续保持原频率点击刷新点（不点击Z点）
                                start_time = time.time()  # 重置超时时间（10秒）
                                last_click_time = 0.0  # 重置点击时间，立即开始点击
                                self._emit(f"✅ 校准成功，继续保持原频率点击刷新点（超时时间10秒）", "INFO")
                                continue
                            else:
                                # 校准失败（target_idx为None）
                                calib_record["calibration_success"] = False
                                self._nieo_calibration_records.append(calib_record)
                                self._emit(f"📝 [校准记录] 点击点：{selected_key} ({tx:.0f},{ty:.0f})，校准失败（无法确定目标组）", "WARN")
                        
                        # 检查内核日志中的fightResource/pet/swf/或PetItem信号
                        # 注意：不要被/resource/pet/swf/混淆（这是校准相关的，不是fightResource/pet/swf/）
                        try:
                            lines = fetch_kernel_since(current_cursor)  # 使用current_cursor而不是initial_cursor
                            if isinstance(lines, list):
                                for idx, line in enumerate(lines):
                                    line_str = str(line)
                                    # 优先检测PetItem（进入对战的直接信号）
                                    if TOKEN_PETITEM in line_str:
                                        petitem_detected = True
                                        
                                        # ✅ 如果已记录swf时间，计算时间差并更新最小值
                                        if self._nieo_swf_to_petitem_swf_time is not None:
                                            petitem_time = time.time()
                                            current_delta = petitem_time - self._nieo_swf_to_petitem_swf_time
                                            self._nieo_swf_to_petitem_current_time = current_delta
                                            
                                            # 更新最小值
                                            if self._nieo_swf_to_petitem_min_time is None or current_delta < self._nieo_swf_to_petitem_min_time:
                                                self._nieo_swf_to_petitem_min_time = current_delta
                                            
                                            # 输出时间测量信息
                                            self._emit(f"📊 [时间测量] fightpetswf到PetItem: {current_delta:.3f}s (最小值: {self._nieo_swf_to_petitem_min_time:.3f}s)", "INFO")
                                        else:
                                            # ✅ 即使没有swf时间记录，也输出当前最小值（如果存在）
                                            if self._nieo_swf_to_petitem_min_time is not None:
                                                self._emit(f"📊 [时间测量] 当前值: 未记录 (最小值: {self._nieo_swf_to_petitem_min_time:.3f}s)", "INFO")
                                            else:
                                                self._emit(f"📊 [时间测量] 当前值: 未记录 (最小值: 未记录)", "INFO")
                                        
                                        self._emit("✅ 检测到PetItem信号（已入战），停止点击", "SUCCESS")
                                        # 立即开始收集pet IDs（从当前行开始向后搜索）
                                        collected_pet_ids = self._collect_fight_pet_ids_immediate(stop_event, current_lines=lines, start_index=idx)
                                        if collected_pet_ids:
                                            self._emit(f"📋 [立即收集] 检测到PetItem时收集到的pet IDs: {sorted(collected_pet_ids)}", "INFO")
                                        result = (tx, ty, collected_pet_ids)
                                        # ✅ 更新最后一个校准记录的入站结果
                                        if self._nieo_calibration_records:
                                            self._nieo_calibration_records[-1]["entry_result"] = "success"
                                            self._emit(f"📝 [入站结果] 点击点：{selected_key}，入站成功（检测到PetItem）", "INFO")
                                        break
                                    # 检测fightResource/pet/swf/信号（注意：不要被/resource/pet/swf/混淆）
                                    # 只检测/resource/fightResource/pet/swf/，不检测/resource/pet/swf/
                                    if TOKEN_FIGHT_PET in line_str and result is None:
                                        # ✅ 记录fightpetswf检测时间（用于时间测量）
                                        self._nieo_swf_to_petitem_swf_time = time.time()
                                        
                                        self._emit(f"✅ 检测到fightResource/pet/swf/信号（已入战），停止点击，开始收集所有pet IDs\n日志行: {line_str[:200]}", "INFO")
                                        
                                        # 先从当前已获取的日志中收集
                                        initial_pet_ids = self._collect_fight_pet_ids_immediate(stop_event, current_lines=lines, start_index=idx)
                                        pet_ids = set(initial_pet_ids) if initial_pet_ids else set()
                                        
                                        # 继续循环，收集所有pet IDs直到检测到skill信号或PetItem信号
                                        collect_start_time = time.time()
                                        collect_timeout = 3.0  # 最多收集3秒
                                        skill_token = "/resource/fightResource/skill/swf/"
                                        found_skill = False
                                        found_petitem = False
                                        collect_cursor = kernel_cursor()  # 记录收集开始时的cursor
                                        
                                        while (time.time() - collect_start_time) < collect_timeout:
                                            if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                                                break
                                            
                                            collect_lines = fetch_kernel_since(collect_cursor)
                                            if isinstance(collect_lines, list):
                                                for collect_line in collect_lines:
                                                    collect_line_str = str(collect_line)
                                                    
                                                    # ✅ 检测PetItem信号（优先于skill信号）
                                                    if TOKEN_PETITEM in collect_line_str:
                                                        found_petitem = True
                                                        # ✅ 计算时间差并更新最小值
                                                        if self._nieo_swf_to_petitem_swf_time is not None:
                                                            petitem_time = time.time()
                                                            current_delta = petitem_time - self._nieo_swf_to_petitem_swf_time
                                                            self._nieo_swf_to_petitem_current_time = current_delta
                                                            
                                                            # 更新最小值
                                                            if self._nieo_swf_to_petitem_min_time is None or current_delta < self._nieo_swf_to_petitem_min_time:
                                                                self._nieo_swf_to_petitem_min_time = current_delta
                                                            
                                                            # 输出时间测量信息
                                                            self._emit(f"📊 [时间测量] fightpetswf到PetItem: {current_delta:.3f}s (最小值: {self._nieo_swf_to_petitem_min_time:.3f}s)", "INFO")
                                                        else:
                                                            # ✅ 即使没有swf时间记录，也输出当前最小值（如果存在）
                                                            if self._nieo_swf_to_petitem_min_time is not None:
                                                                self._emit(f"📊 [时间测量] 当前值: 未记录 (最小值: {self._nieo_swf_to_petitem_min_time:.3f}s)", "INFO")
                                                            else:
                                                                self._emit(f"📊 [时间测量] 当前值: 未记录 (最小值: 未记录)", "INFO")
                                                        break
                                                    
                                                    # 检测skill信号，收集完成
                                                    if skill_token in collect_line_str:
                                                        found_skill = True
                                                        break
                                                    
                                                    # 收集pet IDs（使用finditer确保收集一行中的所有pet IDs）
                                                    for m in self._FIGHT_PET_SWF_RE.finditer(collect_line_str):
                                                        try:
                                                            pet_id = int(m.group(1))
                                                            if pet_id not in pet_ids:
                                                                pet_ids.add(pet_id)
                                                                self._emit(f"📋 收集到pet ID: {pet_id}（已收集：{sorted(pet_ids)}）", "DEBUG")
                                                        except (ValueError, AttributeError):
                                                            pass
                                                
                                                collect_cursor = kernel_cursor()  # 更新cursor
                                            
                                            if found_skill or found_petitem:
                                                break
                                            time.sleep(0.05)  # 降低pet ID收集循环频率，减少CPU占用
                                        
                                        collected_pet_ids = pet_ids if pet_ids else None
                                        if collected_pet_ids:
                                            self._emit(f"📋 [持续收集] 收集到的pet IDs: {sorted(collected_pet_ids)}", "SUCCESS")
                                        result = (tx, ty, collected_pet_ids)
                                        # ✅ 更新最后一个校准记录的入站结果
                                        if self._nieo_calibration_records:
                                            self._nieo_calibration_records[-1]["entry_result"] = "success"
                                            self._emit(f"📝 [入站结果] 点击点：{selected_key}，入站成功（检测到fightResource/pet/swf）", "INFO")
                                        break  # 退出for循环
                                
                                # 如果已经设置了result，退出外层for循环
                                if result is not None or petitem_detected:
                                    break
                            
                            # 更新cursor，避免重复处理相同的日志
                            current_cursor = kernel_cursor()
                        except Exception as e:
                            self._emit(f"⚠️ 检查内核日志异常: {e}", "WARN")
                        
                        # 如果已经设置了result，退出外层while循环
                        if result is not None:
                            break
                        
                        time.sleep(0.02)  # 短暂休眠
                    
                    # 超时检查
                    if result is None:
                        # ✅ 更新最后一个校准记录的入站结果（如果还没有设置，则设置为timeout）
                        if self._nieo_calibration_records:
                            if self._nieo_calibration_records[-1]["entry_result"] is None:
                                self._nieo_calibration_records[-1]["entry_result"] = "timeout"
                                self._emit("⏱️ 点击超时（10秒内未检测到petswf/PetItem）", "WARN")
                                self._emit(f"📝 [入站结果] 点击点：{selected_key}，入站超时（10秒内未检测到petswf/PetItem）", "WARN")
                    

                        # 切换地图逻辑
                        if current_map_id == 11:
                            # 从11切回10：使用to一
                            to_one_key = f"{current_prefix}.to一"
                            self._emit(f"🗺️ 点击{to_one_key}返回10号地图", "SYSTEM")
                            try:
                                to_one_reg = self.regions.get(to_one_key)
                                if to_one_reg:
                                    self._click_region(to_one_reg, use_foreground)
                                    if self._wait_for_map_id(10, stop_event, timeout_s=30.0):
                                        current_map_id = 10
                                        continue
                            except Exception as e:
                                self._emit(f"⚠️ 切换地图失败：{e}", "WARN")
                        else:
                            # 从10切到11
                            self._emit(f"🗺️ 点击{current_prefix}.to二返回11号地图", "SYSTEM")
                            try:
                                to_two_reg = self.regions.get(f"{current_prefix}.to二")
                                if to_two_reg:
                                    self._click_region(to_two_reg, use_foreground)
                                    if self._wait_for_map_id(11, stop_event, timeout_s=30.0):
                                        current_map_id = 11
                                        continue
                            except Exception as e:
                                self._emit(f"⚠️ 切换地图失败：{e}", "WARN")
                        continue
                    
                    tx, ty, collected_pet_ids = result
                    self._current_pos = (tx, ty)
                    
                    # 如果立即收集到了pet IDs，使用它们；否则再收集一次
                    if collected_pet_ids:
                        pet_ids = list(collected_pet_ids)
                        self._emit(f"📋 已收集到战斗精灵ID: {sorted(pet_ids)}", "SUCCESS")
                    else:
                        # 如果没有立即收集到，再收集一次
                        self._emit("📋 开始收集战斗精灵ID", "INFO")
                        pet_ids_set = self._collect_fight_pet_ids(timeout=4.5, collect_window=0.55, stop_event=stop_event)
                        if not pet_ids_set:
                            self._emit("⚠️ 未收集到战斗精灵ID，跳过本次战斗", "WARN")
                            # 切换地图
                            if current_map_id == 11:
                                # 从11切回10：使用to一
                                to_one_key = f"{current_prefix}.to一"
                                self._emit(f"🗺️ 点击{to_one_key}返回10号地图", "SYSTEM")
                                try:
                                    to_one_reg = self.regions.get(to_one_key)
                                    if to_one_reg:
                                        self._click_region(to_one_reg, use_foreground)
                                        if self._wait_for_map_id(10, stop_event, timeout_s=30.0):
                                            current_map_id = 10
                                            continue
                                except Exception as e:
                                    self._emit(f"⚠️ 切换地图失败：{e}", "WARN")
                            else:
                                self._emit(f"🗺️ 点击{current_prefix}.to二返回11号地图", "SYSTEM")
                                try:
                                    to_two_reg = self.regions.get(f"{current_prefix}.to二")
                                    if to_two_reg:
                                        self._click_region(to_two_reg, use_foreground)
                                        if self._wait_for_map_id(11, stop_event, timeout_s=30.0):
                                            current_map_id = 11
                                            continue
                                except Exception as e:
                                    self._emit(f"⚠️ 切换地图失败：{e}", "WARN")
                            continue
                        pet_ids = list(pet_ids_set)
                    
                    # 分析战斗类型（简化逻辑：只检查关键ID）
                    # 虽然会收集所有pet IDs，但判断战斗类型时只需要检查是否有108、77、310、416
                    unique_ids = set(pet_ids)
                    
                    # 关键ID集合
                    KEY_TARGET_IDS = {108, 77, 310, 416}  # 108布鲁、77尼尔、310闪光尼尔、416尼奥
                    
                    # 检查是否有关键ID
                    found_key_ids = unique_ids & KEY_TARGET_IDS
                    
                    self._emit(f"📋 战斗精灵分析：总数={len(unique_ids)}，收集到的IDs={sorted(unique_ids)}，关键ID={sorted(found_key_ids)}", "INFO")
                    
                    # 简化的战斗类型判断：只检查关键ID
                    if 108 in found_key_ids:
                        # 有108布鲁 -> 稀有捕捉
                        self._emit("🎯 [战斗类型] 检测到108布鲁，执行稀有捕捉策略", "SYSTEM")
                        battle_type = "rare_capture"
                    elif found_key_ids & {77, 310, 416}:
                        # 有77、310或416 -> 尼尔家族战斗
                        nie_id = list(found_key_ids & {77, 310, 416})[0]
                        # 如果skip_nie_77为True且检测到77，执行逃跑策略
                        if skip_nie_77 and nie_id == 77:
                            self._emit(f"🎯 [战斗类型] 检测到77尼尔，但已勾选不捕捉尼尔，执行逃跑策略", "SYSTEM")
                            battle_type = "escape"
                            self._last_nie_family_id = 77  # 保存ID用于后续处理
                        else:
                            self._emit(f"🎯 [战斗类型] 检测到尼尔家族{nie_id}，执行尼尔家族战斗策略", "SYSTEM")
                            battle_type = "nie_family"
                            self._last_nie_family_id = nie_id
                    else:
                        # 没有关键ID -> 逃跑
                        self._emit("🎯 [战斗类型] 未检测到关键ID（108/77/310/416），执行逃跑策略", "SYSTEM")
                        battle_type = "escape"
                    
                    # 执行战斗
                    self._is_in_battle = True
                    self._is_scanning_steady_state = False
                    
                    if battle_type == "escape":
                        # 逃跑策略：第一回合逃跑（测试模式下：第一回合切换，第二回合逃跑）
                        from core.unified_battle_framework import BattleConfig, BattleMode
                        from core.logger import kernel_cursor
                        
                        if (test_nieo or test_nie) and not (found_key_ids & {77, 310, 416}):
                            # 测试模式且不是尼尔家族：第一回合技能，第二回合切换，第三回合后使用中级胶囊
                            self._emit("🧪 [测试模式] 执行测试策略：第一回合技能，第二回合切换，第三回合后使用中级胶囊", "SYSTEM")
                            
                            def test_escape_action_callback(round_idx: int) -> str:
                                if round_idx == 1:
                                    # 第一回合：使用技能一
                                    return "skill"
                                elif round_idx == 2:
                                    # 第二回合：切换精灵（根据测试模式选择）
                                    if test_nieo:
                                        # 测试尼奥：切换到闪光艾菲亚（精灵二或三）
                                        if self._flash_aifeia_pos:
                                            pet_num = 2 if self._flash_aifeia_pos == "二" else 3
                                        else:
                                            pet_num = 2  # 默认精灵二
                                    else:  # test_nie
                                        # 测试尼尔：切换到艾斯菲格（精灵二或三）
                                        if self._aisifeige_pos:
                                            pet_num = 2 if self._aisifeige_pos == "二" else 3
                                        else:
                                            pet_num = 3  # 默认精灵三
                                    
                                    # 执行切换
                                    self._switch_pet_for_nie_family(
                                        nie_family_id=416 if test_nieo else 77,  # 使用对应的ID来触发切换逻辑
                                        use_foreground=use_foreground,
                                        stop_event=stop_event,
                                        test_mode=False,  # 不使用test_mode，使用正常的探针扫描结果
                                    )
                                    return "switch"
                                else:
                                    # 第三回合后：使用中级胶囊
                                    return "capsule"
                            
                            config = BattleConfig(
                                mode=BattleMode.WILD,
                                use_foreground=use_foreground,
                                skill_key="对战.使用技能一",
                                action_callback=test_escape_action_callback,
                                abort_check=lambda: stop_event.is_set() or getattr(self.bot, "stop_current", False),
                                test_mode_capsule_only_mid=True,  # 测试模式：只使用中级胶囊
                            )
                        else:
                            # 正常逃跑策略：第一回合逃跑
                            self._emit("🏃 执行逃跑策略：第一回合逃跑", "SYSTEM")
                            
                            def escape_action_callback(round_idx: int) -> str:
                                if round_idx == 1:
                                    return "escape"
                                return "escape"  # 如果第一回合逃跑失败，继续逃跑
                            
                            config = BattleConfig(
                                mode=BattleMode.WILD,
                                use_foreground=use_foreground,
                                skill_key="对战.使用技能一",
                                action_callback=escape_action_callback,
                                abort_check=lambda: stop_event.is_set() or getattr(self.bot, "stop_current", False),
                            )
                        
                        # 调用Stage 2等待PetItem完全加载，并在检测到PetItem时执行第一回合动作
                        self._emit("🔍 等待PetItem信号（使用Stage 2）...", "INFO")
                        stage2_cursor = kernel_cursor()
                        success, calib_result = self._unified_framework.stage2_calibration_and_petitem(
                            trigger_callback=None,
                            use_foreground=use_foreground,
                            timeout_s=15.0,
                            skip_stage1=True,
                            config=config,
                            initial_cursor=stage2_cursor,
                        )
                        
                        if not success:
                            self._emit("❌ Stage 2失败（未检测到PetItem或校准失败），跳过本次战斗", "WARN")
                            # 切换地图
                            if current_map_id == 11:
                                # 从11切回10：使用to一
                                to_one_key = f"{current_prefix}.to一"
                                self._emit(f"🗺️ 点击{to_one_key}返回10号地图", "SYSTEM")
                                try:
                                    to_one_reg = self.regions.get(to_one_key)
                                    if to_one_reg:
                                        self._click_region(to_one_reg, use_foreground)
                                        if self._wait_for_map_id(10, stop_event, timeout_s=30.0):
                                            current_map_id = 10
                                            continue
                                except Exception as e:
                                    self._emit(f"⚠️ 切换地图失败：{e}", "WARN")
                            else:
                                self._emit(f"🗺️ 点击{current_prefix}.to二返回11号地图", "SYSTEM")
                                try:
                                    to_two_reg = self.regions.get(f"{current_prefix}.to二")
                                    if to_two_reg:
                                        self._click_region(to_two_reg, use_foreground)
                                        if self._wait_for_map_id(11, stop_event, timeout_s=30.0):
                                            current_map_id = 11
                                            continue
                                except Exception as e:
                                    self._emit(f"⚠️ 切换地图失败：{e}", "WARN")
                            continue
                        
                        battle_success = self._unified_framework.stage3_battle_loop(config)
                        if battle_success:
                            # stage4_post_battle已经处理了1AND1清理（对于ESCAPE动作会自动执行）
                            self._unified_framework.stage4_post_battle(config, is_training_room=False)
                            nieo_stats["普通逃跑"] += 1
                            self._emit(f"✅ 逃跑成功，统计：普通逃跑={nieo_stats['普通逃跑']}", "SUCCESS")
                        
                        # 切换地图
                        if current_map_id == 11:
                            # 从11切回10：使用to一
                            to_one_key = f"{current_prefix}.to一"
                            self._emit(f"🗺️ 点击{to_one_key}返回10号地图", "SYSTEM")
                            try:
                                to_one_reg = self.regions.get(to_one_key)
                                if to_one_reg:
                                    self._click_region(to_one_reg, use_foreground)
                                    if self._wait_for_map_id(10, stop_event, timeout_s=30.0):
                                        current_map_id = 10
                                        continue
                            except Exception as e:
                                self._emit(f"⚠️ 切换地图失败：{e}", "WARN")
                        else:
                            # 从10切到11
                            self._emit(f"🗺️ 点击{current_prefix}.to二返回11号地图", "SYSTEM")
                            try:
                                to_two_reg = self.regions.get(f"{current_prefix}.to二")
                                if to_two_reg:
                                    self._click_region(to_two_reg, use_foreground)
                                    if self._wait_for_map_id(11, stop_event, timeout_s=30.0):
                                        current_map_id = 11
                                        continue
                            except Exception as e:
                                self._emit(f"⚠️ 切换地图失败：{e}", "WARN")
                    
                    elif battle_type == "rare_capture":
                        # 稀有捕捉策略：普通稀有捕捉逻辑
                        self._emit("🎯 执行稀有精灵108布鲁捕捉策略", "SYSTEM")
                        from core.unified_battle_framework import BattleConfig, BattleMode
                        
                        # ✅ 尼奥模式：不执行OCR监控（已禁用）
                        # （不再启动监控线程）
                        
                        def rare_action_callback(round_idx: int) -> str:
                            # ✅ 野外稀有捕捉：中高中高中高模式
                            # 奇数回合（1,3,5,7...）：中级胶囊
                            # 偶数回合（2,4,6,8...）：高级胶囊
                            if round_idx % 2 == 1:
                                return "capsule"  # 奇数回合：中级胶囊
                            else:
                                return "capsule_high"  # 偶数回合：高级胶囊
                        
                        config = BattleConfig(
                            mode=BattleMode.WILD,
                            use_foreground=use_foreground,
                            skill_key="对战.使用技能一",
                            action_callback=rare_action_callback,
                            abort_check=lambda: stop_event.is_set() or getattr(self.bot, "stop_current", False),
                        )
                        
                        # 调用Stage 2等待PetItem完全加载，并在检测到PetItem时执行第一回合动作
                        self._emit("🔍 等待PetItem信号（使用Stage 2）...", "INFO")
                        from core.logger import kernel_cursor
                        stage2_cursor = kernel_cursor()
                        success, calib_result = self._unified_framework.stage2_calibration_and_petitem(
                            trigger_callback=None,
                            use_foreground=use_foreground,
                            timeout_s=15.0,
                            skip_stage1=True,
                            config=config,
                            initial_cursor=stage2_cursor,
                        )
                        
                        if not success:
                            self._emit("❌ Stage 2失败（未检测到PetItem或校准失败），跳过本次战斗", "WARN")
                            # 切换地图（同escape逻辑）
                            if current_map_id == 11:
                                # 从11切回10：使用to一
                                to_one_key = f"{current_prefix}.to一"
                                self._emit(f"🗺️ 点击{to_one_key}返回10号地图", "SYSTEM")
                                try:
                                    to_one_reg = self.regions.get(to_one_key)
                                    if to_one_reg:
                                        self._click_region(to_one_reg, use_foreground)
                                        if self._wait_for_map_id(10, stop_event, timeout_s=30.0):
                                            current_map_id = 10
                                            continue
                                except Exception as e:
                                    self._emit(f"⚠️ 切换地图失败：{e}", "WARN")
                            else:
                                self._emit(f"🗺️ 点击{current_prefix}.to二返回11号地图", "SYSTEM")
                                try:
                                    to_two_reg = self.regions.get(f"{current_prefix}.to二")
                                    if to_two_reg:
                                        self._click_region(to_two_reg, use_foreground)
                                        if self._wait_for_map_id(11, stop_event, timeout_s=30.0):
                                            current_map_id = 11
                                            continue
                                except Exception as e:
                                    self._emit(f"⚠️ 切换地图失败：{e}", "WARN")
                            continue
                        
                        battle_success = self._unified_framework.stage3_battle_loop(config)
                        if battle_success:
                            self._unified_framework.stage4_post_battle(config, is_training_room=False)
                            # 检查是否捕捉成功
                            from core.unified_battle_framework import LastActionType
                            if self._unified_framework._last_action == LastActionType.CAPSULE:
                                nieo_stats["稀有捕捉"] += 1
                                nieo_stats["108捕捉"] += 1
                                self._emit(f"✅ 108布鲁捕捉成功，统计：稀有捕捉={nieo_stats['稀有捕捉']}，108捕捉={nieo_stats['108捕捉']}", "SUCCESS")
                                
                                # 捕捉成功后的清理和恢复
                                self._emit("🧹 捕捉成功，执行清理和恢复", "SYSTEM")
                                self._sleep_abortable(stop_event, 3.0)
                                
                                # ✅ 停止常态1AND1监控（避免与1AND1清理冲突）
                                self._stop_1and1_monitoring = True
                                
                                # ✅ 1AND1清理已在stage4_post_battle中处理，无需重复调用
                                
                                # 放回仓库和恢复精灵一
                                self._is_recovering = True
                                temp_profile = DEFAULT_PROFILE_DUGULU
                                self._recover_pets(use_foreground, stop_event, skip_return_storage=False, nie_family_id=None, profile=temp_profile)
                                self._is_recovering = False
                                
                                # ✅ 恢复完成后，重新启动常态1AND1监控（如果适用）
                                self._stop_1and1_monitoring = False
                        
                        # 切换地图（同逃跑逻辑）
                        if current_map_id == 11:
                            # 从11切回10：使用to一
                            to_one_key = f"{current_prefix}.to一"
                            self._emit(f"🗺️ 点击{to_one_key}返回10号地图", "SYSTEM")
                            try:
                                to_one_reg = self.regions.get(to_one_key)
                                if to_one_reg:
                                    self._click_region(to_one_reg, use_foreground)
                                    if self._wait_for_map_id(10, stop_event, timeout_s=30.0):
                                        current_map_id = 10
                                        continue
                            except Exception as e:
                                self._emit(f"⚠️ 切换地图失败：{e}", "WARN")
                        else:
                            self._emit(f"🗺️ 点击{current_prefix}.to二返回11号地图", "SYSTEM")
                            try:
                                to_two_reg = self.regions.get(f"{current_prefix}.to二")
                                if to_two_reg:
                                    self._click_region(to_two_reg, use_foreground)
                                    if self._wait_for_map_id(11, stop_event, timeout_s=30.0):
                                        current_map_id = 11
                                        continue
                            except Exception as e:
                                self._emit(f"⚠️ 切换地图失败：{e}", "WARN")
                    
                    elif battle_type == "nie_family":
                        # 尼尔家族战斗策略：第一回合技能、第二回合切换对应精灵、第三回合后一高一中混合
                        nie_id = self._last_nie_family_id
                        self._emit(f"🎯 执行尼尔家族战斗策略（{nie_id}）", "SYSTEM")
                        from core.unified_battle_framework import BattleConfig, BattleMode
                        
                        # ✅ 尼奥模式：不执行OCR监控（已禁用）
                        # （不再启动监控线程）
                        
                        def nie_action_callback(round_idx: int) -> str:
                            if round_idx == 1:
                                return "skill"  # 第一回合使用技能一
                            elif round_idx == 2:
                                # 第二回合切换精灵
                                self._switch_pet_for_nie_family(nie_id, use_foreground, stop_event, test_mode=False)
                                return "switch"
                            elif round_idx == 3:
                                # 第三回合开始捕捉（高级胶囊）
                                return "capsule_high"
                            else:
                                # 第四回合后：一高一中混合（不是每三回合一高，是一高一中）
                                # 回合4=中，回合5=高，回合6=中，回合7=高...
                                if round_idx % 2 == 0:
                                    return "capsule"  # 偶数回合（4,6,8...）中级
                                else:
                                    return "capsule_high"  # 奇数回合（5,7,9...）高级
                        
                        config = BattleConfig(
                            mode=BattleMode.WILD,
                            use_foreground=use_foreground,
                            skill_key="对战.使用技能一",
                            action_callback=nie_action_callback,
                            abort_check=lambda: stop_event.is_set() or getattr(self.bot, "stop_current", False),
                        )
                        
                        # 调用Stage 2等待PetItem完全加载，并在检测到PetItem时执行第一回合动作
                        self._emit("🔍 等待PetItem信号（使用Stage 2）...", "INFO")
                        from core.logger import kernel_cursor
                        stage2_cursor = kernel_cursor()
                        success, calib_result = self._unified_framework.stage2_calibration_and_petitem(
                            trigger_callback=None,
                            use_foreground=use_foreground,
                            timeout_s=15.0,
                            skip_stage1=True,
                            config=config,
                            initial_cursor=stage2_cursor,
                        )
                        
                        if not success:
                            self._emit("❌ Stage 2失败（未检测到PetItem或校准失败），跳过本次战斗", "WARN")
                            # 切换地图（同escape逻辑）
                            if current_map_id == 11:
                                # 从11切回10：使用to一
                                to_one_key = f"{current_prefix}.to一"
                                self._emit(f"🗺️ 点击{to_one_key}返回10号地图", "SYSTEM")
                                try:
                                    to_one_reg = self.regions.get(to_one_key)
                                    if to_one_reg:
                                        self._click_region(to_one_reg, use_foreground)
                                        if self._wait_for_map_id(10, stop_event, timeout_s=30.0):
                                            current_map_id = 10
                                            continue
                                except Exception as e:
                                    self._emit(f"⚠️ 切换地图失败：{e}", "WARN")
                            else:
                                self._emit(f"🗺️ 点击{current_prefix}.to二返回11号地图", "SYSTEM")
                                try:
                                    to_two_reg = self.regions.get(f"{current_prefix}.to二")
                                    if to_two_reg:
                                        self._click_region(to_two_reg, use_foreground)
                                        if self._wait_for_map_id(11, stop_event, timeout_s=30.0):
                                            current_map_id = 11
                                            continue
                                except Exception as e:
                                    self._emit(f"⚠️ 切换地图失败：{e}", "WARN")
                            continue
                        
                        battle_success = self._unified_framework.stage3_battle_loop(config)
                        if battle_success:
                            self._unified_framework.stage4_post_battle(config, is_training_room=False)
                            # 检查是否捕捉成功
                            from core.unified_battle_framework import LastActionType
                            if self._unified_framework._last_action == LastActionType.CAPSULE:
                                nieo_stats["尼尔家族"] += 1
                                if nie_id == 77:
                                    nieo_stats["77捕捉"] += 1
                                elif nie_id == 310:
                                    nieo_stats["310捕捉"] += 1
                                elif nie_id == 416:
                                    nieo_stats["416捕捉"] += 1
                                self._emit(f"✅ 尼尔家族{nie_id}捕捉成功，统计：尼尔家族={nieo_stats['尼尔家族']}", "SUCCESS")
                                
                                # 捕捉成功后的清理和恢复
                                self._emit("🧹 捕捉成功，执行清理和恢复", "SYSTEM")
                                self._sleep_abortable(stop_event, 3.0)
                                
                                # ✅ 停止常态1AND1监控（避免与1AND1清理冲突）
                                self._stop_1and1_monitoring = True
                                
                                # ✅ 1AND1清理已在stage4_post_battle中处理，无需重复调用
                                
                                # 放回仓库和恢复对应精灵
                                self._is_recovering = True
                                temp_profile = DEFAULT_PROFILE_DUGULU
                                self._recover_pets(use_foreground, stop_event, skip_return_storage=False, nie_family_id=nie_id, profile=temp_profile)
                                self._is_recovering = False
                                self._last_nie_family_id = None
                                
                                # ✅ 恢复完成后，重新启动常态1AND1监控（如果适用）
                                self._stop_1and1_monitoring = False
                        
                        # 切换地图（同逃跑逻辑）
                        if current_map_id == 11:
                            # 从11切回10：使用to一
                            to_one_key = f"{current_prefix}.to一"
                            self._emit(f"🗺️ 点击{to_one_key}返回10号地图", "SYSTEM")
                            try:
                                to_one_reg = self.regions.get(to_one_key)
                                if to_one_reg:
                                    self._click_region(to_one_reg, use_foreground)
                                    if self._wait_for_map_id(10, stop_event, timeout_s=30.0):
                                        current_map_id = 10
                                        continue
                            except Exception as e:
                                self._emit(f"⚠️ 切换地图失败：{e}", "WARN")
                        else:
                            self._emit(f"🗺️ 点击{current_prefix}.to二返回11号地图", "SYSTEM")
                            try:
                                to_two_reg = self.regions.get(f"{current_prefix}.to二")
                                if to_two_reg:
                                    self._click_region(to_two_reg, use_foreground)
                                    if self._wait_for_map_id(11, stop_event, timeout_s=30.0):
                                        current_map_id = 11
                                        continue
                            except Exception as e:
                                self._emit(f"⚠️ 切换地图失败：{e}", "WARN")
                    
                    self._is_in_battle = False
                    self._is_scanning_steady_state = True
                    # ✅ 恢复完成后，重新启动常态1AND1监控（如果适用，但尼奥模式不启用）
                    # 注意：尼奥模式不启用常态1AND1监控，所以这里不需要重新启动
            
            # 输出最终统计
            self._emit("📊 尼奥模式统计：", "SYSTEM")
            self._emit(f"   普通逃跑：{nieo_stats['普通逃跑']}", "INFO")
            self._emit(f"   稀有捕捉：{nieo_stats['稀有捕捉']}（108布鲁：{nieo_stats['108捕捉']}）", "INFO")
            self._emit(f"   尼尔家族：{nieo_stats['尼尔家族']}（77：{nieo_stats['77捕捉']}，310：{nieo_stats['310捕捉']}，416：{nieo_stats['416捕捉']}）", "INFO")
            
            # ✅ 输出校准记录总结
            if self._nieo_calibration_records:
                self._emit("📊 尼奥模式校准记录总结：", "SYSTEM")
                total_calibs = len(self._nieo_calibration_records)
                success_calibs = sum(1 for r in self._nieo_calibration_records if r["calibration_success"])
                success_entries = sum(1 for r in self._nieo_calibration_records if r["entry_result"] == "success")
                timeout_entries = sum(1 for r in self._nieo_calibration_records if r["entry_result"] == "timeout")
                failed_entries = sum(1 for r in self._nieo_calibration_records if r["entry_result"] == "failed")
                pending_entries = sum(1 for r in self._nieo_calibration_records if r["entry_result"] is None)
                
                self._emit(f"   总校准次数：{total_calibs}", "INFO")
                self._emit(f"   校准成功：{success_calibs}，校准失败：{total_calibs - success_calibs}", "INFO")
                self._emit(f"   入站成功：{success_entries}，入站超时：{timeout_entries}，入站失败：{failed_entries}，未完成：{pending_entries}", "INFO")
                
                # 详细记录
                self._emit("   详细记录：", "INFO")
                for idx, record in enumerate(self._nieo_calibration_records, 1):
                    point_key = record["point_key"]
                    x, y = record["point_xy"]
                    calib_status = "成功" if record["calibration_success"] else "失败"
                    entry_result = record["entry_result"]
                    if entry_result == "success":
                        entry_status = "成功"
                    elif entry_result == "timeout":
                        entry_status = "超时"
                    elif entry_result == "failed":
                        entry_status = "失败"
                    else:
                        entry_status = "未完成"
                    self._emit(f"     {idx}. 点：{point_key} ({x:.0f},{y:.0f})，校准：{calib_status}，入站：{entry_status}", "INFO")
            else:
                self._emit("📊 尼奥模式校准记录：无校准记录", "INFO")
            
            self._emit("✅ 尼奥模式结束", "SUCCESS")
            
        except Exception as e:
            self._emit(f"❌ 尼奥模式异常: {e}", "ERROR")
            import traceback
            self._emit(f"📋 异常详情: {traceback.format_exc()}", "ERROR")
        finally:
            self._is_in_battle = False
            self._is_scanning_steady_state = False

    def run(
        self,
        stop_event: threading.Event,
        use_foreground: bool,
        profile: Optional[WildCaptureProfile] = None,
        test_mode: bool = False,  # 测试模式：无需声音触发，第一回合技能一+后续只使用中级胶囊
        smart_tracking_mode: bool = False,  # 智能追踪模式：使用智能点击和追踪逻辑
        xiaodouya_nie_test_mode: bool = False,  # 小豆芽尼尔测试模式：捕捉16号精灵，测试切换精灵逻辑
    ) -> None:
        try:
            profile = profile or DEFAULT_PROFILE_MANTIS
            # ✅ 保存当前profile，供刷新重连时使用
            self._current_profile = profile
            # ✅ 重置1AND1监控标志（确保新任务开始时标志正确）
            self._stop_1and1_monitoring = False
            
            # ✅ 获取或初始化该任务的统计（按profile.name分开）
            task_name = profile.name
            if task_name not in self._task_stats:
                self._task_stats[task_name] = {"total": 0, "success": 0}
            task_stats = self._task_stats[task_name]
            
            if smart_tracking_mode:
                self._emit(f"🧪 智能追踪模式启动：profile={profile.route_hint} 前台={use_foreground}", "SYSTEM")
            else:
                self._emit(f"🌲 野外捕捉启动：profile={profile.route_hint} 前台={use_foreground}", "SYSTEM")
            
            # 注意：野外捕捉模式（包括螳螂模式）不会执行swf文件恢复
            # swf文件恢复仅在run_nieo_mode()中执行
            
            # 保存smart_tracking_mode和xiaodouya_nie_test_mode以供后续使用
            self._smart_tracking_mode = smart_tracking_mode
            self._xiaodouya_nie_test_mode = xiaodouya_nie_test_mode

            if not hasattr(self.battle_runner, "run_mantis_capture_mode"):
                self._emit(f"❌ battle_runner 类型错误：{type(self.battle_runner)}（应为 BattleRunner）", "ERROR")
                return

            if not window_manager.ensure_game_hwnd():
                self._emit("❌ 未检测到游戏窗口：请先在 Dashboard 点【启动游戏】", "ERROR")
                return

            # ✅ 检查和删除swf文件（根据profile确定要删除的文件）
            self._check_and_delete_swf_files(profile)

            # ✅ 闪光皮皮特殊流程：直接执行恢复，然后点击进入10号地图
            if profile.name == "闪光皮皮(164)":
                # 1) 执行恢复逻辑（不包含放回仓库）
                self._emit("💊 [闪光皮皮] 执行恢复流程", "SYSTEM")
                self._recover_pets(use_foreground, stop_event, skip_return_storage=True, profile=profile)
                
                # 2) 执行地图进入脚本（切换到10号地图）
                self._emit("🗺️ [闪光皮皮] 执行地图进入脚本：地图\\10.json（切换到10号地图）", "SYSTEM")
                if not self._execute_map_entry_script(10, use_foreground, stop_event):
                    self._emit("⚠️ [闪光皮皮] 地图/10.json执行失败，尝试继续", "WARN")
                
                # 3) 等待进入10号地图（使用白色探针补丁）
                self._emit("⏳ [闪光皮皮] 等待进入10号地图：/resource/map/10.swf + 白色探针检测...", "SYSTEM")
                if not self._wait_for_map_id(10, stop_event, timeout_s=30.0, white_probe_key="闪光皮皮.白色探针"):
                    self._emit("⛔ [闪光皮皮] 等待10号地图进入超时/已停止", "WARN")
                    return
                self._emit("✅ [闪光皮皮] 已进入10号地图：开始解析路线", "SUCCESS")
                
                # 4) 解析路线点 & A/B（闪光皮皮特殊流程也需要解析路线点）
                route_points, reg_a, reg_b = self._resolve_route_regions(profile.route_hint)
                
                # 5) ✅ 检测是否到达A或B点（标定A点，点击A点，2秒检测；如果未到达，标定B点，点击B点）
                reached_point = self._wait_until_reached_ab_point(reg_a, reg_b, use_foreground, stop_event)
            else:
                # 普通流程：先执行恢复逻辑（不包含放回仓库）
                self._emit("💊 执行恢复流程（进入地图前）", "SYSTEM")
                self._recover_pets(use_foreground, stop_event, skip_return_storage=True, profile=profile)
                
                # 2) 执行地图进入脚本（从 地图\\{map_swf_id}.json 读取）
                self._emit(f"🗺️ 执行地图进入脚本：地图\\{profile.map_swf_id}.json", "SYSTEM")
                if not self._execute_map_entry_script(profile.map_swf_id, use_foreground, stop_event):
                    self._emit("⚠️ 地图进入脚本执行失败，尝试继续", "WARN")
                
                # 3) 等待进入地图（检测map+npc信号）
                self._emit(f"⏳ 等待进入地图：/resource/map/{profile.map_swf_id}.swf + newNpc...", "SYSTEM")
                if not self._wait_for_map_ready(profile, stop_event):
                    self._emit("⛔ 等待地图进入超时/已停止", "WARN")
                    return
                self._emit("✅ 已进入地图：开始解析路线", "SUCCESS")

                # ✅ 新增：检测电池状态（进入地图后，A点检测前）
                if self._unified_framework:
                    battery_status = self._unified_framework._detect_battery_status(use_foreground)
                    if battery_status is not None:
                        self._battery_status_before_battle = battery_status
                        self._emit(f"🔋 [启动检测] 电池状态: {battery_status}", "INFO")
                    else:
                        self._emit("⚠️ [启动检测] 电池状态检测失败", "WARN")

                # 4) 解析路线点 & A/B
                route_points, reg_a, reg_b = self._resolve_route_regions(profile.route_hint)

                # 5) ✅ 检测是否到达A或B点（标定A点，点击A点，2秒检测；如果未到达，标定B点，点击B点）
                reached_point = self._wait_until_reached_ab_point(reg_a, reg_b, use_foreground, stop_event)

            # 6) ✅ 初始标定（确认到达A或B点后才开始标定稳态）- 稀有精灵模式：检测所有9个点
            self._emit(f"📏 开始判定稳态（标定所有9个点，使用稳健模式）", "SYSTEM")
            baseline_start_time = time.time()
            # ✅ 稀有精灵模式：使用稳健标定（6次采样，标定所有9个点）
            self._recalibrate_all_robust(route_points, stop_event, map_id=profile.map_swf_id)
            baseline_duration = time.time() - baseline_start_time
            self._emit(f"✅ 稳态标定完成（耗时{baseline_duration:.1f}s）：开始扫描", "SUCCESS")
            
            # ✅ 稀有精灵模式：等待至少5秒确保基线稳定（比尼奥模式的3秒更长，更稳健）
            min_wait_after_baseline = 5.0
            if baseline_duration < min_wait_after_baseline:
                wait_time = min_wait_after_baseline - baseline_duration
                self._emit(f"⏳ 等待{wait_time:.1f}s后开始扫描（确保基线稳定，稳健模式）", "INFO")
                self._sleep_abortable(stop_event, wait_time)
            
            # ❌ DISABLED: 启动持续记录基线数据（在后台线程）
            # ❌ 已摒弃使用长期基线的想法，持续点击稀有精灵即可
            # self._start_continuous_baseline_recording(route_points, profile.map_swf_id, stop_event)
            
            # 标记进入稳态扫描阶段
            self._is_scanning_steady_state = True
            
            # 启动常态1AND1检测（在螳螂模式、小豆芽模式、嘟咕噜模式和双塔模式启用）
            # 检测到1AND1后点击确认，如果5秒内检测到/login/Login.swf，等待0.5s后执行登录+to脚本
            profile_name_lower = profile.name.lower()
            if "螳螂" in profile_name_lower or "小豆芽" in profile_name_lower or "嘟咕噜" in profile_name_lower or "双塔" in profile_name_lower:
                self._start_normal_1and1_monitoring(profile, use_foreground, stop_event)
            else:
                self._emit("ℹ️ [常态1AND1] 当前模式未启用常态1AND1监控（仅螳螂、小豆芽、嘟咕噜和双塔模式启用）", "INFO")
            
            # 注意：探针扫描已移到恢复流程中（在恢复和1AND1之后），不再在这里扫描
            # 首次扫描将在第一次恢复时进行

            # 5) 主循环
            cursor = kernel_cursor()
            next_kernel_poll = 0.0

            # ✅ 设置下一次走位：40秒后点击初始AB的反向点
            if reached_point == "A":
                next_move_is_b = True  # 到达A点，下次走B点
                next_move_at = time.time() + profile.ab_cooldown_sec
            elif reached_point == "B":
                next_move_is_b = False  # 到达B点，下次走A点
                next_move_at = time.time() + profile.ab_cooldown_sec
            else:
                # 默认情况（不应该发生，但保险起见）
                next_move_is_b = True
                next_move_at = time.time() + profile.ab_cooldown_sec

            # mp3 gating：听到 mp3 后允许触发入战的窗口
            last_mp3_ts: Optional[float] = None
            armed_until_ts: float = 0.0  # now <= armed_until_ts 才允许触发入战

            while not stop_event.is_set() and (not getattr(self.bot, "stop_current", False)):
                self._wait_if_paused(stop_event)
                now = time.time()

                # ✅ 检查待处理的突变是否超时（超过2秒未出现MP3则重新标定）
                if self._pending_mutation is not None:
                    pending_key, pending_reg, pending_diff, pending_ts = self._pending_mutation
                    if now - pending_ts >= 2.0:
                        # 超时：重新标定（遗忘）
                        try:
                            mean_sig, jitter = self._measure_baseline(pending_reg, samples=2, downsample=(8, 8))
                            th = jitter * 5.0 + 12.0
                            self._baseline[pending_key] = mean_sig
                            self._jitter[pending_key] = jitter
                            self._threshold[pending_key] = th
                            self._emit(f"🔇 待处理突变超时：{pending_key} diff={pending_diff:.2f} -> 遗忘（重新标定）", "DEBUG")
                        except Exception as e:
                            self._emit(f"⚠️ 重新标定点{pending_key}失败: {e}", "WARN")
                        self._pending_mutation = None

                # (a) kernel 监听（节流）
                if now >= next_kernel_poll:
                    next_kernel_poll = now + self.KERNEL_POLL_SEC
                    cursor, lines = self._fetch_kernel(cursor)

                    # ✅ 地图一致性检查（仅野外模式启用）
                    bad_map = self._detect_unexpected_map(lines, expected_map_id=profile.map_swf_id)
                    if bad_map is not None:
                        self._emit(
                            f"❌ 检测到地图不一致：/resource/map/{bad_map}.swf（期望 {profile.map_swf_id}）-> 立刻停止",
                            "ERROR",
                        )
                        return

                    # mp3 触发：进入武装窗口（初版逻辑：持续burst扫描会检测突变+MP3）
                    # 优先使用 target_mp3_ids，否则使用 target_mp3_id（向后兼容）
                    mp3_ids = profile.target_mp3_ids if profile.target_mp3_ids else profile.target_mp3_id
                    if self._hit_target_mp3(lines, mp3_ids):
                        # ✅ 检测到MP3后，停止1AND1监控（全局1AND1检测只在稳态建立后到下一次MP3前）
                        self._stop_1and1_monitoring = True
                        
                        last_mp3_ts = now

                        # ✅ 计数：总精灵数 = mp3触发的次数（使用任务特定统计）
                        task_stats["total"] += 1
                        
                        # 显示检测到的mp3 ID
                        if isinstance(mp3_ids, (tuple, list)):
                            detected_mp3 = next((mp3_id for mp3_id in mp3_ids if any(f"/{mp3_id}.mp3" in ln for ln in lines)), None)
                            if detected_mp3:
                                self._emit(f"🔔 检测到 mp3：{detected_mp3}.mp3 -> 进入武装窗口", "INFO")
                            else:
                                self._emit(f"🔔 检测到 mp3：{mp3_ids} -> 进入武装窗口", "INFO")
                        else:
                            self._emit(f"🔔 检测到 mp3：{mp3_ids}.mp3 -> 进入武装窗口", "INFO")
                        # 输出统计信息（总精灵数）
                        self._emit(f"📊 [统计] 检测到mp3（总精灵数：{task_stats['total']}）", "INFO")
                        
                        # ✅ 检查是否有待处理的突变（2秒内），如果有则立即触发
                        if self._pending_mutation is not None:
                            pending_key, pending_reg, pending_diff, pending_ts = self._pending_mutation
                            if now - pending_ts <= 2.0:
                                # ✅ 2秒内出现MP3，立即触发该突变点
                                self._pending_mutation = None
                                hit_key, hit_reg, diff = pending_key, pending_reg, pending_diff
                                self._emit(f"✅ 检测到MP3，触发待处理突变：{hit_key} diff={diff:.2f} -> 进入战斗", "SUCCESS")
                                if not test_mode:
                                    armed_until_ts = 0.0  # 消耗武装（测试模式下不需要）
                                
                                # ✅ 新策略：先点击反向点，然后持续点击刷新点直到检测到skill信号
                                result = self._click_opposite_then_click_target_until_skill(
                                    reg_a=reg_a,
                                    reg_b=reg_b,
                                    target_reg=hit_reg,
                                    use_foreground=use_foreground,
                                    stop_event=stop_event,
                                )
                                if result is None:
                                    # 点击失败（超时或停止）
                                    self._emit("❌ 点击刷新点失败，跳过本次对战", "WARN")
                                    next_move_at = time.time() + profile.ab_cooldown_sec
                                    next_move_is_b = True
                                    continue
                                tx, ty, collected_pet_ids = result
                                self._current_pos = (tx, ty)
                                # 存储立即收集到的pet IDs供后续使用（快速操作，无延迟）
                                if collected_pet_ids:
                                    self._immediate_collected_pet_ids = collected_pet_ids

                                # 立即处理对战逻辑（skill后很快就是PetItem，必须紧接着调用）
                                battle_result = self._handle_battle_trigger(
                                    tx, ty, reg_a, route_points, profile, use_foreground, stop_event, test_mode, self._xiaodouya_nie_test_mode, task_stats
                                )
                                
                                # 根据战斗结果更新统计（简化版：只需要两项，使用任务特定统计）
                                if battle_result == "skipped":
                                    # 检测到mp3但未成功进入战斗（校准失败/超时等）
                                    self._emit(f"📊 [统计] 错过稀有精灵（未成功进入战斗）。总计：{task_stats['total']} | 成功：{task_stats['success']}", "WARN")
                                elif battle_result == "captured":
                                    # 成功进入战斗且捕捉成功
                                    self._emit(f"📊 [统计] 捕捉成功！总计：{task_stats['total']} | 成功：{task_stats['success']}", "SUCCESS")
                                    # 捕捉成功后执行战后处理：回A点、恢复、等待窗口关闭、稳态检测
                                    reached_point = self._post_battle_cleanup(reg_a, reg_b, route_points, profile, use_foreground, stop_event, should_recover=True)
                                    # ✅ 确保下一次走位是反向点
                                    if reached_point == "A":
                                        next_move_is_b = True  # 到达A点，下次走B点
                                    elif reached_point == "B":
                                        next_move_is_b = False  # 到达B点，下次走A点
                                    next_move_at = time.time() + profile.ab_cooldown_sec
                                    continue
                                elif battle_result == "battled":
                                    # 成功进入战斗但未捕捉成功（使用技能或战斗失败或逃跑）
                                    self._emit(f"📊 [统计] 进入战斗但未捕捉成功。总计：{task_stats['total']} | 成功：{task_stats['success']}", "INFO")
                                    # 战斗后执行战后处理：回A点、等待窗口关闭、稳态检测（不执行恢复）
                                    reached_point = self._post_battle_cleanup(reg_a, reg_b, route_points, profile, use_foreground, stop_event, should_recover=False)
                                    # ✅ 确保下一次走位是反向点
                                    if reached_point == "A":
                                        next_move_is_b = True  # 到达A点，下次走B点
                                    elif reached_point == "B":
                                        next_move_is_b = False  # 到达B点，下次走A点
                                    next_move_at = time.time() + profile.ab_cooldown_sec
                                    continue
                                continue  # 触发完成后继续循环
                            else:
                                # 清除待处理突变（已经触发或超时处理过了，这里只是清理）
                                self._pending_mutation = None
                        
                        # ✅ 初版逻辑：进入武装窗口（持续burst扫描中会检测突变+MP3）
                        armed_until_ts = now + float(profile.mp3_trigger_window_sec)

                # (b) AB 移动（简单方式：每40秒点击一次，不需要检测）
                if now >= next_move_at:
                    target = reg_b if next_move_is_b else reg_a
                    target_name = "B" if next_move_is_b else "A"
                    
                    self._emit(f"🚶 走位：点击 {target_name}", "INFO")
                    self._click_region(target, use_foreground)
                    self._current_pos = self._region_center(target)
                    self._last_anchor = target_name

                    # 切换到反向点，40秒后再次点击
                    next_move_is_b = not next_move_is_b
                    next_move_at = time.time() + profile.ab_cooldown_sec

                # (c) ✅ 高强度持续burst检测稳态颜色（检测所有9个点）
                # 如果突变+MP3 → 点击，单纯突变 → 直接遗忘（忽略）
                # ✅ 稀有精灵模式：高强度扫描（持续检测，无低频率延迟）
                # 注意：burst扫描应该在每次循环中都执行，特别是在有MP3武装窗口时
                hit = self._continuous_burst_scan_once(route_points, profile, stop_event)
                if hit is None:
                    # 没有检测到突变，继续高强度扫描（无延迟或极短延迟）
                    continue
                
                hit_key, hit_reg, diff = hit
                
                # 检查是否有MP3武装（测试模式下跳过此检查）
                has_mp3_armed = (now <= armed_until_ts) if (not test_mode and profile.require_mp3_before_trigger) else True
                
                if not has_mp3_armed:
                    # ✅ 单纯突变（没有MP3）→ 记录待处理（保留2秒，如果2秒内出现MP3则触发）
                    self._pending_mutation = (hit_key, hit_reg, diff, now)
                    self._emit(f"⏳ 检测到突变但无MP3：{hit_key} diff={diff:.2f} -> 等待2秒（等待MP3）", "DEBUG")
                    continue
                else:
                    # 突变+MP3 → 点击
                    self._emit(f"🎯 检测到突变+MP3：{hit_key} diff={diff:.2f} -> 进入战斗", "SUCCESS")
                    if not test_mode:
                        armed_until_ts = 0.0  # 消耗武装（测试模式下不需要）
                    
                    # ✅ 新策略：先点击反向点，然后持续点击刷新点直到检测到skill信号
                    result = self._click_opposite_then_click_target_until_skill(
                        reg_a=reg_a,
                        reg_b=reg_b,
                        target_reg=hit_reg,
                        use_foreground=use_foreground,
                        stop_event=stop_event,
                    )
                    if result is None:
                        # 点击失败（超时或停止）
                        self._emit("❌ 点击刷新点失败，跳过本次对战", "WARN")
                        next_move_at = time.time() + profile.ab_cooldown_sec
                        next_move_is_b = True
                        continue
                    tx, ty, collected_pet_ids = result
                    self._current_pos = (tx, ty)
                    # 存储立即收集到的pet IDs供后续使用（快速操作，无延迟）
                    if collected_pet_ids:
                        self._immediate_collected_pet_ids = collected_pet_ids

                    # 立即处理对战逻辑（skill后很快就是PetItem，必须紧接着调用）
                    battle_result = self._handle_battle_trigger(
                        tx, ty, reg_a, route_points, profile, use_foreground, stop_event, test_mode, self._xiaodouya_nie_test_mode, task_stats
                    )
                    
                    # 根据战斗结果更新统计（简化版：只需要两项，使用任务特定统计）
                    if battle_result == "skipped":
                        # 检测到mp3但未成功进入战斗（校准失败/超时等）
                        self._emit(f"📊 [统计] 错过稀有精灵（未成功进入战斗）。总计：{task_stats['total']} | 成功：{task_stats['success']}", "WARN")
                    elif battle_result == "captured":
                        # 成功进入战斗且捕捉成功
                        self._emit(f"📊 [统计] 捕捉成功！总计：{task_stats['total']} | 成功：{task_stats['success']}", "SUCCESS")
                        # 捕捉成功后执行战后处理：回A点、恢复、等待窗口关闭、稳态检测
                        reached_point = self._post_battle_cleanup(reg_a, reg_b, route_points, profile, use_foreground, stop_event, should_recover=True)
                        # ✅ 确保下一次走位是反向点
                        if reached_point == "A":
                            next_move_is_b = True  # 到达A点，下次走B点
                        elif reached_point == "B":
                            next_move_is_b = False  # 到达B点，下次走A点
                        next_move_at = time.time() + profile.ab_cooldown_sec
                        continue
                    elif battle_result == "battled":
                        # 成功进入战斗但未捕捉成功（使用技能或战斗失败或逃跑）
                        self._emit(f"📊 [统计] 进入战斗但未捕捉成功。总计：{task_stats['total']} | 成功：{task_stats['success']}", "INFO")
                        # 战斗后执行战后处理：回A点、等待窗口关闭、稳态检测（不执行恢复）
                        reached_point = self._post_battle_cleanup(reg_a, reg_b, route_points, profile, use_foreground, stop_event, should_recover=False)
                        # ✅ 确保下一次走位是反向点
                        if reached_point == "A":
                            next_move_is_b = True  # 到达A点，下次走B点
                        elif reached_point == "B":
                            next_move_is_b = False  # 到达B点，下次走A点
                        next_move_at = time.time() + profile.ab_cooldown_sec
                        continue

            # 输出最终统计信息（使用任务特定统计）
            missed_count = task_stats['total'] - task_stats['success']
            self._emit(f"🛑 野外捕捉停止：{profile.name}", "WARN")
            self._emit(f"📊 [最终统计] 总精灵数：{task_stats['total']} | 成功数：{task_stats['success']} | 失败数：{missed_count}", "INFO")
            
            # ✅ 如果正在执行重连脚本，等待其完成
            if getattr(self, "_reconnect_scripts_executing", False):
                self._emit("⏳ [重连重启] 等待重连脚本执行完成...", "INFO")
                max_wait_time = 60.0  # 最多等待60秒
                wait_start = time.time()
                while getattr(self, "_reconnect_scripts_executing", False) and (time.time() - wait_start) < max_wait_time:
                    time.sleep(0.5)
                
                if getattr(self, "_reconnect_scripts_executing", False):
                    self._emit("⚠️ [重连重启] 等待重连脚本超时，继续检查重启标志", "WARN")
                else:
                    self._emit("✅ [重连重启] 重连脚本执行完成", "SUCCESS")
            
            # 检查是否需要重连后重启（在螳螂、小豆芽、嘟咕噜和双塔模式）
            # 注意：只有在重连脚本执行完成后才检查重启标志
            should_restart = getattr(self, "_should_restart_after_reconnect", False)
            self._emit(f"🔍 [重连重启] 检查重启标志：should_restart={should_restart}, profile={profile.name if profile else 'None'}", "INFO")
            if should_restart:
                self._should_restart_after_reconnect = False  # 重置标志
                profile_name_lower = profile.name.lower()
                if "螳螂" in profile_name_lower or "小豆芽" in profile_name_lower or "嘟咕噜" in profile_name_lower or "双塔" in profile_name_lower:
                    self._emit("🔄 [重连重启] 重连脚本执行完成，自动重新启动捕捉任务（全新启动）", "SYSTEM")
                    
                    # 重置状态标志，确保是一个全新的启动
                    self._is_scanning_steady_state = False
                    self._is_in_battle = False
                    self._is_recovering = False
                    self._stop_1and1_monitoring = False
                    self._last_nie_family_id = None
                    
                    # _execute_reconnect_scripts已经清除了stop_current标志，这里不需要再次清除
                    # 但为了安全，再次确认
                    if self.bot:
                        self.bot.stop_current = False
                    
                    # 重新启动任务（递归调用run方法，从头开始执行）
                    # 注意：重连脚本执行完成后，游戏已经通过to螳螂/to小豆芽/to嘟咕噜/to双塔回到了地图，所以会从头开始执行
                    self.run(stop_event, use_foreground, profile, test_mode, smart_tracking_mode, xiaodouya_nie_test_mode)
                    return  # 递归调用后直接返回
        finally:
            # ✅ 确保启用了常态化1AND1的模式结束后，常态化1AND1消失
            profile_name_lower = (profile or DEFAULT_PROFILE_MANTIS).name.lower()
            if "螳螂" in profile_name_lower or "小豆芽" in profile_name_lower or "嘟咕噜" in profile_name_lower or "双塔" in profile_name_lower:
                self._stop_1and1_monitoring = True
                self._emit("🛑 [常态1AND1] 模式结束，停止1AND1监控", "INFO")
            self._is_scanning_steady_state = False
            self._is_in_battle = False
            self._is_recovering = False

    # ---------------------------
    # new: opposite A/B then target
    # ---------------------------
    def _click_opposite_then_click_target_until_skill(
        self,
        reg_a: Region,
        reg_b: Region,
        target_reg: Region,
        use_foreground: bool,
        stop_event: threading.Event,
    ) -> Optional[Tuple[float, float, Optional[set]]]:
        """
        新策略：先点击反向点，然后持续点击刷新点，每次点击后检测校准，循环直到检测到pet/swf信号
        
        - 若当前锚点是 A：先快速点 B；锚点是 B：先快速点 A
        - 然后进入循环：
          1. 点击目标刷新点
          2. 检测是否出现校准探针（1 AND 1）
          3. 如果出现校准 → 执行校准 → 继续下一次点击
          4. 如果没有校准 → 继续下一次点击
          5. 检测是否出现 fightResource/pet/swf/ 或 PetItem 信号
          6. 如果出现 → 停止点击并返回
        - 返回：目标点击 (x, y, collected_pet_ids)，如果超时或停止返回 None
        """
        # 确保统一框架已初始化
        if not self._unified_framework:
            self._emit("❌ 统一框架未初始化，无法执行校准逻辑", "ERROR")
            return None
        
        # 1) 决定当前 anchor
        anchor = self._last_anchor
        if anchor not in ("A", "B"):
            # 兜底：根据当前位置离 A/B 哪个更近
            try:
                ax, ay = self._region_center(reg_a)
                bx, by = self._region_center(reg_b)
                if self._current_pos is None:
                    anchor = "A"
                else:
                    anchor = "A" if self._dist2(self._current_pos, (ax, ay)) <= self._dist2(self._current_pos, (bx, by)) else "B"
            except Exception:
                anchor = "A"

        # 2) 点反向锚点
        opposite_reg = reg_b if anchor == "A" else reg_a
        opposite_name = "B" if anchor == "A" else "A"
        self._emit(f"⚡ 入战前预点：当前在{anchor} -> 先点{opposite_name}", "DEBUG")
        self._click_region(opposite_reg, use_foreground)
        self._last_anchor = opposite_name  # 更新锚点为刚点过的那个
        self._sleep_abortable(stop_event, 0.06, tick=0.02)

        # 3) 获取目标坐标
        tx, ty = self._region_center(target_reg)
        
        # 4) 持续点击目标刷新点，每次点击后检测校准，循环直到检测到pet/swf/或PetItem信号
        TOKEN_FIGHT_PET = "/resource/fightResource/pet/swf/"
        TOKEN_PETITEM = "/resource/item/petItem/icon/"
        timeout_s = 15.0  # 最多等待15秒
        click_interval = 0.33  # 点击间隔（秒，每秒3次）
        last_click_time = 0.0
        start_time = time.time()
        calibration_attempts = 0
        max_calibration_attempts = 5
        
        self._emit(f"🖱️ 持续点击刷新点，每次点击后检测校准，等待fightResource/pet/swf/或PetItem信号...", "INFO")
        
        # 获取初始cursor
        from core.logger import fetch_kernel_since, kernel_cursor
        initial_cursor = kernel_cursor()
        
        while (time.time() - start_time) < timeout_s:
            # 检查停止信号
            if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                self._emit("⛔ 点击过程中被停止", "WARN")
                return None
            
            # 先点击目标刷新点（每次循环都点击一次）
            now = time.time()
            if now - last_click_time >= click_interval:
                if use_foreground:
                    window_manager.click(tx, ty)
                else:
                    window_manager.click_background(tx, ty)
                last_click_time = now
                # 点击后短暂等待，以便检测校准或pet/swf信号
                time.sleep(0.05)
            
            # 点击后立即检查校准探针（优先于pet/swf检测）
            if self._unified_framework._check_calibration_probes():
                calibration_attempts += 1
                if calibration_attempts > max_calibration_attempts:
                    self._emit("❌ 校准尝试次数过多，暂停并发送邮件", "ERROR")
                    self._unified_framework._send_email(
                        "校准失败 - 尝试次数过多",
                        f"校准尝试了{max_calibration_attempts}次仍无法完成。"
                    )
                    if self.bot:
                        self.bot.is_paused = True
                    return None
                
                self._emit(f"🧭 检测到校准探针（大=2FA7EE AND 小=FFFFFF），第{calibration_attempts}次校准", "WARN")
                
                # 计算X1-X4
                x_values, regions_dict = self._unified_framework._calculate_x_values()
                self._emit(f"📊 X值: X1={x_values[0]}, X2={x_values[1]}, X3={x_values[2]}, X4={x_values[3]}", "INFO")
                
                # 分析分布
                distribution, target_idx = self._unified_framework._analyze_distribution(x_values)
                self._emit(f"📈 分布分析: {distribution}, 目标组: {target_idx}", "INFO")
                
                # 检查是否为异常分布
                if target_idx is None:
                    # 异常分布：4+0或2+2
                    count_0 = x_values.count(0)
                    count_1 = x_values.count(1)
                    count_2 = x_values.count(2)
                    
                    self._emit(f"❌ 异常分布: {distribution}（count_1={count_1}, count_0={count_0}, count_2={count_2}）", "ERROR")
                    self._emit("⚠️ 正常分布应为: 013, 031, 130, 103, 301, 310", "WARN")
                    self._unified_framework._send_email(
                        "校准失败 - 异常分布",
                        f"检测到异常分布: {distribution}\nX值: X1={x_values[0]}, X2={x_values[1]}, X3={x_values[2]}, X4={x_values[3]}\n正常分布应为: 013, 031, 130, 103, 301, 310"
                    )
                    if self.bot:
                        self.bot.is_paused = True
                    return None
                
                # 点击目标组
                self._emit(f"🎯 校准点击：组{target_idx}（分布={distribution}）", "INFO")
                self._unified_framework._calibrate_click_group(target_idx, use_foreground)
                time.sleep(0.3)  # 等待校准点击生效
                
                # 点击后检查探针状态（必须1AND1消失才算校准成功）
                if self._unified_framework._check_calibration_probes():
                    # 仍然1 AND 1，说明校准失败
                    self._emit(f"❌ 校准后探针仍为1 AND 1（点击组{target_idx}后），暂停并发送邮件", "ERROR")
                    self._unified_framework._send_email(
                        "校准失败 - 点击后仍为1 AND 1",
                        f"校准点击组{target_idx}后，探针仍为1 AND 1状态。分布: {distribution}\nX值: X1={x_values[0]}, X2={x_values[1]}, X3={x_values[2]}, X4={x_values[3]}"
                    )
                    if self.bot:
                        self.bot.is_paused = True
                    return None
                
                # 校准成功：大探针小探针不再是1 AND 1（1AND1已消失）
                self._emit(f"✅ 校准成功（1AND1已消失，点击组{target_idx}有效）", "SUCCESS")
                
                # ✅ 校准成功后，快速点击B一下A一下
                self._emit("⚡ 校准成功，快速点击B一下A一下", "INFO")
                bx, by = self._region_center(reg_b)
                ax, ay = self._region_center(reg_a)
                if use_foreground:
                    window_manager.click(bx, by)
                    time.sleep(0.05)
                    window_manager.click(ax, ay)
                else:
                    window_manager.click_background(bx, by)
                    time.sleep(0.05)
                    window_manager.click_background(ax, ay)
                
                # ✅ 改为每秒4次的频率重新点击刷新点
                click_interval = 0.25  # 每秒4次（0.25秒间隔）
                start_time = time.time()  # 重置超时时间
                last_click_time = 0.0  # 重置点击时间，立即开始点击
                self._emit(f"🚀 校准后加速模式：点击频率改为每秒4次", "INFO")
                continue
            
            # 如果没有校准，检查内核日志中的fightResource/pet/swf/或PetItem信号
            try:
                lines = fetch_kernel_since(initial_cursor)
                if isinstance(lines, list):
                    for idx, line in enumerate(lines):
                        line_str = str(line)
                        # 优先检测PetItem（进入对战的直接信号）
                        if TOKEN_PETITEM in line_str:
                            self._emit("✅ 检测到PetItem信号（已入战），停止点击", "SUCCESS")
                            # 立即开始收集pet IDs（从当前行开始向后搜索）
                            pet_ids = self._collect_fight_pet_ids_immediate(stop_event, current_lines=lines, start_index=idx)
                            if pet_ids:
                                self._emit(f"📋 [立即收集] 检测到PetItem时收集到的pet IDs: {sorted(pet_ids)}", "INFO")
                            return (tx, ty, pet_ids)
                        # 检测fightResource/pet/swf/信号（点击成功且未出现校准）
                        if TOKEN_FIGHT_PET in line_str:
                            self._emit(f"✅ 检测到fightResource/pet/swf/信号（已入战），停止点击，开始收集所有pet IDs\n日志行: {line_str[:200]}", "INFO")
                            
                            # 继续循环，收集所有pet IDs直到检测到skill信号
                            pet_ids = set()
                            collect_start_time = time.time()
                            collect_timeout = 3.0  # 最多收集3秒
                            skill_token = "/resource/fightResource/skill/swf/"
                            found_skill = False
                            collect_cursor = kernel_cursor()  # 记录收集开始时的cursor
                            
                            # 先从当前已获取的日志中收集
                            initial_pet_ids = self._collect_fight_pet_ids_immediate(stop_event, current_lines=lines, start_index=idx)
                            if initial_pet_ids:
                                pet_ids.update(initial_pet_ids)
                            
                            # 继续监听日志，收集所有后续的pet IDs直到skill信号
                            while (time.time() - collect_start_time) < collect_timeout:
                                if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                                    break
                                
                                try:
                                    collect_lines = fetch_kernel_since(collect_cursor)
                                    if isinstance(collect_lines, list):
                                        for collect_line in collect_lines:
                                            collect_line_str = str(collect_line)
                                            # 检测skill信号（停止收集）
                                            if skill_token in collect_line_str:
                                                found_skill = True
                                                self._emit("✅ 检测到skill信号，停止收集pet IDs", "INFO")
                                                break
                                            
                                            # 收集fightResource/pet/swf/
                                            for m in self._FIGHT_PET_SWF_RE.finditer(collect_line_str):
                                                try:
                                                    pid = int(m.group(1))
                                                    if pid not in pet_ids:
                                                        pet_ids.add(pid)
                                                        self._emit(f"📋 收集到pet ID: {pid}（已收集：{sorted(pet_ids)}）", "INFO")
                                                except Exception:
                                                    continue
                                        
                                        collect_cursor = kernel_cursor()
                                        if found_skill:
                                            break
                                except Exception:
                                    pass
                                
                                time.sleep(0.02)
                            
                            if pet_ids:
                                self._emit(f"📋 [完整收集] 检测到fightResource/pet/swf/时收集到的所有pet IDs: {sorted(pet_ids)}", "SUCCESS")
                            return (tx, ty, pet_ids)
                # 更新cursor（只检查新日志）
                initial_cursor = kernel_cursor()
            except Exception:
                pass
            
            time.sleep(0.02)  # 短暂休眠，避免过度占用CPU
        
        # 超时
        self._emit("⚠️ 点击刷新点超时，未检测到fightResource/pet/swf/或PetItem信号", "WARN")
        return None
    
    def _click_opposite_then_target(
        self,
        reg_a: Region,
        reg_b: Region,
        target_reg: Region,
        use_foreground: bool,
        stop_event: threading.Event,
    ) -> Tuple[float, float]:
        """
        当命中目标准备入战：
        - 若当前锚点是 A：先快速点 B；锚点是 B：先快速点 A
        - 然后快速点目标点（真正触发对战）
        返回：目标点击 (x, y)
        """
        # 1) 决定当前 anchor
        anchor = self._last_anchor
        if anchor not in ("A", "B"):
            # 兜底：根据当前位置离 A/B 哪个更近
            try:
                ax, ay = self._region_center(reg_a)
                bx, by = self._region_center(reg_b)
                if self._current_pos is None:
                    anchor = "A"
                else:
                    anchor = "A" if self._dist2(self._current_pos, (ax, ay)) <= self._dist2(self._current_pos, (bx, by)) else "B"
            except Exception:
                anchor = "A"

        # 2) 点反向锚点
        opposite_reg = reg_b if anchor == "A" else reg_a
        opposite_name = "B" if anchor == "A" else "A"
        self._emit(f"⚡ 入战前预点：当前在{anchor} -> 先点{opposite_name}", "DEBUG")
        self._click_region(opposite_reg, use_foreground)
        self._last_anchor = opposite_name  # 更新锚点为刚点过的那个
        self._sleep_abortable(stop_event, 0.06, tick=0.02)

        # 3) 点目标
        tx, ty = self._click_region(target_reg, use_foreground)
        return tx, ty

    # ---------------------------
    # click-trigger post hook
    # ---------------------------
    def _after_trigger_calibrate_or_skip(
        self,
        trigger_xy: Tuple[float, float],
        reg_a: Region,
        route_points: Sequence[Tuple[str, Region]],
        profile: WildCaptureProfile,
        use_foreground: bool,
        stop_event: threading.Event,
        test_mode: bool = False,  # 测试模式
        invincible_first_round: bool = False,  # 第一回合是否使用无敌胶囊
        on_petitem_detected: Optional[Callable[[], None]] = None,  # PetItem检测回调
    ) -> bool:
        """
        点击触发对战后：
        - 如果使用统一框架，调用统一框架的校准逻辑
        - 否则调用 battle_runner.calibrate_after_trigger() 做屏幕校准与 PetItem 入战确认
        - 若 10s 无 PetItem -> 回A、等1s、重标定、跳过本次对战
        返回 True 表示继续对战；False 表示已执行跳过逻辑。
        """
        # ✅ 优先使用统一框架
        if self._unified_framework and self._wild_adapter:
            # 野外模式：Stage 1已在外部完成（ABABAB移动+扫描+点击），跳过Stage 1
            # 如果校准成功需要重新触发，返回False让外部重新执行Stage 1
            # 创建动作回调（需要在stage2之前创建，以便检测到PetItem时立即执行第一回合）
            from core.unified_battle_framework import BattleConfig, BattleMode
            
            def action_callback(round_idx: int) -> str:
                if test_mode:
                    if round_idx == 1:
                        return "skill"
                    else:
                        return "capsule"
                else:
                    if round_idx == 1:
                        if invincible_first_round:
                            return "capsule"
                        else:
                            return "skill"
                    return "capsule"
            
            # 创建临时config用于stage2的第一回合执行
            temp_config = BattleConfig(
                mode=BattleMode.WILD,
                use_foreground=use_foreground,
                skill_key="对战.使用技能一",
                action_callback=action_callback,
                invincible_first_round=invincible_first_round,
                on_petitem_detected=on_petitem_detected,  # 直接传递PetItem检测回调
            )
            
            success, calib_result = self._unified_framework.stage2_calibration_and_petitem(
                trigger_callback=None,  # 野外模式不需要trigger_callback
                use_foreground=use_foreground,
                timeout_s=10.0,
                skip_stage1=True,  # 跳过Stage 1，野外模式已在外部完成
                config=temp_config  # 传递config以便检测到PetItem时立即执行第一回合
            )
            
            if success:
                return True
            
            # 失败或需要重新触发：回A、等1s、重标定
            # 如果是校准成功需要重新触发，也会返回False，外部会重新执行Stage 1
            self._emit("↩ 校准后未入战或需要重新触发：回到 A 并跳过本次对战", "WARN")
            self._click_region(reg_a, use_foreground)
            self._current_pos = self._region_center(reg_a)
            self._last_anchor = "A"
            self._sleep_abortable(stop_event, 1.0)
            
            # ✅ 重新标定所有9个点（与初始标定保持一致）
            map_id = getattr(profile, 'map_swf_id', None)
            self._emit("📏 跳过后重新标定稳态（使用稳健模式，检测所有9个点）", "SYSTEM")
            baseline_start_time = time.time()
            self._recalibrate_all_robust(route_points, stop_event, map_id=map_id)
            baseline_duration = time.time() - baseline_start_time
            
            # ✅ 等待至少5秒确保基线稳定（与初始标定一致）
            min_wait_after_baseline = 5.0
            if baseline_duration < min_wait_after_baseline:
                wait_time = min_wait_after_baseline - baseline_duration
                self._emit(f"⏳ 等待{wait_time:.1f}s后继续扫描（确保基线稳定，稳健模式）", "INFO")
                self._sleep_abortable(stop_event, wait_time)
            return False
        
        # 回退到旧实现
        if not hasattr(self.battle_runner, "calibrate_after_trigger"):
            return True

        abort_fn = lambda: stop_event.is_set() or getattr(self.bot, "stop_current", False)
        try:
            ok = bool(
                self.battle_runner.calibrate_after_trigger(
                    trigger_xy=trigger_xy,
                    use_foreground=use_foreground,
                    abort=abort_fn,
                )
            )
        except TypeError:
            ok = bool(
                self.battle_runner.calibrate_after_trigger(
                    trigger_xy,
                    use_foreground=use_foreground,
                    abort=abort_fn,
                )
            )
        except Exception as e:
            self._emit(f"⚠ calibrate_after_trigger 异常：{e}（按继续处理）", "WARN")
            return True

        if ok:
            return True

        # 失败：回A、等1s、重标定
        self._emit("↩ 校准后未入战：回到 A 并跳过本次对战", "WARN")
        self._click_region(reg_a, use_foreground)
        self._current_pos = self._region_center(reg_a)
        self._last_anchor = "A"
        self._sleep_abortable(stop_event, 1.0)

        # ✅ 重新标定所有9个点（与初始标定保持一致）
        map_id = getattr(profile, 'map_swf_id', None) if 'profile' in locals() else None
        route_hint = getattr(profile, 'route_hint', '') if 'profile' in locals() else ''
        self._emit("📏 跳过后重新标定稳态（使用稳健模式，检测所有9个点）", "SYSTEM")
        baseline_start_time = time.time()
        self._recalibrate_all_robust(route_points, stop_event, map_id=map_id)
        baseline_duration = time.time() - baseline_start_time
        
        # ✅ 等待至少5秒确保基线稳定（与初始标定一致）
        min_wait_after_baseline = 5.0
        if baseline_duration < min_wait_after_baseline:
            wait_time = min_wait_after_baseline - baseline_duration
            self._emit(f"⏳ 等待{wait_time:.1f}s后继续扫描（确保基线稳定，稳健模式）", "INFO")
            self._sleep_abortable(stop_event, wait_time)
        return False
    
    def _check_and_delete_swf_files(self, profile: WildCaptureProfile):
        """
        检查并删除指定的swf文件（根据profile确定要删除的文件）
        
        删除规则：
        - 嘟咕噜：删除 252
        - 双塔：删除 104, 144
        - 闪光皮皮：删除 10
        - 小豆芽：删除 16
        - 螳螂：删除 16, 27
        - 尼奥：检查并恢复所有野外稀有精灵要删除的序号的并集（10, 16, 27, 104, 144, 252）
        """
        try:
            from config import GAME_SWF_FOLDER
            swf_folder = GAME_SWF_FOLDER
            
            # 检查目标文件夹是否存在
            if not os.path.exists(swf_folder):
                self._emit(f"⚠️ 目标文件夹不存在: {swf_folder}，跳过swf文件删除", "WARN")
                return
            
            # 根据profile确定要删除的文件列表
            profile_name_lower = profile.name.lower()
            files_to_delete = set()
            
            if "嘟咕噜" in profile_name_lower:
                files_to_delete.add(252)
            elif "双塔" in profile_name_lower:
                files_to_delete.update([104, 144])
            elif "闪光皮皮" in profile_name_lower:
                files_to_delete.add(10)
            elif "小豆芽" in profile_name_lower:
                files_to_delete.add(16)
            elif "螳螂" in profile_name_lower:
                files_to_delete.update([16, 27])
            # ✅ 尼奥模式不在这里删除swf文件（尼奥模式在run_nieo_mode中调用_check_and_fill_missing_swf_files来恢复补齐）
            # 注意：尼奥模式使用run_nieo_mode方法，不会调用此方法，但为了代码清晰性，这里明确跳过
            
            if not files_to_delete:
                self._emit("ℹ️ 当前profile无需删除swf文件", "INFO")
                return
            
            # 删除文件
            deleted_files = []
            for pet_id in files_to_delete:
                target_file = os.path.join(swf_folder, f"{pet_id}.swf")
                if os.path.exists(target_file):
                    try:
                        os.remove(target_file)
                        deleted_files.append(pet_id)
                        self._emit(f"🗑️ 已删除: {pet_id}.swf", "INFO")
                    except Exception as e:
                        self._emit(f"❌ 删除 {pet_id}.swf 失败: {e}", "ERROR")
            
            if deleted_files:
                self._emit(f"✅ swf文件删除完成：共删除 {len(deleted_files)} 个文件 ({', '.join(map(str, sorted(deleted_files)))})", "SUCCESS")
            else:
                self._emit(f"ℹ️ 无需删除的文件（文件不存在）: {', '.join(map(str, sorted(files_to_delete)))}", "INFO")
            
        except Exception as e:
            self._emit(f"⚠️ swf文件删除异常: {e}，继续执行", "WARN")
            import traceback
            self._emit(traceback.format_exc(), "DEBUG")
    
    def _check_and_fill_missing_swf_files(self):
        """
        检查并补齐缺失的swf文件
        - 检查指定文件夹中的swf文件
        - 如果缺少 possible_missing_swf_list 中的任何文件，从 swf_og/254.swf 复制并重命名
        """
        try:
            from config import GAME_SWF_OG_TEMPLATE, GAME_SWF_FOLDER
            swf_og_path = GAME_SWF_OG_TEMPLATE
            swf_folder = GAME_SWF_FOLDER
            # ✅ 尼奥模式：所有野外稀有精灵要删除的swf的并集 {10, 16, 27, 104, 144, 252}
            possible_missing_swf_list = {10, 16, 27, 104, 144, 252}
            
            # 检查源文件是否存在
            if not os.path.exists(swf_og_path):
                self._emit(f"⚠️ 源文件不存在: {swf_og_path}，跳过swf文件补齐", "WARN")
                return
            
            # 检查目标文件夹是否存在
            if not os.path.exists(swf_folder):
                self._emit(f"⚠️ 目标文件夹不存在: {swf_folder}，跳过swf文件补齐", "WARN")
                return
            
            # 检查并补齐缺失的文件
            missing_files = []
            for pet_id in possible_missing_swf_list:
                target_file = os.path.join(swf_folder, f"{pet_id}.swf")
                if not os.path.exists(target_file):
                    missing_files.append(pet_id)
            
            if not missing_files:
                self._emit("✅ swf文件检查完成：所有文件都存在", "INFO")
                return
            
            # 补齐缺失的文件
            self._emit(f"🔧 发现缺失的swf文件: {sorted(missing_files)}，开始补齐...", "INFO")
            for pet_id in missing_files:
                target_file = os.path.join(swf_folder, f"{pet_id}.swf")
                try:
                    shutil.copy2(swf_og_path, target_file)
                    self._emit(f"✅ 已补齐: {pet_id}.swf", "SUCCESS")
                except Exception as e:
                    self._emit(f"❌ 补齐 {pet_id}.swf 失败: {e}", "ERROR")
            
            self._emit(f"✅ swf文件补齐完成：共补齐 {len(missing_files)} 个文件", "SUCCESS")
            
        except Exception as e:
            self._emit(f"⚠️ swf文件检查/补齐异常: {e}，继续执行", "WARN")
            import traceback
            self._emit(traceback.format_exc(), "DEBUG")
    
    def _recover_pets(self, use_foreground: bool, stop_event: threading.Event, skip_return_storage: bool = False, nie_family_id: Optional[int] = None, profile: Optional[WildCaptureProfile] = None):
        """
        恢复流程（包含放回仓库，支持尼尔家族特殊逻辑）：
        
        正常情况（没有尼尔家族）：
        1. 打开精灵背包
        2. 如果skip_return_storage为False（捕捉成功后）：
           - 等待2.5s后双击精灵三 → 放回仓库 → 双击精灵一 → 恢复 → 1AND1确认 → 点击打开精灵背包（关闭）
        3. 如果skip_return_storage为True（第0次战斗后）：
           - 等待2.5s后直接双击精灵一 → 恢复 → 1AND1确认 → 点击打开精灵背包（关闭）
        
        尼尔家族416（尼奥，切出的是精灵二）：
        - 捕捉后：双击精灵三 → 放回仓库 → 双击精灵二 → 恢复 → 1AND1确认 → 点击打开精灵背包（关闭）
        - 注意：只恢复精灵二，不恢复精灵一
        
        尼尔家族77/310（尼尔/闪光尼尔，切出的是精灵三）：
        - 捕捉后：双击精灵三（刚捕捉的精灵） → 放回仓库 → 双击精灵三（之前的精灵三回来了） → 恢复 → 1AND1确认 → 点击打开精灵背包（关闭）
        - 注意：只恢复精灵三，不恢复精灵一
        
        Args:
            nie_family_id: 尼尔家族ID（416=尼奥，77/310=尼尔/闪光尼尔，None=没有尼尔家族）
        """
        try:
            # ✅ 检查是否需要刷新重连（在打开背包之前）
            if getattr(self, '_should_refresh_reconnect', False):
                self._should_refresh_reconnect = False
                self._emit("🔄 [电池刷新] 检测到需要刷新重连，执行刷新流程", "WARN")
                if profile is None:
                    # 如果没有传入profile，尝试从当前任务获取
                    profile = getattr(self, '_current_profile', None)
                if profile:
                    self._execute_battery_refresh_reconnect(profile, use_foreground, stop_event)
                    # 刷新重连后，重新启动任务
                    self.run(stop_event, use_foreground, profile, 
                            getattr(self, '_test_mode', False),
                            getattr(self, '_smart_tracking_mode', False),
                            getattr(self, '_xiaodouya_nie_test_mode', False))
                    return
                else:
                    self._emit("⚠️ [电池刷新] 无法获取profile，跳过刷新重连", "WARN")
            
            # 1. 打开精灵背包
            self._emit("💼 打开精灵背包", "INFO")
            bag_open_key = "精灵背包.打开精灵背包"
            bag_open_btn_key = "精灵背包.打开精灵背包按钮"
            
            # 尝试点击按钮，如果没有按钮则点击区域
            try:
                self._click_region(bag_open_btn_key, use_foreground)
            except KeyError:
                self._click_region(bag_open_key, use_foreground)
            
            # 打开精灵背包后等待2.5s，确保背包完全打开
            self._sleep_abortable(stop_event, 2.5)
            
            # 注意：探针扫描已移到恢复和1AND1之后，避免被其他UI元素污染
            
            # 2. 根据skip_return_storage决定是否执行放回仓库流程
            if not skip_return_storage:
                # 捕捉成功后的恢复：先通过颜色检测识别目标精灵并放回仓库，再恢复
                # 2.1 使用颜色检测识别要放回仓库的精灵位置（精灵二、三、四）
                target_pet_position = None
                # 使用颜色检测替代OCR
                target_pet_position = self._identify_target_pet_by_color(use_foreground)
                
                if target_pet_position:
                    # 找到目标精灵，点击6次对应位置（每次间隔0.25s）
                    pet_key = f"精灵背包.精灵{target_pet_position}"
                    pet_btn_key = f"精灵背包.精灵{target_pet_position}按钮"
                    
                    self._emit(f"🐾 [放回仓库] 颜色检测成功：精灵{target_pet_position}需要放回仓库（蓝色血条），准备点击6次放回仓库", "INFO")
                    
                    # 点击6次，每次间隔0.25秒
                    click_interval = 0.25
                    for i in range(6):
                        try:
                            self._click_region(pet_btn_key, use_foreground)
                        except KeyError:
                            self._click_region(pet_key, use_foreground)
                            if i < 5:  # 最后一次不需要等待
                                self._sleep_abortable(stop_event, click_interval)
                    
                    # 点击6次后等待1.5s（稀有精灵和尼奥模式）
                    self._sleep_abortable(stop_event, 1.5)
                    
                    # 2.2 点击一次"放回仓库"
                    self._emit(f"📦 [放回仓库] 点击放回仓库（精灵{target_pet_position}）", "INFO")
                    return_storage_key = "精灵背包.放回仓库"
                    return_storage_btn_key = "精灵背包.放回仓库按钮"
                    
                    try:
                        self._click_region(return_storage_btn_key, use_foreground)
                    except KeyError:
                        self._click_region(return_storage_key, use_foreground)
                    
                    # 点击放回仓库后等待1s
                    self._sleep_abortable(stop_event, 1.0)
                else:
                    # 颜色检测失败，回退到旧逻辑（点击6次精灵三）
                    self._emit("⚠️ [放回仓库] 颜色检测失败，使用默认逻辑（点击6次精灵三）", "WARN")
                    pet_three_key = "精灵背包.精灵三"
                    pet_three_btn_key = "精灵背包.精灵三按钮"
                    
                    # 点击6次，每次间隔0.25秒
                    click_interval = 0.25
                    for i in range(6):
                        try:
                            self._click_region(pet_three_btn_key, use_foreground)
                        except KeyError:
                            self._click_region(pet_three_key, use_foreground)
                            if i < 5:  # 最后一次不需要等待
                                self._sleep_abortable(stop_event, click_interval)
                    
                    # 点击6次后等待1.5s（稀有精灵和尼奥模式）
                    self._sleep_abortable(stop_event, 1.5)
                    
                    # 点击一次"放回仓库"
                    self._emit("📦 [放回仓库] 点击放回仓库（默认精灵三）", "INFO")
                    return_storage_key = "精灵背包.放回仓库"
                    return_storage_btn_key = "精灵背包.放回仓库按钮"
                    
                    try:
                        self._click_region(return_storage_btn_key, use_foreground)
                    except KeyError:
                        self._click_region(return_storage_key, use_foreground)
                    
                    # 点击放回仓库后等待1s
                    self._sleep_abortable(stop_event, 1.0)
                
                # 2.3 先执行恢复，然后1AND1，最后扫描探针（避免被污染）
                # 注意：这里先不扫描探针，而是在恢复和1AND1之后扫描
                # 恢复逻辑暂时使用默认位置，稍后会在1AND1后扫描并更新
                if nie_family_id == 416:
                    # 416（尼奥）：需要恢复闪光艾菲亚（先使用默认位置，稍后在1AND1后扫描并更新）
                    recover_pos = "二"  # 默认位置，稍后会在1AND1后扫描并更新
                    self._emit(f"🔄 [416尼奥] 恢复闪光艾菲亚（精灵{recover_pos}，默认位置，稍后会扫描更新）", "INFO")
                    
                    # 双击目标精灵
                    self._emit(f"🐾 双击精灵{recover_pos}（准备恢复）", "INFO")
                    pet_key = f"精灵背包.精灵{recover_pos}"
                    pet_btn_key = f"精灵背包.精灵{recover_pos}按钮"
                    try:
                        self._click_region_twice(pet_btn_key, use_foreground)
                    except KeyError:
                        self._click_region_twice(pet_key, use_foreground)
                    
                    # 双击后等待0.5s
                    self._sleep_abortable(stop_event, 0.5)
                    
                    # 点击"精灵恢复"
                    self._emit("💊 点击精灵恢复（精灵二）", "INFO")
                    recover_key = "精灵背包.精灵恢复"
                    recover_btn_key = "精灵背包.精灵恢复按钮"
                    try:
                        self._click_region(recover_btn_key, use_foreground)
                    except KeyError:
                        self._click_region(recover_key, use_foreground)
                    
                    # 精灵恢复后等待1.0s
                    self._sleep_abortable(stop_event, 1.0)
                    
                    # 使用1AND1确认残留的恢复后的确认
                    self._emit(f"⏳ 使用1AND1确认残留的恢复后的确认（精灵{recover_pos}）", "INFO")
                    if self._unified_framework:
                        from core.unified_battle_framework import BattleConfig, BattleMode
                        temp_config = BattleConfig(
                            mode=BattleMode.WILD,
                            use_foreground=use_foreground,
                            abort_check=lambda: stop_event.is_set() or getattr(self.bot, "stop_current", False),
                        )
                        self._unified_framework._wait_for_confirm_probes(temp_config, timeout_s=2.0)
                    
                elif nie_family_id in (77, 310):
                    # 77/310（尼尔/闪光尼尔）：需要恢复艾斯菲格（先使用默认位置，稍后在1AND1后扫描并更新）
                    recover_pos = "三"  # 默认位置，稍后会在1AND1后扫描并更新
                    self._emit(f"🔄 [77/310尼尔] 恢复艾斯菲格（精灵{recover_pos}，默认位置，稍后会扫描更新）", "INFO")
                    
                    # 双击目标精灵
                    self._emit(f"🐾 双击精灵{recover_pos}（准备恢复）", "INFO")
                    pet_key = f"精灵背包.精灵{recover_pos}"
                    pet_btn_key = f"精灵背包.精灵{recover_pos}按钮"
                    try:
                        self._click_region_twice(pet_btn_key, use_foreground)
                    except KeyError:
                        self._click_region_twice(pet_key, use_foreground)
                    
                    # 双击后等待0.5s
                    self._sleep_abortable(stop_event, 0.5)
                    
                    # 点击"精灵恢复"
                    self._emit("💊 点击精灵恢复（精灵三）", "INFO")
                    recover_key = "精灵背包.精灵恢复"
                    recover_btn_key = "精灵背包.精灵恢复按钮"
                    try:
                        self._click_region(recover_btn_key, use_foreground)
                    except KeyError:
                        self._click_region(recover_key, use_foreground)
                    
                    # 精灵恢复后等待1.0s
                    self._sleep_abortable(stop_event, 1.0)
                    
                    # 使用1AND1确认残留的恢复后的确认
                    self._emit(f"⏳ 使用1AND1确认残留的恢复后的确认（精灵{recover_pos}）", "INFO")
                    if self._unified_framework:
                        from core.unified_battle_framework import BattleConfig, BattleMode
                        temp_config = BattleConfig(
                            mode=BattleMode.WILD,
                            use_foreground=use_foreground,
                            abort_check=lambda: stop_event.is_set() or getattr(self.bot, "stop_current", False),
                        )
                        self._unified_framework._wait_for_confirm_probes(temp_config, timeout_s=2.0)
                    
                    # 在1AND1消失后，如果背包未关闭，直接扫描探针（避免被污染）
                    # 注意：1AND1不会关闭背包，所以背包应该还是打开的
                    self._emit("🔍 在1AND1消失后，扫描精灵二和精灵三探针，识别闪光艾菲亚和艾斯菲格", "INFO")
                    flash_aifeia_pos, aisifeige_pos = self._scan_pet_probes_to_identify_pets(use_foreground)
                    # 更新实例变量，供后续使用
                    self._flash_aifeia_pos = flash_aifeia_pos
                    self._aisifeige_pos = aisifeige_pos
                    if flash_aifeia_pos and aisifeige_pos:
                        self._emit(f"✅ 探针扫描完成：闪光艾菲亚=精灵{flash_aifeia_pos}，艾斯菲格=精灵{aisifeige_pos}", "SUCCESS")
                    
                    # 扫描成功后，点击打开精灵背包关闭它（确保背包关闭）
                    self._emit("💼 扫描完成后，点击打开精灵背包关闭它", "INFO")
                    try:
                        self._click_region(bag_open_btn_key, use_foreground)
                    except KeyError:
                        self._click_region(bag_open_key, use_foreground)
                    self._sleep_abortable(stop_event, 0.5)
                    
                else:
                    # 正常情况（没有尼尔家族）：恢复精灵一
                    # 3. 双击精灵一（准备恢复）
                    self._emit("🐾 双击精灵一（准备恢复）", "INFO")
                    pet_one_key = "精灵背包.精灵一"
                    pet_one_btn_key = "精灵背包.精灵一按钮"
                    
                    try:
                        self._click_region_twice(pet_one_btn_key, use_foreground)
                    except KeyError:
                        self._click_region_twice(pet_one_key, use_foreground)
                    
                    # 双击后等待0.5s
                    self._sleep_abortable(stop_event, 0.5)
                    
                    # 4. 点击"精灵恢复"
                    self._emit("💊 点击精灵恢复", "INFO")
                    recover_key = "精灵背包.精灵恢复"
                    recover_btn_key = "精灵背包.精灵恢复按钮"
                    
                    try:
                        self._click_region(recover_btn_key, use_foreground)
                    except KeyError:
                        self._click_region(recover_key, use_foreground)
                    
                    # 精灵恢复后等待1.0s，确保恢复操作完成
                    self._sleep_abortable(stop_event, 1.0)
                    
                    # 5. 用1AND1来确认残留的恢复后的确认
                    self._emit("⏳ 使用1AND1确认残留的恢复后的确认", "INFO")
                    if self._unified_framework:
                        from core.unified_battle_framework import BattleConfig, BattleMode
                        # 创建临时配置用于1AND1确认
                        temp_config = BattleConfig(
                            mode=BattleMode.WILD,
                            use_foreground=use_foreground,
                            abort_check=lambda: stop_event.is_set() or getattr(self.bot, "stop_current", False),
                        )
                        # 使用统一框架的1AND1确认方法
                        self._unified_framework._wait_for_confirm_probes(temp_config, timeout_s=2.0)
                    
                    # 5.5 在1AND1消失后，如果背包未关闭，直接扫描探针（避免被污染）
                    # 注意：1AND1不会关闭背包，所以背包应该还是打开的
                    self._emit("🔍 在1AND1消失后，扫描精灵二和精灵三探针，识别闪光艾菲亚和艾斯菲格", "INFO")
                    flash_aifeia_pos, aisifeige_pos = self._scan_pet_probes_to_identify_pets(use_foreground)
                    # 更新实例变量，供后续使用
                    self._flash_aifeia_pos = flash_aifeia_pos
                    self._aisifeige_pos = aisifeige_pos
                    if flash_aifeia_pos and aisifeige_pos:
                        self._emit(f"✅ 探针扫描完成：闪光艾菲亚=精灵{flash_aifeia_pos}，艾斯菲格=精灵{aisifeige_pos}", "SUCCESS")
                    
                    # 5.6 扫描成功后，点击打开精灵背包关闭它（确保背包关闭）
                    self._emit("💼 扫描完成后，点击打开精灵背包关闭它", "INFO")
                    try:
                        self._click_region(bag_open_btn_key, use_foreground)
                    except KeyError:
                        self._click_region(bag_open_key, use_foreground)
                    self._sleep_abortable(stop_event, 0.5)
            else:
                # skip_return_storage=True（第0次战斗后）：使用默认位置恢复（探针扫描将在1AND1后进行）
                if nie_family_id == 416:
                    # 416（尼奥）：恢复闪光艾菲亚（使用默认位置精灵二，稍后在1AND1后扫描并更新）
                    recover_pos = "二"
                    self._emit(f"🔄 [416尼奥-第0次] 恢复闪光艾菲亚（精灵{recover_pos}，默认位置，稍后会扫描更新）", "INFO")
                elif nie_family_id in (77, 310):
                    # 77/310（尼尔/闪光尼尔）：恢复艾斯菲格（使用默认位置精灵三，稍后在1AND1后扫描并更新）
                    recover_pos = "三"
                    self._emit(f"🔄 [77/310尼尔-第0次] 恢复艾斯菲格（精灵{recover_pos}，默认位置，稍后会扫描更新）", "INFO")
                else:
                    # 正常情况（没有尼尔家族）：恢复精灵一
                    recover_pos = "一"
                    self._emit("🔄 [正常-第0次] 恢复精灵一", "INFO")
                
                # 双击目标精灵（准备恢复）
                self._emit(f"🐾 双击精灵{recover_pos}（准备恢复）", "INFO")
                pet_key = f"精灵背包.精灵{recover_pos}"
                pet_btn_key = f"精灵背包.精灵{recover_pos}按钮"
                
                try:
                    self._click_region_twice(pet_btn_key, use_foreground)
                except KeyError:
                    self._click_region_twice(pet_key, use_foreground)
                
                # 双击后等待0.5s
                self._sleep_abortable(stop_event, 0.5)
                
                # 4. 点击"精灵恢复"
                self._emit("💊 点击精灵恢复", "INFO")
                recover_key = "精灵背包.精灵恢复"
                recover_btn_key = "精灵背包.精灵恢复按钮"
                
                try:
                    self._click_region(recover_btn_key, use_foreground)
                except KeyError:
                    self._click_region(recover_key, use_foreground)
                
                # 精灵恢复后等待1.0s，确保恢复操作完成
                self._sleep_abortable(stop_event, 1.0)
                
                # 5. 用1AND1来确认残留的恢复后的确认
                self._emit("⏳ 使用1AND1确认残留的恢复后的确认", "INFO")
                if self._unified_framework:
                    from core.unified_battle_framework import BattleConfig, BattleMode
                    # 创建临时配置用于1AND1确认
                    temp_config = BattleConfig(
                        mode=BattleMode.WILD,
                        use_foreground=use_foreground,
                        abort_check=lambda: stop_event.is_set() or getattr(self.bot, "stop_current", False),
                    )
                    # 使用统一框架的1AND1确认方法
                    self._unified_framework._wait_for_confirm_probes(temp_config, timeout_s=2.0)
                
                # 5.5 在1AND1消失后，如果背包未关闭，直接扫描探针（避免被污染）
                # 注意：1AND1不会关闭背包，所以背包应该还是打开的
                self._emit("🔍 在1AND1消失后，扫描精灵二和精灵三探针，识别闪光艾菲亚和艾斯菲格", "INFO")
                flash_aifeia_pos, aisifeige_pos = self._scan_pet_probes_to_identify_pets(use_foreground)
                # 更新实例变量，供后续使用
                self._flash_aifeia_pos = flash_aifeia_pos
                self._aisifeige_pos = aisifeige_pos
                if flash_aifeia_pos and aisifeige_pos:
                    self._emit(f"✅ 探针扫描完成：闪光艾菲亚=精灵{flash_aifeia_pos}，艾斯菲格=精灵{aisifeige_pos}", "SUCCESS")
                
                # 5.6 扫描成功后，点击打开精灵背包关闭它（确保背包关闭）
                self._emit("💼 扫描完成后，点击打开精灵背包关闭它", "INFO")
                try:
                    self._click_region(bag_open_btn_key, use_foreground)
                except KeyError:
                    self._click_region(bag_open_key, use_foreground)
                self._sleep_abortable(stop_event, 0.5)
            
            if skip_return_storage:
                self._emit("✅ 恢复流程完成（第0次战斗，未执行放回仓库）", "SUCCESS")
            else:
                self._emit("✅ 恢复流程完成（包含放回仓库）", "SUCCESS")
            
        except KeyError as e:
            self._emit(f"⚠️ 恢复流程失败：缺少区域 {e}，跳过恢复", "WARN")
        except Exception as e:
            self._emit(f"⚠️ 恢复流程异常：{e}，跳过恢复", "WARN")

    # ---------------------------
    # map entry script
    # ---------------------------
    def _execute_map_entry_script(self, map_swf_id: int, use_foreground: bool, stop_event: threading.Event) -> bool:
        """
        执行地图进入脚本（从 地图\\{map_swf_id}.json 读取并执行）
        
        Args:
            map_swf_id: 地图ID
            use_foreground: 是否前台执行
            stop_event: 停止事件
            
        Returns:
            True=执行成功，False=执行失败
        """
        try:
            import json
            from core.utils import window_manager
            
            # 构建脚本路径：assets/regions/地图/{map_swf_id}.json
            from config import REGIONS_PATH
            script_path = os.path.join(REGIONS_PATH, "地图", f"{map_swf_id}.json")
            
            if not os.path.exists(script_path):
                self._emit(f"⚠️ 地图进入脚本不存在: {script_path}，跳过", "WARN")
                return False
            
            # 读取脚本（这是一个region文件，需要检查是否有region key）
            with open(script_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # 检查是否是region格式（有key字段）
            if "key" in data:
                # 这是region文件，使用region来点击
                region_key = data.get("key")
                self._emit(f"🗺️ 点击地图进入点：{region_key}", "SYSTEM")
                try:
                    region = self.regions.get(region_key)
                    if region:
                        self._click_region(region, use_foreground)
                        self._emit(f"✅ 已点击地图进入点：{region_key}", "SUCCESS")
                        return True
                    else:
                        self._emit(f"⚠️ 找不到region: {region_key}，跳过", "WARN")
                        return False
                except Exception as e:
                    self._emit(f"⚠️ 点击地图进入点失败: {e}，跳过", "WARN")
                    return False
            else:
                # 这是脚本格式（有steps字段），执行脚本步骤
                steps = data.get("steps", [])
                if not steps:
                    self._emit(f"⚠️ 地图进入脚本为空（没有 steps）：{os.path.basename(script_path)}", "WARN")
                    return False
                
                self._emit(f"📜 开始执行地图进入脚本: {os.path.basename(script_path)}", "SYSTEM")
                
                # 执行脚本步骤
                for idx, step in enumerate(steps, start=1):
                    # 检查停止信号
                    if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                        self._emit("⛔ 地图进入脚本中止（stop_current）", "WARN")
                        return False
                    
                    # 解析步骤（兼容新格式和老格式）
                    action = step.get("action", "").lower()
                    if action == "click":
                        # 新格式：{"action":"click","x":..,"y":..,"delay":..}
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
                    elif "pos" in step:
                        # 老格式：{"pos":[x,y],"delay":..,"bg": true/false}
                        pos = step.get("pos")
                        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
                            continue
                        
                        gx, gy = float(pos[0]), float(pos[1])
                        delay = float(step.get("delay", 0.2))
                        if delay < 0:
                            delay = 0.0
                        
                        # 检查是否需要前台执行（优先使用传入的参数）
                        step_bg = step.get("bg", True)
                        step_use_foreground = not step_bg if not use_foreground else use_foreground
                        
                        time.sleep(delay)
                        
                        if step_use_foreground:
                            window_manager.click(gx, gy)
                        else:
                            window_manager.click_background(gx, gy)
                
                self._emit(f"✅ 地图进入脚本执行完成: {os.path.basename(script_path)}", "SUCCESS")
                return True
            
        except Exception as e:
            self._emit(f"💥 执行地图进入脚本异常: {e}", "ERROR")
            import traceback
            self._emit(traceback.format_exc(), "ERROR")
        return False

    # ---------------------------
    # map / route resolve
    # ---------------------------
    def _get_last_map_id(self) -> Optional[int]:
        """
        获取最后一个出现的map ID（从所有历史日志中搜索）
        
        返回: map ID（如果找到），否则返回None
        """
        try:
            from core.logger import fetch_kernel_since
            
            # 从cursor=0开始获取所有历史日志
            all_lines = fetch_kernel_since(0)
            if not isinstance(all_lines, list):
                all_lines = []
            
            # 从最新的日志开始，向前搜索最后一个map
            last_map_id = None
            for line in reversed(all_lines):  # 从最新到最旧
                line_str = str(line)
                m = self._MAP_SWF_RE.search(line_str)
                if m:
                    try:
                        last_map_id = int(m.group(1))
                        break  # 找到最新的map就停止
                    except Exception:
                        continue
            
            if last_map_id is not None:
                self._emit(f"🔍 找到最后一个map ID: {last_map_id}", "DEBUG")
            
            return last_map_id
        except Exception as e:
            self._emit(f"⚠️ 获取最后map ID失败: {e}", "DEBUG")
            return None
    
    def _wait_for_map_ready(self, profile: WildCaptureProfile, stop_event: threading.Event) -> bool:
        """
        等待进入目标地图（支持在等待过程中被停止）
        如果已经在地图中（历史日志中已有），立即返回True
        """
        if stop_event.is_set() or getattr(self.bot, "stop_current", False):
            return False

        # 等待地图，但需要频繁检查停止信号
        # 使用较短的超时时间，因为toXXX.json脚本执行完后通常已经在地图中
        # _wait_kernel_contains_with_abort 会先检查历史日志，所以通常能立即返回
        if not self._wait_kernel_contains_with_abort(
            f"/resource/map/{profile.map_swf_id}.swf", 
            timeout_s=10.0,  # 缩短超时时间，因为脚本执行后通常已经在地图中
            poll=0.05,
            stop_event=stop_event
        ):
            return False
            
        if stop_event.is_set() or getattr(self.bot, "stop_current", False):
            return False
            
        # 等待newNpc信号，也使用较短的超时时间
        if not self._wait_kernel_contains_with_abort(
            self.KEY_NEWNPC_MULTI, 
            timeout_s=10.0,  # 缩短超时时间
            poll=0.05,
            stop_event=stop_event
        ):
            return False
        return True

    def _wait_kernel_contains_with_abort(self, substr: str, timeout_s: float, poll: float, stop_event: threading.Event) -> bool:
        """
        等待内核日志包含指定字符串，但在等待过程中频繁检查停止信号
        不从历史日志中查找，只等待新的日志出现（确保是地图进入脚本执行后的结果）
        """
        start_time = time.time()
        from core.logger import kernel_cursor
        
        # 记录当前cursor，只检查新的日志（不检查历史日志）
        cursor = kernel_cursor()
        
        while (time.time() - start_time) < timeout_s:
            # 频繁检查停止信号
            if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                return False
            
            # 检查新的内核日志（从上次cursor之后的新日志）
            try:
                from core.logger import fetch_kernel_since
                lines = fetch_kernel_since(cursor)
                if isinstance(lines, list):
                    for line in lines:
                        if substr in str(line):
                            return True
                    # 更新cursor
                    cursor = kernel_cursor()
            except Exception:
                pass
            
            # 短暂休眠后继续检查
            time.sleep(poll)
        
        # 超时
        return False

    def _wait_kernel_contains_compat(self, substr: str, timeout_s: float, poll: float) -> bool:
        try:
            return bool(wait_kernel_contains(substr, timeout_s=timeout_s, poll=poll, cursor=None))
        except TypeError:
            try:
                return bool(wait_kernel_contains(substr, timeout=timeout_s, poll=poll))
            except Exception:
                return False
        except Exception:
            return False

    @staticmethod
    def _prefix_variants(prefix: str) -> List[str]:
        prefix = (prefix or "").strip()
        if not prefix:
            return []
        variants = [prefix]
        if prefix.endswith("地图"):
            variants.append(prefix.replace("地图", ""))
        else:
            variants.append(prefix + "地图")
        out: List[str] = []
        for v in variants:
            if v and v not in out:
                out.append(v)
        return out

    def _resolve_route_regions(self, prefix: str, require_b: bool = True, use_z: bool = False) -> Tuple[List[Tuple[str, Region]], Region, Region]:
        """
        解析路线区域（1-9点和A/B点或Z点）
        
        Args:
            prefix: 路线前缀（如"尼奥一"、"尼奥二"）
            require_b: 是否要求B点（尼奥模式不需要B点，设为False）
            use_z: 是否使用Z点代替A点（尼奥模式使用Z点，设为True）
        
        Returns:
            (route_points, reg_a_or_z, reg_b) - 路线点列表、A点/Z点区域、B点区域（如果require_b=False，reg_b可能为None）
        """
        candidates = self._prefix_variants(prefix)

        reg_a_or_z = reg_b = None
        used_ab = None
        point_key = "Z" if use_z else "A"
        
        for p in candidates:
            rz = self.regions.get(f"{p}.{point_key}")
            if not rz:
                continue
            
            if require_b:
                # 需要B点：同时查找A/Z和B
                rb = self.regions.get(f"{p}.B")
                if rz and rb:
                    reg_a_or_z, reg_b = rz, rb
                used_ab = p
                break
            else:
                # 不需要B点：只查找A/Z点
                reg_a_or_z = rz
                used_ab = p
                break
        
        if not reg_a_or_z:
            raise KeyError(
                f"缺少 {point_key} 区域：prefix candidates={candidates} | suggest={self.regions.suggest(prefix, limit=30)}"
            )
        
        if require_b and not reg_b:
            raise KeyError(
                f"缺少 B 区域：prefix candidates={candidates} | suggest={self.regions.suggest(prefix, limit=30)}"
            )
        
        # 如果不需要B点但找到了B点，使用B点；如果没找到B点，使用A/Z点作为B点（兼容性）
        if not require_b and not reg_b:
            reg_b = reg_a_or_z  # 使用A/Z点作为B点（虽然不会使用，但保持返回类型一致）

        def pick_key(p: str, i: int) -> Optional[str]:
            tries = [
                f"{p}.{i}",
                f"{p}.路径{i}",
                f"{p}.路径 {i}",
                f"{p}.path{i}",
                f"{p}.path {i}",
            ]
            for k in tries:
                if self.regions.get(k):
                    return k
            return None

        points: List[Tuple[str, Region]] = []
        used_pts = None
        for p in candidates:
            tmp: List[Tuple[str, Region]] = []
            ok = True
            for i in range(1, 10):
                kk = pick_key(p, i)
                if not kk:
                    ok = False
                    break
                rr = self.regions.get(kk)
                if not rr:
                    ok = False
                    break
                tmp.append((kk, rr))
            if ok:
                points = tmp
                used_pts = p
                break

        if not points:
            raise KeyError(
                f"未发现 1~9 刷新点：prefix candidates={candidates} | suggest={self.regions.suggest(prefix, limit=80)}"
            )

        used = used_pts or used_ab or prefix
        self._emit(f"🧩 路线解析成功 prefix={used} points={len(points)}", "INFO")
        return points, reg_a_or_z, reg_b

    def _detect_unexpected_map(self, lines: Sequence[str], expected_map_id: int) -> Optional[int]:
        exp = int(expected_map_id)
        for ln in lines or []:
            m = self._MAP_SWF_RE.search(str(ln))
            if not m:
                continue
            try:
                mid = int(m.group(1))
            except Exception:
                continue
            if mid != exp:
                return mid
        return None

    # ---------------------------
    # baseline / scan
    # ---------------------------
    def _recalibrate_all(self, points: Sequence[Tuple[str, Region]], stop_event: threading.Event, map_id: Optional[int] = None) -> None:
        """
        重新标定所有区域的基线
        
        Args:
            points: 区域点列表
            stop_event: 停止事件
            map_id: 地图ID（❌ DISABLED: 已摒弃使用长期基线的想法）
        """
        # ❌ DISABLED: 已摒弃使用长期基线的想法，持续点击稀有精灵即可
        
        for key, reg in points:
            if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                return
            self._wait_if_paused(stop_event)

            # 使用正常测量（不使用长期基线数据）
            mean_sig, jitter = self._measure_baseline(reg, samples=4, downsample=(8, 8))
            
            th = jitter * 5.0 + 12.0
            self._baseline[key] = mean_sig
            self._jitter[key] = jitter
            self._threshold[key] = th
    
    def _recalibrate_all_robust(self, points: Sequence[Tuple[str, Region]], stop_event: threading.Event, map_id: Optional[int] = None) -> None:
        """
        稳健标定所有区域的基线（用于稀有精灵模式，有充足时间做更稳健的标定）
        
        Args:
            points: 区域点列表
            stop_event: 停止事件
            map_id: 地图ID（❌ DISABLED: 已摒弃使用长期基线的想法）
        """
        for key, reg in points:
            if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                return
            self._wait_if_paused(stop_event)

            # ✅ 稳健标定：使用6次采样（比正常标定的4次更多，更稳健）
            mean_sig, jitter = self._measure_baseline(reg, samples=6, downsample=(8, 8))
            
            th = jitter * 5.0 + 12.0
            self._baseline[key] = mean_sig
            self._jitter[key] = jitter
            self._threshold[key] = th
    
    def _wait_for_ui_stable_before_recalibrate(
        self, 
        points: Sequence[Tuple[str, Region]], 
        stop_event: threading.Event,
        timeout_s: float = 3.0
    ) -> None:
        """
        在重新标定前等待UI稳定（检测是否有UI干扰，如打开精灵仓库）
        
        如果检测到UI干扰（多个点的diff异常大），等待UI关闭后再继续
        这样可以确保重新标定使用的是正常的场景颜色，而不是UI界面颜色
        
        Args:
            points: 区域点列表
            stop_event: 停止事件
            timeout_s: 最大等待时间（秒）
        """
        start_time = time.time()
        check_interval = 0.2  # 每0.2秒检查一次
        last_check_time = 0.0
        
        # 计算异常阈值：如果某个点的diff超过其阈值的3倍，认为是异常（可能是UI干扰）
        abnormal_count_threshold = 3  # 如果超过3个点异常，认为是UI干扰
        
        while (time.time() - start_time) < timeout_s:
            if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                return
            
            now = time.time()
            if now - last_check_time < check_interval:
                time.sleep(0.05)
                continue
            
            last_check_time = now
            
            # 快速扫描所有点，检查是否有异常大的变化
            abnormal_count = 0
            for key, reg in points:
                base = self._baseline.get(key)
                if base is None:
                    continue
                
                sig = self._grab_sig(reg, downsample=(8, 8))
                diff = self._sig_diff(sig, base)
                th = self._threshold.get(key, 9999.0)
                
                # 如果diff超过阈值的3倍，认为是异常（可能是UI干扰）
                if diff > th * 3.0:
                    abnormal_count += 1
            
            # 如果异常点数量少于阈值，认为UI稳定，可以继续标定
            if abnormal_count < abnormal_count_threshold:
                if abnormal_count > 0:
                    self._emit(f"✅ UI稳定检查通过（{abnormal_count}个点异常，低于阈值{abnormal_count_threshold}），开始重新标定", "INFO")
                return
            
            # 如果检测到UI干扰，继续等待
            # 节流输出：每1秒输出一次
            elapsed = now - start_time
            if int(elapsed) != int(elapsed - check_interval):
                self._emit(
                    f"⏳ 检测到UI干扰（{abnormal_count}个点异常），等待UI关闭后重新标定...（已等待{elapsed:.1f}s）",
                    "WARN"
                )
        
        # 超时后仍然有干扰，输出警告但继续标定（可能是其他原因导致的异常）
        self._emit(f"⚠️ UI稳定检查超时（{timeout_s}s），可能有UI干扰，但继续标定", "WARN")
    
    def _quick_recalibrate_all(self, points: Sequence[Tuple[str, Region]], stop_event: threading.Event, samples: int = 4) -> None:
        """
        重新标定所有区域的基线（用于mp3出现时清除过期扫描命中和噪声）
        
        Args:
            points: 区域点列表
            stop_event: 停止事件
            samples: 采样次数（默认4次，中等稳健标定）
                     - 稀有精灵模式：4次（初始6次已经很稳健，重新标定用4次清除噪声即可）
                     - 如果需要快速标定，可以传2次
        """
        for key, reg in points:
            if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                return
            self._wait_if_paused(stop_event)

            # 重新测量基线（清除可能的噪声和过期扫描命中）
            mean_sig, jitter = self._measure_baseline(reg, samples=samples, downsample=(8, 8))
            
            th = jitter * 5.0 + 12.0
            self._baseline[key] = mean_sig
            self._jitter[key] = jitter
            self._threshold[key] = th

    # ❌ DISABLED: 已摒弃使用长期基线的想法，持续点击稀有精灵即可
    def _load_long_term_baseline(self, map_id: int) -> bool:
        """
        从本地文件加载长期保存的基线数据
        
        Args:
            map_id: 地图ID
            
        Returns:
            True表示成功加载，False表示加载失败或文件不存在
        """
        return False  # ❌ DISABLED
        # try:
        #     file_path = os.path.join(self._baseline_data_dir, f"baseline_map_{map_id}.pkl")
        #     if not os.path.exists(file_path):
        #         return False
        #     
        #     with open(file_path, 'rb') as f:
        #         data = pickle.load(f)
        #     
        #     if isinstance(data, dict):
        #         self._long_term_baseline = data
        #         total_samples = sum(item.get('sample_count', 0) for item in data.values())
        #         self._emit(f"✅ 成功加载长期基线数据（地图{map_id}，共{len(data)}个区域，总样本数{total_samples}）", "INFO")
        #         return True
        #     else:
        #         self._emit(f"⚠️ 基线数据文件格式错误（地图{map_id}）", "WARN")
        #         return False
        # except Exception as e:
        #     self._emit(f"⚠️ 加载长期基线数据失败（地图{map_id}）: {e}", "WARN")
        #     return False

    # ❌ DISABLED: 已摒弃使用长期基线的想法，持续点击稀有精灵即可
    def _save_long_term_baseline(self, map_id: int) -> bool:
        """
        保存长期基线数据到本地文件
        
        Args:
            map_id: 地图ID
            
        Returns:
            True表示成功保存，False表示保存失败
        """
        return False  # ❌ DISABLED
        # try:
        #     file_path = os.path.join(self._baseline_data_dir, f"baseline_map_{map_id}.pkl")
        #     with open(file_path, 'wb') as f:
        #         pickle.dump(self._long_term_baseline, f)
        #     
        #     total_samples = sum(item.get('sample_count', 0) for item in self._long_term_baseline.values())
        #     self._emit(f"💾 保存长期基线数据（地图{map_id}，共{len(self._long_term_baseline)}个区域，总样本数{total_samples}）", "DEBUG")
        #     return True
        # except Exception as e:
        #     self._emit(f"⚠️ 保存长期基线数据失败（地图{map_id}）: {e}", "WARN")
        #     return False

    # ❌ DISABLED: 已摒弃使用长期基线的想法，持续点击稀有精灵即可
    def _start_continuous_baseline_recording(
        self,
        route_points: Sequence[Tuple[str, Region]],
        map_id: int,
        stop_event: threading.Event,
    ) -> None:
        """
        启动持续记录基线数据的后台线程
        
        在稳态扫描阶段，持续记录9个区域的像素数据，并更新长期基线平均值
        
        Args:
            route_points: 9个路线点区域列表
            map_id: 地图ID（用于保存数据）
            stop_event: 停止事件
        """
        return  # ❌ DISABLED
        # # 停止之前的记录线程（如果存在）
        # if self._baseline_recording_thread is not None and self._baseline_recording_thread.is_alive():
        #     if self._baseline_recording_stop_event:
        #         self._baseline_recording_stop_event.set()
        #     self._baseline_recording_thread.join(timeout=2.0)
        # 
        # # 创建新的停止事件和线程
        # self._baseline_recording_stop_event = threading.Event()
        # 
        # def recording_loop():
        #     """后台记录循环"""
        #     recording_interval = 1.0  # 每1秒记录一次
        #     save_interval = 60.0  # 每60秒保存一次到文件
        #     last_save_time = time.time()
        #     
        #     self._emit("📊 启动持续基线数据记录线程", "INFO")
        #     
        #     while not stop_event.is_set() and not self._baseline_recording_stop_event.is_set():
        #         try:
        #             # 只有当处于稳态扫描阶段时才记录
        #             if not self._is_scanning_steady_state:
        #                 time.sleep(0.5)
        #                 continue
        #             
        #             # 遍历9个区域，记录像素数据
        #             for key, reg in route_points:
        #                 if stop_event.is_set() or self._baseline_recording_stop_event.is_set():
        #                     break
        #                 
        #                 try:
        #                     # 获取当前像素签名
        #                     current_sig = self._grab_sig(reg, downsample=(8, 8))
        #                     
        #                     # 更新长期基线数据
        #                     if key not in self._long_term_baseline:
        #                         # 初始化
        #                         self._long_term_baseline[key] = {
        #                             'mean_sig': current_sig.copy(),
        #                             'sample_count': 1,
        #                             'last_updated': time.time(),
        #                         }
        #                     else:
        #                         # 增量更新平均值（使用移动平均）
        #                         old_data = self._long_term_baseline[key]
        #                         old_mean = old_data['mean_sig']
        #                         old_count = old_data.get('sample_count', 1)
        #                         
        #                         # 计算新的平均值（加权平均）
        #                         new_count = old_count + 1
        #                         # 使用简单的移动平均：new_mean = (old_mean * old_count + current) / new_count
        #                         # 但由于mean_sig是RGB三元组列表，需要逐像素计算
        #                         new_mean = []
        #                         for i in range(len(current_sig)):
        #                             old_r, old_g, old_b = old_mean[i] if i < len(old_mean) else (0, 0, 0)
        #                             cur_r, cur_g, cur_b = current_sig[i]
        #                             new_r = int((old_r * old_count + cur_r) / new_count)
        #                             new_g = int((old_g * old_count + cur_g) / new_count)
        #                             new_b = int((old_b * old_count + cur_b) / new_count)
        #                             new_mean.append((new_r, new_g, new_b))
        #                         
        #                         self._long_term_baseline[key] = {
        #                             'mean_sig': new_mean,
        #                             'sample_count': new_count,
        #                             'last_updated': time.time(),
        #                         }
        #                 except Exception as e:
        #                     self._emit(f"⚠️ 记录区域{key}基线数据时出错: {e}", "WARN")
        #                     continue
        #             
        #             # 定期保存到文件
        #             now = time.time()
        #             if now - last_save_time >= save_interval:
        #                 self._save_long_term_baseline(map_id)
        #                 last_save_time = now
        #             
        #             # 等待下一次记录
        #             time.sleep(recording_interval)
        #             
        #         except Exception as e:
        #             self._emit(f"⚠️ 基线数据记录线程异常: {e}", "WARN")
        #             time.sleep(1.0)
        #     
        #     # 线程退出前保存一次
        #     try:
        #         self._save_long_term_baseline(map_id)
        #     except Exception:
        #         pass
        #     
        #     self._emit("📊 基线数据记录线程已停止", "INFO")
        # 
        # self._baseline_recording_thread = threading.Thread(
        #     target=recording_loop,
        #     daemon=True,
        #     name="BaselineRecordingThread"
        # )
        # self._baseline_recording_thread.start()

    def _low_scan_select(
        self,
        points: Sequence[Tuple[str, Region]],
        profile: WildCaptureProfile,
    ) -> Optional[Tuple[str, Region, float]]:
        """
        低强度扫描：扫描所有点，检测颜色变化
        
        ❌ DISABLED: 已摒弃使用长期基线的想法，持续点击稀有精灵即可
        """
        diffs: List[Tuple[float, str, Region, float]] = []
        hits: List[Tuple[float, str, Region, float]] = []

        for key, reg in points:
            base = self._baseline.get(key)
            if base is None:
                continue
            
            sig = self._grab_sig(reg, downsample=(8, 8))
            diff = self._sig_diff(sig, base)
            th = self._threshold.get(key, 9999.0)
            
            diffs.append((diff, key, reg, th))
            if diff >= th:
                hits.append((diff, key, reg, th))

        if not diffs:
            return None

        if hits:
            hits.sort(key=lambda x: x[0], reverse=True)
            top_diff = hits[0][0]
            top = [h for h in hits if h[0] >= top_diff - 0.5]
            if len(top) > 1 and self._current_pos is not None:
                cx, cy = self._current_pos
                top.sort(key=lambda h: self._dist2((cx, cy), self._region_center(h[2])))
                best = top[0]
            else:
                best = hits[0]
            return best[1], best[2], best[0]

        diffs.sort(key=lambda x: x[0], reverse=True)
        best = diffs[0]
        second = diffs[1] if len(diffs) >= 2 else (0.0, "", points[0][1], 0.0)

        best_diff = best[0]
        second_diff = second[0]

        if best_diff >= profile.low_abs_min and (best_diff - second_diff) >= profile.low_best_over_second:
            return best[1], best[2], best_diff

        return None

    def _burst_scan(
        self,
        points: Sequence[Tuple[str, Region]],
        profile: WildCaptureProfile,
        stop_event: threading.Event,
        armed_until_ts: Optional[float] = None,
    ) -> Optional[Tuple[str, Region]]:
        """
        burst扫描：在mp3出现后的短时间内进行高频扫描
        
        ❌ DISABLED: 已摒弃使用长期基线的想法，持续点击稀有精灵即可
        
        Args:
            armed_until_ts: 武装窗口到期时间，如果提供且已过期，立即停止扫描
        """
        start = time.time()
        while time.time() - start < profile.burst_window_sec:
            if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                return None
            self._wait_if_paused(stop_event)
            
            # ✅ 如果武装窗口已过期，立即停止burst扫描
            if armed_until_ts is not None and profile.require_mp3_before_trigger:
                now = time.time()
                if now > armed_until_ts:
                    self._emit("⏱️ burst扫描：武装窗口已过期，停止扫描", "DEBUG")
                    return None

            hits: List[Tuple[float, str, Region]] = []
            for key, reg in points:
                base = self._baseline.get(key)
                if base is None:
                    continue
                
                sig = self._grab_sig(reg, downsample=(8, 8))
                diff = self._sig_diff(sig, base)
                th = self._threshold.get(key, 9999.0)
                
                if diff >= th:
                    hits.append((diff, key, reg))

            if hits:
                hits.sort(key=lambda x: x[0], reverse=True)
                top_diff = hits[0][0]
                top = [h for h in hits if h[0] >= top_diff - 0.5]
                if len(top) > 1 and self._current_pos is not None:
                    cx, cy = self._current_pos
                    top.sort(key=lambda h: self._dist2((cx, cy), self._region_center(h[2])))
                    return top[0][1], top[0][2]
                return hits[0][1], hits[0][2]

            time.sleep(0.01)

        return None
    
    def _continuous_burst_scan_once(
        self,
        points: Sequence[Tuple[str, Region]],
        profile: WildCaptureProfile,
        stop_event: threading.Event,
    ) -> Optional[Tuple[str, Region, float]]:
        """
        持续burst扫描：单次扫描检测突变（用于初版逻辑）
        
        Returns:
            None或(hit_key, hit_reg, diff)
        """
        if stop_event.is_set() or getattr(self.bot, "stop_current", False):
            return None
        self._wait_if_paused(stop_event)

        hits: List[Tuple[float, str, Region]] = []
        for key, reg in points:
            base = self._baseline.get(key)
            if base is None:
                continue
            
            sig = self._grab_sig(reg, downsample=(8, 8))
            diff = self._sig_diff(sig, base)
            th = self._threshold.get(key, 9999.0)
            
            if diff >= th:
                hits.append((diff, key, reg))

        if hits:
            hits.sort(key=lambda x: x[0], reverse=True)
            top_diff = hits[0][0]
            top = [h for h in hits if h[0] >= top_diff - 0.5]
            if len(top) > 1 and self._current_pos is not None:
                cx, cy = self._current_pos
                top.sort(key=lambda h: self._dist2((cx, cy), self._region_center(h[2])))
                return top[0][1], top[0][2], top[0][0]
            return hits[0][1], hits[0][2], hits[0][0]

        return None

    def _measure_baseline(
        self,
        reg: Region,
        samples: int,
        downsample: Tuple[int, int],
    ) -> Tuple[List[Tuple[int, int, int]], float]:
        sigs = [self._grab_sig(reg, downsample) for _ in range(samples)]
        mean_sig = self._sig_mean(sigs)
        jitter = sum(self._sig_diff(s, mean_sig) for s in sigs) / max(1, len(sigs))
        return mean_sig, jitter

    def _grab_sig(self, reg: Region, downsample: Tuple[int, int] = (8, 8)) -> List[Tuple[int, int, int]]:
        x1, y1, x2, y2 = reg.outer_bbox()
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0

        w, h = downsample
        gx1 = int(round(cx - w / 2))
        gy1 = int(round(cy - h / 2))
        gx2 = gx1 + w
        gy2 = gy1 + h

        img = window_manager.grab_game_bbox(gx1, gy1, gx2, gy2)
        if img.size != downsample:
            img = img.resize(downsample, Image.NEAREST)
        return list(img.getdata())

    @staticmethod
    def _sig_mean(sigs: List[List[Tuple[int, int, int]]]) -> List[Tuple[int, int, int]]:
        if not sigs:
            return []
        n = len(sigs)
        m = len(sigs[0])
        out: List[Tuple[int, int, int]] = []
        for i in range(m):
            r = sum(s[i][0] for s in sigs) / n
            g = sum(s[i][1] for s in sigs) / n
            b = sum(s[i][2] for s in sigs) / n
            out.append((int(r), int(g), int(b)))
        return out

    @staticmethod
    def _sig_diff(a: List[Tuple[int, int, int]], b: List[Tuple[int, int, int]]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        total = 0.0
        for (ar, ag, ab), (br, bg, bb) in zip(a, b):
            total += abs(ar - br) + abs(ag - bg) + abs(ab - bb)
        return total / (len(a) * 3.0)
    
    def _check_if_point_has_changed(self, reg: Region, baseline_sig: List[Tuple[int, int, int]], threshold: float = 15.0, check_duration: float = 5.0, stop_event: Optional[threading.Event] = None) -> bool:
        """
        检测一个点是否在指定时间内发生了变化
        
        Args:
            reg: 区域
            baseline_sig: 基准签名（从初始扫描获取）
            threshold: 变化阈值（diff值）
            check_duration: 检测时长（秒）
            stop_event: 停止事件（可选）
        
        Returns:
            True=检测到变化，False=没有变化
        """
        start_time = time.time()
        check_interval = 0.2  # 每0.2秒检查一次
        
        while (time.time() - start_time) < check_duration:
            if stop_event and (stop_event.is_set() or getattr(self.bot, "stop_current", False)):
                return False
            
            try:
                current_sig = self._grab_sig(reg, downsample=(8, 8))
                diff = self._sig_diff(current_sig, baseline_sig)
                
                if diff >= threshold:
                    return True  # 检测到变化
                
                time.sleep(check_interval)
            except Exception as e:
                self._emit(f"⚠️ 检测点变化时出错: {e}", "WARN")
                time.sleep(check_interval)
        
        return False  # 没有检测到变化
    
    def _check_if_reached_point(self, target_reg: Region, baseline_before_click: List[Tuple[int, int, int]], threshold: float, check_duration: float = 1.0, stop_event: Optional[threading.Event] = None) -> bool:
        """
        检测是否到达目标点（1秒内检测）
        
        Args:
            target_reg: 目标区域
            baseline_before_click: 点击前的基准签名
            threshold: 变化阈值
            check_duration: 检测时长（默认1秒）
            stop_event: 停止事件（可选）
        
        Returns:
            True=已到达，False=未到达
        """
        return self._check_if_point_has_changed(target_reg, baseline_before_click, threshold=threshold, check_duration=check_duration, stop_event=stop_event)
    
    def _wait_until_reached_ab_point(self, reg_a: Region, reg_b: Region, use_foreground: bool, stop_event: threading.Event) -> str:
        """
        刚进地图时：等待直到到达A或B点（通过检测A/B点的变化）
        
        流程：
        1. 先标定A点，然后点击A点
        2. 如果2秒内没检测到到达A点，那么2秒过期后不等待，直接标定B点，然后点击B点
        3. 直到到达了某个AB点（A或B），返回到达的点
        
        Args:
            reg_a: A点区域
            reg_b: B点区域
            use_foreground: 是否前台运行
            stop_event: 停止事件
        
        Returns:
            到达的点（"A"或"B"），如果都失败则默认返回"B"
        """
        self._emit("🔍 开始检测是否已到达A或B点...", "INFO")
        
        # 1. 先标定A点，然后点击A点
        self._emit("📏 标定A点基准...", "INFO")
        baseline_a = self._grab_sig(reg_a, downsample=(8, 8))
        mean_a, jitter_a = self._measure_baseline(reg_a, samples=2, downsample=(8, 8))
        threshold_a = jitter_a * 5.0 + 12.0
        
        self._emit("📍 点击A点...", "INFO")
        self._click_region(reg_a, use_foreground)
        self._current_pos = self._region_center(reg_a)
        self._last_anchor = "A"
        self._sleep_abortable(stop_event, 0.5)  # 等待点击生效
        
        # 检测是否到达A点（2秒内）
        self._emit("⏳ 检测是否到达A点（2秒内）...", "INFO")
        reached_a = self._check_if_reached_point(reg_a, baseline_a, threshold_a, check_duration=2.0, stop_event=stop_event)
        
        if reached_a:
            self._emit("✅ 检测到A点有变化，确认已到达A点！", "SUCCESS")
            return "A"
        
        # 2. 如果2秒内没检测到到达A点，那么2秒过期后不等待，直接标定B点，然后点击B点
        self._emit("⚠️ 2秒内未到达A点，切换到B点...", "WARN")
        
        self._emit("📏 标定B点基准...", "INFO")
        baseline_b = self._grab_sig(reg_b, downsample=(8, 8))
        mean_b, jitter_b = self._measure_baseline(reg_b, samples=2, downsample=(8, 8))
        threshold_b = jitter_b * 5.0 + 12.0
        
        self._emit("📍 点击B点...", "INFO")
        self._click_region(reg_b, use_foreground)
        self._current_pos = self._region_center(reg_b)
        self._last_anchor = "B"
        self._sleep_abortable(stop_event, 0.5)  # 等待点击生效
        
        # 检测是否到达B点（2秒内）
        self._emit("⏳ 检测是否到达B点（2秒内）...", "INFO")
        reached_b = self._check_if_reached_point(reg_b, baseline_b, threshold_b, check_duration=2.0, stop_event=stop_event)
        
        if reached_b:
            self._emit("✅ 检测到B点有变化，确认已到达B点！", "SUCCESS")
            return "B"
        
        # 如果B点也没到达，默认返回B点（避免无限循环）
        self._emit("⚠️ 2秒内未到达B点，默认使用B点", "WARN")
        return "B"
    
    def _wait_until_left_spawn_point(self, reg_a: Region, reg_b: Region, use_foreground: bool, stop_event: threading.Event, max_attempts: int = 10) -> Optional[str]:
        """
        等待直到真正离开刷新点（通过检测A/B点的变化）
        
        流程：
        1. 点击A点前，先扫描A点基准（当前在刷新点上时的颜色）
        2. 点击A点
        3. 等待5秒，检测A点是否有变化（相对于点击前的基准）
           - 如果A点有变化，说明到达了A点（地图颜色显露）
           - 如果A点没有变化，说明还在刷新点上（被精灵挡住）
        4. 如果A点5秒内没有变化，点击B点前先扫描B点基准，然后点击B点
        5. 等待5秒，检测B点是否有变化（相对于点击前的基准）
           - 如果B点有变化，说明到达了B点
        6. 来回往复直到A或B中有一个点被检测到变化
        
        Args:
            reg_a: A点区域
            reg_b: B点区域
            use_foreground: 是否前台运行
            stop_event: 停止事件
            max_attempts: 最大尝试次数（防止无限循环）
        
        Returns:
            到达的点（"A"或"B"），如果失败返回None
        """
        self._emit("🔍 开始检测是否已离开刷新点...", "INFO")
        
        attempt = 0
        current_point_is_a = True  # 当前尝试的点是A还是B
        
        while attempt < max_attempts:
            if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                return None  # 停止时返回None
            
            attempt += 1
            
            # 选择当前要点击的点
            if current_point_is_a:
                target_reg = reg_a
                target_name = "A"
            else:
                target_reg = reg_b
                target_name = "B"
            
            # ✅ 在点击前扫描基准（在刷新点上时的颜色）
            self._emit(f"📏 点击{target_name}点前，扫描{target_name}点基准（当前在刷新点上）...", "INFO")
            baseline_before_click = self._grab_sig(target_reg, downsample=(8, 8))
            # 计算阈值（基于基准的jitter）
            mean_target, jitter_target = self._measure_baseline(target_reg, samples=2, downsample=(8, 8))
            threshold = jitter_target * 5.0 + 12.0
            
            # 点击目标点
            self._emit(f"📍 点击{target_name}点（尝试{attempt}/{max_attempts}）...", "INFO")
            self._click_region(target_reg, use_foreground)
            self._current_pos = self._region_center(target_reg)
            self._last_anchor = target_name
            self._sleep_abortable(stop_event, 0.5)  # 等待点击生效
            
            # 等待5秒，检测目标点是否有变化（相对于点击前的基准）
            self._emit(f"⏳ 等待5秒，检测{target_name}点是否有变化（相对于点击前的基准）...", "INFO")
            has_changed = self._check_if_point_has_changed(target_reg, baseline_before_click, threshold=threshold, check_duration=5.0, stop_event=stop_event)
            
            if has_changed:
                self._emit(f"✅ 检测到{target_name}点有变化，确认已离开刷新点（到达{target_name}点）！", "SUCCESS")
                return target_name  # 返回到达的点（"A"或"B"）
            else:
                self._emit(f"⚠️ {target_name}点5秒内没有变化，可能还在刷新点上，切换到另一个点...", "WARN")
                current_point_is_a = not current_point_is_a  # 切换到另一个点
        
        # 超过最大尝试次数，返回None
        self._emit(f"⚠️ 超过最大尝试次数（{max_attempts}），返回None", "WARN")
        return None  # 失败返回None

    # ---------------------------
    # battle & dialogs
    # ---------------------------
    def _post_battle_cleanup(
        self,
        reg_a: Region,
        reg_b: Region,  # ✅ 新增：需要B点用于检测
        route_points: Sequence[Tuple[str, Region]],
        profile: WildCaptureProfile,
        use_foreground: bool,
        stop_event: threading.Event,
        should_recover: bool = False,
    ) -> str:
        """
        战后清理流程：
        1. 等待战后延迟
        2. ❌ 移除：清理对话框（1AND1已经在_recover_pets中处理了）
        3. 如果should_recover为True，执行恢复流程（包括关闭背包）
        4. ✅ 确认已离开刷新点（通过A/B点变化检测，如果返回None则默认用B）
        5. 重新标定稳态（9个点）
        
        Returns:
            到达的点（"A"或"B"），用于设置下一次走位的反向点
        """
        # 标记战斗结束
        self._is_in_battle = False
        
        # 1. 等待战后延迟
        self._sleep_abortable(stop_event, profile.post_battle_delay_sec)
        
        # 2. ❌ 移除：清理对话框（1AND1已经在_recover_pets中处理了，不需要再次清理）
        # self._emit("🧹 战后清理对话框", "SYSTEM")
        # self._clear_dialogs_by_probes(use_foreground, stop_event=stop_event)

        # 3. 如果捕捉成功，执行恢复流程（包括OCR识别目标精灵并放回仓库）
        if should_recover:
            # ✅ 嘟咕噜模式：检查OCR识别是否失败
            is_dugulu_mode = "嘟咕噜" in profile.name.lower()
            if is_dugulu_mode and self._dugulu_ocr_failed:
                self._emit("⚠️ [嘟咕噜OCR] 捕捉成功，但OCR识别结果不符合预期（等级或血量不在15级43-47或16级45-50范围内），可能识别错误", "WARN")
            
            self._emit("💊 捕捉成功，执行恢复流程", "SYSTEM")
            self._is_recovering = True
            # 传递最后一次战斗的尼尔家族ID（如果有）和profile
            # skip_return_storage=False表示捕捉成功后需要放回仓库（使用OCR识别）
            self._recover_pets(use_foreground, stop_event, skip_return_storage=False, nie_family_id=self._last_nie_family_id, profile=profile)
            self._is_recovering = False
            # 恢复完成后清空记录
            self._last_nie_family_id = None
        
        # ✅ 4. 确认已离开刷新点（通过A/B点变化检测，如果返回None则默认用B）
        reached_point = self._wait_until_left_spawn_point(reg_a, reg_b, use_foreground, stop_event)
        if reached_point is None:
            self._emit("⚠️ 离开刷新点检测返回None，默认使用B点", "WARN")
            reached_point = "B"
        
        self._emit(f"✅ 确认已离开刷新点，到达{reached_point}点", "SUCCESS")
        
        # ✅ 5. 重新标定稳态（确认离开刷新点后才开始，检测所有9个点）
        self._emit("📏 重新标定稳态（确认离开刷新点后，使用稳健模式，检测所有9个点）", "SYSTEM")
        baseline_start_time = time.time()
        self._recalibrate_all_robust(route_points, stop_event, map_id=profile.map_swf_id)
        baseline_duration = time.time() - baseline_start_time
        
        # ✅ 等待至少5秒确保基线稳定（与初始标定一致）
        min_wait_after_baseline = 5.0
        if baseline_duration < min_wait_after_baseline:
            wait_time = min_wait_after_baseline - baseline_duration
            self._emit(f"⏳ 等待{wait_time:.1f}s后继续扫描（确保基线稳定，稳健模式）", "INFO")
            self._sleep_abortable(stop_event, wait_time)
        
        # 标记回到稳态扫描阶段
        self._is_scanning_steady_state = True
        # 重置1AND1监控停止标志（稳态建立后重新开始监控）
        self._stop_1and1_monitoring = False
        
        # ✅ 返回到达的点，用于设置下一次走位的反向点
        return reached_point
    
    def _handle_battle_trigger(
        self,
        tx: float, ty: float,
        reg_a: Region,
        route_points: Sequence[Tuple[str, Region]],
        profile: WildCaptureProfile,
        use_foreground: bool,
        stop_event: threading.Event,
        test_mode: bool = False,
        xiaodouya_nie_test_mode: bool = False,
        task_stats: Optional[Dict[str, int]] = None,  # 任务特定统计
    ) -> str:
        """
        处理对战触发后的逻辑
        
        注意：校准和点击逻辑已经在_click_opposite_then_click_target_until_skill中完成，
        这里只需要等待PetItem（如果还没有检测到）并进入战斗循环。
        
        Returns:
            "captured": 捕捉成功（成功进入战斗且使用胶囊）
            "battled": 成功进入战斗但未捕捉成功（使用技能等）
            "skipped": 跳过（检测到mp3但未成功进入战斗，算作"错过"）
        """
        # ✅ 校准和点击逻辑已经在_click_opposite_then_click_target_until_skill中完成
        # 现在需要：1) 迅速进行pattern检测判断对战类型 2) 等待PetItem并立即执行对应逻辑
        
        # ✅ 第一步：收集所有pet IDs，判断对战类型（只看尼尔家族）
        pet_ids = self._immediate_collected_pet_ids  # 使用立即收集到的pet IDs
        nie_family_id = None
        
        # ✅ 螳螂模式特殊逻辑：除非出现尼尔家族，否则都使用无敌胶囊
        invincible_first_round = False
        is_mantis_mode = "螳螂" in profile.name.lower() or profile.target_pet_id == 122
        
        # ✅ 如果pet_ids为None或空，在螳螂模式下尝试重新收集
        if not pet_ids and is_mantis_mode:
            self._emit("⚠️ [螳螂模式] 未收集到pet IDs，尝试重新收集...", "WARN")
            from core.logger import kernel_cursor
            retry_pet_ids = self._collect_fight_pet_ids(timeout=3.0, collect_window=0.5, stop_event=stop_event)
            if retry_pet_ids:
                pet_ids = retry_pet_ids
                self._immediate_collected_pet_ids = retry_pet_ids  # 更新立即收集到的pet IDs
                self._emit(f"✅ [螳螂模式] 重新收集成功：{sorted(pet_ids)}", "SUCCESS")
            else:
                self._emit("⚠️ [螳螂模式] 重新收集仍然失败，默认使用无敌胶囊（安全措施）", "WARN")
                # 螳螂模式下，如果收集不到pet IDs，默认使用无敌胶囊（因为无法判断是否有尼尔家族）
                invincible_first_round = True
                self._emit("🛡️ [螳螂模式] 由于pet IDs收集失败，默认使用无敌精灵胶囊（安全措施）", "SYSTEM")
        
        if pet_ids:
            unique_ids = set(pet_ids)
            
            # 标注并记录pet IDs
            self._classify_and_log_pet_ids(pet_ids, profile)
            
            # 检查是否有尼尔家族（77、310、416）
            nie_family_ids = unique_ids & {77, 310, 416}
            if nie_family_ids:
                nie_family_id_candidate = list(nie_family_ids)[0]  # 取第一个
                
                # ✅ 所有尼尔家族（77、310、416）都正常捕捉
                nie_family_id = nie_family_id_candidate
                self._last_nie_family_id = nie_family_id
                if nie_family_id == 416:
                    self._emit("🎯 [对战类型] 检测到尼奥（416），执行尼尔家族逻辑", "SYSTEM")
                elif nie_family_id == 310:
                    self._emit("🎯 [对战类型] 检测到闪光尼尔（310），执行尼尔家族逻辑", "SYSTEM")
                elif nie_family_id == 77:
                        self._emit("🎯 [对战类型] 检测到尼尔（77），执行尼尔家族逻辑", "SYSTEM")
                else:
                    self._emit(f"🎯 [对战类型] 检测到尼尔家族（{nie_family_id}），执行尼尔家族逻辑", "SYSTEM")
            else:
                # 没有尼尔家族
                self._last_nie_family_id = None
                
                # ✅ 螳螂模式：除非出现尼尔家族，否则都使用无敌胶囊
                if is_mantis_mode:
                    invincible_first_round = True
                    self._emit("🛡️ [螳螂模式] 未检测到尼尔家族，使用无敌精灵胶囊", "SYSTEM")
                else:
                    self._emit("🎯 [对战类型] 目标精灵（正常稀有精灵抓捕逻辑）", "SYSTEM")
        else:
            # 非螳螂模式：未收集到pet IDs，使用默认逻辑
            if not is_mantis_mode:
                self._emit("⚠️ 未收集到pet IDs，使用默认逻辑", "WARN")
            # 螳螂模式：如果收集不到pet IDs，默认使用无敌胶囊（已在上面处理）
        
        # ✅ 第二步：如果统一框架已初始化，使用统一框架等待PetItem并执行第一回合动作
        if self._unified_framework and self._wild_adapter:
            from core.unified_battle_framework import BattleConfig, BattleMode
            
            # ✅ 双塔模式：重置逃跑标志（在创建action_callback之前）
            is_shuangta_mode = "双塔" in profile.name.lower()
            if is_shuangta_mode:
                self._shuangta_should_escape = False
            
            # 创建动作回调（根据是否检测到尼尔家族，支持尼尔家族切换逻辑、双塔逃跑逻辑和嘟咕噜逃跑逻辑）
            def action_callback(round_idx: int) -> str:
                # ✅ 双塔模式：如果满足逃跑条件，第二回合执行逃跑
                if is_shuangta_mode and self._shuangta_should_escape:
                    if round_idx == 2:
                        self._emit("🏃 [双塔逃跑] 第二回合执行逃跑", "SUCCESS")
                        return "escape"
                    elif round_idx > 2:
                        # 如果第二回合逃跑失败，继续逃跑
                        return "escape"
                
                # ✅ 嘟咕噜模式：如果满足逃跑条件，第二回合执行逃跑
                if is_dugulu_mode and self._dugulu_should_escape:
                    if round_idx == 2:
                        self._emit("🏃 [嘟咕噜逃跑] 第二回合执行逃跑", "SUCCESS")
                        return "escape"
                    elif round_idx > 2:
                        # 如果第二回合逃跑失败，继续逃跑
                        return "escape"
                
                # ✅ 如果有尼尔家族，执行尼尔家族逻辑（所有尼尔家族都正常捕捉）
                if nie_family_id is not None:
                    if round_idx == 1:
                        return "skill"  # 第一回合使用技能一
                    elif round_idx == 2:
                        # 注意：第二回合切换精灵应该在stage3中执行，这里不应该被调用（因为round_idx=1已在stage2中执行）
                        # 但为了完整性，这里仍然返回switch标记
                        return "switch"  # 返回"switch"表示切换精灵（统一框架会忽略这个动作，切换已在action_callback中执行）
                    else:
                        # 后续回合只使用高级胶囊
                        return "capsule_high"  # 返回特殊标记，表示高级胶囊
                
                # 没有尼尔家族或pattern无效，使用正常逻辑
                if test_mode:
                    # 测试模式：第一回合技能一，后续回合只使用中级胶囊
                    if round_idx == 1:
                        return "skill"
                    else:
                        return "capsule"
                else:
                    # 正常模式：螳螂逻辑/稀有逻辑
                    if round_idx == 1:
                        if invincible_first_round:
                            return "capsule"
                        else:
                            return "skill"
                    # 第2回合开始：捕捉逻辑（中级/高级交替）
                    return "capsule"
            
            # ✅ 双塔模式：启动敌方信息监控（在后台线程中运行）
            is_dugulu_mode = "嘟咕噜" in profile.name.lower()
            if is_shuangta_mode:
                self._emit("🚀 [双塔模式] 启动敌方信息监控线程（后台运行）", "INFO")
                
                # 定义条件检查回调：检测到符合合理范围的等级和血量时停止扫描
                def check_shuangta_condition(level: Optional[int], hp: Optional[int]) -> bool:
                    if level is None or hp is None:
                        return False
                    # 合理的血量组合：11级32-36，或12级34-39（检测到这些就停止扫描）
                    if (level == 11 and hp in [32, 33, 34, 35, 36]) or \
                       (level == 12 and hp in [34, 35, 36, 37, 38, 39]):
                        return True
                    return False
                
                def monitor_worker():
                    try:
                        self._emit("🔍 [敌方信息监控] 监控线程已启动，开始监控流程", "INFO")
                        level, hp, status = self._monitor_enemy_info_after_skill(
                            use_foreground=use_foreground,
                            stop_event=stop_event,
                            timeout_s=30.0,
                            check_condition_callback=check_shuangta_condition
                        )
                        if status == "success":
                            self._emit(f"✅ [敌方信息] 最终结果 - 等级: {level}, 血量: {hp}", "SUCCESS")
                        elif status == "timeout":
                            self._emit(f"⏱️ [敌方信息] 监控超时", "WARN")
                        elif status == "ocr_failed":
                            self._emit(f"❌ [敌方信息] OCR识别失败，区域中无法识别数字", "WARN")
                        else:
                            self._emit(f"⚠️ [敌方信息] 监控被停止", "WARN")
                    except Exception as e:
                        self._emit(f"⚠️ [敌方信息监控] 异常: {e}", "WARN")
                        import traceback
                        self._emit(f"📋 异常详情: {traceback.format_exc()}", "ERROR")
                
                monitor_thread = threading.Thread(target=monitor_worker, daemon=True)
                monitor_thread.start()
                self._emit("✅ [双塔模式] 敌方信息监控线程已启动", "SUCCESS")
            
            # ✅ 嘟咕噜模式：启动敌方信息监控（在后台线程中运行）
            if is_dugulu_mode:
                self._emit("🚀 [嘟咕噜模式] 启动敌方信息监控线程（后台运行）", "INFO")
                
                # 定义条件检查回调：检测到符合合理范围的等级和血量时停止扫描
                def check_dugulu_condition(level: Optional[int], hp: Optional[int]) -> bool:
                    if level is None or hp is None:
                        return False
                    # 合理的血量组合：15级43-47，或16级45-50（检测到这些就停止扫描）
                    if (level == 15 and hp in [43, 44, 45, 46, 47]) or \
                       (level == 16 and hp in [45, 46, 47, 48, 49, 50]):
                        return True
                    return False
                
                def monitor_worker():
                    try:
                        self._emit("🔍 [敌方信息监控] 监控线程已启动，开始监控流程", "INFO")
                        level, hp, status = self._monitor_enemy_info_after_skill(
                            use_foreground=use_foreground,
                            stop_event=stop_event,
                            timeout_s=30.0,
                            check_condition_callback=check_dugulu_condition
                        )
                        if status == "success":
                            self._emit(f"✅ [敌方信息] 最终结果 - 等级: {level}, 血量: {hp}", "SUCCESS")
                        elif status == "timeout":
                            self._emit(f"⏱️ [敌方信息] 监控超时", "WARN")
                        elif status == "ocr_failed":
                            self._emit(f"❌ [敌方信息] OCR识别失败，区域中无法识别数字", "WARN")
                        else:
                            self._emit(f"⚠️ [敌方信息] 监控被停止", "WARN")
                    except Exception as e:
                        self._emit(f"⚠️ [敌方信息监控] 异常: {e}", "WARN")
                        import traceback
                        self._emit(f"📋 异常详情: {traceback.format_exc()}", "ERROR")
                
                monitor_thread = threading.Thread(target=monitor_worker, daemon=True)
                monitor_thread.start()
                self._emit("✅ [嘟咕噜模式] 敌方信息监控线程已启动", "SUCCESS")
            
            # ✅ 双塔模式和嘟咕噜模式：重置逃跑标志
            if is_shuangta_mode:
                self._shuangta_should_escape = False
            if is_dugulu_mode:
                self._dugulu_should_escape = False
                self._dugulu_ocr_failed = False
            
            # PetItem检测回调：增加成功计数，并在双塔模式和嘟咕噜模式中检查逃跑条件
            def on_petitem_detected():
                if task_stats is not None:
                    task_stats["success"] += 1
                    self._emit(f"📊 [统计] 检测到PetItem（成功数：{task_stats['success']}）", "SUCCESS")
                else:
                    # 向后兼容：如果没有传入task_stats，使用旧逻辑（不应该发生）
                    self._emit(f"📊 [统计] 检测到PetItem（警告：task_stats未传入）", "WARN")
                
                # ✅ 双塔模式：检测到petitem后检查是否需要逃跑
                if is_shuangta_mode:
                    # 等待一小段时间，确保监控线程有时间保存最后一次OCR数据
                    time.sleep(0.2)
                    should_escape = self._check_shuangta_escape_condition(pet_ids, use_foreground, stop_event)
                    if should_escape:
                        self._shuangta_should_escape = True
                        self._emit("🏃 [双塔逃跑] 已设置逃跑标志，将在第一回合执行逃跑", "SUCCESS")
                
                # ✅ 嘟咕噜模式：检测到petitem后检查是否需要逃跑
                if is_dugulu_mode:
                    # 等待一小段时间，确保监控线程有时间保存最后一次OCR数据
                    time.sleep(0.3)
                    # ✅ 添加调试日志，显示当前保存的数据
                    self._emit(f"🔍 [嘟咕噜判断] 当前保存的数据 - 等级: {self._last_enemy_level}, 血量: {self._last_enemy_hp}", "DEBUG")
                    should_escape, ocr_failed = self._check_dugulu_escape_condition(use_foreground, stop_event)
                    if should_escape:
                        self._dugulu_should_escape = True
                        self._emit("🏃 [嘟咕噜逃跑] 已设置逃跑标志，将在第二回合执行逃跑", "SUCCESS")
                    if ocr_failed:
                        self._dugulu_ocr_failed = True
                        self._emit("⚠️ [嘟咕噜OCR] OCR识别结果不符合预期，继续捕捉但提醒失败", "WARN")
            
            # 保存尼尔家族ID到实例变量，供后续stage3使用（不再使用pattern_valid）
            self._current_battle_nie_family_id = nie_family_id
            
            # 创建临时config用于stage2的第一回合执行
            temp_config = BattleConfig(
                mode=BattleMode.WILD,
                use_foreground=use_foreground,
                skill_key="对战.使用技能一",
                action_callback=action_callback,
                invincible_first_round=invincible_first_round,
                on_petitem_detected=on_petitem_detected,
            )
            
            # 调用stage2等待PetItem（skill后很快就是PetItem，必须立即开始监听）
            # 记录当前cursor，确保从skill信号之后的新日志开始检查
            from core.logger import kernel_cursor
            current_cursor = kernel_cursor()
            success, calib_result = self._unified_framework.stage2_calibration_and_petitem(
                trigger_callback=None,  # 野外模式不需要trigger_callback
                use_foreground=use_foreground,
                timeout_s=10.0,
                skip_stage1=True,  # 跳过Stage 1，野外模式已在外部完成
                config=temp_config,  # 传递config以便检测到PetItem时立即执行第一回合
                initial_cursor=current_cursor  # 传递当前cursor，从skill信号之后的新日志开始检查
            )
            
            if not success:
                # ✅ 即使PetItem检测超时，也要检查是否已经进入战斗
                # 检测战斗状态：回合探针、内核日志等
                self._emit("⚠️ PetItem检测超时，检查是否已进入战斗状态...", "WARN")
                
                # 检查是否已经进入战斗（检测回合探针）
                in_battle = False
                try:
                    # 方法1：检测回合探针（右下角蓝色探针）
                    if hasattr(self._unified_framework, '_detect_round_probe') and hasattr(self._unified_framework, '_load_probe_templates'):
                        probe_model = self._unified_framework._load_probe_templates()
                        if probe_model:
                            probe_state, blue_score, gray_score = self._unified_framework._detect_round_probe(probe_model)
                            if probe_state == "BLUE" and blue_score >= 0.90:
                                in_battle = True
                                self._emit("✅ 检测到回合探针为蓝色，确认已进入战斗，继续战斗流程", "SUCCESS")
                    
                    # 方法2：检查内核日志中是否有战斗相关的其他信号
                    if not in_battle:
                        from core.logger import fetch_kernel_since
                        recent_cursor = max(0, current_cursor - 100)  # 检查最近100条日志
                        try:
                            lines = fetch_kernel_since(recent_cursor)
                            if lines:
                                for line in lines[-30:]:  # 检查最近30条
                                    line_str = str(line)
                                    # 检查是否有战斗相关的其他信号
                                    if any(keyword in line_str for keyword in ["/fightResource/", "/pet/swf/", "/skill/swf/", "/resource/item/petItem/icon/"]):
                                        in_battle = True
                                        self._emit("✅ 检测到战斗相关日志信号，确认已进入战斗，继续战斗流程", "SUCCESS")
                                        break
                        except Exception as e:
                            self._emit(f"⚠️ 检查内核日志时出错: {e}", "DEBUG")
                
                except Exception as e:
                    self._emit(f"⚠️ 检查战斗状态时出错: {e}", "WARN")
                
                if not in_battle:
                    # 确实没有进入战斗，跳过本次对战
                    self._emit("↩ 确认未进入战斗，回到 A 并跳过本次对战", "WARN")
                self._click_region(reg_a, use_foreground)
                self._current_pos = self._region_center(reg_a)
                self._last_anchor = "A"
                self._sleep_abortable(stop_event, 1.0)
                # ✅ 重新标定所有9个点（与初始标定保持一致）
                map_id = getattr(profile, 'map_swf_id', None)
                self._emit("📏 跳过后重新标定稳态（使用稳健模式，检测所有9个点）", "SYSTEM")
                baseline_start_time = time.time()
                self._recalibrate_all_robust(route_points, stop_event, map_id=map_id)
                baseline_duration = time.time() - baseline_start_time
                
                # ✅ 等待至少5秒确保基线稳定（与初始标定一致）
                min_wait_after_baseline = 5.0
                if baseline_duration < min_wait_after_baseline:
                    wait_time = min_wait_after_baseline - baseline_duration
                    self._emit(f"⏳ 等待{wait_time:.1f}s后继续扫描（确保基线稳定，稳健模式）", "INFO")
                    self._sleep_abortable(stop_event, wait_time)
                    return "skipped"  # 确实失败，跳过本次对战
                else:
                    # 已经进入战斗，继续执行战斗逻辑
                    self._emit("✅ 虽然PetItem检测超时，但已确认进入战斗，继续战斗流程", "SUCCESS")
                    # 标记进入战斗，继续执行后续战斗逻辑
                    # 注意：第一回合动作可能已经在stage2中执行，如果还没有执行，需要补上
                    # 检查技能按钮是否存在（如果不存在，说明可能还没执行第一回合）
                    skill_region = self.regions.get("对战.使用技能一")
                    if skill_region is not None:
                        # 如果技能按钮还存在，说明可能还没执行第一回合，尝试执行
                        try:
                            # 延迟一小段时间确保战斗界面完全加载
                            self._sleep_abortable(stop_event, 0.2)
                            # 检查回合探针状态，如果还是蓝色，可能需要执行第一回合
                            if hasattr(self._unified_framework, '_detect_round_probe') and hasattr(self._unified_framework, '_load_probe_templates'):
                                probe_model = self._unified_framework._load_probe_templates()
                                if probe_model:
                                    probe_state, blue_score, gray_score = self._unified_framework._detect_round_probe(probe_model)
                                    if probe_state == "BLUE" and blue_score >= 0.90:
                                        # 回合探针还是蓝色，说明可能还没执行第一回合，尝试执行
                                        self._emit("⚔️ 检测到可能需要补执行第一回合动作，尝试执行", "INFO")
                                        if invincible_first_round:
                                            # 第一回合使用无敌胶囊
                                            # ✅ 先双击切换战斗面板
                                            battle_panel_key = "对战.切换战斗面板"
                                            if self.regions.get(battle_panel_key):
                                                self._emit("🔄 切换对战面板（双击）...", "INFO")
                                                self._click_region_twice(battle_panel_key, use_foreground, gap=0.06)
                                                self._sleep_abortable(stop_event, 0.3)  # 等待面板切换
                                            
                                            inv_capsule_key = "对战.捕捉.切换捕捉面板"
                                            inv_panel = self.regions.get(inv_capsule_key)
                                            if inv_panel:
                                                self._emit("🔄 切换捕捉面板（双击）...", "INFO")
                                                self._click_region_twice(inv_capsule_key, use_foreground, gap=0.10)
                                                self._sleep_abortable(stop_event, 0.50)
                                                # 点击无敌胶囊
                                                inv_key = "对战.捕捉.无敌精灵胶囊"
                                                if self.regions.get(inv_key):
                                                    self._click_region_twice(inv_key, use_foreground, gap=0.08)
                                                    self._sleep_abortable(stop_event, 0.55)
                                                    self._emit("🛡 补执行第一回合：无敌精灵胶囊", "INFO")
                                                else:
                                                    # 回退到技能一
                                                    self._click_region_twice("对战.使用技能一", use_foreground, gap=0.06)
                                                    self._emit("⚠️ 无敌胶囊区域缺失，回退为技能一", "WARN")
                                            else:
                                                # 回退到技能一
                                                self._click_region_twice("对战.使用技能一", use_foreground, gap=0.06)
                                                self._emit("⚠️ 切换捕捉面板区域缺失，回退为技能一", "WARN")
                                        else:
                                            # 第一回合使用技能一
                                            self._click_region_twice("对战.使用技能一", use_foreground, gap=0.06)
                                            self._emit("⚔️ 补执行第一回合：技能一", "INFO")
                        except Exception as e:
                            self._emit(f"⚠️ 补执行第一回合动作时出错: {e}", "WARN")
        
        # 标记进入战斗，离开稳态扫描
        self._is_scanning_steady_state = False
        self._is_in_battle = True
        
        # 战斗计数（在战斗开始前）
        battle_num = self._battle_count + 1  # 即将开始的战斗编号
        self._emit(f"⚔️ [第 {battle_num} 次战斗] 开始战斗/捕捉流程", "SYSTEM")
        
        # 执行战斗并获取结果
        capture_success = self._do_battle_capture(profile, use_foreground, stop_event, test_mode=test_mode, invincible_first_round=invincible_first_round, xiaodouya_nie_test_mode=xiaodouya_nie_test_mode)
        
        # 战斗完成后计数
        self._battle_count += 1  # 战斗计数+1
        
        # 根据战斗结果返回状态
        if capture_success:
            return "captured"  # 捕捉成功
        else:
            return "battled"   # 成功进入战斗但未捕捉成功（使用技能等）
    
    def _do_battle_capture(
        self, 
        profile: WildCaptureProfile, 
        use_foreground: bool, 
        stop_event: threading.Event,
        test_mode: bool = False,  # 测试模式：第一回合技能一+后续只使用中级胶囊
        invincible_first_round: bool = False,  # 第一回合是否使用无敌胶囊（已在外部确定）
        xiaodouya_nie_test_mode: bool = False,  # 小豆芽尼尔测试模式：捕捉16号精灵，测试切换精灵逻辑
    ) -> bool:
        """
        执行战斗捕捉流程
        
        Returns:
            True: 捕捉成功（使用了胶囊且战斗正常结束）
            False: 捕捉失败/战斗失败
        """
        if stop_event.is_set() or getattr(self.bot, "stop_current", False):
            return False

        # ✅ 所有捕捉逻辑前：先双击切换对战面板，再双击切换捕捉面板
        try:
            # 使用确定的 region key（对应 json 文件名）
            battle_panel_key = "对战.切换战斗面板"
            capture_panel_key = "对战.捕捉.切换捕捉面板"
            
            if self.regions.get(battle_panel_key):
                self._emit("🔄 切换对战面板（双击）...", "INFO")
                self._click_region_twice(battle_panel_key, use_foreground, gap=0.06)  # 双击内部间隔0.06秒
                time.sleep(0.3)  # 等待面板切换（和切换精灵到出战之间的间隔一致）
            else:
                self._emit("⚠️ 未找到切换对战面板的 region，跳过", "WARN")
            
            if self.regions.get(capture_panel_key):
                self._emit("🔄 切换捕捉面板（双击）...", "INFO")
                self._click_region_twice(capture_panel_key, use_foreground, gap=0.06)  # 双击内部间隔0.06秒
                time.sleep(0.3)  # 等待捕捉面板切换
            else:
                self._emit("⚠️ 未找到切换捕捉面板的 region，跳过", "WARN")
        except Exception as e:
            self._emit(f"⚠️ 切换面板时出错：{e}，继续执行战斗逻辑", "WARN")

        # ✅ 尼尔家族检测已经在_handle_battle_trigger中完成，这里直接使用保存的结果（不再使用pattern_valid）
        nie_family_id = getattr(self, '_current_battle_nie_family_id', None)
        
        # 清空立即收集到的pet IDs（已在_handle_battle_trigger中使用过）
        self._immediate_collected_pet_ids = None

        # ✅ 使用统一框架的战斗循环（Stage 2已在_handle_battle_trigger中完成）
        if self._unified_framework and self._wild_adapter:
            from core.unified_battle_framework import BattleConfig, BattleMode
            
            # 创建动作回调（支持尼尔家族切换逻辑、双塔逃跑逻辑和嘟咕噜逃跑逻辑）
            def action_callback(round_idx: int) -> str:
                # ✅ 双塔模式：如果满足逃跑条件，第二回合执行逃跑
                is_shuangta_mode_local = "双塔" in profile.name.lower()
                if is_shuangta_mode_local and self._shuangta_should_escape:
                    if round_idx == 2:
                        self._emit("🏃 [双塔逃跑] 第二回合执行逃跑", "SUCCESS")
                        return "escape"
                    elif round_idx > 2:
                        # 如果第二回合逃跑失败，继续逃跑
                        return "escape"
                
                # ✅ 嘟咕噜模式：如果满足逃跑条件，第二回合执行逃跑
                is_dugulu_mode_local = "嘟咕噜" in profile.name.lower()
                if is_dugulu_mode_local and self._dugulu_should_escape:
                    if round_idx == 2:
                        self._emit("🏃 [嘟咕噜逃跑] 第二回合执行逃跑", "SUCCESS")
                        return "escape"
                    elif round_idx > 2:
                        # 如果第二回合逃跑失败，继续逃跑
                        return "escape"
                
                # ✅ 如果有尼尔家族，执行尼尔家族逻辑（所有尼尔家族都正常捕捉）
                if nie_family_id is not None:
                    if round_idx == 1:
                        return "skill"  # 第一回合使用技能一
                    elif round_idx == 2:
                        # 第二回合切换精灵
                        self._switch_pet_for_nie_family(
                            nie_family_id, use_foreground, stop_event, test_mode=xiaodouya_nie_test_mode
                        )
                        return "switch"  # 返回"switch"表示切换精灵（统一框架会忽略这个动作）
                    elif round_idx == 3:
                        # 第三回合开始捕捉（高级胶囊）
                        return "capsule_high"
                    else:
                        # 第四回合后：一高一中混合（不是每三回合一高，是一高一中）
                        # 回合4=中，回合5=高，回合6=中，回合7=高...
                        if round_idx % 2 == 0:
                            return "capsule"  # 偶数回合（4,6,8...）中级
                        else:
                            return "capsule_high"  # 奇数回合（5,7,9...）高级
                
                # 没有尼尔家族，使用正常逻辑
                if test_mode:
                    # 测试模式：第一回合技能一，后续回合只使用中级胶囊（不交替高级）
                    if round_idx == 1:
                        return "skill"  # 技能1
                    else:
                        return "capsule"  # 后续回合只使用胶囊（统一框架会使用中级胶囊）
                else:
                    # 正常模式
                    if round_idx == 1:
                        if invincible_first_round:
                            return "capsule"  # 无敌胶囊
                        else:
                            return "skill"  # 技能1
                    # 第2回合开始：捕捉逻辑（中级/高级交替）
                    return "capsule"
            
            # 创建配置
            config = BattleConfig(
                mode=BattleMode.WILD,
                use_foreground=use_foreground,
                skill_key="对战.使用技能一",
                action_callback=action_callback,
                abort_check=lambda: stop_event.is_set() or getattr(self.bot, "stop_current", False),
                invincible_first_round=invincible_first_round,
                test_mode_capsule_only_mid=test_mode,  # 测试模式：后续回合只使用中级胶囊
                test_mode=test_mode,  # 测试模式标志（用于战斗内恢复判断）
            )
            
            # 执行Stage 3和Stage 4（Stage 2已在_handle_battle_trigger中完成）
            battle_success = self._unified_framework.stage3_battle_loop(config)
            if battle_success:
                self._unified_framework.stage4_post_battle(config, is_training_room=False)
                # 检查是否使用了胶囊（捕捉）
                from core.unified_battle_framework import LastActionType
                if self._unified_framework._last_action == LastActionType.CAPSULE:
                    return True  # 使用了胶囊，认为捕捉成功
                else:
                    return False  # 使用了技能或逃跑，不算捕捉成功
            else:
                return False  # 战斗失败
        else:
            # 回退到旧实现
            try:
                self.battle_runner.run_mantis_capture_mode(
                    map_swf_id=profile.map_swf_id,
                    use_foreground=use_foreground,
                    invincible_first_round=invincible_first_round,
                )
                # 旧实现无法判断捕捉结果，返回False（算作错过）
                return False
            except TypeError:
                self.battle_runner.run_mantis_capture_mode(
                    map_swf_id=profile.map_swf_id,
                    use_foreground=use_foreground,
                )
                # 旧实现无法判断捕捉结果，返回False（算作错过）
                return False

    def _classify_and_log_pet_ids(self, pet_ids: set, profile: WildCaptureProfile) -> None:
        """
        分类并记录pet IDs（我方精灵、目标精灵、尼尔家族精灵）
        
        Args:
            pet_ids: 收集到的pet IDs集合
            profile: 野外捕捉配置
        """
        if not pet_ids:
            return
        
        # 定义类别
        MY_PETS = {162, 312, 418}  # 我方精灵（注意：不是166）
        NIE_FAMILY = {77, 310, 416}
        target_pet_id_set = set(profile.target_pet_ids) if profile.target_pet_ids else {profile.target_pet_id}
        
        # 检查是否有重复（系统可能重复一遍，所以是4/5或8/10）
        unique_ids = pet_ids
        if len(pet_ids) in [8, 10]:
            sorted_ids = sorted(pet_ids)
            mid = len(sorted_ids) // 2
            first_half = set(sorted_ids[:mid])
            second_half = set(sorted_ids[mid:])
            if first_half == second_half:
                unique_ids = first_half
                self._emit(f"📋 检测到系统重复，去重后的IDs: {sorted(unique_ids)}", "INFO")
        
        # 分类
        my_pets_in_battle = unique_ids & MY_PETS
        target_pets_in_battle = unique_ids & target_pet_id_set
        nie_pets_in_battle = unique_ids & NIE_FAMILY
        
        # 一行列出所有ID（4或5个）
        all_ids_str = str(sorted(unique_ids))
        self._emit(f"📋 所有fight pet IDs（{len(unique_ids)}个）: {all_ids_str}", "INFO")
        
        # 标注类型
        my_pets_str = str(sorted(my_pets_in_battle)) if my_pets_in_battle else "{}"
        target_pets_str = str(sorted(target_pets_in_battle)) if target_pets_in_battle else "{}"
        nie_pets_str = str(sorted(nie_pets_in_battle)) if nie_pets_in_battle else "{}"
        
        self._emit(f"   我方精灵: {my_pets_str}", "INFO")
        self._emit(f"   目标精灵: {target_pets_str}", "INFO")
        if nie_pets_in_battle:
            self._emit(f"   尼尔家族精灵出现: {nie_pets_str}", "INFO")
    
    def _check_fight_pet_pattern(
        self,
        pet_ids: set,
        profile: WildCaptureProfile,
        test_mode: bool = False,
    ) -> Tuple[bool, Optional[int]]:
        """
        检测fight pet pattern是否符合预期
        
        Args:
            pet_ids: 收集到的所有pet IDs
            profile: 捕捉配置
            test_mode: 是否为测试模式（测试模式下目标精灵为16）
        
        Returns:
            (is_valid, nie_family_id)
            - is_valid: pattern是否有效
            - nie_family_id: 尼尔家族ID（如果有，77/310/416），None表示没有
        """
        # 定义三类精灵
        MY_PETS = {162, 312, 418}  # 我方精灵（注意：不是166）  # 第一类：我方精灵
        NIE_FAMILY = {77, 310, 416}  # 第三类：尼尔家族
        
        # 确定第二类（目标精灵）
        if test_mode:
            target_pet_id_set = {16}  # 测试模式：捕捉16号精灵
        else:
            # 优先使用target_pet_ids，否则使用target_pet_id
            target_pet_id_set = set(profile.target_pet_ids) if profile.target_pet_ids else {profile.target_pet_id}
        
        # 检查是否有重复（系统可能重复一遍，所以是4/5或8/10）
        unique_ids = pet_ids
        if len(pet_ids) in [4, 5, 8, 10]:
            # 可能是重复的，需要去重检查
            if len(pet_ids) in [8, 10]:
                # 系统重复了一遍，需要检查是否是4或5的重复
                # 检查前一半和后一半是否相同
                sorted_ids = sorted(pet_ids)
                mid = len(sorted_ids) // 2
                first_half = set(sorted_ids[:mid])
                second_half = set(sorted_ids[mid:])
                if first_half == second_half:
                    unique_ids = first_half
                    self._emit(f"📋 检测到系统重复，去重后的IDs: {sorted(unique_ids)}", "INFO")
        
        # 检查pattern
        my_pets_in_battle = unique_ids & MY_PETS
        target_pets_in_battle = unique_ids & target_pet_id_set
        nie_pets_in_battle = unique_ids & NIE_FAMILY
        other_pets = unique_ids - MY_PETS - target_pet_id_set - NIE_FAMILY
        
        # 检查是否符合预期pattern
        expected_count = 4 if len(nie_pets_in_battle) == 0 else 5
        is_valid = (
            len(my_pets_in_battle) == 3 and  # 必须包含3只我方精灵
            len(target_pets_in_battle) == 1 and  # 必须包含1只目标精灵
            len(other_pets) == 0 and  # 不能有其他精灵
            len(unique_ids) == expected_count  # 总数必须符合预期
        )
        
        nie_family_id = None
        if len(nie_pets_in_battle) == 1:
            nie_family_id = next(iter(nie_pets_in_battle))
        
        if is_valid:
            if nie_family_id:
                self._emit(
                    f"✅ Pattern检测通过：我方3只({sorted(my_pets_in_battle)}) + "
                    f"目标1只({sorted(target_pets_in_battle)}) + 尼尔家族1只({nie_family_id})",
                    "SUCCESS"
                )
            else:
                self._emit(
                    f"✅ Pattern检测通过：我方3只({sorted(my_pets_in_battle)}) + "
                    f"目标1只({sorted(target_pets_in_battle)})",
                    "SUCCESS"
                )
        else:
            self._emit(
                f"⚠️ Pattern检测异常：我方{len(my_pets_in_battle)}只({sorted(my_pets_in_battle)}) + "
                f"目标{len(target_pets_in_battle)}只({sorted(target_pets_in_battle)}) + "
                f"尼尔{len(nie_pets_in_battle)}只({sorted(nie_pets_in_battle)}) + "
                f"其他{len(other_pets)}只({sorted(other_pets)}) | "
                f"总数：{len(unique_ids)}（期望：{expected_count}）",
                "WARN"
            )
        
        return is_valid, nie_family_id

    def _detect_switch_probe_pet_type(self, use_foreground: bool) -> Optional[str]:
        """
        检测切换探针区域，判断当前选中的精灵是艾斯菲格还是闪光艾菲亚
        
        Args:
            use_foreground: 是否前台运行
        
        Returns:
            "aisifeige" 表示艾斯菲格，"flash_aifeia" 表示闪光艾菲亚，None表示检测失败
        """
        try:
            # 获取切换探针区域
            # 根据用户提供的JSON文件，key是"对战.切换二探针"
            # 尝试多个可能的key
            probe_keys = ["对战.切换二探针", "对战.切换探针", "对战.切换精灵.切换探针", "对战.切换精灵.切换二探针"]
            probe_reg = None
            probe_key = None
            for key in probe_keys:
                try:
                    probe_reg = self.regions.get(key)
                    if probe_reg:
                        probe_key = key
                        break
                except KeyError:
                    continue
            
            if probe_reg is None:
                self._emit(f"⚠️ 找不到切换探针区域（尝试了{probe_keys}）", "WARN")
                return None
            
            # 截取探针区域图像
            x1, y1, x2, y2 = probe_reg.outer_bbox()
            img = window_manager.grab_game_bbox(x1, y1, x2, y2)
            if img is None:
                self._emit("⚠️ 无法截取切换探针区域图像", "WARN")
                return None
            
            # 转换为RGB并计算平均RGB
            if img.mode != 'RGB':
                img = img.convert('RGB')
            arr = np.array(img)
            mean_rgb = arr.mean(axis=(0, 1)).astype(int)
            r, g, b = int(mean_rgb[0]), int(mean_rgb[1]), int(mean_rgb[2])
            
            self._emit(f"📋 [切换探针检测] 探针区域平均RGB: ({r}, {g}, {b})", "DEBUG")
            
            # 计算与两个参考RGB的欧氏距离
            def euclidean_distance(rgb1, rgb2):
                return np.sqrt(sum((a - b) ** 2 for a, b in zip(rgb1, rgb2)))
            
            dist_to_aisifeige = euclidean_distance((r, g, b), AISIFEIGE_PROBE_RGB)
            dist_to_flash_aifeia = euclidean_distance((r, g, b), FLASH_AIFEIA_PROBE_RGB)
            
            self._emit(f"📋 [切换探针检测] 距离艾斯菲格参考RGB: {dist_to_aisifeige:.2f}，距离闪光艾菲亚参考RGB: {dist_to_flash_aifeia:.2f}", "DEBUG")
            
            # 选择距离更近的
            if dist_to_aisifeige < dist_to_flash_aifeia:
                self._emit(f"✅ [切换探针检测] 检测到艾斯菲格（距离={dist_to_aisifeige:.2f}）", "SUCCESS")
                return "aisifeige"
            else:
                self._emit(f"✅ [切换探针检测] 检测到闪光艾菲亚（距离={dist_to_flash_aifeia:.2f}）", "SUCCESS")
                return "flash_aifeia"
                
        except Exception as e:
            self._emit(f"⚠️ 检测切换探针异常: {e}", "WARN")
            import traceback
            self._emit(f"📋 异常详情: {traceback.format_exc()}", "DEBUG")
            return None

    def _switch_pet_for_nie_family(
        self,
        nie_family_id: int,
        use_foreground: bool,
        stop_event: threading.Event,
        test_mode: bool = False,
    ) -> bool:
        """
        根据尼尔家族ID和探针扫描结果切换精灵，并在切换后验证是否正确
        
        Args:
            nie_family_id: 尼尔家族ID（77/310/416）
            use_foreground: 是否前台运行
            stop_event: 停止事件
            test_mode: 是否为测试模式（测试模式下轮流切换精灵二/三）
        
        Returns:
            True表示成功，False表示失败
        """
        try:
            # 确定要切换的精灵编号和期望的精灵类型
            expected_pet_type = None  # "aisifeige" 或 "flash_aifeia"
            if test_mode:
                # 测试模式（小豆芽尼尔测试）：轮流切换精灵二/三
                pet_num = 2 if self._nie_switch_counter == 0 else 3
                self._nie_switch_counter = 1 - self._nie_switch_counter  # 切换计数器
                self._emit(f"🔄 测试模式：切换到精灵{pet_num}（轮流切换）", "INFO")
            else:
                # 正常模式：根据尼尔家族ID和探针扫描结果决定
                if nie_family_id == 416:
                    # 尼奥：切换到闪光艾菲亚的位置
                    if self._flash_aifeia_pos:
                        # 将"二"或"三"转换为数字
                        pet_num = 2 if self._flash_aifeia_pos == "二" else 3
                        expected_pet_type = "flash_aifeia"
                        self._emit(f"🔄 [416尼奥] 根据探针扫描结果，切换到闪光艾菲亚（精灵{self._flash_aifeia_pos}）", "INFO")
                    else:
                        # 如果探针扫描失败，回退到默认逻辑（精灵二）
                        pet_num = 2
                        expected_pet_type = "flash_aifeia"
                        self._emit(f"⚠️ [416尼奥] 探针扫描结果不可用，使用默认逻辑切换到精灵二", "WARN")
                elif nie_family_id in (77, 310):
                    # 尼尔/闪光尼尔：切换到艾斯菲格的位置
                    if self._aisifeige_pos:
                        # 将"二"或"三"转换为数字
                        pet_num = 2 if self._aisifeige_pos == "二" else 3
                        expected_pet_type = "aisifeige"
                        self._emit(f"🔄 [77/310尼尔] 根据探针扫描结果，切换到艾斯菲格（精灵{self._aisifeige_pos}）", "INFO")
                    else:
                        # 如果探针扫描失败，回退到默认逻辑（精灵三）
                        pet_num = 3
                        expected_pet_type = "aisifeige"
                        self._emit(f"⚠️ [77/310尼尔] 探针扫描结果不可用，使用默认逻辑切换到精灵三", "WARN")
                else:
                    self._emit(f"⚠️ 未知的尼尔家族ID: {nie_family_id}，无法切换精灵", "WARN")
                    return False
            
            # 1. 双击切换精灵面板
            self._emit(f"🔄 双击切换精灵面板", "INFO")
            switch_panel_key = "对战.切换精灵.切换精灵面板"
            try:
                self._click_region_twice(switch_panel_key, use_foreground)
            except KeyError:
                # 尝试使用"战斗"前缀
                switch_panel_key = "战斗.切换精灵面板"
                self._click_region_twice(switch_panel_key, use_foreground)
            
            time.sleep(0.5)  # 等待面板打开
            
            # 2. 双击切换精灵X（使用汉字：一、二、三、四、五、六）
            self._emit(f"🔄 双击切换精灵{pet_num}", "INFO")
            # 将数字转换为汉字
            pet_num_chinese = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六"}.get(pet_num, str(pet_num))
            switch_pet_key = f"对战.切换精灵.切换精灵{pet_num_chinese}"
            try:
                self._click_region_twice(switch_pet_key, use_foreground)
            except KeyError:
                self._emit(f"⚠️ 找不到切换精灵{pet_num_chinese}（{pet_num}）的区域，切换失败", "WARN")
                return False
            
            time.sleep(0.3)  # 短暂等待，让切换生效
            
            # ✅ 3. 在双击出战之前，检测切换探针，验证切换的精灵是否正确
            if expected_pet_type:
                self._emit(f"🔍 检测切换探针，验证切换的精灵是否正确（期望：{expected_pet_type}）", "INFO")
                detected_pet_type = self._detect_switch_probe_pet_type(use_foreground)
                
                if detected_pet_type is None:
                    self._emit(f"⚠️ 无法检测切换探针，跳过验证，直接出战", "WARN")
                elif detected_pet_type != expected_pet_type:
                    # 检测到的精灵类型与期望不一致，需要切换到另一个精灵
                    self._emit(f"⚠️ 切换验证失败：期望{expected_pet_type}，但检测到{detected_pet_type}，切换到另一个精灵", "WARN")
                    
                    # 切换到另一个精灵（如果当前是二，切换到三；如果当前是三，切换到二）
                    other_pet_num = 3 if pet_num == 2 else 2
                    other_pet_num_chinese = {2: "二", 3: "三"}.get(other_pet_num, str(other_pet_num))
                    other_switch_pet_key = f"对战.切换精灵.切换精灵{other_pet_num_chinese}"
                    
                    self._emit(f"🔄 双击切换精灵{other_pet_num}（另一个精灵）", "INFO")
                    try:
                        self._click_region_twice(other_switch_pet_key, use_foreground)
                        time.sleep(0.3)  # 短暂等待
                        
                        # 再次验证
                        self._emit(f"🔍 再次检测切换探针，验证切换的精灵是否正确（期望：{expected_pet_type}）", "INFO")
                        detected_pet_type_retry = self._detect_switch_probe_pet_type(use_foreground)
                        if detected_pet_type_retry == expected_pet_type:
                            self._emit(f"✅ 切换验证成功：检测到{detected_pet_type_retry}，与期望一致", "SUCCESS")
                        else:
                            self._emit(f"⚠️ 切换验证仍然失败：期望{expected_pet_type}，但检测到{detected_pet_type_retry}，继续出战", "WARN")
                    except KeyError:
                        self._emit(f"⚠️ 找不到切换精灵{other_pet_num_chinese}（{other_pet_num}）的区域，跳过验证", "WARN")
                else:
                    self._emit(f"✅ 切换验证成功：检测到{detected_pet_type}，与期望一致", "SUCCESS")
            
            # 4. 双击出战
            self._emit(f"🔄 双击出战", "INFO")
            battle_key = "战斗.出战"
            try:
                self._click_region_twice(battle_key, use_foreground)
            except KeyError:
                self._emit(f"⚠️ 找不到出战区域，切换失败", "WARN")
                return False
            
            time.sleep(0.5)  # 等待切换完成
            
            self._emit(f"✅ 成功切换到精灵{pet_num}", "SUCCESS")
            return True
            
        except Exception as e:
            self._emit(f"⚠️ 切换精灵异常: {e}", "WARN")
            import traceback
            self._emit(f"📋 异常详情: {traceback.format_exc()}", "DEBUG")
            return False

    def _handle_nie_family_placeholder(self, profile: WildCaptureProfile, pet_ids: set, nie_hit: List[int]) -> None:
        self._emit(
            f"🧿 检测到尼尔家族 swf={nie_hit} | profile={profile.name} | fight_pets={sorted(pet_ids)} | TODO: 这里接不同处理逻辑",
            "WARN",
        )
    
    def _scan_pet_probes_to_identify_pets(self, use_foreground: bool) -> Tuple[Optional[str], Optional[str]]:
        """
        扫描精灵二和精灵三的探针，识别哪个是闪光艾菲亚，哪个是艾斯菲格
        
        规则：
        - 如果探针颜色全部是 #184992（纯色），那么这只精灵是艾斯菲格（77/310需要切换和恢复的精灵）
        - 如果颜色不纯（不是全部184992），则这只精灵是闪光艾菲亚（416尼奥需要切换和恢复的精灵）
        
        Returns:
            (flash_aifeia_pos, aisifeige_pos) - 闪光艾菲亚位置（"二"或"三"）和艾斯菲格位置（"二"或"三"），如果识别失败则为None
        """
        COLOR_TARGET = (24, 73, 146)  # #184992 - 纯蓝色（艾斯菲格的纯色）
        COLOR_TOLERANCE = 5  # 颜色容差
        
        self._emit("🔍 开始扫描精灵二和精灵三探针，识别闪光艾菲亚和艾斯菲格", "INFO")
        
        probe_results = {}  # {pos: (mean_rgb, match_ratio, distance)}
        
        # 扫描精灵二和精灵三的探针，收集所有结果
        for pos in ["二", "三"]:
            try:
                probe_key = f"精灵背包.精灵{pos}探针"
                probe_reg = self.regions.require(probe_key)
                
                # 获取探针区域的图像
                from core.utils import window_manager
                x1, y1, x2, y2 = probe_reg.outer_bbox()
                img = window_manager.grab_game_bbox(x1, y1, x2, y2)
                if img is None:
                    self._emit(f"⚠️ [探针扫描] 精灵{pos}探针无法获取图像", "WARN")
                    continue
                
                # 转换为numpy数组进行检查
                import numpy as np
                arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
                if arr.size == 0:
                    self._emit(f"⚠️ [探针扫描] 精灵{pos}探针图像为空", "WARN")
                    continue
                
                # 计算平均RGB
                mean_rgb = np.round(arr.mean(axis=(0, 1))).astype(int)
                
                # 检查所有像素是否都在目标颜色的容差范围内
                tr, tg, tb = COLOR_TARGET
                # 创建一个布尔掩码，表示每个像素是否在容差范围内
                r_match = np.abs(arr[:, :, 0].astype(np.int16) - tr) <= COLOR_TOLERANCE
                g_match = np.abs(arr[:, :, 1].astype(np.int16) - tg) <= COLOR_TOLERANCE
                b_match = np.abs(arr[:, :, 2].astype(np.int16) - tb) <= COLOR_TOLERANCE
                all_match = r_match & g_match & b_match
                
                # 计算匹配的像素比例
                total_pixels = arr.shape[0] * arr.shape[1]
                matched_pixels = np.sum(all_match)
                match_ratio = matched_pixels / total_pixels if total_pixels > 0 else 0.0
                
                # 计算平均RGB与目标颜色的欧几里得距离
                distance = ((mean_rgb[0] - tr) ** 2 + (mean_rgb[1] - tg) ** 2 + (mean_rgb[2] - tb) ** 2) ** 0.5
                
                # 保存结果
                probe_results[pos] = (mean_rgb, match_ratio, distance)
                
                self._emit(f"📋 [探针扫描] 精灵{pos}探针：匹配比例={match_ratio*100:.1f}%，平均RGB=({mean_rgb[0]}, {mean_rgb[1]}, {mean_rgb[2]})，距离目标颜色={distance:.1f}", "INFO")
                    
            except KeyError:
                self._emit(f"⚠️ [探针扫描] 找不到区域：{probe_key}", "WARN")
                continue
            except Exception as e:
                self._emit(f"⚠️ [探针扫描] 检测精灵{pos}探针时发生异常：{e}", "WARN")
                continue
        
        # 根据收集的结果判断：更接近纯蓝色 #184992 的是艾斯菲格（纯色），另一个是闪光艾菲亚（非纯色）
        flash_aifeia_pos = None
        aisifeige_pos = None
        
        if len(probe_results) == 2:
            # 有两个结果，比较距离目标颜色的距离
            pos_list = list(probe_results.keys())
            pos1, pos2 = pos_list[0], pos_list[1]
            _, _, dist1 = probe_results[pos1]
            _, _, dist2 = probe_results[pos2]
            
            # 距离更小的（更接近纯蓝色）是艾斯菲格（纯色）
            if dist1 < dist2:
                aisifeige_pos = pos1
                flash_aifeia_pos = pos2
            else:
                aisifeige_pos = pos2
                flash_aifeia_pos = pos1
            
            self._emit(f"🔷 [探针扫描] 精灵{aisifeige_pos}是艾斯菲格（更接近纯蓝色 #184992，距离={min(dist1, dist2):.1f}）", "INFO")
            self._emit(f"✨ [探针扫描] 精灵{flash_aifeia_pos}是闪光艾菲亚（非纯色，距离={max(dist1, dist2):.1f}）", "SUCCESS")
        elif len(probe_results) == 1:
            # 只有一个结果，无法比较，使用旧的匹配比例方法作为后备
            pos = list(probe_results.keys())[0]
            mean_rgb, match_ratio, distance = probe_results[pos]
            is_pure_color = match_ratio >= 0.95  # 95%以上像素匹配认为是纯色
            
            if is_pure_color:
                aisifeige_pos = pos
                self._emit(f"🔷 [探针扫描] 精灵{pos}是艾斯菲格（纯色184992，匹配比例={match_ratio*100:.1f}%）", "INFO")
            else:
                flash_aifeia_pos = pos
                self._emit(f"✨ [探针扫描] 精灵{pos}是闪光艾菲亚（非纯色，匹配比例={match_ratio*100:.1f}%）", "SUCCESS")
        
        # 根据"纯色和非纯色一定各有一个"的规则，如果只识别到一个，推断另一个
        if flash_aifeia_pos and aisifeige_pos:
            self._emit(f"✅ [探针扫描] 识别完成：闪光艾菲亚=精灵{flash_aifeia_pos}，艾斯菲格=精灵{aisifeige_pos}", "SUCCESS")
        elif flash_aifeia_pos:
            # 如果只识别到闪光艾菲亚（非纯色），那么另一个一定是艾斯菲格（纯色）
            other_pos = "三" if flash_aifeia_pos == "二" else "二"
            aisifeige_pos = other_pos
            self._emit(f"✅ [探针扫描] 识别到闪光艾菲亚=精灵{flash_aifeia_pos}，推断艾斯菲格=精灵{aisifeige_pos}（纯色和非纯色一定各有一个）", "INFO")
        elif aisifeige_pos:
            # 如果只识别到艾斯菲格（纯色），那么另一个一定是闪光艾菲亚（非纯色）
            other_pos = "三" if aisifeige_pos == "二" else "二"
            flash_aifeia_pos = other_pos
            self._emit(f"✅ [探针扫描] 识别到艾斯菲格=精灵{aisifeige_pos}，推断闪光艾菲亚=精灵{flash_aifeia_pos}（纯色和非纯色一定各有一个）", "INFO")
        else:
            self._emit("❌ [探针扫描] 未识别到任何精灵类型，无法推断", "WARN")
        
        return (flash_aifeia_pos, aisifeige_pos)
    
    def _handle_hengmu_before_to_script(
        self,
        profile: WildCaptureProfile,
        use_foreground: bool,
        stop_event: threading.Event
    ) -> bool:
        """
        在执行to脚本之前，检测和处理亨姆的流程
        
        流程：
        1. 点击精灵背包
        2. sleep 2.5s（参考扫描艾斯菲格的逻辑）
        3. 扫描 登录.亨姆二、登录.亨姆三、登录.亨姆四 三个区域
        4. 纯蓝色的是亨姆（参考艾斯菲格的逻辑，#184992）
        5. 判断是精灵二、三还是四
        6. 双击对应精灵区域
        7. 先点击 精灵背包.身边跟随（背包会被自动关闭）
        8. 再次点击精灵背包
        9. sleep 2.5s
        10. 双击对应区域
        11. 点击放回仓库
        12. 点击打开精灵背包
        13. 关闭精灵背包
        
        Returns:
            True=成功，False=失败
        """
        try:
            COLOR_TARGET = (24, 73, 146)  # #184992 - 纯蓝色（亨姆的纯色，参考艾斯菲格）
            COLOR_TOLERANCE = 5  # 颜色容差
            
            self._emit("🔍 [亨姆检测] 开始检测亨姆流程", "INFO")
            
            # 1. 打开精灵背包
            self._emit("💼 [亨姆检测] 打开精灵背包", "INFO")
            bag_open_key = "精灵背包.打开精灵背包"
            bag_open_btn_key = "精灵背包.打开精灵背包按钮"
            
            try:
                self._click_region(bag_open_btn_key, use_foreground)
            except KeyError:
                self._click_region(bag_open_key, use_foreground)
            
            # 2. sleep 2.5s（参考扫描艾斯菲格的逻辑）
            self._sleep_abortable(stop_event, 2.5)
            
            # 3. 扫描 登录.亨姆二、登录.亨姆三、登录.亨姆四 三个区域
            self._emit("🔍 [亨姆检测] 扫描登录.亨姆二、三、四探针，识别亨姆位置", "INFO")
            
            probe_results = {}  # {pos: (mean_rgb, match_ratio, distance)}
            
            for pos in ["二", "三", "四"]:
                try:
                    probe_key = f"登录.亨姆{pos}"
                    probe_reg = self.regions.require(probe_key)
                    
                    # 获取探针区域的图像
                    from core.utils import window_manager
                    x1, y1, x2, y2 = probe_reg.outer_bbox()
                    img = window_manager.grab_game_bbox(x1, y1, x2, y2)
                    if img is None:
                        self._emit(f"⚠️ [亨姆检测] 登录.亨姆{pos}探针无法获取图像", "WARN")
                        continue
                    
                    # 转换为numpy数组进行检查
                    import numpy as np
                    arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
                    if arr.size == 0:
                        self._emit(f"⚠️ [亨姆检测] 登录.亨姆{pos}探针图像为空", "WARN")
                        continue
                    
                    # 计算平均RGB
                    mean_rgb = np.round(arr.mean(axis=(0, 1))).astype(int)
                    
                    # 检查所有像素是否都在目标颜色的容差范围内
                    tr, tg, tb = COLOR_TARGET
                    r_match = np.abs(arr[:, :, 0].astype(np.int16) - tr) <= COLOR_TOLERANCE
                    g_match = np.abs(arr[:, :, 1].astype(np.int16) - tg) <= COLOR_TOLERANCE
                    b_match = np.abs(arr[:, :, 2].astype(np.int16) - tb) <= COLOR_TOLERANCE
                    all_match = r_match & g_match & b_match
                    
                    # 计算匹配的像素比例
                    total_pixels = arr.shape[0] * arr.shape[1]
                    matched_pixels = np.sum(all_match)
                    match_ratio = matched_pixels / total_pixels if total_pixels > 0 else 0.0
                    
                    # 计算平均RGB与目标颜色的欧几里得距离
                    distance = ((mean_rgb[0] - tr) ** 2 + (mean_rgb[1] - tg) ** 2 + (mean_rgb[2] - tb) ** 2) ** 0.5
                    
                    # 保存结果
                    probe_results[pos] = (mean_rgb, match_ratio, distance)
                    
                    self._emit(f"📋 [亨姆检测] 登录.亨姆{pos}探针：匹配比例={match_ratio*100:.1f}%，平均RGB=({mean_rgb[0]}, {mean_rgb[1]}, {mean_rgb[2]})，距离目标颜色={distance:.1f}", "INFO")
                        
                except KeyError:
                    self._emit(f"⚠️ [亨姆检测] 找不到区域：登录.亨姆{pos}", "WARN")
                    continue
                except Exception as e:
                    self._emit(f"⚠️ [亨姆检测] 检测登录.亨姆{pos}探针时发生异常：{e}", "WARN")
                    continue
            
            # 4. 判断哪个是亨姆（纯蓝色，参考艾斯菲格的逻辑）
            hengmu_pos = None
            
            if len(probe_results) > 0:
                # 找到最接近纯蓝色的（距离最小或匹配比例最高）
                best_pos = None
                best_score = float('inf')
                
                for pos, (mean_rgb, match_ratio, distance) in probe_results.items():
                    # 优先使用匹配比例，如果匹配比例相同，使用距离
                    score = distance - match_ratio * 100  # 距离越小越好，匹配比例越大越好
                    if score < best_score:
                        best_score = score
                        best_pos = pos
                
                if best_pos:
                    hengmu_pos = best_pos
                    _, match_ratio, distance = probe_results[best_pos]
                    self._emit(f"✅ [亨姆检测] 识别完成：亨姆=精灵{hengmu_pos}（匹配比例={match_ratio*100:.1f}%，距离={distance:.1f}）", "SUCCESS")
            
            if not hengmu_pos:
                self._emit("❌ [亨姆检测] 未识别到亨姆，跳过亨姆处理流程", "WARN")
                # 关闭精灵背包
                try:
                    self._click_region(bag_open_btn_key, use_foreground)
                except KeyError:
                    self._click_region(bag_open_key, use_foreground)
                self._sleep_abortable(stop_event, 0.5)
                return False
            
            # 5. 双击对应精灵区域
            self._emit(f"🖱️ [亨姆检测] 双击精灵{hengmu_pos}区域", "INFO")
            pet_key = f"精灵背包.精灵{hengmu_pos}"
            pet_btn_key = f"精灵背包.精灵{hengmu_pos}按钮"
            
            try:
                self._click_region_twice(pet_btn_key, use_foreground)
            except KeyError:
                self._click_region_twice(pet_key, use_foreground)
            
            # 双击后等待0.5s
            self._sleep_abortable(stop_event, 0.5)
            
            # 6. 点击 精灵背包.身边跟随（背包会被自动关闭）
            self._emit(f"👥 [亨姆检测] 点击精灵背包.身边跟随", "INFO")
            follow_key = "精灵背包.身边跟随"
            follow_btn_key = "精灵背包.身边跟随按钮"
            
            try:
                self._click_region(follow_btn_key, use_foreground)
            except KeyError:
                self._click_region(follow_key, use_foreground)
            
            # 等待背包关闭
            self._sleep_abortable(stop_event, 0.5)
            
            # 7. 再次点击精灵背包
            self._emit("💼 [亨姆检测] 再次打开精灵背包", "INFO")
            try:
                self._click_region(bag_open_btn_key, use_foreground)
            except KeyError:
                self._click_region(bag_open_key, use_foreground)
            
            # 8. sleep 2.5s
            self._sleep_abortable(stop_event, 2.5)
            
            # 9. 双击对应区域
            self._emit(f"🖱️ [亨姆检测] 再次双击精灵{hengmu_pos}区域", "INFO")
            try:
                self._click_region_twice(pet_btn_key, use_foreground)
            except KeyError:
                self._click_region_twice(pet_key, use_foreground)
            
            # 双击后等待0.5s
            self._sleep_abortable(stop_event, 0.5)
            
            # 10. 点击放回仓库
            self._emit(f"📦 [亨姆检测] 点击放回仓库（精灵{hengmu_pos}）", "INFO")
            return_storage_key = "精灵背包.放回仓库"
            return_storage_btn_key = "精灵背包.放回仓库按钮"
            
            try:
                self._click_region(return_storage_btn_key, use_foreground)
            except KeyError:
                self._click_region(return_storage_key, use_foreground)
            
            # 点击放回仓库后等待1s
            self._sleep_abortable(stop_event, 1.0)
            
            # 11. 点击打开精灵背包（只保留这一个动作，不关闭）
            self._emit("💼 [亨姆检测] 点击打开精灵背包", "INFO")
            try:
                self._click_region(bag_open_btn_key, use_foreground)
            except KeyError:
                self._click_region(bag_open_key, use_foreground)
            
            self._sleep_abortable(stop_event, 0.5)
            
            self._emit("✅ [亨姆检测] 亨姆处理流程完成", "SUCCESS")
            return True
            
        except Exception as e:
            self._emit(f"❌ [亨姆检测] 处理异常: {e}", "ERROR")
            import traceback
            self._emit(f"📋 异常详情: {traceback.format_exc()}", "ERROR")
            return False
    
    def _check_dark_blue_probe(self, use_foreground: bool) -> bool:
        """
        检测回合探针是否为深蓝色（#0E203F 到 #003366 之间）
        
        Returns:
            True: 探针是深蓝色
            False: 探针不是深蓝色
        """
        try:
            probe_key = "对战.回合探针"
            r = self.regions.get(probe_key)
            if not r:
                return False
            
            # 截取探针区域
            x1, y1, x2, y2 = r.outer_bbox()
            img = window_manager.grab_game_bbox(x1, y1, x2, y2, min_size_px=2)
            if img is None:
                return False
            
            # 转换为RGB数组
            arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
            h, w = arr.shape[:2]
            
            # 检查中心区域的平均颜色
            cy, cx = h // 2, w // 2
            patch_size = min(3, h // 2, w // 2)
            y1_patch = max(0, cy - patch_size)
            y2_patch = min(h, cy + patch_size + 1)
            x1_patch = max(0, cx - patch_size)
            x2_patch = min(w, cx + patch_size + 1)
            patch = arr[y1_patch:y2_patch, x1_patch:x2_patch, :]
            
            # 计算平均RGB
            avg_rgb = patch.mean(axis=(0, 1)).astype(int)
            r_val, g_val, b_val = avg_rgb[0], avg_rgb[1], avg_rgb[2]
            
            # 深蓝色范围：#0E203F (14, 32, 63) 到 #003366 (0, 51, 102)
            # 检查是否在范围内
            min_r, min_g, min_b = 0, 0, 51
            max_r, max_g, max_b = 14, 51, 102
            
            is_dark_blue = (
                min_r <= r_val <= max_r and
                min_g <= g_val <= max_g and
                min_b <= b_val <= max_b
            )
            
            if is_dark_blue:
                self._emit(f"🔵 检测到深蓝色探针 (RGB=({r_val},{g_val},{b_val}), HEX=#{r_val:02X}{g_val:02X}{b_val:02X})", "DEBUG")
            
            return is_dark_blue
            
        except Exception as e:
            self._emit(f"⚠️ 检测深蓝色探针异常: {e}", "WARN")
            return False

    def _ocr_enemy_info_fast(self, use_foreground: bool) -> Tuple[Optional[int], Optional[int], Optional[Image.Image], Optional[Image.Image]]:
        """
        快速OCR扫描敌方精灵等级和血量（只尝试一次，但保存截图）
        
        Returns:
            (level, hp, level_img, hp_img): (等级, 血量, 等级原图, 血量原图)，如果识别失败则返回None
        """
        level = None
        hp = None
        level_img = None
        hp_img = None
        
        # OCR等级
        level_key = "对战信息.敌方精灵等级"
        try:
            r = self.regions.get(level_key)
            if r and pytesseract:
                x1, y1, x2, y2 = r.outer_bbox()
                img = window_manager.grab_game_bbox(x1, y1, x2, y2, min_size_px=2)
                if img:
                    level_img = img
                    from PIL import ImageOps
                    import re
                    
                    # 预处理图像
                    gray = img.convert("L")
                    gray = ImageOps.autocontrast(gray)
                    w, h = gray.size
                    gray = gray.resize((max(1, w * 4), max(1, h * 4)))
                    
                    # 尝试多种预处理和PSM模式
                    variants = []
                    variants.append(gray.point(lambda p: 255 if p > 160 else 0))  # normal
                    inv = ImageOps.invert(gray)
                    variants.append(inv.point(lambda p: 255 if p > 160 else 0))   # inverted
                    
                    configs = [
                        "--psm 7 -c tessedit_char_whitelist=0123456789",  # 单行文本
                        "--psm 8 -c tessedit_char_whitelist=0123456789",  # 单个单词
                        "--psm 6 -c tessedit_char_whitelist=0123456789",  # 单块文本
                    ]
                    
                    best_level = None
                    for i, variant in enumerate(variants):
                        for j, config in enumerate(configs):
                            try:
                                txt = pytesseract.image_to_string(variant, lang="eng", config=config)
                                # 优先匹配两位数
                                nums = re.findall(r"\d{2}", txt or "")
                                if not nums:
                                    # 如果没有两位数，回退到匹配任意长度数字
                                    nums = re.findall(r"\d{1,3}", txt or "")
                                
                                if nums:
                                    candidate_level = int(nums[0])
                                    if 1 <= candidate_level <= 120:
                                        best_level = candidate_level
                                        break  # 找到有效结果就退出内层循环
                            except Exception as e:
                                self._emit(f"⚠️ [OCR] 等级OCR尝试失败 (变体{i}, 配置{j}): {e}", "DEBUG")
                                continue
                        if best_level is not None:
                            break  # 找到有效结果就退出外层循环
                    
                    if best_level is not None:
                        level = best_level
        except Exception as e:
            self._emit(f"⚠️ OCR等级异常: {e}", "WARN")
        
        # OCR血量
        hp_key = "对战信息.敌方精灵血量"
        try:
            r = self.regions.get(hp_key)
            if r and pytesseract:
                x1, y1, x2, y2 = r.outer_bbox()
                img = window_manager.grab_game_bbox(x1, y1, x2, y2, min_size_px=2)
                if img:
                    hp_img = img
                    from PIL import ImageOps
                    import re
                    
                    # 预处理图像
                    gray = img.convert("L")
                    gray = ImageOps.autocontrast(gray)
                    w, h = gray.size
                    gray = gray.resize((max(1, w * 4), max(1, h * 4)))
                    
                    # 尝试多种预处理和PSM模式
                    variants = []
                    variants.append(gray.point(lambda p: 255 if p > 160 else 0))  # normal
                    inv = ImageOps.invert(gray)
                    variants.append(inv.point(lambda p: 255 if p > 160 else 0))   # inverted
                    
                    configs = [
                        "--psm 7 -c tessedit_char_whitelist=0123456789",  # 单行文本
                        "--psm 8 -c tessedit_char_whitelist=0123456789",  # 单个单词
                        "--psm 6 -c tessedit_char_whitelist=0123456789",  # 单块文本
                    ]
                    
                    best_hp = None
                    for i, variant in enumerate(variants):
                        for j, config in enumerate(configs):
                            try:
                                txt = pytesseract.image_to_string(variant, lang="eng", config=config)
                                # ✅ 强制只匹配两位数，不接受一位数
                                nums = re.findall(r"\d{2}", txt or "")
                                
                                if nums:
                                    candidate_hp = int(nums[0])
                                    if 1 <= candidate_hp <= 9999:
                                        best_hp = candidate_hp
                                        break  # 找到有效结果就退出内层循环
                                else:
                                    # ✅ 如果没有匹配到两位数，记录调试信息
                                    self._emit(f"⚠️ [OCR] 未匹配到两位数血量，原始文本: {txt!r} (变体{i}, 配置{j})", "DEBUG")
                            except Exception as e:
                                self._emit(f"⚠️ [OCR] 血量OCR尝试失败 (变体{i}, 配置{j}): {e}", "DEBUG")
                                continue
                        if best_hp is not None:
                            break  # 找到有效结果就退出外层循环
                    
                    if best_hp is not None:
                        hp = best_hp
                    else:
                        # ✅ 如果所有尝试都失败（没有匹配到两位数）
                        self._emit(f"⚠️ [OCR] 所有尝试均未匹配到两位数血量", "WARN")
        except Exception as e:
            self._emit(f"⚠️ OCR血量异常: {e}", "WARN")
        
        return level, hp, level_img, hp_img

    def _save_enemy_ocr_images(
        self,
        level: int,
        hp: int,
        level_img: Optional[Image.Image],
        hp_img: Optional[Image.Image],
        battle_num: int,
    ) -> None:
        """保存敌方等级/血量OCR成功时的原图（仅保存一份）"""
        if level_img is None or hp_img is None:
            return
        if not self.bot or not hasattr(self.bot, "project_root"):
            return
        try:
            save_dir = os.path.join(self.bot.project_root, "debug_ocr_enemy")
            os.makedirs(save_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            ms = int((time.time() % 1) * 1000)
            ts_full = f"{ts}_{ms:03d}"
            hp_path = os.path.join(save_dir, f"{hp}_{battle_num}_{ts_full}.png")
            level_path = os.path.join(save_dir, f"{level}_{battle_num}_{ts_full}.png")
            hp_img.save(hp_path)
            level_img.save(level_path)
            self._emit(f"📷 [OCR] 保存原图：{os.path.basename(hp_path)} / {os.path.basename(level_path)}", "DEBUG")
        except Exception as e:
            self._emit(f"⚠️ [OCR] 保存原图失败: {e}", "WARN")

    def _ocr_enemy_info(self, use_foreground: bool) -> Tuple[Optional[int], Optional[int]]:
        """
        OCR扫描敌方精灵等级和血量
        
        Returns:
            (level, hp): (等级, 血量)，如果识别失败则返回None
        """
        level = None
        hp = None
        
        # OCR等级
        level_key = "对战信息.敌方精灵等级"
        try:
            r = self.regions.get(level_key)
            if not r:
                self._emit(f"⚠️ [OCR] 未找到区域: {level_key}", "WARN")
            else:
                self._emit(f"🔍 [OCR] 开始扫描等级区域: {level_key}", "DEBUG")
                x1, y1, x2, y2 = r.outer_bbox()
                self._emit(f"🔍 [OCR] 等级区域坐标: ({x1}, {y1}) -> ({x2}, {y2})", "DEBUG")
                img = window_manager.grab_game_bbox(x1, y1, x2, y2, min_size_px=2)
                if not img:
                    self._emit(f"⚠️ [OCR] 无法截取等级区域图像", "WARN")
                elif not pytesseract:
                    self._emit(f"⚠️ [OCR] pytesseract不可用，无法执行OCR", "WARN")
                else:
                    from PIL import ImageOps
                    import re
                    
                    # 预处理图像
                    gray = img.convert("L")
                    gray = ImageOps.autocontrast(gray)
                    w, h = gray.size
                    gray = gray.resize((max(1, w * 4), max(1, h * 4)))
                    
                    # 尝试多种配置
                    variants = [
                        gray.point(lambda p: 255 if p > 160 else 0),
                        ImageOps.invert(gray).point(lambda p: 255 if p > 160 else 0),
                    ]
                    
                    configs = [
                        "--psm 7 -c tessedit_char_whitelist=0123456789",
                        "--psm 8 -c tessedit_char_whitelist=0123456789",
                        "--psm 6 -c tessedit_char_whitelist=0123456789",
                    ]
                    
                    txt_result = ""
                    for v_idx, v in enumerate(variants):
                        for cfg_idx, cfg in enumerate(configs):
                            try:
                                txt = pytesseract.image_to_string(v, lang="eng", config=cfg)
                                nums = re.findall(r"\d{1,3}", txt or "")
                                if nums:
                                    level = int(nums[0])
                                    if 1 <= level <= 120:
                                        txt_result = txt
                                        self._emit(f"✅ [OCR] 等级识别成功: {level} (variant={v_idx}, config={cfg_idx}, raw_text={txt!r})", "INFO")
                                        break
                            except Exception as e:
                                self._emit(f"⚠️ [OCR] 等级OCR尝试失败 (variant={v_idx}, config={cfg_idx}): {e}", "DEBUG")
                        if level:
                            break
                    
                    if level:
                        self._emit(f"📟 [OCR] 敌方精灵等级: {level}", "INFO")
                    else:
                        self._emit(f"📟 [OCR] 敌方精灵等级识别失败，原始文本: {txt_result if txt_result else 'N/A'}", "WARN")
        except Exception as e:
            self._emit(f"⚠️ OCR等级异常: {e}", "WARN")
            import traceback
            self._emit(f"📋 OCR等级异常详情: {traceback.format_exc()}", "DEBUG")
        
        # OCR血量
        hp_key = "对战信息.敌方精灵血量"
        try:
            r = self.regions.get(hp_key)
            if not r:
                self._emit(f"⚠️ [OCR] 未找到区域: {hp_key}", "WARN")
            else:
                self._emit(f"🔍 [OCR] 开始扫描血量区域: {hp_key}", "DEBUG")
                x1, y1, x2, y2 = r.outer_bbox()
                self._emit(f"🔍 [OCR] 血量区域坐标: ({x1}, {y1}) -> ({x2}, {y2})", "DEBUG")
                img = window_manager.grab_game_bbox(x1, y1, x2, y2, min_size_px=2)
                if not img:
                    self._emit(f"⚠️ [OCR] 无法截取血量区域图像", "WARN")
                elif not pytesseract:
                    self._emit(f"⚠️ [OCR] pytesseract不可用，无法执行OCR", "WARN")
                else:
                    from PIL import ImageOps
                    import re
                    
                    # 预处理图像
                    gray = img.convert("L")
                    gray = ImageOps.autocontrast(gray)
                    w, h = gray.size
                    gray = gray.resize((max(1, w * 4), max(1, h * 4)))
                    
                    # 尝试多种配置
                    variants = [
                        gray.point(lambda p: 255 if p > 160 else 0),
                        ImageOps.invert(gray).point(lambda p: 255 if p > 160 else 0),
                    ]
                    
                    configs = [
                        "--psm 7 -c tessedit_char_whitelist=0123456789",
                        "--psm 8 -c tessedit_char_whitelist=0123456789",
                        "--psm 6 -c tessedit_char_whitelist=0123456789",
                    ]
                    
                    txt_result = ""
                    for v_idx, v in enumerate(variants):
                        for cfg_idx, cfg in enumerate(configs):
                            try:
                                txt = pytesseract.image_to_string(v, lang="eng", config=cfg)
                                nums = re.findall(r"\d+", txt or "")
                                if nums:
                                    hp = int(nums[0])
                                    if 1 <= hp <= 9999:
                                        txt_result = txt
                                        self._emit(f"✅ [OCR] 血量识别成功: {hp} (variant={v_idx}, config={cfg_idx}, raw_text={txt!r})", "INFO")
                                        break
                            except Exception as e:
                                self._emit(f"⚠️ [OCR] 血量OCR尝试失败 (variant={v_idx}, config={cfg_idx}): {e}", "DEBUG")
                        if hp:
                            break
                    
                    if hp:
                        self._emit(f"📟 [OCR] 敌方精灵血量: {hp}", "INFO")
                    else:
                        self._emit(f"📟 [OCR] 敌方精灵血量识别失败，原始文本: {txt_result if txt_result else 'N/A'}", "WARN")
        except Exception as e:
            self._emit(f"⚠️ OCR血量异常: {e}", "WARN")
            import traceback
            self._emit(f"📋 OCR血量异常详情: {traceback.format_exc()}", "DEBUG")
        
        return level, hp

    def _monitor_enemy_info_after_skill(
        self, 
        use_foreground: bool, 
        stop_event: threading.Event,
        timeout_s: float = 30.0,
        check_condition_callback: Optional[Callable[[Optional[int], Optional[int]], bool]] = None
    ) -> Tuple[Optional[int], Optional[int], str]:
        """
        监控敌方精灵信息（等级和血量）
        
        流程：
        1. 等待第一个 /resource/fightResource/skill/ 出现（作为检测深蓝色探针的开始标志）
        2. skill信号出现后，开始检测回合探针是否为深蓝色（#0E203F 到 #003366 之间）
        3. 等待探针由深蓝变灰
        4. 探针变灰后，开始OCR扫描，持续到出现PetItem
        5. 返回扫描结果和状态
        
        Args:
            use_foreground: 是否前台运行
            stop_event: 停止事件
            timeout_s: 总超时时间（秒）
        
        Returns:
            (level, hp, status): (等级, 血量, 状态)
            status: "success" / "timeout" / "ocr_failed" / "stopped"
        """
        from core.logger import fetch_kernel_since, kernel_cursor
        
        self._emit("🔍 [敌方信息监控] 开始监控流程", "INFO")
        
        # 1. 等待第一个 /resource/fightResource/skill/ 出现（作为检测深蓝色探针的开始标志）
        skill_token = "/resource/fightResource/skill/"
        start_cursor = kernel_cursor()
        skill_detected = False
        skill_detected_time = None
        
        t0 = time.time()
        
        # 先检查已有日志
        try:
            lines = fetch_kernel_since(start_cursor)
            if isinstance(lines, list):
                for line in lines:
                    if skill_token in str(line):
                        skill_detected = True
                        skill_detected_time = time.time()
                        self._emit("✅ [敌方信息监控] 检测到 /resource/fightResource/skill/ 信号", "SUCCESS")
                        break
        except Exception:
            pass
        
        # 如果没有检测到，继续监控
        if not skill_detected:
            self._emit("⏳ [敌方信息监控] 等待 /resource/fightResource/skill/ 信号（作为检测深蓝色探针的开始标志）...", "INFO")
            while not skill_detected:
                if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                    return None, None, "stopped"
                
                if (time.time() - t0) > timeout_s:
                    self._emit(f"⏱️ [敌方信息监控] 等待skill信号超时 ({timeout_s}s)", "WARN")
                    return None, None, "timeout"
                
                try:
                    lines = fetch_kernel_since(start_cursor)
                    if isinstance(lines, list):
                        for line in lines:
                            if skill_token in str(line):
                                skill_detected = True
                                skill_detected_time = time.time()
                                self._emit("✅ [敌方信息监控] 检测到 /resource/fightResource/skill/ 信号", "SUCCESS")
                                break
                    start_cursor = kernel_cursor()
                except Exception:
                    pass
                
                time.sleep(0.1)
        
        # 2. skill信号出现后，开始检测回合探针是否为深蓝色（#0E203F 到 #003366 之间）
        self._emit("🔵 [敌方信息监控] skill信号已出现，开始检测回合探针（深蓝色，范围：#0E203F 到 #003366）...", "INFO")
        dark_blue_detected = False
        dark_blue_time = None
        
        while not dark_blue_detected:
            if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                return None, None, "stopped"
            
            if (time.time() - t0) > timeout_s:
                self._emit(f"⏱️ [敌方信息监控] 等待深蓝色探针超时 ({timeout_s}s)", "WARN")
                return None, None, "timeout"
            
            if self._check_dark_blue_probe(use_foreground):
                dark_blue_detected = True
                dark_blue_time = time.time()
                self._emit("✅ [敌方信息监控] 检测到深蓝色探针", "SUCCESS")
                break
            
            time.sleep(0.1)
        
        # 3. 等待探针由深蓝变灰（只有变灰后才开始OCR）
        self._emit("⏳ [敌方信息监控] 深蓝色探针已检测到，等待探针由深蓝变灰（变灰后开始OCR）...", "INFO")
        
        # 加载探针模型
        if not hasattr(self, '_unified_framework') or not self._unified_framework:
            from core.unified_battle_framework import UnifiedBattleFramework
            from config import TEMPLATES_PATH
            self._unified_framework = UnifiedBattleFramework(self.bot, self.regions, TEMPLATES_PATH)
        
        probe_model = self._unified_framework._load_probe_templates()
        probe_turned_gray = False
        probe_gray_time = None
        
        while not probe_turned_gray:
            if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                return None, None, "stopped"
            
            if (time.time() - t0) > timeout_s:
                self._emit(f"⏱️ [敌方信息监控] 等待探针变灰超时 ({timeout_s}s)", "WARN")
                return None, None, "timeout"
            
            if probe_model:
                state, blue_score, gray_score = self._unified_framework._detect_round_probe(probe_model)
                if state == "GRAY":
                    probe_turned_gray = True
                    probe_gray_time = time.time()
                    self._emit(f"✅ [敌方信息监控] 检测到探针变灰 (blue_score={blue_score:.3f}, gray_score={gray_score:.3f})", "SUCCESS")
                    break
            
            time.sleep(0.1)
        
        # 4. 探针已变灰，现在开始持续OCR扫描直到PetItem（如果检测到符合条件则提前停止）
        self._emit("📟 [敌方信息监控] 探针已变灰，开始持续OCR扫描（直到PetItem或检测到符合条件）...", "INFO")
        
        # ✅ 检查OCR依赖
        if not pytesseract:
            self._emit("⚠️ [敌方信息监控] pytesseract不可用，跳过OCR扫描", "WARN")
            level = None
            hp = None
            last_level = None
            last_hp = None
        else:
            # ✅ 检查region是否存在
            level_key = "对战信息.敌方精灵等级"
            hp_key = "对战信息.敌方精灵血量"
            level_region = self.regions.get(level_key)
            hp_region = self.regions.get(hp_key)
            
            if not level_region or not hp_region:
                self._emit(f"⚠️ [敌方信息监控] 区域缺失，跳过OCR扫描 (等级区域: {level_region is not None}, 血量区域: {hp_region is not None})", "WARN")
                level = None
                hp = None
                last_level = None
                last_hp = None
            else:
                level = None
                hp = None
                last_level = None
                last_hp = None
                ocr_scan_count = 0
                condition_met = False  # 是否检测到符合条件
                saved_ocr_images = False
                battle_num = self._battle_count + 1
                
                petitem_token = "/resource/item/petItem/icon/"
                petitem_cursor = kernel_cursor()
                petitem_detected = False
                
                # 持续OCR扫描直到PetItem或检测到符合条件
                while not petitem_detected and not condition_met:
                    if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                        self._last_enemy_level = last_level
                        self._last_enemy_hp = last_hp
                        return level, hp, "stopped"
                    
                    if (time.time() - t0) > timeout_s:
                        self._last_enemy_level = last_level
                        self._last_enemy_hp = last_hp
                        if level is None and hp is None:
                            self._emit(f"⏱️ [敌方信息监控] OCR扫描超时且未识别到任何信息 ({timeout_s}s)", "WARN")
                            return None, None, "timeout"
                        else:
                            self._emit(f"⏱️ [敌方信息监控] OCR扫描超时，但已识别到部分信息", "WARN")
                            return level, hp, "timeout"
                    
                    # 检查PetItem
                    try:
                        lines = fetch_kernel_since(petitem_cursor)
                        if isinstance(lines, list):
                            for line in lines:
                                if petitem_token in str(line):
                                    petitem_detected = True
                                    self._emit("✅ [敌方信息监控] 检测到PetItem信号，停止OCR扫描", "SUCCESS")
                                    break
                        petitem_cursor = kernel_cursor()
                    except Exception:
                        pass
                    
                    # 如果还没检测到PetItem，继续OCR扫描
                    if not petitem_detected:
                        ocr_scan_count += 1
                        self._emit(f"🔍 [敌方信息监控] 执行第 {ocr_scan_count} 次OCR扫描...", "DEBUG")
                        scan_level, scan_hp, level_img, hp_img = self._ocr_enemy_info_fast(use_foreground)
                        
                        if not saved_ocr_images and scan_level is not None and scan_hp is not None:
                            self._save_enemy_ocr_images(scan_level, scan_hp, level_img, hp_img, battle_num)
                            saved_ocr_images = True
                        
                        # 更新最后一次成功测得的数据
                        if scan_level is not None:
                            level = scan_level
                            last_level = scan_level
                            # ✅ 立即保存到实例变量，避免竞态条件
                            self._last_enemy_level = last_level
                            self._emit(f"✅ [敌方信息监控] 等级OCR成功: {level}", "INFO")
                        
                        if scan_hp is not None:
                            # ✅ 验证血量是否为两位数（强制只接受两位数）
                            if 10 <= scan_hp <= 99:
                                hp = scan_hp
                                last_hp = scan_hp
                                # ✅ 立即保存到实例变量，避免竞态条件
                                self._last_enemy_hp = last_hp
                                self._emit(f"✅ [敌方信息监控] 血量OCR成功: {hp} (两位数验证通过)", "INFO")
                            else:
                                self._emit(f"⚠️ [敌方信息监控] 血量OCR结果不是两位数: {scan_hp}，忽略", "WARN")
                                # 不更新hp和last_hp，保持之前的值
                        
                        # 如果检测到符合条件，提前停止扫描
                        if check_condition_callback and last_level is not None and last_hp is not None:
                            if check_condition_callback(last_level, last_hp):
                                condition_met = True
                                self._emit(f"✅ [敌方信息监控] 检测到符合条件（等级={last_level}, 血量={last_hp}），提前停止OCR扫描", "SUCCESS")
                                # ✅ 确保数据已保存（虽然上面已经保存了，但这里再次确认）
                                self._last_enemy_level = last_level
                                self._last_enemy_hp = last_hp
                                break
                        
                        if last_level is not None or last_hp is not None:
                            self._emit(f"📊 [敌方信息监控] 当前状态 - 等级: {last_level}, 血量: {last_hp} (扫描次数: {ocr_scan_count})", "INFO")
                        
                        time.sleep(0.3)  # OCR扫描间隔
        
        # 保存最终结果（如果还没保存，作为兜底）
        if self._last_enemy_level is None:
            self._last_enemy_level = last_level if last_level is not None else level
        if self._last_enemy_hp is None:
            self._last_enemy_hp = last_hp if last_hp is not None else hp
        
        # 如果还没检测到PetItem，继续等待
        if not petitem_detected:
            self._emit("⏳ [敌方信息监控] OCR扫描完成，继续等待PetItem信号...", "INFO")
            while True:
                if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                    return self._last_enemy_level, self._last_enemy_hp, "stopped"
                
                if (time.time() - t0) > timeout_s:
                    self._emit(f"⏱️ [敌方信息监控] 等待PetItem信号超时 ({timeout_s}s)", "WARN")
                    return self._last_enemy_level, self._last_enemy_hp, "timeout"
                
                # 检查PetItem
                try:
                    lines = fetch_kernel_since(petitem_cursor)
                    if isinstance(lines, list):
                        for line in lines:
                            if petitem_token in str(line):
                                self._emit("✅ [敌方信息监控] 检测到PetItem信号", "SUCCESS")
                                if self._last_enemy_level is not None or self._last_enemy_hp is not None:
                                    self._emit(f"📊 [敌方信息监控] 最终结果 - 等级: {self._last_enemy_level}, 血量: {self._last_enemy_hp}", "INFO")
                                return self._last_enemy_level, self._last_enemy_hp, "success"
                    petitem_cursor = kernel_cursor()
                except Exception:
                    pass
                
                time.sleep(0.1)  # 检查间隔
        else:
            # 已经检测到PetItem
            if self._last_enemy_level is not None or self._last_enemy_hp is not None:
                self._emit(f"📊 [敌方信息监控] 最终结果 - 等级: {self._last_enemy_level}, 血量: {self._last_enemy_hp}", "INFO")
            return self._last_enemy_level, self._last_enemy_hp, "success"

    def _check_dugulu_escape_condition(
        self,
        use_foreground: bool,
        stop_event: threading.Event
    ) -> Tuple[bool, bool]:
        """
        检查嘟咕噜模式是否需要逃跑（使用和双塔完全一样的结构）
        
        策略：
        1. 首先检查等级和血量是否在合理范围内（15级43-47，或16级45-50）
           - 如果不在合理范围内，说明OCR出问题了，继续捕捉但提醒失败
        2. 如果在合理范围内，再检查是否需要逃跑：
           - 只有两种情况不逃跑（继续捕捉）：15级47血量，或16级50血量
           - 其他情况：逃跑
        
        Args:
            use_foreground: 是否前台运行
            stop_event: 停止事件
        
        Returns:
            (should_escape, ocr_failed): (是否需要逃跑, OCR识别是否失败)
        """
        # 获取最后一次测得的等级和血量
        level = self._last_enemy_level
        hp = self._last_enemy_hp
        
        if level is None or hp is None:
            self._emit(f"⚠️ [嘟咕噜判断] 未获取到等级或血量数据（等级={level}, 血量={hp}），跳过判断", "WARN")
            return False, False
        
        # ✅ 首先检查是否在合理的血量组合范围内
        is_valid_combination = False
        if (level == 15 and hp in [43, 44, 45, 46, 47]) or \
           (level == 16 and hp in [45, 46, 47, 48, 49, 50]):
            is_valid_combination = True
        else:
            # 不在合理范围内，说明OCR出问题了
            self._emit(f"⚠️ [嘟咕噜判断] 检测到不合理的血量组合（等级={level}，血量={hp}），不在15级43-47或16级45-50范围内，OCR识别可能失败", "WARN")
            return False, True  # 继续捕捉但提醒失败
        
        # ✅ 在合理范围内，检查是否需要逃跑
        # 只有两种情况不逃跑（继续捕捉）：15级47血量，或16级50血量
        if (level == 15 and hp == 47) or (level == 16 and hp == 50):
            self._emit(f"✅ [嘟咕噜判断] 检测到符合捕捉条件（等级={level}，血量={hp}），继续捕捉", "SUCCESS")
            return False, False
        else:
            # 其他情况：逃跑
            self._emit(f"🏃 [嘟咕噜逃跑判断] 检测到等级={level}，血量={hp}，不满足捕捉条件，执行逃跑策略", "SUCCESS")
            return True, False

    def _check_shuangta_escape_condition(
        self,
        pet_ids: Optional[set],
        use_foreground: bool,
        stop_event: threading.Event
    ) -> bool:
        """
        检查双塔模式是否需要逃跑
        
        策略：
        1. 首先检查等级和血量是否在合理范围内（11级32-36，或12级34-39）
           - 如果不在合理范围内，说明OCR出问题了，继续捕捉但提醒失败
        2. 如果在合理范围内，再检查是否需要逃跑：
           - 卡塔（143）：11级32/33，或12级34/35 → 逃跑
           - 奇塔（102）：11级32/33/34，或12级34/35/36 → 逃跑
           - 其他情况：继续捕捉
        
        Args:
            pet_ids: 战斗中的pet IDs
            use_foreground: 是否前台运行
            stop_event: 停止事件
        
        Returns:
            True: 需要逃跑
            False: 不需要逃跑，正常捕捉
        """
        if not pet_ids:
            self._emit("⚠️ [双塔逃跑判断] 未获取到pet IDs，跳过逃跑判断", "WARN")
            return False
        
        # 获取最后一次测得的等级和血量
        level = self._last_enemy_level
        hp = self._last_enemy_hp
        
        if level is None or hp is None:
            self._emit(f"⚠️ [双塔逃跑判断] 未获取到等级或血量数据（等级={level}, 血量={hp}），跳过逃跑判断", "WARN")
            return False
        
        # ✅ 首先检查是否在合理的血量组合范围内
        is_valid_combination = False
        if (level == 11 and hp in [32, 33, 34, 35, 36]) or \
           (level == 12 and hp in [34, 35, 36, 37, 38, 39]):
            is_valid_combination = True
        else:
            # 不在合理范围内，说明OCR出问题了
            self._emit(f"⚠️ [双塔判断] 检测到不合理的血量组合（等级={level}，血量={hp}），不在11级32-36或12级34-39范围内，OCR识别可能失败", "WARN")
            return False  # 继续捕捉但提醒失败（通过_dugulu_ocr_failed类似的机制，但双塔模式目前没有这个标志）
        
        # ✅ 在合理范围内，检查是否需要逃跑
        # 检查是否有143（卡塔）
        if 143 in pet_ids:
            # 逃跑条件：11级32/33，或12级34/35
            if (level == 11 and hp in [32, 33]) or (level == 12 and hp in [34, 35]):
                self._emit(f"✅ [双塔逃跑判断] 检测到143卡塔，等级={level}，血量={hp}，满足逃跑条件，执行逃跑策略", "SUCCESS")
                return True
            else:
                self._emit(f"✅ [双塔判断] 检测到143卡塔，等级={level}，血量={hp}，不满足逃跑条件，继续捕捉", "SUCCESS")
        
        # 检查是否有102（奇塔）
        if 102 in pet_ids:
            # 逃跑条件：11级32/33/34，或12级34/35/36
            if (level == 11 and hp in [32, 33, 34]) or (level == 12 and hp in [34, 35, 36]):
                self._emit(f"✅ [双塔逃跑判断] 检测到102奇塔，等级={level}，血量={hp}，满足逃跑条件，执行逃跑策略", "SUCCESS")
                return True
            else:
                self._emit(f"✅ [双塔判断] 检测到102奇塔，等级={level}，血量={hp}，不满足逃跑条件，继续捕捉", "SUCCESS")
        
        # 如果既不是卡塔也不是奇塔，或者不满足逃跑条件，继续捕捉
        return False
    
    def _identify_target_pet_by_color(self, use_foreground: bool) -> Optional[str]:
        """
        通过颜色检测识别要放回仓库的精灵位置（精灵二、三、四）
        
        检测规则：
        - 颜色 #184992（蓝色，RGB=24, 73, 146）= 要放回仓库的精灵
        - 颜色 #E50000（红色，RGB=229, 0, 0）= 要留在背包里的精灵
        - 如果不严格匹配，比较哪个更接近（红vs蓝）
        
        Returns:
            "二", "三", "四" 或 None（如果识别失败）
        """
        # 定义目标颜色（RGB）
        COLOR_BLUE_STORAGE = (24, 73, 146)  # #184992 - 要放回仓库的精灵（蓝色血条）
        COLOR_RED_KEEP = (229, 0, 0)  # #E50000 - 要留在背包里的精灵（红色血条）
        COLOR_TOLERANCE = 5  # 颜色容差（颜色很纯，降低容差以提高精确度）
        
        self._emit("🔍 开始颜色检测识别要放回仓库的精灵（检测血条颜色）", "INFO")
        
        # 尝试检测精灵四、三、二的血条颜色（优先检测四号，然后三号，最后二号）
        for pos in ["四", "三", "二"]:
            try:
                hp_bar_key = f"精灵背包.血条{pos}"
                hp_bar_reg = self.regions.require(hp_bar_key)
                
                # 获取血条区域的平均RGB
                if self._unified_framework:
                    rgb = self._unified_framework._mean_rgb(hp_bar_key)
                else:
                    # 如果没有统一框架，使用直接方法
                    from core.utils import window_manager
                    x1, y1, x2, y2 = hp_bar_reg.outer_bbox()
                    img = window_manager.grab_game_bbox(x1, y1, x2, y2)
                    if img is None:
                        continue
                    import numpy as np
                    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
                    if arr.size == 0:
                        continue
                    mean = np.round(arr.mean(axis=(0, 1))).astype(int)
                    rgb = (int(mean[0]), int(mean[1]), int(mean[2]))
                
                if rgb is None:
                    self._emit(f"⚠️ [颜色检测] 精灵{pos}血条无法获取颜色", "WARN")
                    continue
                
                r, g, b = rgb
                self._emit(f"📋 [颜色检测] 精灵{pos}血条颜色：RGB({r}, {g}, {b})", "DEBUG")
                
                # 计算与蓝色和红色的距离
                def color_distance(c1, c2):
                    """计算两个RGB颜色的欧几里得距离"""
                    return ((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2 + (c1[2] - c2[2]) ** 2) ** 0.5
                
                dist_to_blue = color_distance(rgb, COLOR_BLUE_STORAGE)
                dist_to_red = color_distance(rgb, COLOR_RED_KEEP)
                
                # 由于颜色很纯（要么是 E50000 要么是 184992），直接比较距离即可
                # 优先检查是否在容差范围内（提供额外的验证），但主要依赖距离比较
                if dist_to_blue <= COLOR_TOLERANCE:
                    self._emit(f"✅ [颜色检测] 精灵{pos}血条为蓝色（距离={dist_to_blue:.1f}，要放回仓库）", "SUCCESS")
                    return pos
                elif dist_to_red <= COLOR_TOLERANCE:
                    self._emit(f"🔴 [颜色检测] 精灵{pos}血条为红色（距离={dist_to_red:.1f}，留在背包）", "INFO")
                    continue  # 红色表示要留在背包，继续检查下一个
                else:
                    # 颜色很纯时，距离会比较明显，直接比较哪个更接近
                    if dist_to_blue < dist_to_red:
                        self._emit(f"✅ [颜色检测] 精灵{pos}血条更接近蓝色（距离蓝={dist_to_blue:.1f}，距离红={dist_to_red:.1f}），判定为要放回仓库", "SUCCESS")
                        return pos
                    else:
                        self._emit(f"🔴 [颜色检测] 精灵{pos}血条更接近红色（距离蓝={dist_to_blue:.1f}，距离红={dist_to_red:.1f}），判定为留在背包", "INFO")
                        continue  # 更接近红色，继续检查下一个
                        
            except KeyError:
                self._emit(f"⚠️ [颜色检测] 找不到区域：{hp_bar_key}", "WARN")
                continue
            except Exception as e:
                self._emit(f"⚠️ [颜色检测] 检测精灵{pos}血条颜色时发生异常：{e}", "WARN")
                continue
        
        self._emit("❌ [颜色检测] 未找到要放回仓库的精灵（未检测到蓝色血条）", "WARN")
        return None
    
    def _ocr_identify_target_pet(self, use_foreground: bool, profile: WildCaptureProfile) -> Optional[str]:
        """
        [已禁用] 通过OCR识别目标精灵的位置（精灵二、三、四）
        
        注意：此方法已被 _identify_target_pet_by_color 替代，现在使用颜色检测而非OCR。
        此方法保留但不使用，未来将被删除。
        
        Returns:
            "二", "三", "四" 或 None（如果识别失败）
        """
        # OCR功能已禁用，不再使用
        self._emit("⚠️ OCR识别已禁用，应使用颜色检测方法", "WARN")
        return None
        
        # 以下代码保留但不执行（用于未来删除，注释掉避免语法错误）
        # if False:  # 永远不执行
        #     if not pytesseract:
        #         self._emit("⚠️ pytesseract不可用，无法OCR识别精灵名称", "WARN")
        #         return None
        
        # 获取目标精灵名称列表
        target_pet_ids = profile.target_pet_ids if profile.target_pet_ids else [profile.target_pet_id]
        target_names = []
        for pet_id in target_pet_ids:
            if pet_id in PET_ID_TO_NAME:
                target_names.append(PET_ID_TO_NAME[pet_id])
        
        if not target_names:
            self._emit(f"⚠️ 无法找到目标精灵ID {target_pet_ids} 对应的名称", "WARN")
            return None
        
        self._emit(f"🔍 开始OCR识别目标精灵（目标名称：{target_names}）", "INFO")
        
        # 尝试识别精灵四、三、二的名称（优先识别四号，然后三号，最后二号）
        for pos in ["四", "三", "二"]:
            try:
                name_key = f"精灵背包.名称{pos}"
                name_reg = self.regions.require(name_key)
                x1, y1, x2, y2 = name_reg.outer_bbox()
                
                # 截图
                img = window_manager.grab_game_bbox(x1, y1, x2, y2)
                if img is None:
                    continue
                
                # OCR识别
                try:
                    # 使用中文OCR
                    if pytesseract:
                        txt = pytesseract.image_to_string(img, lang="chi_sim")
                    else:
                        txt = ""
                    txt = txt.strip()
                    
                    self._emit(f"📋 [OCR调试] 精灵{pos}名称识别结果：{txt}", "DEBUG")
                    
                    # 检查是否包含目标名称
                    for target_name in target_names:
                        if target_name in txt:
                            self._emit(f"✅ OCR识别成功：精灵{pos}是目标精灵（{target_name}）", "SUCCESS")
                            return pos
                except Exception as e:
                    self._emit(f"⚠️ OCR识别精灵{pos}名称失败：{e}", "WARN")
                    continue
                    
            except KeyError:
                self._emit(f"⚠️ 找不到区域：精灵背包.名称{pos}", "WARN")
                continue
            except Exception as e:
                self._emit(f"⚠️ 识别精灵{pos}时发生异常：{e}", "WARN")
                continue
        
        self._emit("❌ OCR识别失败，未找到目标精灵", "WARN")
        return None
    
    def _collect_fight_pet_ids_immediate(self, stop_event: threading.Event, current_lines: Optional[List[str]] = None, start_index: int = 0) -> Optional[set]:
        """
        立即收集fightResource/pet/swf/的pet IDs（在检测到fightResource/pet/swf/信号时调用）
        
        从已存在的日志行中，从检测到的第一个pet/swf开始，向下扫描所有行，直到遇到skill信号。
        把所有不同的序号都列出来。
        
        Args:
            stop_event: 停止事件
            current_lines: 当前已获取的日志行列表（如果提供，将从这些行中搜索）
            start_index: 从current_lines的哪个索引开始搜索（检测到第一个pet/swf的行索引）
        
        Returns:
            收集到的pet IDs集合，如果失败返回None
        """
        pet_ids = []
        skill_token = "/resource/fightResource/skill/swf/"
        
        self._emit("📋 开始从已存在的日志中收集fightResource/pet/swf/的pet IDs（从第一个pet/swf开始向下扫所有行直到skill）...", "INFO")
        
        # 如果提供了current_lines，从这些行中搜索
        if current_lines is not None and isinstance(current_lines, list):
            # 从start_index开始，向下扫描所有行（直到列表末尾或遇到skill信号）
            max_lines = len(current_lines)
            start_collecting_index = -1  # 找到第一个pet/swf的行索引
            
            # 第一步：从start_index开始，找到第一个包含pet/swf的行（不要求是166）
            for i in range(start_index, max_lines):
                line_str = str(current_lines[i])
                matches = list(self._FIGHT_PET_SWF_RE.finditer(line_str))
                if matches:
                    # 找到第一个pet/swf，从这一行开始收集
                    start_collecting_index = i
                    self._emit(f"📋 找到第一个pet/swf在第{i-start_index+1}行（索引{i}），开始收集", "INFO")
                    break
            
            # 第二步：从第一个pet/swf所在的行开始，收集所有pet IDs，直到遇到skill
            if start_collecting_index >= 0:
                for i in range(start_collecting_index, max_lines):
                    line_str = str(current_lines[i])
                    
                    # 检查是否出现skill（停止收集）
                    if skill_token in line_str:
                        self._emit("✅ 检测到skill信号，停止收集pet IDs", "INFO")
                        break
                    
                    # 检查这一行是否包含pet/swf（一行可能包含多个pet ID）
                    matches = list(self._FIGHT_PET_SWF_RE.finditer(line_str))
                    if matches:
                        for m in matches:
                            try:
                                pid_str = m.group(1)
                                pid = int(pid_str)
                                # 收集所有不同的pet ID（去重）
                                if pid not in pet_ids:
                                    pet_ids.append(pid)  # 按顺序添加到列表
                                    self._emit(f"📋 检测到pet ID: {pid}（原始: {pid_str}，已收集{len(pet_ids)}个）", "INFO")
                            except Exception as e:
                                self._emit(f"⚠️ 解析pet ID失败: {e} (原始字符串: {pid_str if 'pid_str' in locals() else 'N/A'})", "WARN")
                                continue
            else:
                self._emit("⚠️ 未找到任何pet/swf，无法开始收集pet IDs", "WARN")
        
        # 转换为set并去重（但保持顺序信息用于日志）
        unique_pet_ids = list(dict.fromkeys(pet_ids))  # 保持顺序的去重
        pet_ids_set = set(unique_pet_ids)
        
        if pet_ids_set:
            self._emit(f"✅ 收集完成，共收集到{len(unique_pet_ids)}个唯一pet IDs: {unique_pet_ids}", "INFO")
            return pet_ids_set
        else:
            self._emit(f"⚠️ 未收集到任何pet ID", "WARN")
            return None

    def _collect_fight_pet_ids(self, timeout: float, collect_window: float, stop_event: threading.Event) -> set:
        """
        收集战斗中的pet IDs（从kernel日志中提取/resource/fightResource/pet/swf/X.swf）
        
        新逻辑：在触发fightresources之后，寻找这两条日志后面的所有 fightResource/pet/swf/：
        - 提供文件: /resource/pet/swf/102.swf
        - 提供文件: /resource/pet/sound/102.mp3
        后面直到skill出现以前，应该有4/5/8/10个pet IDs
        
        Args:
            timeout: 总超时时间（秒）
            collect_window: 从第一次检测到pet ID开始，继续收集的时间窗口（秒）
            stop_event: 停止事件
            
        Returns:
            收集到的pet IDs集合
        """
        cursor = kernel_cursor()
        t0 = time.time()
        first_seen_at: Optional[float] = None
        pet_ids = set()
        last_log_time = 0.0
        found_fightresources_trigger = False  # 是否找到了/resource/pet/swf/或/resource/pet/sound/
        skill_token = "/resource/fightResource/skill/swf/"

        while time.time() - t0 < timeout:
            if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                break
            self._wait_if_paused(stop_event)

            cursor, lines = self._fetch_kernel(cursor)
            found_new = False
            
            for ln in lines:
                line_str = str(ln)
                
                # 检查是否出现了/resource/pet/swf/或/resource/pet/sound/（fightresources触发）
                if not found_fightresources_trigger:
                    if self._PET_SWF_RE.search(line_str) or self._PET_SOUND_RE.search(line_str):
                        found_fightresources_trigger = True
                        self._emit("✅ 检测到fightresources触发（/resource/pet/swf/或/resource/pet/sound/），开始收集fightResource/pet/swf/", "INFO")
                
                # 如果已经找到fightresources触发，开始收集fightResource/pet/swf/
                if found_fightresources_trigger:
                    # 检查是否出现skill（停止收集）
                    if skill_token in line_str:
                        self._emit("✅ 检测到skill信号，停止收集pet IDs", "INFO")
                        break
                    
                    # 收集fightResource/pet/swf/
                    for m in self._FIGHT_PET_SWF_RE.finditer(line_str):
                        try:
                            pid = int(m.group(1))
                            if pid not in pet_ids:
                                pet_ids.add(pid)
                                found_new = True
                            if first_seen_at is None:
                                first_seen_at = time.time()
                                    # 输出新发现的pet ID（节流：每0.5秒最多输出一次）
                                now = time.time()
                                if now - last_log_time >= 0.5:
                                    self._emit(f"📋 收集到pet ID: {pid}（已收集：{sorted(pet_ids)}）", "DEBUG")
                                    last_log_time = now
                        except Exception as e:
                            self._emit(f"⚠️ 解析pet ID失败: {e}", "WARN")
                            continue

            if found_fightresources_trigger and first_seen_at is not None and (time.time() - first_seen_at) >= collect_window:
                self._emit(f"✅ 收集窗口结束（{collect_window}s），已收集pet IDs: {sorted(pet_ids)}", "DEBUG")
                break

            self._sleep_abortable(stop_event, 0.03, tick=0.03)

        if not pet_ids:
            if found_fightresources_trigger:
                self._emit(f"⚠️ 超时（{timeout}s）或未检测到任何pet ID（已找到fightresources触发）", "WARN")
            else:
                self._emit(f"⚠️ 超时（{timeout}s）或未检测到fightresources触发", "WARN")
        return pet_ids

    def _clear_dialogs_by_probes(
        self,
        use_foreground: bool,
        max_clicks: int = 20,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        reg_white = self.regions.require(self.KEY_PROBE_WHITE)
        reg_blue = self.regions.require(self.KEY_PROBE_BLUE)
        reg_confirm = self.regions.require(self.KEY_NORMAL_CONFIRM)

        for _ in range(max_clicks):
            if stop_event is not None and stop_event.is_set():
                return
            if getattr(self.bot, "stop_current", False):
                return

            w_ok = self._probe_match(reg_white, (255, 255, 255), tol=18, min_ratio=0.6)
            b_ok = self._probe_match(reg_blue, (47, 167, 238), tol=22, min_ratio=0.5)

            if not (w_ok and b_ok):
                break

            self._click_region(reg_confirm, use_foreground)
            self._sleep_abortable(stop_event, 0.20, tick=0.05)

    def _probe_match(self, reg: Region, target_rgb: Tuple[int, int, int], tol: int, min_ratio: float) -> bool:
        gx1, gy1, gx2, gy2 = reg.outer_bbox()
        img = window_manager.grab_game_bbox(gx1, gy1, gx2, gy2)
        pixels = list(img.getdata())
        if not pixels:
            return False

        tr, tg, tb = target_rgb
        ok = 0
        for r, g, b in pixels:
            if abs(r - tr) <= tol and abs(g - tg) <= tol and abs(b - tb) <= tol:
                ok += 1
        return (ok / len(pixels)) >= min_ratio
    
    def _check_1and1_probes(self) -> bool:
        """
        检测1AND1探针（通用探针白色 + 普通确认探针蓝色）
        
        Returns:
            True: 检测到1AND1（白色和蓝色探针同时出现）
            False: 未检测到1AND1
        """
        try:
            reg_white = self.regions.require(self.KEY_PROBE_WHITE)
            reg_blue = self.regions.require(self.KEY_PROBE_BLUE)
            
            w_ok = self._probe_match(reg_white, (255, 255, 255), tol=18, min_ratio=0.6)
            b_ok = self._probe_match(reg_blue, (47, 167, 238), tol=22, min_ratio=0.5)
            
            return w_ok and b_ok
        except Exception:
            return False
    
    def _get_to_script_name(self, profile: WildCaptureProfile) -> Optional[str]:
        """
        根据profile获取对应的to脚本名称
        
        Returns:
            "to螳螂", "to嘟咕噜", "to双塔", "to小豆芽", "to尼奥" 或 None
        """
        # 根据profile.name或route_hint判断
        name_lower = profile.name.lower()
        route_hint = profile.route_hint
        
        if "螳螂" in name_lower or route_hint == "达尔":
            return "to螳螂"
        elif "嘟咕噜" in name_lower or route_hint == "嘟咕噜":
            return "to嘟咕噜"
        elif "双塔" in name_lower or route_hint == "双塔":
            return "to双塔"
        elif "小豆芽" in name_lower:
            return "to小豆芽"
        elif "尼奥" in name_lower:
            return "to尼奥"
        else:
            return None
    
    def _check_login_swf_non_blocking(self, start_cursor, start_time: float, timeout_s: float = 5.0) -> Tuple[bool, Optional[int]]:
        """
        非阻塞检查是否出现/login/Login.swf信号（单次检查，不阻塞）
        
        Args:
            start_cursor: 开始检查时的cursor
            start_time: 开始检查时的时间戳
            timeout_s: 超时时间（秒）
            
        Returns:
            (是否检测到, 新的cursor) - 如果检测到返回(True, cursor)，如果超时返回(False, None)，如果未超时返回(False, new_cursor)
        """
        from core.logger import fetch_kernel_since, kernel_cursor
        
        # 检查是否超时
        if (time.time() - start_time) >= timeout_s:
            return False, None
        
        try:
            new_cursor, lines = self._fetch_kernel(start_cursor)
            if isinstance(lines, list):
                for line in lines:
                    line_str = str(line)
                    # ✅ 检查是否包含/login/Login.swf（支持带参数的情况，如 /login/Login.swf?g4fphljs）
                    if self.TOKEN_LOGIN_SWF in line_str:
                        self._emit(f"⚠️ 检测到/login/Login.swf信号（断线重连）", "WARN")
                        return True, new_cursor
            return False, new_cursor
        except Exception as e:
            self._emit(f"⚠️ [login检测] 检查异常: {e}", "WARN")
            return False, start_cursor
    
    def _start_normal_1and1_monitoring(
        self, 
        profile: WildCaptureProfile, 
        use_foreground: bool, 
        stop_event: threading.Event
    ) -> None:
        """
        启动常态1AND1监控（在后台线程中运行）
        
        功能：
        1. 持续检测1AND1探针（仅在稳态扫描阶段，不在恢复过程中）
        2. 检测到后点击一次普通确认
        3. 然后非阻塞地检查5s内是否出现/login/Login.swf
        4. 如果出现，等待0.5s后终止当前任务并执行登录+to脚本
        5. 如果没出现，继续监控
        6. 1AND1出现时的颜色变化不作数（直到有对应的mp3播放才停止常态1AND1）
        7. 直到战斗结束稳态开始（在战斗期间不监控）
        """
        def monitor_loop():
            self._emit("🔍 启动常态1AND1监控（后台线程）", "INFO")
            last_check_time = 0.0
            check_interval = 0.5  # 每0.5秒检查一次（不高频，可能半小时到两小时触发一次）
            paused_logged = False
            last_debug_skip_log = 0.0
            
            # 用于非阻塞检查/login/Login.swf的状态
            login_swf_check_active = False
            login_swf_check_start_cursor = None
            login_swf_check_start_time = None
            
            while not stop_event.is_set() and not getattr(self.bot, "stop_current", False):
                # ✅ 关键：如果停止监控标志被设置，退出监控
                if getattr(self, "_stop_1and1_monitoring", False):
                    if not paused_logged:
                        self._emit("⏸️ [常态1AND1] 暂停监控（等待恢复）", "INFO")
                        paused_logged = True
                    time.sleep(1.0)
                    continue
                elif paused_logged:
                    self._emit("▶️ [常态1AND1] 监控已恢复", "INFO")
                    paused_logged = False
                
                # ✅ 关键：如果不在稳态扫描阶段（在战斗中），暂停监控
                if not self._is_scanning_steady_state:
                    if time.time() - last_debug_skip_log >= 10.0:
                        self._emit("🐞 [常态1AND1] 跳过检测：未处于稳态扫描阶段", "DEBUG")
                        last_debug_skip_log = time.time()
                    time.sleep(1.0)
                    continue
                
                # ✅ 关键：如果正在恢复中，完全跳过检测（不进行任何1AND1检查）
                if getattr(self, "_is_recovering", False):
                    if time.time() - last_debug_skip_log >= 10.0:
                        self._emit("🐞 [常态1AND1] 跳过检测：恢复流程中", "DEBUG")
                        last_debug_skip_log = time.time()
                    time.sleep(0.5)
                    continue
                
                now = time.time()
                
                # 如果正在进行/login/Login.swf检查，非阻塞地检查一次
                if login_swf_check_active and login_swf_check_start_cursor is not None and login_swf_check_start_time is not None:
                    detected, new_cursor = self._check_login_swf_non_blocking(
                        login_swf_check_start_cursor, 
                        login_swf_check_start_time, 
                        timeout_s=5.0
                    )
                    
                    if detected:
                        # 检测到/login/Login.swf，等待0.5s后终止当前任务并执行重连脚本
                        self._emit("⚠️ [常态1AND1] 检测到/login/Login.swf，等待0.5s后终止当前任务并执行重连脚本", "WARN")
                        
                        # 等待0.5s
                        time.sleep(0.5)
                        
                        # 设置停止标志（彻底退出当前任务）
                        if self.bot:
                            self.bot.stop_current = True
                        
                        # 等待一小段时间确保任务停止
                        time.sleep(1.0)
                        
                        # 执行重连脚本（脚本执行完成后会根据profile判断是否需要重启）
                        profile_name_lower = profile.name.lower()
                        if "双塔" in profile_name_lower:
                            # 双塔模式：使用带重试的版本（不限次数）
                            self._execute_reconnect_scripts_for_shuangta(profile, use_foreground, stop_event, retry_count=0, max_retries=None)
                        elif "嘟咕噜" in profile_name_lower:
                            # 嘟咕噜模式：使用带重试的版本（不限次数）
                            self._execute_reconnect_scripts_for_dugulu(profile, use_foreground, stop_event, retry_count=0, max_retries=None)
                        else:
                            # 其他模式：使用普通版本
                            self._execute_reconnect_scripts(profile, use_foreground, stop_event)
                        
                        # 重连脚本执行完成后，如果支持重启，会在脚本内部设置重启标志
                        return  # 退出监控线程
                    elif new_cursor is None:
                        # 超时，未检测到/login/Login.swf
                        self._emit("✅ [常态1AND1] 5秒内未检测到/login/Login.swf，继续监控", "INFO")
                        login_swf_check_active = False
                        login_swf_check_start_cursor = None
                        login_swf_check_start_time = None
                    else:
                        # 更新cursor，继续检查
                        login_swf_check_start_cursor = new_cursor
                
                # 只在没有进行/login/Login.swf检查时才检查1AND1
                if not login_swf_check_active and (now - last_check_time >= check_interval):
                    last_check_time = now
                    
                    # 检测1AND1
                    if self._check_1and1_probes():
                        self._emit("🔔 [常态1AND1] 检测到1AND1探针，点击普通确认（后台点击）", "INFO")
                        
                        try:
                            # 点击一次普通确认（使用后台点击，不干扰前台操作）
                            self._click_region(self.KEY_NORMAL_CONFIRM, use_foreground=False)
                            time.sleep(0.2)  # 等待点击生效
                            
                            # ✅ 新增：1AND1常态化确认后，检测电池状态
                            if self._unified_framework:
                                battery_status = self._unified_framework._detect_battery_status(use_foreground=False)
                                if battery_status is not None:
                                    self._emit(f"🔋 [常态1AND1检测] 电池状态: {battery_status}", "INFO")
                                    # 更新入站前的电池状态（作为最新的基准）
                                    self._battery_status_before_battle = battery_status
                            
                            # ✅ 等待一小段时间，确保login信号（如果会出现）已经出现
                            time.sleep(0.5)
                            
                            # 开始非阻塞检查/login/Login.swf（不阻塞，在下一次循环中检查）
                            # 注意：在点击确认后等待0.5s再获取cursor，确保能检测到之后出现的login信号
                            from core.logger import kernel_cursor
                            login_swf_check_start_cursor = kernel_cursor()
                            login_swf_check_start_time = time.time()
                            login_swf_check_active = True
                            self._emit("🔍 [常态1AND1] 开始检查/login/Login.swf信号（5秒超时）", "INFO")
                            
                        except Exception as e:
                            self._emit(f"⚠️ [常态1AND1] 处理异常: {e}", "WARN")
                            import traceback
                            self._emit(f"📋 异常详情: {traceback.format_exc()}", "ERROR")
                
                time.sleep(0.5)  # 主循环休眠
        
        # 启动后台线程
        monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        monitor_thread.start()
        self._emit("✅ 常态1AND1监控线程已启动", "INFO")
    
    def _check_last_map_and_newnpc(self, map_id: int, timeout_s: float = 10.0) -> Tuple[Optional[int], bool]:
        """
        检测最后的map序号和newNPC信号
        
        逻辑：从已经出现的日志中，从下向上扫描（从最新到最旧）
        1. 找到第一个NewNPC信号
        2. 在这个NewNPC上面（更早的日志）找到第一个map序号
        3. 检查这个map序号是否是目标map_id
        
        Args:
            map_id: 要检测的map ID（例如315）
            timeout_s: 超时时间（秒，未使用，保留用于兼容）
        
        Returns:
            (检测到的map_id, 是否检测到newNPC) - 如果检测到map 315且上面有newNPC，返回(315, True)
        """
        try:
            from core.logger import fetch_kernel_since, kernel_cursor
            
            self._emit(f"🔍 [地图检测] 从已出现的日志中扫描：找到第一个NewNPC，然后在其上方找到第一个map序号（目标map={map_id}）", "INFO")
            
            # 等待一段时间，确保to脚本执行后的日志都被收集
            time.sleep(2.0)
            
            # 获取所有历史日志（从cursor=0开始）
            all_lines = fetch_kernel_since(0)
            if not isinstance(all_lines, list):
                all_lines = []
            
            if not all_lines:
                self._emit("⚠️ [地图检测] 未获取到任何日志", "WARN")
                return (None, False)
            
            # 从最新的日志开始，向前（向上）扫描，找到第一个NewNPC
            newnpc_line_idx = -1
            for i in range(len(all_lines) - 1, -1, -1):  # 从最新到最旧
                line_str = str(all_lines[i])
                if self.KEY_NEWNPC_MULTI in line_str:
                    newnpc_line_idx = i
                    self._emit(f"✅ [地图检测] 找到第一个NewNPC信号（行索引：{i}）", "SUCCESS")
                    break
            
            if newnpc_line_idx == -1:
                self._emit("⚠️ [地图检测] 未找到NewNPC信号", "WARN")
                return (None, False)
            
            # 从NewNPC行开始，继续向前（向上）扫描，找到第一个map信号
            found_map_id = None
            for i in range(newnpc_line_idx - 1, -1, -1):  # 从NewNPC的上一行开始，向前扫描
                line_str = str(all_lines[i])
                m = self._MAP_SWF_RE.search(line_str)
                if m:
                    try:
                        found_map_id = int(m.group(1))
                        self._emit(f"✅ [地图检测] 在NewNPC上方找到map ID: {found_map_id}（行索引：{i}）", "SUCCESS")
                        break
                    except Exception:
                        continue
            
            if found_map_id is None:
                self._emit("⚠️ [地图检测] 在NewNPC上方未找到任何map信号", "WARN")
                return (None, True)  # 找到了newNPC，但没找到map
            
            # 检查找到的map ID是否是目标map
            if found_map_id == map_id:
                self._emit(f"✅ [地图检测] 确认：NewNPC上方的map是目标map {map_id}", "SUCCESS")
                return (map_id, True)
            else:
                self._emit(f"⚠️ [地图检测] NewNPC上方的map是 {found_map_id}，不是目标map {map_id}", "WARN")
                return (found_map_id, True)  # 找到了newNPC和map，但map不是目标
                
        except Exception as e:
            self._emit(f"❌ [地图检测] 检测异常: {e}", "ERROR")
            import traceback
            self._emit(f"📋 异常详情: {traceback.format_exc()}", "ERROR")
            return (None, False)
    
    def _execute_refresh_flow_and_wait_login(self, profile: WildCaptureProfile, use_foreground: bool, stop_event: threading.Event, retry_count: int = 0, max_retries: Optional[int] = None) -> None:
        """
        执行刷新流程，然后等待login信号后重新执行脚本
        
        刷新流程：
        1. 点击client左上角原始坐标x y各自+5（在1200x700以外）
        2. 按向下箭头⬇
        3. 按enter
        4. 等待client执行重启加进入双塔的流程
        
        Args:
            profile: 当前捕捉配置
            use_foreground: 是否前台执行
            stop_event: 停止事件
        """
        try:
            self._emit("🔄 [刷新流程] 开始执行刷新流程", "INFO")
            
            # 1. 点击client左上角原始坐标x y各自+5（在1200x700以外）
            self._emit("🖱️ [刷新流程] 点击client左上角+5位置（屏幕坐标）", "INFO")
            if not window_manager.click_client_origin_offset(offset_x=5, offset_y=5):
                self._emit("⚠️ [刷新流程] 点击client左上角失败", "WARN")
                return
            
            time.sleep(0.5)  # 等待点击生效
            
            # 2. 按向下箭头⬇
            self._emit("⌨️ [刷新流程] 按下向下箭头键", "INFO")
            if use_foreground:
                import win32api
                import win32con
                win32api.keybd_event(win32con.VK_DOWN, 0, 0, 0)
                time.sleep(0.1)
                win32api.keybd_event(win32con.VK_DOWN, 0, win32con.KEYEVENTF_KEYUP, 0)
            else:
                window_manager.send_key_arrow_down()
            
            time.sleep(0.5)  # 等待按键生效
            
            # 3. 按enter
            self._emit("⌨️ [刷新流程] 按下Enter键", "INFO")
            if use_foreground:
                import win32api
                import win32con
                win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
                time.sleep(0.1)
                win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)
            else:
                window_manager.send_key_enter()
            
            self._emit("✅ [刷新流程] 刷新操作完成，等待client重启", "SUCCESS")
            time.sleep(2.0)  # 等待client重启
            
            # 4. 等待login信号
            self._emit("⏳ [刷新流程] 等待/login/Login.swf信号...", "INFO")
            from core.logger import fetch_kernel_since, kernel_cursor
            
            start_cursor = kernel_cursor()
            start_time = time.time()
            max_wait_time = 300.0  # 最多等待5分钟
            
            while (time.time() - start_time) < max_wait_time:
                if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                    self._emit("⛔ [刷新流程] 等待login信号时被停止", "WARN")
                    return
                
                # 检查日志
                lines = fetch_kernel_since(start_cursor)
                if isinstance(lines, list):
                    for line in lines:
                        if self.TOKEN_LOGIN_SWF in str(line):
                            self._emit("✅ [刷新流程] 检测到/login/Login.swf信号，重新执行脚本", "SUCCESS")
                            
                            # 根据profile类型选择正确的重连方法
                            profile_name_lower = profile.name.lower()
                            if max_retries is None or retry_count < max_retries:
                                max_retries_str = f"/{max_retries}" if max_retries is not None else ""
                                self._emit(f"🔄 [刷新流程] 重新尝试执行重连脚本（第 {retry_count + 1}{max_retries_str} 次）", "INFO")
                                if "双塔" in profile_name_lower:
                                    # 双塔模式：递归调用双塔重连方法（检测map 315）
                                    self._execute_reconnect_scripts_for_shuangta(profile, use_foreground, stop_event, retry_count + 1, max_retries)
                                elif "嘟咕噜" in profile_name_lower:
                                    # 嘟咕噜模式：递归调用嘟咕噜重连方法（检测map 323）
                                    self._execute_reconnect_scripts_for_dugulu(profile, use_foreground, stop_event, retry_count + 1, max_retries)
                                else:
                                    # 其他模式：使用通用重连方法（但这种情况不应该发生，因为只有双塔和嘟咕噜会调用这个方法）
                                    self._emit(f"⚠️ [刷新流程] 未知的profile类型：{profile.name}，使用通用重连方法", "WARN")
                                    self._execute_reconnect_scripts(profile, use_foreground, stop_event)
                            return
                
                start_cursor = kernel_cursor()
                time.sleep(0.5)
            
            self._emit("⚠️ [刷新流程] 等待login信号超时", "WARN")
            
        except Exception as e:
            self._emit(f"❌ [刷新流程] 执行异常: {e}", "ERROR")
            import traceback
            self._emit(f"📋 异常详情: {traceback.format_exc()}", "ERROR")
    
    def _execute_battery_refresh_reconnect(self, profile: WildCaptureProfile, use_foreground: bool, stop_event: threading.Event) -> None:
        """
        执行电池状态触发的刷新重连（参考双塔刷新逻辑）
        """
        try:
            self._emit("🔄 [电池刷新] 电池状态变化触发刷新重连", "WARN")
            
            # 执行刷新流程（参考 _execute_refresh_flow）
            self._emit("🔄 [电池刷新] 开始执行刷新流程", "INFO")
            
            # 1. 点击client左上角原始坐标x y各自+5
            self._emit("🖱️ [电池刷新] 点击client左上角+5位置（屏幕坐标）", "INFO")
            if not window_manager.click_client_origin_offset(offset_x=5, offset_y=5):
                self._emit("⚠️ [电池刷新] 点击client左上角失败", "WARN")
                return
            
            time.sleep(0.5)
            
            # 2. 按向下箭头⬇
            self._emit("⌨️ [电池刷新] 按下向下箭头键", "INFO")
            if use_foreground:
                import win32api
                import win32con
                win32api.keybd_event(win32con.VK_DOWN, 0, 0, 0)
                time.sleep(0.1)
                win32api.keybd_event(win32con.VK_DOWN, 0, win32con.KEYEVENTF_KEYUP, 0)
            else:
                window_manager.send_key_arrow_down()
            
            time.sleep(0.5)
            
            # 3. 按enter
            self._emit("⌨️ [电池刷新] 按下Enter键", "INFO")
            if use_foreground:
                import win32api
                import win32con
                win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
                time.sleep(0.1)
                win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)
            else:
                window_manager.send_key_enter()
            
            self._emit("✅ [电池刷新] 刷新操作完成，等待client重启", "SUCCESS")
            time.sleep(2.0)
            
            # 4. 等待login信号
            self._emit("⏳ [电池刷新] 等待/login/Login.swf信号...", "INFO")
            from core.logger import fetch_kernel_since, kernel_cursor
            
            start_cursor = kernel_cursor()
            start_time = time.time()
            max_wait_time = 300.0  # 最多等待5分钟
            
            while (time.time() - start_time) < max_wait_time:
                if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                    self._emit("⛔ [电池刷新] 等待login信号时被停止", "WARN")
                    return
                
                # 检查日志
                lines = fetch_kernel_since(start_cursor)
                if isinstance(lines, list):
                    for line in lines:
                        if "/login/Login.swf" in str(line):
                            self._emit("✅ [电池刷新] 检测到/login/Login.swf信号，重新执行脚本", "SUCCESS")
                            
                            # 根据profile类型选择正确的重连方法
                            profile_name_lower = profile.name.lower()
                            if "双塔" in profile_name_lower:
                                # 双塔模式：使用带循环重试的版本（不限次数）
                                self._execute_reconnect_scripts_for_shuangta(profile, use_foreground, stop_event, retry_count=0, max_retries=None)
                            elif "嘟咕噜" in profile_name_lower:
                                # 嘟咕噜模式：使用带循环重试的版本（不限次数）
                                self._execute_reconnect_scripts_for_dugulu(profile, use_foreground, stop_event, retry_count=0, max_retries=None)
                            else:
                                # 其他模式：使用通用版本
                                self._execute_reconnect_scripts(profile, use_foreground, stop_event)
                            return
                
                start_cursor = kernel_cursor()
                time.sleep(0.5)
            
            self._emit("⚠️ [电池刷新] 等待login信号超时", "WARN")
            
        except Exception as e:
            self._emit(f"❌ [电池刷新] 执行异常: {e}", "ERROR")
            import traceback
            self._emit(f"📋 异常详情: {traceback.format_exc()}", "ERROR")
    
    def _execute_reconnect_scripts_for_shuangta(self, profile: WildCaptureProfile, use_foreground: bool, stop_event: Optional[threading.Event] = None, retry_count: int = 0, max_retries: Optional[int] = None) -> None:
        """
        执行重连脚本（双塔模式专用），如果map不是315则循环刷新+重连直到成功
        
        Args:
            profile: 当前捕捉配置
            use_foreground: 是否前台执行
            stop_event: 停止事件
            retry_count: 当前重试次数
            max_retries: 最大重试次数（None 表示不限次数）
        """
        if stop_event and stop_event.is_set():
            self._emit("⛔ [重连脚本] 停止重试", "WARN")
            return
        
        if getattr(self.bot, "stop_current", False):
            self._emit("⛔ [重连脚本] stop_current被设置，停止重试", "WARN")
            return
        
        # 执行重连脚本的核心逻辑
        self._execute_reconnect_scripts_core(profile, use_foreground, stop_event, retry_count, max_retries)
    
    def _execute_reconnect_scripts_for_dugulu(self, profile: WildCaptureProfile, use_foreground: bool, stop_event: Optional[threading.Event] = None, retry_count: int = 0, max_retries: Optional[int] = None) -> None:
        """
        执行重连脚本（嘟咕噜模式专用），如果map不是323则循环刷新+重连直到成功
        
        Args:
            profile: 当前捕捉配置
            use_foreground: 是否前台执行
            stop_event: 停止事件
            retry_count: 当前重试次数
            max_retries: 最大重试次数（None 表示不限次数）
        """
        if stop_event and stop_event.is_set():
            self._emit("⛔ [重连脚本] 停止重试", "WARN")
            return
        
        if getattr(self.bot, "stop_current", False):
            self._emit("⛔ [重连脚本] stop_current被设置，停止重试", "WARN")
            return
        
        # 执行重连脚本的核心逻辑
        self._execute_reconnect_scripts_core(profile, use_foreground, stop_event, retry_count, max_retries)
    
    def _execute_reconnect_scripts_core(self, profile: WildCaptureProfile, use_foreground: bool, stop_event: Optional[threading.Event] = None, retry_count: int = 0, max_retries: Optional[int] = None) -> None:
        """
        执行重连脚本的核心逻辑：登录.json + to{profile}.json
        执行完成后，检测最后的map信号，根据结果决定执行双塔捕捉流程或刷新流程
        
        Args:
            profile: 当前捕捉配置
            use_foreground: 是否前台执行
            stop_event: 停止事件（可选，用于执行双塔捕捉流程）
            retry_count: 当前重试次数（用于双塔模式的循环重试）
            max_retries: 最大重试次数（用于双塔模式的循环重试，None 表示不限次数）
        """
        # 设置标志表示正在执行重连脚本
        self._reconnect_scripts_executing = True
        
        try:
            # 获取to脚本名称
            to_script_name = self._get_to_script_name(profile)
            if not to_script_name:
                self._emit(f"⚠️ [重连脚本] 无法确定to脚本名称（profile={profile.name}），跳过", "WARN")
                return
            
            self._emit(f"🔄 [重连脚本] 开始执行：登录.json + {to_script_name}.json", "SYSTEM")
            
            # 检查是否有daily_runner
            if not hasattr(self.bot, "daily_runner"):
                self._emit("⚠️ [重连脚本] bot.daily_runner不存在，无法执行脚本", "WARN")
                return
            
            daily_runner = self.bot.daily_runner
            bg_mode = not use_foreground
            
            # 1. 执行登录.json
            if daily_runner.run_single_script("登录", bg_mode=bg_mode):
                self._emit("✅ [重连脚本] 登录.json执行完成", "SUCCESS")
            else:
                self._emit("⚠️ [重连脚本] 登录.json执行失败，继续执行后续步骤", "WARN")
            
            # ✅ 1.5. 等待0.5s，点击登录.亨姆区域，执行亨姆.json
            time.sleep(0.5)
            
            # 点击登录.亨姆区域
            try:
                hengmu_region = self.regions.get("登录.亨姆")
                if hengmu_region:
                    self._emit("🖱️ [重连脚本] 点击登录.亨姆区域", "INFO")
                    self._click_region(hengmu_region, use_foreground)
                    time.sleep(0.2)  # 等待点击生效
                else:
                    self._emit("⚠️ [重连脚本] 找不到登录.亨姆区域，跳过点击", "WARN")
            except Exception as e:
                self._emit(f"⚠️ [重连脚本] 点击登录.亨姆区域时出错: {e}", "WARN")
            
            # 执行亨姆.json
            if daily_runner.run_single_script("亨姆", bg_mode=bg_mode):
                self._emit("✅ [重连脚本] 亨姆.json执行完成", "SUCCESS")
            else:
                self._emit("⚠️ [重连脚本] 亨姆.json执行失败，继续执行后续步骤", "WARN")
            
            # ✅ 亨姆脚本执行完成后，等待0.5s再执行亨姆检测流程
            time.sleep(0.5)
            
            # ✅ 螳螂模式下不再执行飞行脚本，直接执行to脚本
            
            # ✅ 1.6. 在执行to脚本之前，执行亨姆检测流程
            self._emit("🔍 [重连脚本] 开始执行亨姆检测流程", "INFO")
            # 创建一个临时stop_event（重连脚本执行时不会被中断）
            temp_stop_event = threading.Event()
            if self._handle_hengmu_before_to_script(profile, use_foreground, temp_stop_event):
                self._emit("✅ [重连脚本] 亨姆检测流程完成", "SUCCESS")
            else:
                self._emit("⚠️ [重连脚本] 亨姆检测流程失败，继续执行to脚本", "WARN")
            
            # 2. 执行to脚本
            if daily_runner.run_single_script(to_script_name, bg_mode=bg_mode):
                self._emit(f"✅ [重连脚本] {to_script_name}.json执行完成", "SUCCESS")
            else:
                self._emit(f"⚠️ [重连脚本] {to_script_name}.json执行失败", "WARN")
            
            self._emit("✅ [重连脚本] 重连脚本执行完成", "SUCCESS")
            
            # ✅ 3. 检测最后的map信号和newNPC信号
            profile_name_lower = profile.name.lower()
            if "双塔" in profile_name_lower:
                # 等待一小段时间，确保日志都被收集
                time.sleep(2.0)
                
                # 清除stop_current标志
                if self.bot:
                    self.bot.stop_current = False
                    self._emit("✅ [重连脚本] stop_current标志已清除", "SUCCESS")
                
                # 检测最后的map信号（315）和newNPC
                last_map_id, has_newNPC = self._check_last_map_and_newnpc(315, timeout_s=10.0)
                
                if last_map_id == 315 and has_newNPC:
                    # 检测到map 315且后面跟着newNPC，执行双塔捕捉流程
                    self._emit("✅ [重连脚本] 检测到map 315 + newNPC，执行双塔捕捉流程", "SUCCESS")
                    
                    # ✅ 标记重连脚本执行完成
                    self._reconnect_scripts_executing = False
                    
                    # 递归调用run方法，执行双塔捕捉流程
                    # 使用传入的stop_event（如果提供了），否则创建新的
                    actual_stop_event = stop_event if stop_event is not None else threading.Event()
                    self.run(
                        actual_stop_event, 
                        use_foreground, 
                        profile, 
                        test_mode=False, 
                        smart_tracking_mode=False, 
                        xiaodouya_nie_test_mode=False
                    )
                    return
                else:
                    # 没有检测到map 315 + newNPC，执行刷新流程并重试
                    self._emit(f"⚠️ [重连脚本] 未检测到map 315 + newNPC（检测到map={last_map_id}, has_newNPC={has_newNPC}），执行刷新流程并重试", "WARN")
                    
                    # ✅ 标记重连脚本执行完成
                    self._reconnect_scripts_executing = False
                    
                    # 执行刷新流程并等待login信号，然后重新执行重连脚本（循环直到成功）
                    actual_stop_event = stop_event if stop_event is not None else threading.Event()
                    self._execute_refresh_flow_and_wait_login(profile, use_foreground, actual_stop_event, retry_count, max_retries)
                    return
            elif "嘟咕噜" in profile_name_lower:
                # 嘟咕噜模式：和双塔模式一样的逻辑（循环重试、地图验证、直接调用run）
                # 等待一小段时间，确保日志都被收集
                time.sleep(2.0)
                
                # 清除stop_current标志
                if self.bot:
                    self.bot.stop_current = False
                    self._emit("✅ [重连脚本] stop_current标志已清除", "SUCCESS")
                
                # 检测最后的map信号（323）和newNPC
                last_map_id, has_newNPC = self._check_last_map_and_newnpc(323, timeout_s=10.0)
                
                if last_map_id == 323 and has_newNPC:
                    # 检测到map 323且后面跟着newNPC，执行嘟咕噜捕捉流程
                    self._emit("✅ [重连脚本] 检测到map 323 + newNPC，执行嘟咕噜捕捉流程", "SUCCESS")
                    
                    # ✅ 标记重连脚本执行完成
                    self._reconnect_scripts_executing = False
                    
                    # 递归调用run方法，执行嘟咕噜捕捉流程
                    # 使用传入的stop_event（如果提供了），否则创建新的
                    actual_stop_event = stop_event if stop_event is not None else threading.Event()
                    self.run(
                        actual_stop_event, 
                        use_foreground, 
                        profile, 
                        test_mode=False, 
                        smart_tracking_mode=False, 
                        xiaodouya_nie_test_mode=False
                    )
                    return
                else:
                    # 没有检测到map 323 + newNPC，执行刷新流程并重试
                    max_retries_str = f"/{max_retries}" if max_retries is not None else ""
                    if last_map_id is not None and last_map_id != 323:
                        self._emit(f"⚠️ [重连脚本] 检测到错误地图 {last_map_id}（期望323），执行刷新流程并重试（第 {retry_count + 1}{max_retries_str} 次）", "WARN")
                    else:
                        self._emit(f"⚠️ [重连脚本] 未检测到map 323 + newNPC（检测到map={last_map_id}, has_newNPC={has_newNPC}），执行刷新流程并重试（第 {retry_count + 1}{max_retries_str} 次）", "WARN")
                    
                    # ✅ 标记重连脚本执行完成
                    self._reconnect_scripts_executing = False
                    
                    # 执行刷新流程并等待login信号，然后重新执行重连脚本（循环直到成功）
                    actual_stop_event = stop_event if stop_event is not None else threading.Event()
                    self._execute_refresh_flow_and_wait_login(profile, use_foreground, actual_stop_event, retry_count, max_retries)
                    return
            elif "螳螂" in profile_name_lower or "小豆芽" in profile_name_lower:
                # 对于其他模式，保持原有逻辑（设置重启标志）
                self._emit("⏳ [重连脚本] 等待2秒后清除stop_current标志，设置重启标志", "INFO")
                time.sleep(2.0)
                if self.bot:
                    self.bot.stop_current = False
                    self._emit("✅ [重连脚本] stop_current标志已清除", "SUCCESS")
                
                # ✅ 重连脚本执行完成后，设置重启标志（主循环退出后会检查并重启）
                self._should_restart_after_reconnect = True
                self._emit("🔄 [重连脚本] 已设置重连后重启标志（主循环退出后将自动重新启动任务）", "INFO")
            
            # ✅ 标记重连脚本执行完成
            self._reconnect_scripts_executing = False
            
        except Exception as e:
            # 确保异常时也清除标志
            self._reconnect_scripts_executing = False
            self._emit(f"❌ [重连脚本] 执行异常: {e}", "ERROR")
            import traceback
            self._emit(f"📋 异常详情: {traceback.format_exc()}", "ERROR")
    
    def _execute_reconnect_scripts(self, profile: WildCaptureProfile, use_foreground: bool, stop_event: Optional[threading.Event] = None) -> None:
        """
        执行重连脚本：登录.json + to{profile}.json（通用入口，用于非双塔模式）
        
        Args:
            profile: 当前捕捉配置
            use_foreground: 是否前台执行
            stop_event: 停止事件（可选，用于执行双塔捕捉流程）
        """
        self._execute_reconnect_scripts_core(profile, use_foreground, stop_event, retry_count=0, max_retries=1)

    # ---------------------------
    # click helpers
    # ---------------------------
    def _click_region(self, reg_or_key, use_foreground: bool) -> Tuple[float, float]:
        """点击region（支持Region对象或region key字符串）"""
        if isinstance(reg_or_key, str):
            # 如果是字符串，尝试获取region
            reg = self.regions.get(reg_or_key)
            if not reg:
                raise KeyError(f"Region not found: {reg_or_key}")
        else:
            reg = reg_or_key
        
        x, y = reg.sample_click_point()
        if use_foreground:
            window_manager.click(x, y)
        else:
            window_manager.click_background(x, y)
        return float(x), float(y)

    @staticmethod
    def _region_center(reg: Region) -> Tuple[float, float]:
        x1, y1, x2, y2 = reg.outer_bbox()
        return (x1 + x2) / 2.0, (y1 + y2) / 2.0

    @staticmethod
    def _dist2(a: Tuple[float, float], b: Tuple[float, float]) -> float:
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2
    
    def _sort_route_points_by_distance(
        self, 
        route_points: List[Tuple[str, Region]], 
        prefix: str, 
        map_id: int, 
        reg_z: Region
    ) -> List[Tuple[str, Region]]:
        """
        根据距离对路线点进行排序，并直接排除距离最远的两个点
        
        Args:
            route_points: 原始路线点列表 (key, Region)
            prefix: 当前前缀（"尼奥一"或"尼奥二"）
            map_id: 当前地图ID（10或11）
            reg_z: Z点区域（不再使用，保留以兼容）
        
        Returns:
            排序后的路线点列表（只返回7个点，排除最远的2个点，距离近的在前）
        """
        # 确定参考点（用于距离计算）：基于地图切换点
        if map_id == 10:  # 尼奥一（10号地图）：使用"尼奥一.to二"区域
            reference_key = f"{prefix}.to二"
            reference_reg = self.regions.get(reference_key)
            if not reference_reg:
                self._emit(f"⚠️ 未找到参考点{reference_key}，使用默认顺序", "WARN")
                # 如果找不到参考点，排除最后两个点
                return route_points[:-2] if len(route_points) > 2 else route_points
            reference_center = self._region_center(reference_reg)
        else:  # map11（尼奥二）：使用"尼奥二.to一"区域
            reference_key = f"{prefix}.to一"
            reference_reg = self.regions.get(reference_key)
            if not reference_reg:
                self._emit(f"⚠️ 未找到参考点{reference_key}，使用默认顺序", "WARN")
                # 如果找不到参考点，排除最后两个点
                return route_points[:-2] if len(route_points) > 2 else route_points
            reference_center = self._region_center(reference_reg)
        
        # 计算每个点到参考点的距离
        def calc_distance(key_reg: Tuple[str, Region]) -> Tuple[float, str, Region]:
            key, reg = key_reg
            point_center = self._region_center(reg)
            dist = self._dist2(point_center, reference_center)
            return (dist, key, reg)
        
        # 计算所有点的距离
        point_distances = [calc_distance(p) for p in route_points]
        
        # 按距离排序（距离近的在前）
        point_distances.sort(key=lambda x: x[0])
        
        # ✅ 直接排除最远的两个点，只返回7个点
        # 取前7个点（排除最后2个最远的点）
        filtered_points = point_distances[:-2] if len(point_distances) > 2 else point_distances
        
        sorted_points = [(key, reg) for _, key, reg in filtered_points]
        
        # 输出排序信息（用于调试）
        excluded_points = [(key, reg) for _, key, reg in point_distances[-2:]] if len(point_distances) > 2 else []
        excluded_keys = [k for k, _ in excluded_points]
        self._emit(f"📐 [map{map_id}] 扫描顺序（距{reference_key}由近到远，排除最远2个点）：扫描={', '.join([k for k, _ in sorted_points])} | 排除={', '.join(excluded_keys)}", "DEBUG")
        
        return sorted_points
    
    def _get_farthest_two_points(
        self,
        route_points: List[Tuple[str, Region]],
        reg_a: Region,
        map_id: int,
        prefix: str
    ) -> List[Tuple[str, Region]]:
        """
        获取最远的2个点（用于闪光皮皮和稀有精灵模式）
        
        Args:
            route_points: 原始路线点列表 (key, Region)
            reg_a: A点区域（作为参考点）
            map_id: 地图ID
            prefix: 前缀（用于日志输出）
        
        Returns:
            最远的2个点的列表（按距离从远到近排序）
        """
        # 使用A点作为参考点计算距离
        reference_center = self._region_center(reg_a)
        
        # 计算每个点到参考点的距离
        def calc_distance(key_reg: Tuple[str, Region]) -> Tuple[float, str, Region]:
            key, reg = key_reg
            point_center = self._region_center(reg)
            dist = self._dist2(point_center, reference_center)
            return (dist, key, reg)
        
        # 计算所有点的距离
        point_distances = [calc_distance(p) for p in route_points]
        
        # 按距离排序（距离远的在前）
        point_distances.sort(key=lambda x: x[0], reverse=True)
        
        # ✅ 只返回最远的2个点
        if len(point_distances) >= 2:
            farthest_points = point_distances[:2]
            sorted_points = [(key, reg) for _, key, reg in farthest_points]
            excluded_points = [(key, reg) for _, key, reg in point_distances[2:]]
            excluded_keys = [k for k, _ in excluded_points]
            included_keys = [k for k, _ in sorted_points]
            self._emit(f"📐 [稀有精灵模式] 只检测最远的2个点（距A点由远到近）：检测={', '.join(included_keys)} | 排除={', '.join(excluded_keys)}", "INFO")
            return sorted_points
        else:
            # 如果不足2个点，返回所有点
            self._emit(f"⚠️ 路线点不足2个，返回所有点", "WARN")
            return route_points

    # ---------------------------
    # 智能追踪相关方法
    # ---------------------------
    def _click_region_twice(self, reg_or_key, use_foreground: bool, gap: float = 0.08):
        """
        点击region两次（支持Region对象或region key字符串）
        """
        self._click_region(reg_or_key, use_foreground)
        time.sleep(gap)
        self._click_region(reg_or_key, use_foreground)

    def _detect_self_movement(self, timeout: float = 0.8) -> bool:
        """
        检测自己是否成功移动到目标位置
        
        方法：检测PetItem信号
        Returns: True=已移动/已触发对战, False=未移动
        """
        try:
            if wait_kernel_contains("/resource/item/petItem/icon/", timeout=timeout, poll=0.05):
                return True
        except Exception:
            pass
        return False

    def _scan_orange_around_point(
        self, 
        center_reg: Region, 
        scan_radius: int = 100
    ) -> Optional[Tuple[float, float]]:
        """
        以某个刷新点为中心，扫描附近区域的橙色像素，找到橙色精灵的中心位置
        
        Args:
            center_reg: 刷新点region（作为扫描中心）
            scan_radius: 扫描半径（像素）
        
        Returns:
            橙色精灵的中心坐标 (x, y)，如果没找到返回None
        """
        ORANGE_COLOR = (254, 103, 0)  # FE6700
        ORANGE_TOLERANCE = 15  # 橙色检测容差
        
        # 获取刷新点中心
        cx, cy = self._region_center(center_reg)
        
        # 计算扫描区域（以刷新点为中心的正方形）
        x1 = max(0, int(cx - scan_radius))
        y1 = max(0, int(cy - scan_radius))
        x2 = int(cx + scan_radius)
        y2 = int(cy + scan_radius)
        
        try:
            # 截取扫描区域
            img = window_manager.grab_game_bbox(x1, y1, x2, y2)
            if img is None:
                return None
            
            # 转换为RGB数组
            arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
            h, w = arr.shape[:2]
            
            # 查找橙色像素
            orange_pixels = []
            tr, tg, tb = ORANGE_COLOR
            
            for y in range(h):
                for x in range(w):
                    r, g, b = arr[y, x]
                    if (abs(r - tr) <= ORANGE_TOLERANCE and 
                        abs(g - tg) <= ORANGE_TOLERANCE and 
                        abs(b - tb) <= ORANGE_TOLERANCE):
                        # 转换为游戏坐标
                        gx = x1 + x
                        gy = y1 + y
                        orange_pixels.append((gx, gy))
            
            if not orange_pixels:
                return None
            
            # 计算橙色像素的中心（质心）
            if len(orange_pixels) == 1:
                return orange_pixels[0]
            
            # 使用平均
            avg_x = sum(p[0] for p in orange_pixels) / len(orange_pixels)
            avg_y = sum(p[1] for p in orange_pixels) / len(orange_pixels)
            
            return (avg_x, avg_y)
            
        except Exception as e:
            self._emit(f"⚠️ 橙色扫描异常: {e}", "WARN")
            return None

    def _track_orange_movement(
        self,
        initial_center: Tuple[float, float],
        center_reg: Region,
        scan_radius: int = 100,
        max_track_time: float = 2.0,
        poll_interval: float = 0.1,
        stop_event: threading.Event = None,
    ) -> Optional[Tuple[float, float]]:
        """
        持续追踪橙色精灵的移动，返回最新的中心位置
        
        Args:
            initial_center: 初始中心位置
            center_reg: 刷新点region（扫描中心）
            scan_radius: 扫描半径
            max_track_time: 最大追踪时间
            poll_interval: 采样间隔
        
        Returns:
            最新的橙色中心位置，如果丢失返回None
        """
        last_center = initial_center
        t0 = time.time()
        
        while (time.time() - t0) < max_track_time:
            if stop_event and stop_event.is_set():
                return None
            
            current_center = self._scan_orange_around_point(center_reg, scan_radius)
            
            if current_center is None:
                # 如果短暂丢失，使用上一次位置
                if time.time() - t0 < 0.3:
                    continue
                else:
                    self._emit("⚠️ 追踪丢失橙色目标", "WARN")
                    return last_center
            
            # 检测移动方向
            dx = current_center[0] - last_center[0]
            dy = current_center[1] - last_center[1]
            dist = (dx**2 + dy**2)**0.5
            
            if dist > 5:  # 如果移动超过5像素，更新位置
                last_center = current_center
                self._emit(f"📍 追踪到橙色移动: ({current_center[0]:.0f}, {current_center[1]:.0f}) 位移={dist:.1f}px", "DEBUG")
            
            time.sleep(poll_interval)
        
        return last_center

    def _find_safe_nearby_point(
        self,
        target_reg: Region,
        target_key: str,
        route_points: Sequence[Tuple[str, Region]],
        exclude_keys: List[str] = None,
    ) -> Optional[Tuple[str, Region]]:
        """
        找到刷新点附近的安全点（用于补点）
        
        策略：选择距离目标最近但不是目标本身的其他刷新点
        """
        target_center = self._region_center(target_reg)
        exclude_keys = exclude_keys or []
        exclude_keys.append(target_key)
        
        candidates = []
        for key, reg in route_points:
            if key in exclude_keys:
                continue
            center = self._region_center(reg)
            dist = self._dist2(target_center, center)
            candidates.append((dist, key, reg))
        
        if not candidates:
            return None
        
        # 选择最近的
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1], candidates[0][2]

    def _smart_click_and_track_rare_pet(
        self,
        target_key: str,
        target_reg: Region,
        route_points: Sequence[Tuple[str, Region]],
        reg_a: Region,
        reg_b: Region,
        profile: WildCaptureProfile,
        use_foreground: bool,
        stop_event: threading.Event,
        max_retries: int = 2,
    ) -> Optional[Tuple[float, float]]:
        """
        智能点击并追踪稀有精灵（橙毛球/嘟咕噜）
        
        完整流程：
        1. 先快速点两下刷新点
        2. 检测是否成功移动
        3. 如果失败，实施补点追踪逻辑
        4. 持续追踪橙色精灵移动
        5. 点击新的精灵位置
        
        Returns:
            成功返回最终点击坐标，失败返回None
        """
        self._emit(f"🎯 开始智能追踪：刷新点={target_key}", "INFO")
        
        # ========== 第一阶段：点击刷新点两次 ==========
        self._emit("📍 第一阶段：点击刷新点两次", "DEBUG")
        self._click_region_twice(target_reg, use_foreground, gap=0.08)
        
        # 检测是否成功移动
        moved = self._detect_self_movement(timeout=0.8)
        
        if moved:
            self._emit("✅ 检测到成功移动，继续正常流程", "SUCCESS")
            return self._region_center(target_reg)
        
        # ========== 第二阶段：补点追踪逻辑 ==========
        self._emit("⚠️ 未检测到移动，启动补点追踪", "WARN")
        
        # 扫描初始橙色位置
        initial_orange = self._scan_orange_around_point(target_reg, scan_radius=100)
        
        if initial_orange is None:
            self._emit("❌ 无法找到橙色目标，放弃", "ERROR")
            return None
        
        self._emit(f"🔍 找到初始橙色位置: ({initial_orange[0]:.0f}, {initial_orange[1]:.0f})", "INFO")
        
        # 找到附近的安全点
        nearby_point = self._find_safe_nearby_point(
            target_reg, target_key, route_points
        )
        
        if not nearby_point:
            self._emit("❌ 无法找到附近安全点，放弃", "ERROR")
            return None
        
        nearby_key, nearby_reg = nearby_point
        self._emit(f"📍 选择附近安全点: {nearby_key}", "DEBUG")
        
        # 点击附近安全点
        self._click_region(nearby_reg, use_foreground)
        self._current_pos = self._region_center(nearby_reg)
        
        # 等待移动到附近点
        self._sleep_abortable(stop_event, 1.0)
        
        # 持续追踪橙色移动
        tracked_center = self._track_orange_movement(
            initial_orange,
            target_reg,  # 仍然以原始刷新点为扫描中心
            scan_radius=120,  # 稍微扩大扫描范围
            max_track_time=1.5,
            poll_interval=0.1,
            stop_event=stop_event,
        )
        
        if tracked_center is None:
            self._emit("❌ 追踪丢失，放弃", "ERROR")
            return None
        
        self._emit(f"✅ 追踪到橙色新位置: ({tracked_center[0]:.0f}, {tracked_center[1]:.0f})", "INFO")
        
        # ========== 第三阶段：点击新的精灵位置 ==========
        # 找到最接近追踪位置的刷新点
        best_point = None
        best_dist = float('inf')
        
        for key, reg in route_points:
            center = self._region_center(reg)
            dist = self._dist2(tracked_center, center)
            if dist < best_dist:
                best_dist = dist
                best_point = (key, reg)
        
        if best_point is None or best_dist > 50**2:  # 如果距离超过50像素，可能不在任何刷新点
            self._emit(f"⚠️ 橙色位置不在任何刷新点附近（距离最近点{best_dist**0.5:.1f}px），尝试直接点击橙色中心", "WARN")
            # 直接点击橙色中心位置
            if use_foreground:
                window_manager.click(tracked_center[0], tracked_center[1])
            else:
                window_manager.click_background(tracked_center[0], tracked_center[1])
            return tracked_center
        
        new_key, new_reg = best_point
        self._emit(f"📍 橙色移动到刷新点: {new_key}，点击两次", "INFO")
        
        # 点击新的刷新点两次
        self._click_region_twice(new_reg, use_foreground, gap=0.08)
        
        # 检测是否成功入战
        battle_triggered = self._detect_self_movement(timeout=0.8)
        
        if battle_triggered:
            self._emit("✅ 成功触发对战", "SUCCESS")
            return self._region_center(new_reg)
        
        # ========== 第四阶段：如果还没入战，移动到另一侧再点击 ==========
        self._emit("⚠️ 仍未入战，移动到精灵另一侧", "WARN")
        
        # 找到精灵另一侧的安全点
        opposite_point = self._find_safe_nearby_point(
            new_reg, new_key, route_points, exclude_keys=[target_key, nearby_key]
        )
        
        if opposite_point:
            opp_key, opp_reg = opposite_point
            self._emit(f"📍 移动到另一侧安全点: {opp_key}", "DEBUG")
            self._click_region(opp_reg, use_foreground)
            self._current_pos = self._region_center(opp_reg)
            self._sleep_abortable(stop_event, 1.0)
            
            # 再次追踪橙色位置
            latest_orange = self._scan_orange_around_point(new_reg, scan_radius=120)
            if latest_orange:
                # 再次点击橙色位置附近的刷新点
                final_point = None
                final_dist = float('inf')
                for key, reg in route_points:
                    center = self._region_center(reg)
                    dist = self._dist2(latest_orange, center)
                    if dist < final_dist:
                        final_dist = dist
                        final_point = (key, reg)
                
                if final_point and final_dist <= 50**2:
                    final_key, final_reg = final_point
                    self._emit(f"📍 最终点击刷新点: {final_key}", "INFO")
                    self._click_region_twice(final_reg, use_foreground, gap=0.08)
                    
                    if self._detect_self_movement(timeout=0.8):
                        return self._region_center(final_reg)
        
        self._emit("❌ 补点追踪失败，放弃本次对战", "ERROR")
        return None

    @staticmethod
    def _hit_target_mp3(lines: List[str], mp3_id_or_ids) -> bool:
        """检测是否命中目标mp3 ID（支持单个ID或ID元组）"""
        if isinstance(mp3_id_or_ids, (tuple, list)):
            # 多个mp3 ID：任意一个匹配即可
            return any(f"/{mp3_id}.mp3" in ln for ln in lines for mp3_id in mp3_id_or_ids)
        else:
            # 单个mp3 ID
            token = f"/{mp3_id_or_ids}.mp3"
        return any(token in ln for ln in lines)

    def _sleep_abortable(self, stop_event: Optional[threading.Event], seconds: float, tick: float = 0.1):
        end = time.time() + float(max(0.0, seconds))
        while time.time() < end:
            if stop_event is not None and stop_event.is_set():
                return
            if getattr(self.bot, "stop_current", False):
                return
            if getattr(self.bot, "is_paused", False):
                time.sleep(0.05)
                continue
            time.sleep(tick)

    def _wait_if_paused(self, stop_event: threading.Event):
        if hasattr(self.bot, "wait_if_paused") and callable(getattr(self.bot, "wait_if_paused")):
            self.bot.wait_if_paused()
            return
        while getattr(self.bot, "is_paused", False) and (not stop_event.is_set()) and (not getattr(self.bot, "stop_current", False)):
            time.sleep(0.05)

    def _emit(self, text: str, level: str = "INFO"):
        if hasattr(self.bot, "emit_and_log") and callable(getattr(self.bot, "emit_and_log")):
            try:
                self.bot.emit_and_log(text, level)
                return
            except Exception:
                pass
        if level == "ERROR":
            log.error(text)
        elif level in ("WARN", "WARNING"):
            log.warning(text)
        else:
            log.info(text)
