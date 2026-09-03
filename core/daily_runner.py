# core/daily_runner.py
import csv
import json
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple, Callable, Sequence

from core.logger import logger
from core.utils import (
    window_manager,
    wait_pet_bag_ui_ready_after_open,
    scan_pet_bag_count,
    wait_map10_white_probe_ready,
    mean_rgb_for_region_key,
    BAG_UI_READY_PROBE_KEY,
    BAG_OPEN_READY_POLL_SEC,
    MAP10_WHITE_PROBE_KEY_NIEO,
)
from core.post_battle_cleaner import PostBattleCleaner
from core.unified_battle_framework import UnifiedBattleFramework, BattleConfig, BattleMode
from core.fixed_mode_adapter import FixedModeAdapter
from core.signin_record import (
    append_monthly_bee_award,
    append_signin_record,
    business_day_number_6am,
    has_monthly_bee_award,
)
from core.kernel_log_match import (
    line_matches,
    first_map_id_in_line,
    iter_fight_pet_swf_ids_in_line,
    RE_FIGHT_SKILL_SWF,
    RE_PETITEM,
    RE_PETSTORAGE_SWF,
    RE_NEWNPC_MULTI,
    RE_MAP_PATH_LOOSE,
    RE_MONKEY_KUNGFU_TASK_SWF,
    RE_NPC_IRIS_SWF,
    RE_NONO_SUPER_ACTION_PATH,
    line_has_target_map_bgm_id,
    RE_ITEM_DOODLE_ICON_3,
    RE_ITEM_PETITEM_ICON_300012,
    RE_ITEM_DOODLE_ICON_1,
)

# 勇者之塔：独立按钮与一键日常后续的默认对战场数（原 10）。
DEFAULT_HERO_TOWER_BATTLES = 2


def _is_release_selection_yellow(rgb: Tuple[int, int, int]) -> bool:
    """Accept the observed yellow selection highlight across render variants."""
    r, g, b = (int(value) for value in rgb)
    return (
        r >= 160
        and g >= 200
        and b <= 100
        and b <= min(r, g) * 0.5
    )


def _is_release_display_light_blue(rgb: Tuple[int, int, int]) -> bool:
    """Accept both observed light-blue display probe renderings."""
    reference = (163, 189, 216)
    r, g, b = (int(value) for value in rgb)
    return max(
        abs(r - reference[0]),
        abs(g - reference[1]),
        abs(b - reference[2]),
    ) <= 45

# 一键日常链 / 独立按钮：单场 1v1 或大乱斗超过此时长则结束该阶段
DAILY_CHAOS_SINGLE_BATTLE_TIMEOUT_S = 30 * 60
DAILY_1V1_SINGLE_BATTLE_TIMEOUT_S = 30 * 60

# 勇者之塔：自开始等待 map 信号起的最长时限（防进图失败无限卡住）
HERO_TOWER_MAP_GUARD_TIMEOUT_S = 5 * 60

PSYCHIC_EXP_MACHINE_DOG_PANEL_RE = re.compile(
    r"(?:module[\\/]com[\\/]robot[\\/]module[\\/]app[\\/]MachineDogPanel\.swf|MachineDogPanel\.swf)",
    re.IGNORECASE,
)
PSYCHIC_EXP_ADM_PANEL_RE = re.compile(
    r"(?:module[\\/]com[\\/]robot[\\/]module[\\/]app[\\/]ExpAdmPanel\.swf|ExpAdmPanel\.swf)",
    re.IGNORECASE,
)
PICK_PET_EXP_MACHINE_DOG_PANEL_RE = re.compile(
    r"(?:module[\\/]com[\\/]robot[\\/]module[\\/]app[\\/]MachineDogPanel\.swf|MachineDogPanel\.swf)",
    re.IGNORECASE,
)
PICK_PET_EXP_ADM_PANEL_RE = re.compile(
    r"(?:module[\\/]com[\\/]robot[\\/]module[\\/]app[\\/]ExpAdmPanel\.swf|ExpAdmPanel\.swf)",
    re.IGNORECASE,
)
NONO_SOUL_TRANSFORM_PANEL_RE = re.compile(
    r"(?:module[\\/]com[\\/]robot[\\/]module[\\/]app[\\/]SoulTransformPanel\.swf|SoulTransformPanel\.swf)",
    re.IGNORECASE,
)
HATCH_PET_BREED_PANEL_RE = re.compile(
    r"(?:module[\\/]com[\\/]robot[\\/]module[\\/]app[\\/]PetBreedPanel\.swf|PetBreedPanel\.swf)",
    re.IGNORECASE,
)
HATCH_CN = ("一", "二", "三", "四", "五", "六")
HATCH_OPEN_KEY = "孵化.孵化"
HATCH_LEFT_KEY = "孵化.左"
HATCH_RIGHT_KEY = "孵化.右"
HATCH_START_KEY = "孵化.开始"
HATCH_CLAIM_KEY = "孵化.领取"
HATCH_LEFT_PROBE_KEY = "孵化.右探针"
HATCH_RIGHT_PROBE_KEY = "孵化.左探针"
HATCH_EGG_PROBE_KEY = "孵化.蛋探针"
HATCH_EGG_CLICK_KEY = "孵化.蛋点击"
HATCH_RED_PROBE_KEY = "孵化.红探针"
HATCH_CLOSE_KEY = "孵化.关闭"
HATCH_COLOR_WAIT_TIMEOUT_SEC = 20.0
HATCH_SELECT_TIMEOUT_SEC = 8.0
HATCH_PROBE_TIMEOUT_SEC = 20.0
HATCH_SIDE_CLICK_GAP_SEC = 0.18
HATCH_EGG_PROBE_TIMEOUT_SEC = 30.0
NONO_FUSION_PROBE_1_KEY = "nono.融合探针一"
NONO_FUSION_PROBE_2_KEY = "nono.融合探针二"
NONO_FUSION_START_CONFIRM_LEFT_KEY = "nono.融合开始确认左"
NONO_FUSION_START_CONFIRM_KEY = "nono.融合开始确认"
NONO_FUSION_PROBE_STABLE_TIMEOUT_SEC = 5.0
NONO_FUSION_START_ORACLE_TIMEOUT_SEC = 8.0
NONO_FUSION_NORMALIZE_MAX_REOPENS = 3
# 全局保留 nono 融合实现但禁用所有自动/手动执行入口。
NONO_SOUL_FUSION_GLOBAL_ENABLED = False
FUSION_PANEL_RE = re.compile(
    r"(?:module[\\/]com[\\/]robot[\\/]module[\\/]app[\\/]SpriteFusionPanel\.swf|SpriteFusionPanel\.swf)",
    re.IGNORECASE,
)
FUSION_LAB_MAP_ID = 5
FUSION_FUSE_WHITE_TIMEOUT_SEC = 30.0
HERO_TOWER_MAP_ID = 500

# 一键周常：地图打开使用白色探针；区域到达使用对应 map + newNpc 数字门控。
CHIP_GOLD_LAB_MAP_ID = 5
CHIP_GOLD_LAB_NEWNPC_ID = 3
CHIP_GOLD_TERRACE_MAP_ID = 103
CHIP_GOLD_TERRACE_NEWNPC_ID = 9
CHIP_GOLD_MAP_WHITE_PROBE_KEY = "地图.白色探针"
CHIP_GOLD_SCRIPT_NAME = "金豆"
CHIP_GOLD_SCRIPT_TIMES = 5
CHIP_GOLD_SHOP_YELLOW_RGB = (255, 204, 0)
CHIP_GOLD_SHOP_DIALOG_BLUE_RGB = (47, 167, 238)
CHIP_GOLD_SHOP_PROBE_TOLERANCE = 8
CHIP_GOLD_CRYSTAL_SCRIPT_NAME = "to晶化气泡"
CHIP_GOLD_CRYSTAL_MAP_ID = 55
CHIP_GOLD_CRYSTAL_WHITE_PROBE_KEY = "尼奥一.白色探针"
SUKE_EXCHANGE_CLICK0_KEY = "苏克兑换.0"
SUKE_EXCHANGE_CLICK1_KEY = "苏克兑换.1"
SUKE_EXCHANGE_CLICK2_KEY = "苏克兑换.2"
SUKE_EXCHANGE_WHITE_PROBE_KEY = "苏克兑换.白色探针"
CHIP_GOLD_SUKE_CYCLES = 10
CHIP_GOLD_SUKE_WHITE_TIMEOUT_SEC = 45.0
BUY_PET_PROPS_RE = re.compile(
    r"(?:resource[\\/]module[\\/]petProps[\\/]buyPetProps\.swf|\bbuyPetProps\.swf\b)",
    re.IGNORECASE,
)

# 挖矿（苏克黑/白探针 + 挖矿开始 + 1AND1）
MINING_SUKE_BLACK_KEY = "日常.苏克黑色探针"
MINING_SUKE_WHITE_KEY = "日常.苏克白色探针"
MINING_START_KEY = "日常.挖矿开始探针"
MINING_CONFIRM_KEY = "日常.挖矿确认探针"
NEW_DAILY_MINING_SPOT_KEY = "日常.11甲烷"
NEW_DAILY_MINING_TIMES = 2
NEW_DAILY_STEP_GAP_SEC = 0.5
NEW_DAILY_MAP_NPC_POST_DELAY_SEC = 0.5
NEW_DAILY_MAP_WAIT_TIMEOUT_SEC = 45.0
# 新日常方案「1」
NEW_DAILY_SEQ1_SWITCH_11_KEY = "日常.11切换"
NEW_DAILY_SEQ1_SWITCH_12_KEY = "日常.12切换"
NEW_DAILY_SEQ1_GOLD_SPOT_KEY = "日常.12黄金矿"
NEW_DAILY_SEQ1_GOLD_TIMES = 5
NEW_DAILY_SEQ1_MAP_AFTER_11 = 21
NEW_DAILY_SEQ1_MAP_AFTER_12 = 22
NEW_DAILY_SEQ1_SCRIPT_NAME = "13伊优"
NEW_DAILY_SEQ1_MAX_STEP = 5
NEW_DAILY_VARIANT_MAX_STEPS: Dict[str, int] = {"1": NEW_DAILY_SEQ1_MAX_STEP}
# 新日常方案「2」
NEW_DAILY_SEQ2_MAX_STEP = 6
NEW_DAILY_SEQ2_RETURN_BASE_KEY = "刷新.基地"
NEW_DAILY_SEQ2_BASE_RIGHT_KEY = "日常.基地右侧"
NEW_DAILY_SEQ2_SCRIPT_NAME = "20布布"
NEW_DAILY_SEQ2_SPOT_21_KEY = "日常.21布布"
NEW_DAILY_SEQ2_SWITCH_21_KEY = "日常.21切换"
NEW_DAILY_SEQ2_GOLD_SPOT_22_KEY = "日常.22黄金矿"
NEW_DAILY_SEQ2_GOLD_TIMES = 5
NEW_DAILY_SEQ2_MAP_AFTER_20 = 11
NEW_DAILY_SEQ2_MAP_AFTER_21_SWITCH = 10
NEW_DAILY_SEQ2_1AND1_TIMEOUT_SEC = 20.0
NEW_DAILY_VARIANT_MAX_STEPS["2"] = NEW_DAILY_SEQ2_MAX_STEP
# 新日常方案「3」
NEW_DAILY_SEQ3_MAX_STEP = 7
NEW_DAILY_SEQ3_SCRIPT_NAME = "30猩猩"
NEW_DAILY_SEQ3_SPOT_31_METHANE_KEY = "日常.31甲烷"
NEW_DAILY_SEQ3_METHANE_TIMES = 2
NEW_DAILY_SEQ3_MAP_AFTER_30 = 16
NEW_DAILY_SEQ3_SWITCH_31_KEY = "日常.31切换"
NEW_DAILY_SEQ3_MAP_AFTER_31_SWITCH = 15
NEW_DAILY_SEQ3_GOLD_32_KEY = "日常.32黄金矿"
NEW_DAILY_SEQ3_GOLD_TIMES = 5
NEW_DAILY_SEQ3_SPOT_32_GORILLA_KEY = "日常.32猩猩"
NEW_DAILY_SEQ3_CONFIRM_32_KEY = "日常.32确认"
NEW_DAILY_SEQ3_ENCOURAGE_32_KEY = "日常.32鼓励"
NEW_DAILY_SEQ3_CONFIRM_BURST_SEC = 2.0
NEW_DAILY_SEQ3_ENCOURAGE_BURST_SEC = 27.0
NEW_DAILY_SEQ3_KERNEL_WAIT_TIMEOUT_SEC = 60.0
NEW_DAILY_SEQ3_1AND1_TIMEOUT_SEC = 20.0
NEW_DAILY_SEQ3_RAPID_CLICK_GAP_SEC = 0.05
NEW_DAILY_SEQ3_REWARD_BAG_WAIT_TIMEOUT_SEC = 30.0
NEW_DAILY_VARIANT_MAX_STEPS["3"] = NEW_DAILY_SEQ3_MAX_STEP
# 新日常方案「4」
NEW_DAILY_SEQ4_MAX_STEP = 5
NEW_DAILY_SEQ4_WAREHOUSE_SINGLE_ATTR_KEY = "精灵仓库.单属性"
NEW_DAILY_SEQ4_WAREHOUSE_CATEGORY = "普通系"
NEW_DAILY_SEQ4_WAREHOUSE_REVERSE_POSITIONS = (3, 5, 12)
NEW_DAILY_SEQ4_WAREHOUSE_RIGHT_CLICKS = 15
NEW_DAILY_SEQ4_SCRIPT_40_NAME = "40云霄"
NEW_DAILY_SEQ4_SCRIPT_41_NAME = "41云霄"
NEW_DAILY_SEQ4_MAP_AFTER_40 = 25
NEW_DAILY_SEQ4_SPOT_41_METHANE_KEY = "日常.41甲烷"
NEW_DAILY_SEQ4_METHANE_TIMES = 2
NEW_DAILY_VARIANT_MAX_STEPS["4"] = NEW_DAILY_SEQ4_MAX_STEP
# 新日常方案「5」
NEW_DAILY_SEQ5_MAX_STEP = 2
NEW_DAILY_SEQ5_SCRIPT_50_NAME = "50赫尔卡"
NEW_DAILY_SEQ5_SPOT_51_VINES_KEY = "日常.51藤蔓"
NEW_DAILY_SEQ5_MAP_AFTER_50 = 34
NEW_DAILY_SEQ5_POST_IRIS_DELAY_SEC = 0.5
NEW_DAILY_SEQ5_MINING_TIMES = 1
NEW_DAILY_VARIANT_MAX_STEPS["5"] = NEW_DAILY_SEQ5_MAX_STEP
# 新日常方案「6」
NEW_DAILY_SEQ6_MAX_STEP = 7
NEW_DAILY_SEQ6_SCRIPT_60_1_NAME = "60阿尔法一"
NEW_DAILY_SEQ6_SCRIPT_60_2_NAME = "60阿尔法二"
NEW_DAILY_SEQ6_DOWN_KEY = "日常.60向下"
NEW_DAILY_SEQ6_DOWN_RAPID_SEC = 2.0
NEW_DAILY_SEQ6_MAP_AFTER_60_2 = 105
NEW_DAILY_SEQ6_SPOT_61_MUSHROOM_KEY = "日常.61蘑菇结晶"
NEW_DAILY_SEQ6_MUSHROOM_TIMES = 2
NEW_DAILY_SEQ6_SWITCH_61_KEY = "日常.61切换"
NEW_DAILY_SEQ6_MAP_AFTER_61_SWITCH = 106
NEW_DAILY_SEQ6_SPOT_62_NAGA_KEY = "日常.62纳格晶体"
NEW_DAILY_SEQ6_NAGA_TIMES = 1
NEW_DAILY_SEQ6_SPOT_62_BEAN_KEY = "日常.62豆豆果实"
NEW_DAILY_SEQ6_BEAN_TIMES = 1
NEW_DAILY_SEQ6_SWITCH_62_KEY = "日常.62切换"
NEW_DAILY_SEQ6_MAP_AFTER_62_SWITCH = 46
NEW_DAILY_SEQ6_DOUBLE_63_1_KEY = "日常.63双击一"
NEW_DAILY_SEQ6_DOUBLE_63_2_KEY = "日常.63双击二"
NEW_DAILY_SEQ6_SWITCH_63_KEY = "日常.63切换"
NEW_DAILY_SEQ6_MAP_AFTER_63_SWITCH = 49
NEW_DAILY_SEQ6_SPOT_64_POWER_KEY = "日常.64电能石"
NEW_DAILY_SEQ6_POWER_TIMES = 2
NEW_DAILY_VARIANT_MAX_STEPS["6"] = NEW_DAILY_SEQ6_MAX_STEP
# 新日常方案「7」
NEW_DAILY_SEQ7_MAX_STEP = 4
NEW_DAILY_SEQ7_SCRIPT_70_NAME = "70露西欧"
NEW_DAILY_SEQ7_SCRIPT_72_NAME = "72珊瑚"
NEW_DAILY_SEQ7_MAP_AFTER_70 = 54
NEW_DAILY_SEQ7_SPOT_71_KEYS = (
    "日常.71一",
    "日常.71二",
    "日常.71三",
)
NEW_DAILY_SEQ7_71_BOTTOM_RIGHT_KEY = "日常.71右下"
NEW_DAILY_SEQ7_71_SUMMON_KEY = "日常.71召唤"
NEW_DAILY_SEQ7_SUMMON_CLICK_GAP_SEC = 0.35
NEW_DAILY_SEQ7_SUMMON_ACTION_TIMEOUT_SEC = 120.0
NEW_DAILY_VARIANT_MAX_STEPS["7"] = NEW_DAILY_SEQ7_MAX_STEP
# 新日常方案「8」
NEW_DAILY_SEQ8_MAX_STEP = 4
NEW_DAILY_SEQ8_SCRIPT_80_NAME = "80飞船"
NEW_DAILY_SEQ8_SCRIPT_81_NAME = "81星系"
NEW_DAILY_SEQ8_MAP_AFTER_81 = 325
NEW_DAILY_SEQ8_BGM_AFTER_81 = 228
NEW_DAILY_SEQ8_SPOT_81_GOLD_KEY = "日常.81黄金矿"
NEW_DAILY_SEQ8_GOLD_TIMES = 5
NEW_DAILY_SEQ8_MAP_BTN_KEY = "日常.地图"
NEW_DAILY_SEQ8_MAP_BTN_DELAY_SEC = 2.0
NEW_DAILY_SEQ8_SPOT_82_KEY = "日常.82斯科尔"
NEW_DAILY_SEQ8_MAP_AFTER_82 = 328
NEW_DAILY_SEQ8_SPOT_83_METHANE_KEY = "日常.83甲烷"
NEW_DAILY_SEQ8_METHANE_TIMES = 2
NEW_DAILY_SEQ8_SPOT_83_PULEI_KEY = "日常.83普雷"
NEW_DAILY_SEQ8_MAP_AFTER_83_PULEI = 333
NEW_DAILY_SEQ8_SWITCH_84_KEY = "日常.84切换"
NEW_DAILY_SEQ8_MAP_AFTER_84_SWITCH = 339
NEW_DAILY_SEQ8_SPOT_84_KEYS = (
    "日常.84一",
    "日常.84二",
    "日常.84三",
)
NEW_DAILY_SEQ8_COLLECT_ITEM_SPECS = (
    ("doodle_3", RE_ITEM_DOODLE_ICON_3, "doodle/icon/3.swf"),
    ("petitem_300012", RE_ITEM_PETITEM_ICON_300012, "petItem/icon/300012.swf"),
    ("doodle_1", RE_ITEM_DOODLE_ICON_1, "doodle/icon/1.swf"),
)
NEW_DAILY_SEQ8_COLLECT_MAX_ROUNDS = 15
NEW_DAILY_VARIANT_MAX_STEPS["8"] = NEW_DAILY_SEQ8_MAX_STEP
# 新日常方案「9」
NEW_DAILY_SEQ9_MAX_STEP = 3
NEW_DAILY_SEQ9_SCRIPT_91_NAME = "91勇者"
NEW_DAILY_SEQ9_SPOT_90_KEY = "日常.90太空站"
LANLAN_TO_108_KEY = "地图.102to108"
LANLAN_NPC_KEY = "日常.岚岚"
LANLAN_WHITE_PROBE_KEY = "日常.岚岚白色探针"
LANLAN_START_KEY = "日常.岚岚开始"
LANLAN_SKILL_PLAN_DEFAULT = "default"
LANLAN_SKILL_PLAN_BY_WEEKDAY = {
    1: {
        "key": "tuesday_ancient_fish_dragon",
        "label": "周二远古鱼龙",
        "first_skill2_count": 3,
        "second_sequence": "32221144444111",
        "start": dt_time(0, 0),
        "end": dt_time(23, 45),
    },
    3: {
        "key": "thursday_ancient_fish_dragon",
        "label": "周四远古鱼龙",
        "first_skill2_count": 3,
        "second_sequence": "322231111144444",
        "start": dt_time(0, 0),
        "end": dt_time(23, 45),
    },
    5: {
        "key": "saturday_683",
        "label": "周六683",
        "first_skill2_count": 3,
        "second_sequence": "",
        "second_repeat": "",
        "start": dt_time(0, 0),
        "end": dt_time(23, 45),
    },
    6: {
        "key": "sunday_aifeidesi",
        "label": "周日艾菲德斯",
        "first_skill2_count": 3,
        "second_sequence": "3322244444",
        "start": dt_time(6, 0),
        "end": dt_time(23, 45),
    },
}
LIGHT_MANTIS_CLICK0_KEY = "光螳螂.0"
LIGHT_MANTIS_WHITE_PROBE_KEY = "光螳螂.白色探针"
LIGHT_MANTIS_CLICK_KEYS = ("光螳螂.1", "光螳螂.2", "光螳螂.3")
LIGHT_MANTIS_ENTRY_KEY = "光螳螂.4"
LIGHT_MANTIS_ENTRY_ORANGE_RGB = (254, 103, 0)
LIGHT_MANTIS_ENTRY_ORANGE_TOLERANCE = 24
LIGHT_MANTIS_ENTRY_READY_TIMEOUT_SEC = 45.0
LIGHT_MANTIS_NORMAL_CONFIRM_CLICK_GAP_SEC = 0.12
LIGHT_MANTIS_RANDOM_CONFIRM_CENTER_KEY = "对话框.普通确认"
LIGHT_MANTIS_RANDOM_CONFIRM_RADIUS_PX = 100.0
YILU_POINT_KEYS = tuple(f"依卢.{idx}" for idx in range(1, 7))
YILU_GRAY_RGB = (102, 102, 102)
YILU_ORANGE_RGB = (254, 103, 0)
YILU_ORANGE_WAIT_TIMEOUT_SEC = 45.0
NEW_DAILY_SEQ9_MAP_AFTER_90 = 102
NEW_DAILY_SEQ9_MAP_AFTER_LEAVE = 108
NEW_DAILY_SEQ9_MAP_AFTER_92 = 102
NEW_DAILY_SEQ9_SWITCH_92_KEY = "日常.92切换"
NEW_DAILY_SEQ9_MAP108_WAIT_SEC = 5.0
NEW_DAILY_SEQ9_HERO_TOWER_LEAVE_KEY = "勇者之塔.离开"
NEW_DAILY_CHAIN_VARIANTS = tuple(str(i) for i in range(1, 10))
NEW_DAILY_VARIANT_MAX_STEPS["9"] = NEW_DAILY_SEQ9_MAX_STEP
NEW_DAILY_BAG_PET_ONE_PROBE_KEY = BAG_UI_READY_PROBE_KEY  # 精灵一探针（清空精灵一）
NEW_DAILY_BAG_HP3_KEY = "精灵背包.血条三"
NEW_DAILY_BAG_HP4_KEY = "精灵背包.血条四"
NEW_DAILY_BAG_HP5_KEY = "精灵背包.血条五"
NEW_DAILY_BAG_HP6_KEY = "精灵背包.血条六"
NEW_DAILY_BAG_HP_BLUE_RGB = (23, 73, 145)  # #174991
NEW_DAILY_BAG_HP_BLUE_TOLERANCE = 25.0
NEW_DAILY_BAG_HP_WAIT_TIMEOUT_SEC = 60.0
NEW_DAILY_BAG_POST_ORANGE_DELAY_SEC = 3.0  # 精灵一探针变橙后，再等此时长才点放回仓库
NEW_DAILY_BAG_POST_BLUE_DELAY_SEC = 1.0  # 血条变蓝后，再等此时长才点身边跟随
NEW_DAILY_BAG_PUTBACK_RETRY_INTERVAL_SEC = 5.0  # 点放回仓库后仍未变蓝，间隔此时长补点
FOLLOW_TO_NEXT_UI_DELAY_SEC = 0.5  # 跟随自动关包后，进入地图/重开背包前等待 UI 稳定
MINING_PROBE_POLL_SEC = 0.12
MINING_SUKE_WAIT_TIMEOUT_SEC = 45.0
MINING_1AND1_TIMEOUT_SEC = 30.0
SHANNI_TO_106_KEY = "闪尼.105to106"
SHANNI_TO_105_KEY = "闪尼.106to105"
SHANNI_DRAIN_KEY = "闪尼.吸能"
SHANNI_MAP_105 = 105
SHANNI_MAP_106 = 106
SHANNI_1AND1_TIMEOUT_SEC = 20.0
SHANNI_DELETE_SWF_IDS = (89, 90)
SHANNI_CHANGE_PROBE_X = 390
SHANNI_CHANGE_PROBE_Y = 200
SHANNI_CHANGE_WATCH_SEC = 1.0
SHANNI_CHANGE_POLL_SEC = 0.05
SHANNI_CHANGE_MIN_CHANNEL_DELTA = 8
GACHA_TEST_KEY_1 = "扭蛋.1"
GACHA_TEST_KEY_2 = "扭蛋.2"
GACHA_TEST_KEY_3 = "扭蛋.3"
GACHA_TEST_KEY_4 = "扭蛋.4"
GACHA_TEST_DEEP_CYAN_RGB = (14, 99, 133)
GACHA_TEST_LIGHT_CYAN_RGB = (25, 167, 190)
GACHA_TEST_YELLOW_RGB = (255, 204, 0)
GACHA_TEST_OLIVE_RGB = (152, 142, 41)
GACHA_TEST_TARGET_RGB_TOLERANCE = 8.0
GACHA_TEST_PROBE_POLL_SEC = 0.08
GACHA_TEST_PROBE_LOG_INTERVAL_SEC = 1.0
GACHA_TEST_PROBE_TIMEOUT_SEC = 5.0
GACHA_TEST_1AND1_TIMEOUT_SEC = 20.0
GACHA_RECONNECT_EARLY_FAILURE_CYCLES = 3
GACHA_RUN_RECORD_LOCK = threading.Lock()
WAREHOUSE_PAGE_TURN_MAX_COUNT = 300
ONE_CLICK_RELEASE_CATEGORIES = (
    ("单属性", "机械系"),
    ("单属性", "超能系"),
    ("单属性", "普通系"),
    ("单属性", "冰系"),
    ("单属性", "暗影系"),
    ("双属性", "水超能"),
)
HONOR_EXCHANGE_TO_GACHA_KEY = "荣誉兑换.to扭蛋"
HONOR_EXCHANGE_GACHA_TIMES = 99999
MASTER_CUP_ENTRY_KEY = "大师杯.1"
MASTER_CUP_START_KEY = "大师杯.开始"
MASTER_CUP_MAP108_TO_111_KEY = "地图.map108to111"
MASTER_CUP_SUPPORTED_TYPES = {
    "水系",
    "火系",
    "草系",
    "电系",
    "冰系",
    "地面系",
    "飞行系",
    "机械系",
    "战斗系",
}
MASTER_CUP_568_TYPES = frozenset(MASTER_CUP_SUPPORTED_TYPES - {"飞行系"})
MASTER_CUP_568_PRE_SETUP_SPEC = {
    "warehouse_mode_key": "精灵仓库.单属性",
    "warehouse_category": "普通系",
    "pet_id": 568,
    "scan_first_cyan": True,
}
MASTER_CUP_PRE_SETUP_SPECS = {
    cup: dict(MASTER_CUP_568_PRE_SETUP_SPEC) for cup in MASTER_CUP_568_TYPES
}
MASTER_CUP_PRE_SETUP_SPECS.update({
    "飞行系": {
        "warehouse_mode_key": "精灵仓库.单属性",
        "warehouse_category": "飞行系",
        "pet_id": 268,
    },
})
MASTER_CUP_NORM_FIRE_PRE_SETUP_SPEC = {
    "warehouse_mode_key": "精灵仓库.单属性",
    "warehouse_category": "火系",
    "pet_id": 40,
}
MASTER_CUP_RESTORE_67_SPEC = {
    "pet_id": 67,
}
MASTER_CUP_ENTRY_TIMEOUT_SEC = 60.0
MASTER_CUP_CLICK_GAP_SEC = 1.0
MASTER_CUP_1AND1_TIMEOUT_SEC = 10.0
MASTER_CUP_568_SKILL_SEQUENCE = (4, 2, 4, 2, 2)
MASTER_CUP_568_OWN_PARTY_IDS = frozenset({166, 197, 1459, 606, 568, 1337})
MASTER_CUP_DEFAULT_OWN_PARTY_IDS = frozenset({166, 197, 1459, 606, 67, 1337})

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
       step: {"action":"move_start","x":..,"y":..,"delay":..}
       step: {"action":"move_end","x":..,"y":..,"duration":..}
    2) 老格式：
       step: {"pos":[x,y],"delay":..,"bg": true/false}

    ✅ 给 BotWorker 使用的 API：
    - run_all(background_mode=True, include_hero_tower_after_daily=False)
    - run_single_script(name, bg_mode=True)
    - run_script(script_path, bg_override=None)
    """

    SCRIPT_FOLDER_NAME = "fix_script"

    @staticmethod
    def _business_day_6am() -> int:
        return business_day_number_6am()

    def __init__(self, bot):
        self.bot = bot
        self.script_dir = os.path.join(BASE_PATH, self.SCRIPT_FOLDER_NAME)
        self._unified_framework = None
        self._fixed_adapter = None
        self._outer_mode_restart_enabled = False
        self._one_click_daily_progress: Optional[Dict[str, Any]] = None
        self._nono_fusion_guard = threading.RLock()
        self._nono_fusion_guard_owner: Optional[int] = None
        self._nono_fusion_guard_tag: str = ""

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
        include_hero_tower_after_daily：勾选时执行 DAILY_SEQUENCE 含脚本「6」，并在日常后打勇者之塔两场再接大乱斗×2；
            不勾选（默认）只跑 1–5（从队列中去掉「6」），跳过勇者之塔，直接进入大乱斗×2。
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

        # ✅ 日常脚本完成后：可选勇者之塔两回合，再接大乱斗x2
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
                    tail_intro = "⏱ 勇者之塔完成：3s 后开始【大乱斗x2】…"
                else:
                    tail_ready = bool(ok_all and not self._should_abort())
                    tail_intro = "⏱ 日常完成：跳过勇者之塔，3s 后开始【大乱斗x2】…"

                if tail_ready:
                    try:
                        self._emit(tail_intro, "SYSTEM")
                        time.sleep(3.0)
                        if not self._should_abort():
                            ok_chaos = self.run_chaos_battle_x2(
                                use_foreground=use_foreground,
                                from_daily_chain=True,
                            )
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
    def run_hero_tower(
        self,
        times: int = DEFAULT_HERO_TOWER_BATTLES,
        background_mode: bool = True,
        use_unified_framework: bool = False,
        *,
        require_map_guard: bool = True,
    ) -> bool:
        """日常后续：勇者之塔循环 times 次。"""
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口：无法执行勇者之塔", "ERROR")
            return False

        if require_map_guard:
            latest_map_id = None
            try:
                from core.logger import fetch_kernel_since

                for line in reversed(fetch_kernel_since(0)):
                    latest_map_id = first_map_id_in_line(str(line))
                    if latest_map_id is not None:
                        break
            except Exception:
                latest_map_id = None

            if latest_map_id == HERO_TOWER_MAP_ID:
                self._emit(
                    f"🗺 勇者之塔：最后一个 map 已是 {HERO_TOWER_MAP_ID}，直接继续",
                    "INFO",
                )
            else:
                self._emit(
                    f"🗺 勇者之塔：最后一个 map={latest_map_id}，等待 map{HERO_TOWER_MAP_ID}（{HERO_TOWER_MAP_GUARD_TIMEOUT_S // 60} 分钟保护）…",
                    "INFO",
                )
                if not self._wait_for_map_kernel(
                    HERO_TOWER_MAP_ID,
                    timeout_s=float(HERO_TOWER_MAP_GUARD_TIMEOUT_S),
                ):
                    self._emit(
                        f"❌ 勇者之塔：{HERO_TOWER_MAP_GUARD_TIMEOUT_S // 60} 分钟内未检测到 map{HERO_TOWER_MAP_ID}，中止",
                        "ERROR",
                    )
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

    def _sleep_respecting_deadline(self, seconds: float, deadline: Optional[float]) -> bool:
        """睡眠至多 ``seconds`` 秒；若中止或已超过 ``deadline``，返回 False。"""
        end_wake = time.time() + seconds
        while time.time() < end_wake:
            if self._should_abort():
                return False
            if deadline is not None and time.time() >= deadline:
                return False
            time.sleep(min(0.25, end_wake - time.time()))
        return True

    def _handoff_daily_chaos_timeout(self, from_daily_chain: bool, chaos_deadline: Optional[float]) -> None:
        """一键日常链：单场大乱斗超时后标记失败，任务结束后仍由 UI 交接轮换。"""
        if not from_daily_chain or chaos_deadline is None:
            return
        if self._should_abort():
            return
        if time.time() < chaos_deadline:
            return
        setattr(self.bot, "rotation_handoff_after_chaos_timeout", True)
        self._emit(
            "⏱️ 日常大乱斗：本场自「开始大乱斗」起已超时，判定一键日常失败；任务结束后交接轮换模式",
            "ERROR",
        )

    def _chaos_battle_fail(self, from_daily_chain: bool, chaos_deadline: Optional[float]) -> bool:
        self._handoff_daily_chaos_timeout(from_daily_chain, chaos_deadline)
        return False
    
    # ----------------------------
    # 大乱斗x2：特殊战斗模式
    # ----------------------------
    def run_chaos_battle_x2(
        self,
        use_foreground: bool = True,
        from_daily_chain: bool = False,
        *,
        battle_timeout_s: Optional[float] = None,
    ) -> bool:
        """执行大乱斗x2：两次特殊战斗循环。

        from_daily_chain：一键日常链调用时为 True。
        battle_timeout_s：每场自「开始大乱斗」起的最长时限（默认 30 分钟）。
        """
        if battle_timeout_s is None and from_daily_chain:
            battle_timeout_s = float(DAILY_CHAOS_SINGLE_BATTLE_TIMEOUT_S)
        if battle_timeout_s is None:
            battle_timeout_s = float(DAILY_CHAOS_SINGLE_BATTLE_TIMEOUT_S)
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
            chaos_deadline: Optional[float] = None
            
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

            chaos_deadline = time.time() + float(battle_timeout_s)
            self._emit(
                f"⏱ 本场限时：自「开始大乱斗」起 {int(battle_timeout_s) // 60} 分钟内须结束",
                "INFO",
            )
            
            # 6. 等待PetItem并执行第一回合技能
            if from_daily_chain:
                self._emit("⏳ 等待PetItem进入对战（日常链每场限时见上）...", "INFO")
            else:
                self._emit("⏳ 等待PetItem进入对战（无单场总时限）...", "INFO")
            if not self._wait_for_petitem_and_first_skill(
                regions, use_foreground, timeout_s=None, deadline=chaos_deadline
            ):
                self._emit("❌ 等待PetItem或第一回合失败（可能被中止或已超时）", "ERROR")
                return self._chaos_battle_fail(from_daily_chain, chaos_deadline)
            
            # 7. 执行战斗循环（大乱斗模式）
            if not self._run_chaos_battle_loop(
                regions, use_foreground, cleaner, is_chaos=True, deadline=chaos_deadline
            ):
                self._emit("❌ 战斗循环失败", "ERROR")
                return self._chaos_battle_fail(from_daily_chain, chaos_deadline)
            
            # 8. 检测胜利探针（黄色或白色）并点击确认，然后1AND1清理（参考训练室/勇者之塔）
            # 先等待UI稳定（与训练室保持一致，延迟2.5秒）
            self._emit("⏳ 等待UI稳定（2.5秒）...", "INFO")
            if not self._sleep_respecting_deadline(2.5, chaos_deadline):
                return self._chaos_battle_fail(from_daily_chain, chaos_deadline)
            self._emit("🟡 检测胜利探针（黄色或白色）...", "INFO")
            victory_detected = self._detect_victory_probe_yellow_or_white(
                cleaner, use_foreground, timeout_s=8.0, deadline=chaos_deadline
            )
            if not victory_detected:
                self._emit("❌ 未检测到胜利探针（超时）", "ERROR")
                return self._chaos_battle_fail(from_daily_chain, chaos_deadline)
            
            # 9. 点击"对话框.对战胜利确认"（参考stage4_post_battle的逻辑）
            self._emit("🖱 点击：对话框.对战胜利确认", "INFO")
            if not self._click_region_safe(regions, "对话框.对战胜利确认", use_foreground):
                return self._chaos_battle_fail(from_daily_chain, chaos_deadline)
            
            # 10. 1AND1清理对话框（使用统一框架的方法，参考训练室/勇者之塔）
            self._emit("⏳ 清理对话框（1 AND 1，10秒超时）...", "INFO")
            from core.unified_battle_framework import BattleConfig, BattleMode
            if self._unified_framework is None:
                self._emit("❌ 缺少unified_framework，无法执行1AND1清理", "ERROR")
                return self._chaos_battle_fail(from_daily_chain, chaos_deadline)

            def abort_check_chaos():
                if self._should_abort():
                    return True
                if chaos_deadline is not None and time.time() >= chaos_deadline:
                    return True
                return False

            config = BattleConfig(
                mode=BattleMode.FIXED,  # 大乱斗和1v1使用固定模式
                use_foreground=use_foreground,
                abort_check=abort_check_chaos,
            )
            try:
                # 使用10秒超时（大乱斗模式）
                self._unified_framework._wait_for_confirm_probes(config, timeout_s=10.0)
            except Exception as e:
                self._emit(f"⚠️ 1AND1清理异常: {e}", "WARN")

            if (
                chaos_deadline is not None
                and time.time() >= chaos_deadline
                and not self._should_abort()
            ):
                return self._chaos_battle_fail(from_daily_chain, chaos_deadline)
        
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
            if not wait_pet_bag_ui_ready_after_open(
                regions,
                emit_fn=self._emit,
                stop_check=self._should_abort,
                log_tag="恢复精灵一二",
            ):
                self._emit("❌ [恢复精灵一二] 背包UI未就绪，停止恢复", "ERROR")
                self._request_outer_mode_restart("小号对战-背包UI未就绪")
                return False

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

    def _click_region_safe_twice(
        self,
        regions,
        key: str,
        use_foreground: bool,
        *,
        gap_s: float = 0.06,
    ) -> bool:
        """安全双击区域。"""
        if not self._click_region_safe(regions, key, use_foreground):
            return False
        time.sleep(max(0.0, gap_s))
        return self._click_region_safe(regions, key, use_foreground)

    def _click_region_btn_fallback(
        self, regions, key: str, use_foreground: bool
    ) -> bool:
        """对齐刷新重连/轮换 step2：有「key+按钮」则点按钮，否则点 key（不报缺按钮）。"""
        btn_key = f"{key}按钮"
        if regions.get(btn_key):
            return self._click_region_safe(regions, btn_key, use_foreground)
        return self._click_region_safe(regions, key, use_foreground)

    def _wait_for_map_kernel(
        self,
        map_id: int,
        timeout_s: float = 30.0,
        *,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """等待内核 map 信号。"""
        from core.logger import fetch_kernel_since, kernel_cursor

        start_time = time.time()
        cursor = kernel_cursor()
        should_stop = stop_check or self._should_abort
        while (time.time() - start_time) < timeout_s:
            if should_stop():
                return False
            try:
                lines = fetch_kernel_since(cursor)
                if isinstance(lines, list):
                    for line in lines:
                        if _kernel_line_has_map_id(str(line), map_id):
                            self._emit(f"🗺 检测到map信号：map_id={map_id}", "INFO")
                            return True
                cursor = kernel_cursor()
            except Exception:
                pass
            time.sleep(0.05)
        return False

    def _wait_for_map_and_npc(
        self,
        map_id: int,
        timeout_s: float = 30.0,
        *,
        start_cursor=None,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """等待进入指定地图（map10 必须先观察到白色探针，再等白→非白；其余用 newNpc）。"""
        should_stop = stop_check or self._should_abort
        if map_id == 10:
            regions = getattr(self.bot, "regions", None)
            if regions is None:
                return False
            if not self._wait_for_map_kernel(
                10,
                timeout_s=timeout_s,
                stop_check=should_stop,
            ):
                return False
            return wait_map10_white_probe_ready(
                regions,
                emit_fn=self._emit,
                stop_check=should_stop,
                white_probe_key=MAP10_WHITE_PROBE_KEY_NIEO,
                log_tag="map10",
                timeout_s=timeout_s,
                two_phase=True,
            )

        from core.logger import fetch_kernel_since, kernel_cursor

        start_time = time.time()
        cursor = start_cursor if start_cursor is not None else kernel_cursor()
        map_seen = False
        npc_seen = False

        while (time.time() - start_time) < timeout_s:
            if should_stop():
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
    
    def _wait_for_petitem_and_first_skill(
        self,
        regions,
        use_foreground: bool,
        timeout_s: Optional[float] = None,
        deadline: Optional[float] = None,
    ) -> bool:
        """等待PetItem或第一次灰变蓝，然后使用第一回合技能
        
        Args:
            regions: 区域存储
            use_foreground: 是否前台运行
            timeout_s: 超时时间（秒），如果为None表示不按本参数限时（仍受 deadline 约束）
            deadline: 若设置，超过该时间戳则返回 False（用于日常链大乱斗单场总时限）
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
        
        if timeout_s is not None:
            loop_end = start_time + timeout_s
        else:
            loop_end = float("inf")
        if deadline is not None:
            loop_end = min(loop_end, deadline)
        
        while time.time() < loop_end:
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
        
        # 超过 loop_end 仍未成功
        if deadline is not None and time.time() >= deadline:
            self._emit("⏱️ 等待PetItem或首回合：已超过大乱斗单场时限", "ERROR")
            return False
        if timeout_s is not None:
            self._emit(f"⏱️ 等待PetItem或灰变蓝超时（{timeout_s}秒），放弃检测继续下一步", "WARN")
            return True
        self._emit("⏱️ 等待PetItem或灰变蓝超时（未知原因），放弃检测继续下一步", "WARN")
        return True
    
    def _run_chaos_battle_loop(
        self,
        regions,
        use_foreground: bool,
        cleaner,
        is_chaos: bool = True,
        deadline: Optional[float] = None,
    ) -> bool:
        """执行战斗循环
        
        Args:
            regions: 区域存储
            use_foreground: 是否前台运行
            cleaner: PostBattleCleaner实例
            is_chaos: 是否为大乱斗模式（True=大乱斗，False=1v1）
            deadline: 日常链大乱斗单场总时限（时间戳），超时则返回 False
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
            if deadline is not None and time.time() >= deadline:
                self._emit("⏱️ 战斗循环：已超过大乱斗单场时限", "ERROR")
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
    
    def _detect_victory_probe_yellow_or_white(
        self,
        cleaner,
        use_foreground: bool,
        timeout_s: float = 8.0,
        deadline: Optional[float] = None,
    ) -> bool:
        """检测胜利探针（黄色或白色FFFFFF）
        
        注意：此方法仅用于大乱斗x2和1v1x2模式
        训练室和勇者之塔仍使用只检测黄色的detect_victory_probe_yellow方法
        
        支持的探针颜色：
        - 黄色（通过cleaner.detect_victory_probe_yellow检测）
        - 白色（FFFFFF，RGB值都>=245）
        """
        result = self._detect_victory_probe_result(
            cleaner, use_foreground, timeout_s, deadline=deadline
        )
        return result in ("yellow", "white")

    def _detect_victory_probe_result(
        self,
        cleaner,
        use_foreground: bool,
        timeout_s: float = 8.0,
        deadline: Optional[float] = None,
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
                if deadline is not None and time.time() >= deadline:
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
    def run_1v1_x2(
        self,
        use_foreground: bool = True,
        *,
        battle_timeout_s: Optional[float] = None,
    ) -> bool:
        """执行1v1x2：两次特殊战斗循环（包含恢复）"""
        if battle_timeout_s is None:
            battle_timeout_s = float(DAILY_1V1_SINGLE_BATTLE_TIMEOUT_S)
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
            battle_deadline = time.time() + float(battle_timeout_s)
            self._emit(
                f"⏱ 本场限时：自入战流程起 {int(battle_timeout_s) // 60} 分钟内须结束",
                "INFO",
            )
            
            # 第一场：直接移动1v1；第二场：直接点击精灵王之战
            if battle_num == 0:
                self._emit("🖱 点击：勇者之塔.移动1v1", "INFO")
                if not self._click_region_safe(regions, "勇者之塔.移动1v1", use_foreground):
                    return False
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
            
            # 8. 等待PetItem并执行第一回合技能
            self._emit("⏳ 等待PetItem进入对战…", "INFO")
            if not self._wait_for_petitem_and_first_skill(
                regions,
                use_foreground,
                timeout_s=None,
                deadline=battle_deadline,
            ):
                self._emit("❌ 等待PetItem或第一回合失败（超时或中止）", "ERROR")
                return False
            
            # 9. 执行战斗循环（1v1模式，不需要灰色期间点击）
            if not self._run_chaos_battle_loop(
                regions, use_foreground, cleaner, is_chaos=False, deadline=battle_deadline
            ):
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
                    self._emit("❌ 恢复精灵一失败，交给模式外层重连重启", "ERROR")
                    return False
        
        # 第二场战斗后再次恢复精灵一
        self._emit("🩹 恢复精灵一（任务结束前）...", "INFO")
        if not self._recover_pet_one(regions, use_foreground):
            self._emit("❌ 恢复精灵一失败，交给模式外层重连重启", "ERROR")
            return False
        
        self._emit("✅ 1v1x2：2场对战全部完成", "SUCCESS")
        return True
    
    def _recover_pet_one(
        self,
        regions,
        use_foreground: bool,
        *,
        bag_already_open: bool = False,
        log_tag: str = "恢复精灵一",
    ) -> bool:
        """恢复精灵一：打开背包 -> 双击精灵一 -> 点击恢复 -> 1AND1确认 -> 关闭背包。"""
        # ✅ 使用正确的region keys（和野外稀有精灵模式一致）
        bag_open_key = "精灵背包.打开精灵背包"
        pet_one_key = "精灵背包.精灵一"
        recover_key = "精灵背包.精灵恢复"
        
        # 检查统一框架是否可用（用于1AND1确认）
        if self._unified_framework is None:
            self._emit("❌ 恢复精灵一：缺少unified_framework，无法执行1AND1确认", "ERROR")
            return False
        
        try:
            if bag_already_open:
                self._emit(f"💼 [{log_tag}] 背包已打开，跳过打开精灵背包", "INFO")
            else:
                # 1. 打开精灵背包（参考野外稀有精灵模式）
                self._emit("💼 打开精灵背包", "INFO")
                if not self._click_region_safe(regions, bag_open_key, use_foreground):
                    return False
            if not wait_pet_bag_ui_ready_after_open(
                regions,
                emit_fn=self._emit,
                stop_check=self._should_abort,
                log_tag=log_tag,
            ):
                self._emit(f"❌ [{log_tag}] 背包UI未就绪，停止恢复精灵一", "ERROR")
                self._request_outer_mode_restart(f"{log_tag}-背包UI未就绪")
                return False
            
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
            # 使用2秒超时（参考野外模式）
            self._unified_framework._wait_for_confirm_probes(temp_config, timeout_s=2.0)
            
            self._emit("💼 扫描完成后，点击打开精灵背包关闭它", "INFO")
            if not self._click_region_safe(regions, bag_open_key, use_foreground):
                return False
            time.sleep(0.5)
            
            self._emit(f"✅ [{log_tag}] 恢复精灵一完成", "SUCCESS")
            return True
        except Exception as e:
            self._emit(f"❌ [{log_tag}] 恢复精灵一异常: {e}", "ERROR")
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
            if not wait_pet_bag_ui_ready_after_open(
                regions,
                emit_fn=self._emit,
                stop_check=self._should_abort,
                log_tag="特训恢复",
            ):
                self._emit("❌ [特训恢复] 背包UI未就绪，停止恢复", "ERROR")
                self._request_outer_mode_restart("特训恢复-背包UI未就绪")
                return False

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
        """雷伊特训 / 嘟嘟卡拉 / 劳克蒙德。
        training_battle_mode:
          - \"leiyi\": loop_count 由输入框（1–999）；特训.1/2；战斗 4→2→1→3；白失败点特训.3 再恢复；黄探针胜利结束。
          - \"dudukala\": 无限循环直至黄探针胜利或 stop；嘟嘟卡拉1/2 入战；战斗每回合仅技能一；
            退场以「最近一次出手之后」kernel 的 map 信号为准；白失败不点特训.3，直接恢复；loop_count 忽略。
          - \"laokemengde\": 劳克蒙德.1→2→3 循环入战；第一回合技能二，第二回合技能四；
            达到 loop_count 或黄色探针时结束。"""
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
        if mode not in ("leiyi", "dudukala", "laokemengde"):
            mode = "leiyi"

        if mode == "dudukala":
            self._emit(
                "🎪 嘟嘟卡拉：无限循环，仅在黄色胜利探针时结束（或点停止）；"
                "每场每回合技能一；每次出手后重认 kernel map+newNpc；白探针则恢复再继续；无特训.3",
                "SYSTEM",
            )
            loop_count = 0  # unused
        elif mode == "laokemengde":
            loop_count = max(1, min(999, loop_count))
            self._emit(
                f"🦬 劳克蒙德：最多 {loop_count} 次循环；1→2→3 循环入战；"
                "第一回合技能二、第二回合技能四；黄色探针提前结束",
                "SYSTEM",
            )
        else:
            loop_count = max(1, min(999, loop_count))
            self._emit(f"⚡ 雷伊特训：最多 {loop_count} 次循环（黄=胜利退出，白=失败恢复）", "SYSTEM")

        entry_key = "特训.嘟嘟卡拉1" if mode == "dudukala" else "特训.1"
        trigger_key = "特训.嘟嘟卡拉2" if mode == "dudukala" else "特训.2"
        label = {
            "leiyi": "雷伊特训",
            "dudukala": "嘟嘟卡拉",
            "laokemengde": "劳克蒙德",
        }[mode]

        def _do_single_training_round(loop_display: str) -> str:
            """返回 \"yellow_win\" | \"white_retry\" | \"fatal\" | \"aborted\""""
            self._emit(loop_display, "SYSTEM")

            if mode == "laokemengde":
                from core.logger import kernel_cursor

                start_cursor = kernel_cursor()
                click_stop = threading.Event()
                click_failed = threading.Event()
                entry_keys = ("劳克蒙德.1", "劳克蒙德.2", "劳克蒙德.3")

                def _entry_click_loop() -> None:
                    cycle_idx = 0
                    while not click_stop.is_set() and not self._should_abort():
                        cycle_idx += 1
                        self._emit(
                            f"🦬 劳克蒙德入战点击循环 {cycle_idx}：1→2→3",
                            "INFO" if cycle_idx == 1 else "DEBUG",
                        )
                        for key in entry_keys:
                            if click_stop.is_set() or self._should_abort():
                                return
                            try:
                                if self._unified_framework._check_calibration_probes():
                                    self._emit(
                                        "🧭 劳克蒙德入战触发校准，停止 1→2→3 外层点击并交给 Stage2",
                                        "INFO",
                                    )
                                    click_stop.set()
                                    return
                            except Exception:
                                pass
                            if not self._click_region_safe(regions, key, use_foreground):
                                click_failed.set()
                                click_stop.set()
                                return
                            wait_started = time.time()
                            while time.time() - wait_started < 0.25:
                                if click_stop.is_set() or self._should_abort():
                                    return
                                time.sleep(0.05)

                self._emit("⏳ 劳克蒙德：循环点击 1→2→3，等待 PetItem 入战...", "INFO")
                click_thread = threading.Thread(target=_entry_click_loop, daemon=True)
                click_thread.start()
                try:
                    success, _ = self._unified_framework.stage2_calibration_and_petitem(
                        trigger_callback=None,
                        use_foreground=use_foreground,
                        timeout_s=60.0,
                        skip_stage1=True,
                        initial_cursor=start_cursor,
                    )
                finally:
                    click_stop.set()
                    click_thread.join(timeout=1.0)
                if click_failed.is_set():
                    self._emit("❌ 劳克蒙德 1→2→3 入战点击失败", "ERROR")
                    return "fatal"
            else:
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
            skill_sequence = (2, 4) if mode == "laokemengde" else None
            round_at_end = self._run_leiyi_battle_loop(
                regions,
                use_foreground,
                repeat_skill=rs,
                skill_sequence=skill_sequence,
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
                self._emit("❌ 恢复精灵一失败，交给模式外层重连重启", "ERROR")
                return "fatal"
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

        # 雷伊 / 劳克蒙德：固定次数循环
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

        self._emit(f"✅ {label}：已完成 {loop_count} 次循环", "SUCCESS")
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
        skill_sequence: Optional[Sequence[int]] = None,
        max_skill_uses: Optional[int] = None,
    ) -> Optional[int]:
        """雷伊特训战斗循环：默认技能顺序 4→2→1→3。
        repeat_skill 若指定（如 1）：每检测到可出手则点该技能；出手后以带数字 id 的 map swf（如 path=resource\\map\\429.swf）
        判定退场即可，无需 newNpc。skill_sequence 若指定则逐回合使用该技能序列，并同样以 map swf 判定退场。
        雷伊特训（两者均未指定）：仍为 map + newNpc。
        返回战斗结束时的回合计（内部计数），None 表示失败/中止。"""
        from core.logger import fetch_kernel_since, kernel_cursor

        battle_runner = getattr(self.bot, "battle_runner", None)
        if battle_runner is None:
            self._emit("❌ 缺少 battle_runner，无法执行战斗循环", "ERROR")
            return None

        probe_model = battle_runner._load_probe_templates()
        custom_skill_sequence = skill_sequence is not None
        skill_order = [4, 2, 1, 3]
        if custom_skill_sequence:
            skill_order = [int(skill) for skill in skill_sequence or () if int(skill) in (1, 2, 3, 4)]
            if not skill_order:
                self._emit("❌ 自定义技能序列为空或无效", "ERROR")
                return None
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
        elif custom_skill_sequence:
            sequence_text = "→".join(str(skill) for skill in skill_order)
            self._emit(f"⚔️ 劳克蒙德战斗循环：技能顺序 {sequence_text}", "INFO")
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
                        if (repeat_skill is not None or custom_skill_sequence) and reload_map_swf_seen:
                            extra = f" id={reload_map_mid}" if reload_map_mid is not None else ""
                            self._emit(
                                f"🏁 战斗结束（退场 resource/map/*.swf{extra}，第 {round_idx} 回合后）",
                                "SUCCESS",
                            )
                            return round_idx
                        if repeat_skill is None and not custom_skill_sequence and map_seen and npc_seen:
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

    def run_single_script(
        self,
        script_name: str,
        bg_mode: bool = True,
        stop_event: Optional[Any] = None,
    ) -> bool:
        script_path = self._resolve_script_path(script_name)
        if not script_path:
            self._emit(f"❌ 脚本不存在: {script_name}", "ERROR")
            return False
        return self.run_script(
            script_path,
            bg_override=bg_mode,
            stop_event=stop_event,
        )

    # ----------------------------
    # 核心执行器
    # ----------------------------
    def run_script(
        self,
        script_path: str,
        bg_override: Optional[bool] = None,
        stop_event: Optional[Any] = None,
    ) -> bool:
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
            window_manager.move_cancel()

            for idx, step in enumerate(steps, start=1):
                abort_reason = self._abort_reason(stop_event)
                if abort_reason is not None:
                    window_manager.move_cancel()
                    self._emit(f"⛔ 脚本中止（{abort_reason}）", "SYSTEM")
                    return False

                self._wait_if_paused()

                action = str(step.get("action") or "click").strip().lower()
                if action == "move_start":
                    if not self._execute_script_move_start_step(step, idx, bg_override):
                        continue
                    continue
                if action == "move_end":
                    if not self._execute_script_move_end_step(step, idx, bg_override):
                        continue
                    continue

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

            window_manager.move_cancel()
            self._emit(f"✅ 脚本完成: {os.path.basename(script_path)}", "SUCCESS")
            return True

        except Exception as e:
            window_manager.move_cancel()
            logger.error(f"读取或执行脚本异常: {e}")
            self._emit(f"💥 脚本执行异常: {e}", "ERROR")
            return False

    def _script_step_bg(self, step: Dict[str, Any], bg_override: Optional[bool]) -> bool:
        if bg_override is None:
            return bool(step.get("bg", True))
        return bool(bg_override)

    def _execute_script_move_start_step(
        self, step: Dict[str, Any], idx: int, bg_override: Optional[bool] = None
    ) -> bool:
        gx, gy = self._parse_step_xy(step)
        if gx is None or gy is None:
            self._emit(f"⚠ [步骤 {idx}] move_start 坐标无效，已跳过: {step}", "WARN")
            return False

        delay = float(step.get("delay", 0.0))
        if delay < 0:
            delay = 0.0
        bg = self._script_step_bg(step, bg_override)
        mode_text = "后台" if bg else "前台"

        self._emit(
            f"✅ [步骤 {idx}] move_start: ({int(gx)}, {int(gy)})"
            f" | delay {delay:.2f}s | 模式: {mode_text}",
            "DEBUG",
        )
        time.sleep(delay)
        ok = window_manager.move_start(gx, gy, foreground=not bg)
        if not ok:
            self._emit(f"⚠ [步骤 {idx}] move_start 执行失败", "WARN")
        return ok

    def _execute_script_move_end_step(
        self, step: Dict[str, Any], idx: int, bg_override: Optional[bool] = None
    ) -> bool:
        gx, gy = self._parse_step_xy(step)
        if gx is None or gy is None:
            self._emit(f"⚠ [步骤 {idx}] move_end 坐标无效，已跳过: {step}", "WARN")
            return False

        duration = float(step.get("duration", 0.2))
        if duration < 0:
            duration = 0.0
        bg = self._script_step_bg(step, bg_override)
        mode_text = "后台" if bg else "前台"

        self._emit(
            f"✅ [步骤 {idx}] move_end: ({int(gx)}, {int(gy)})"
            f" | duration {duration:.2f}s | 模式: {mode_text}",
            "DEBUG",
        )
        ok = window_manager.move_end(
            gx,
            gy,
            duration,
            foreground=not bg,
            abort_check=self._should_abort,
        )
        if not ok:
            self._emit(f"⚠ [步骤 {idx}] move_end 执行失败（需先 move_start）", "WARN")
        return ok

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

    # ----------------------------
    # 挖矿（苏克探针）
    # ----------------------------
    def _probe_match_region(
        self,
        regions,
        key: str,
        target_rgb: Tuple[int, int, int],
        *,
        tol: int = 20,
        min_ratio: float = 0.55,
    ) -> bool:
        try:
            from core.region_store import Region

            reg = regions.get(key)
            if reg is None:
                return False
            if not isinstance(reg, Region):
                return False
            gx1, gy1, gx2, gy2 = reg.outer_bbox()
            img = window_manager.grab_game_bbox(gx1, gy1, gx2, gy2)
            if img is None:
                return False
            pixels = list(img.convert("RGB").getdata())
            if not pixels:
                return False
            tr, tg, tb = target_rgb
            ok = 0
            for r, g, b in pixels:
                if abs(r - tr) <= tol and abs(g - tg) <= tol and abs(b - tb) <= tol:
                    ok += 1
            return (ok / len(pixels)) >= min_ratio
        except Exception:
            return False

    def _suke_bw_probes_ready(self, regions) -> bool:
        """苏克黑色探针≈纯黑 且 苏克白色探针≈纯白。"""
        black_ok = self._probe_match_region(
            regions,
            MINING_SUKE_BLACK_KEY,
            (0, 0, 0),
            tol=35,
            min_ratio=0.55,
        )
        white_ok = self._probe_match_region(
            regions,
            MINING_SUKE_WHITE_KEY,
            (255, 255, 255),
            tol=18,
            min_ratio=0.6,
        )
        return black_ok and white_ok

    def _wait_suke_bw_probes(
        self,
        regions,
        *,
        timeout_s: float = MINING_SUKE_WAIT_TIMEOUT_SEC,
        log_tag: str = "挖矿",
    ) -> bool:
        self._emit(
            f"⏳ [{log_tag}] 等待苏克黑探针纯黑 + 白探针纯白…",
            "INFO",
        )
        t0 = time.time()
        while (time.time() - t0) < timeout_s:
            if self._should_abort():
                return False
            self._wait_if_paused()
            if self._suke_bw_probes_ready(regions):
                self._emit(f"✅ [{log_tag}] 苏克黑+白探针就绪", "SUCCESS")
                return True
            time.sleep(MINING_PROBE_POLL_SEC)
        self._emit(f"❌ [{log_tag}] 等待苏克探针超时（{timeout_s:.0f}s）", "ERROR")
        return False

    def _ensure_unified_framework(self, regions) -> bool:
        if self._unified_framework is not None:
            return True
        try:
            from config import TEMPLATES_PATH
        except Exception:
            TEMPLATES_PATH = os.path.join(BASE_PATH, "assets", "templates")
        try:
            self._unified_framework = UnifiedBattleFramework(
                self.bot, regions, TEMPLATES_PATH
            )
            return True
        except Exception as e:
            self._emit(f"❌ 初始化 UnifiedBattleFramework 失败: {e}", "ERROR")
            return False

    def _wait_1and1_clear(
        self,
        regions,
        use_foreground: bool,
        *,
        timeout_s: float = MINING_1AND1_TIMEOUT_SEC,
        min_confirm_clicks: int = 1,
        log_tag: str = "挖矿",
        on_first_detected: Optional[Callable[[], None]] = None,
        quiet: bool = False,
    ) -> bool:
        if not self._ensure_unified_framework(regions):
            return False
        min_clicks = max(1, int(min_confirm_clicks or 1))
        if min_clicks > 1 and not quiet:
            self._emit(
                f"⏳ [{log_tag}] 普通 1AND1 直到消失（至少探测点击 {min_clicks} 次）…",
                "INFO",
            )
        elif not quiet:
            self._emit(f"⏳ [{log_tag}] 普通 1AND1 直到消失…", "INFO")

        def abort_check():
            return self._should_abort()

        config = BattleConfig(
            mode=BattleMode.FIXED,
            use_foreground=use_foreground,
            abort_check=abort_check,
        )
        try:
            ok = self._unified_framework._wait_for_confirm_probes(
                config,
                timeout_s=timeout_s,
                min_confirm_clicks=min_clicks,
                on_first_detected=on_first_detected,
                quiet=quiet,
            )
            if ok:
                if not quiet:
                    self._emit(f"✅ [{log_tag}] 1AND1 已处理", "SUCCESS")
            else:
                self._emit(f"❌ [{log_tag}] 1AND1 未在时限内清完", "ERROR")
            return ok
        except Exception as e:
            self._emit(f"⚠️ [{log_tag}] 1AND1 清理异常: {e}", "WARN")
            return False

    def _clear_1and1_if_present(
        self,
        regions,
        use_foreground: bool,
        *,
        timeout_s: float = 4.0,
        log_tag: str = "1AND1清理",
    ) -> bool:
        """短窗口清理普通1AND1：出现则点到消失，没出现也算通过。"""
        if not self._ensure_unified_framework(regions):
            return False
        t0 = time.time()
        saw = False
        last_rgb_log = 0.0

        def _rgb_state() -> str:
            return (
                f"通用探针={mean_rgb_for_region_key(regions, self._unified_framework.KEY_GENERAL_PROBE)}, "
                f"普通确认探针={mean_rgb_for_region_key(regions, self._unified_framework.KEY_NORMAL_CONFIRM_PROBE)}, "
                f"普通确认={mean_rgb_for_region_key(regions, '对话框.普通确认')}, "
                f"仓库关闭={mean_rgb_for_region_key(regions, '精灵仓库.关闭')}"
            )

        self._emit(f"⏳ [{log_tag}] 短窗口检查普通1AND1，有则清理；RGB：{_rgb_state()}", "INFO")
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            ok_white, ok_blue = self._unified_framework._check_probe_pair(
                self._unified_framework.KEY_GENERAL_PROBE,
                (255, 255, 255),
                self._unified_framework.KEY_NORMAL_CONFIRM_PROBE,
                (47, 167, 238),
                tolerance=5,
            )
            now = time.time()
            if now - last_rgb_log >= 0.5:
                last_rgb_log = now
                self._emit(
                    f"🔎 [{log_tag}] 1AND1状态 white={ok_white} blue={ok_blue} saw={saw}；RGB：{_rgb_state()}",
                    "DEBUG",
                )
            if ok_white and ok_blue:
                saw = True
                if not self._click_region_safe(regions, "对话框.普通确认", use_foreground):
                    return False
                time.sleep(0.12)
                continue
            if saw:
                self._emit(f"✅ [{log_tag}] 1AND1 已消失；RGB：{_rgb_state()}", "SUCCESS")
                return True
            time.sleep(0.08)
        if saw:
            self._emit(f"❌ [{log_tag}] 1AND1 未在短窗口内清完；最终RGB：{_rgb_state()}", "ERROR")
            return False
        self._emit(f"✅ [{log_tag}] 未检测到1AND1，继续；最终RGB：{_rgb_state()}", "INFO")
        return True

    def _wait_left_1and1_clear(
        self,
        regions,
        use_foreground: bool,
        *,
        timeout_s: float = MINING_1AND1_TIMEOUT_SEC,
        min_confirm_clicks: int = 1,
        log_tag: str = "左边1AND1",
    ) -> bool:
        """左边1AND1：检测逻辑同普通1AND1，但点击「对话框.左边确认」。"""
        if not self._ensure_unified_framework(regions):
            return False
        min_clicks = max(1, int(min_confirm_clicks or 1))
        t0 = time.time()
        saw_1and1 = False
        confirm_click_count = 0
        click_interval = 0.1
        self._emit(f"⏳ [{log_tag}] 左边1AND1 直到消失…", "INFO")

        def _probe_pair_ok() -> bool:
            ok_white, ok_blue = self._unified_framework._check_probe_pair(
                self._unified_framework.KEY_GENERAL_PROBE,
                (255, 255, 255),
                self._unified_framework.KEY_NORMAL_CONFIRM_PROBE,
                (47, 167, 238),
                tolerance=5,
            )
            return bool(ok_white and ok_blue)

        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            if _probe_pair_ok():
                saw_1and1 = True
                if not self._click_region_safe(regions, "对话框.左边确认", use_foreground):
                    return False
                confirm_click_count += 1
                time.sleep(click_interval)
                continue
            if saw_1and1 and confirm_click_count >= min_clicks:
                self._emit(
                    f"✅ [{log_tag}] 左边1AND1 已处理（点击 {confirm_click_count} 次）",
                    "SUCCESS",
                )
                return True
            time.sleep(0.05)

        if saw_1and1:
            self._emit(
                f"❌ [{log_tag}] 左边1AND1 未在时限内清完（点击 {confirm_click_count}/{min_clicks} 次）",
                "ERROR",
            )
        else:
            self._emit(f"❌ [{log_tag}] {timeout_s:.0f}s 内未检测到左边1AND1", "ERROR")
        return False

    @staticmethod
    def _honor_rgb_is_white(rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        return r >= 245 and g >= 245 and b >= 245

    @staticmethod
    def _honor_rgb_is_silver_gray(rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        return min(r, g, b) >= 170 and (max(r, g, b) - min(r, g, b)) <= 18

    @staticmethod
    def _honor_rgb_is_blueish(rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        return r >= 20 and g >= 40 and b >= 90 and b >= r + 20 and b >= g + 15

    @staticmethod
    def _honor_rgb_is_pure_black(rgb: Optional[Tuple[int, int, int]]) -> bool:
        return rgb == (0, 0, 0)

    def _wait_region_honor_white(
        self,
        regions,
        key: str,
        *,
        timeout_s: float,
        log_tag: str,
    ) -> bool:
        t0 = time.time()
        last_log = 0.0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            rgb = mean_rgb_for_region_key(regions, key)
            if self._honor_rgb_is_white(rgb):
                self._emit(f"✅ [{log_tag}] {key} 已变白：RGB={rgb}", "SUCCESS")
                return True
            now = time.time()
            if now - last_log >= 1.0:
                self._emit(f"⏳ [{log_tag}] 等待 {key} 变白，当前 RGB={rgb}", "DEBUG")
                last_log = now
            time.sleep(0.08)
        rgb = mean_rgb_for_region_key(regions, key)
        self._emit(f"❌ [{log_tag}] 等待 {key} 变白超时，当前 RGB={rgb}", "ERROR")
        return False

    def _wait_region_honor_pure_black(
        self,
        regions,
        key: str,
        *,
        timeout_s: float,
        log_tag: str,
    ) -> bool:
        t0 = time.time()
        last_log = 0.0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            rgb = mean_rgb_for_region_key(regions, key)
            if self._honor_rgb_is_pure_black(rgb):
                self._emit(f"✅ [{log_tag}] {key} 已变纯黑 000：RGB={rgb}", "SUCCESS")
                return True
            now = time.time()
            if now - last_log >= 1.0:
                self._emit(f"⏳ [{log_tag}] 等待 {key} 变纯黑 000，当前 RGB={rgb}", "DEBUG")
                last_log = now
            time.sleep(0.08)
        rgb = mean_rgb_for_region_key(regions, key)
        self._emit(f"❌ [{log_tag}] 等待 {key} 变纯黑 000 超时，当前 RGB={rgb}", "ERROR")
        return False

    def _wait_honor_right_state_by_clicking(
        self,
        regions,
        click_key: str,
        state_name: str,
        predicate: Callable[[Optional[Tuple[int, int, int]]], bool],
        use_foreground: bool,
        *,
        timeout_s: float,
        log_tag: str,
    ) -> bool:
        t0 = time.time()
        attempts = 0
        last_log = 0.0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            rgb = mean_rgb_for_region_key(regions, "荣誉兑换.右")
            if predicate(rgb):
                self._emit(
                    f"✅ [{log_tag}] 荣誉兑换.右 已到 {state_name}：RGB={rgb}，点击 {attempts} 次",
                    "SUCCESS",
                )
                return True
            attempts += 1
            now = time.time()
            if now - last_log >= 1.0:
                self._emit(
                    f"⏳ [{log_tag}] 点击 {click_key} 等待右侧变 {state_name}，当前 RGB={rgb}，尝试 {attempts}",
                    "DEBUG",
                )
                last_log = now
            if not self._click_region_safe(regions, click_key, use_foreground):
                return False
            time.sleep(0.18)
        rgb = mean_rgb_for_region_key(regions, "荣誉兑换.右")
        self._emit(f"❌ [{log_tag}] 等待右侧变 {state_name} 超时，当前 RGB={rgb}", "ERROR")
        return False

    def _honor_left_1and1_present(self, regions) -> bool:
        white_rgb = mean_rgb_for_region_key(regions, "荣誉兑换.白色探针")
        blue_rgb = mean_rgb_for_region_key(regions, "荣誉兑换.蓝色探针")
        return (
            self._honor_rgb_is_white(white_rgb)
            and self._honor_rgb_is_blueish(blue_rgb)
        )

    def _wait_honor_left_1and1_clear(
        self,
        regions,
        use_foreground: bool,
        *,
        timeout_s: float = 20.0,
        log_tag: str = "荣誉兑换·左边1AND1",
    ) -> bool:
        t0 = time.time()
        saw = False
        clicks = 0
        last_log = 0.0
        self._emit(f"⏳ [{log_tag}] 等待荣誉左边1AND1并点击到消失", "INFO")
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            present = self._honor_left_1and1_present(regions)
            now = time.time()
            if now - last_log >= 1.0:
                state = {
                    "白": mean_rgb_for_region_key(regions, "荣誉兑换.白色探针"),
                    "蓝": mean_rgb_for_region_key(regions, "荣誉兑换.蓝色探针"),
                    "左": mean_rgb_for_region_key(regions, "荣誉兑换.左边确认"),
                }
                self._emit(f"🔎 [{log_tag}] present={present}, RGB={state}", "DEBUG")
                last_log = now
            if present:
                saw = True
                if not self._click_region_safe(regions, "荣誉兑换.左边确认", use_foreground):
                    return False
                clicks += 1
                time.sleep(0.10)
                continue
            if saw and clicks >= 1:
                self._emit(f"✅ [{log_tag}] 已处理（点击 {clicks} 次）", "SUCCESS")
                return True
            time.sleep(0.06)
        if saw:
            self._emit(f"❌ [{log_tag}] 已出现但未在时限内消失（点击 {clicks} 次）", "ERROR")
        else:
            self._emit(f"❌ [{log_tag}] {timeout_s:.0f}s 内未出现", "ERROR")
        return False

    def _click_honor_chip_until_left_1and1(
        self,
        regions,
        use_foreground: bool,
        *,
        timeout_s: float = 20.0,
        log_tag: str = "荣誉兑换",
    ) -> bool:
        t0 = time.time()
        attempts = 0
        last_log = 0.0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            if self._honor_left_1and1_present(regions):
                return self._wait_honor_left_1and1_clear(
                    regions,
                    use_foreground,
                    timeout_s=timeout_s,
                    log_tag=f"{log_tag}·左边1AND1",
                )
            attempts += 1
            now = time.time()
            if now - last_log >= 1.0:
                state = {
                    "白": mean_rgb_for_region_key(regions, "荣誉兑换.白色探针"),
                    "蓝": mean_rgb_for_region_key(regions, "荣誉兑换.蓝色探针"),
                    "左": mean_rgb_for_region_key(regions, "荣誉兑换.左边确认"),
                }
                self._emit(
                    f"🖱️ [{log_tag}] 点击芯片等待荣誉左边1AND1（第 {attempts} 次），RGB={state}",
                    "DEBUG",
                )
                last_log = now
            if not self._click_region_safe(regions, "荣誉兑换.芯片", use_foreground):
                return False
            time.sleep(0.18)
        self._emit(f"❌ [{log_tag}] 点击芯片后未等到荣誉左边1AND1", "ERROR")
        return False

    def run_honor_exchange_mode(self, use_foreground: bool = False) -> bool:
        """荣誉兑换前置扭蛋入口，兑换完成后执行 99999 轮扭蛋。"""
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口", "ERROR")
            return False
        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ 荣誉兑换缺少 bot.regions", "ERROR")
            return False
        required = (
            HONOR_EXCHANGE_TO_GACHA_KEY,
            "荣誉兑换.打开",
            "荣誉兑换.白色探针零",
            "荣誉兑换.白色探针",
            "荣誉兑换.其他",
            "荣誉兑换.右",
            "荣誉兑换.芯片",
            "荣誉兑换.蓝色探针",
            "荣誉兑换.左边确认",
            "荣誉兑换.关闭",
            "对话框.通用探针",
            "对话框.普通确认探针",
            "对话框.普通确认",
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ 荣誉兑换缺少区域：{key}", "ERROR")
                return False

        tag = "荣誉兑换"
        self._emit(
            f"📋 [{tag}] to扭蛋→打开→白色探针零变白→其他→右侧银灰/偏蓝门控→"
            f"芯片→左边1AND1→普通1AND1→关闭→扭蛋{HONOR_EXCHANGE_GACHA_TIMES}次",
            "SYSTEM",
        )
        if not self._ensure_unified_framework(regions):
            return False
        self._emit(f"🎲 [{tag}] 点击 {HONOR_EXCHANGE_TO_GACHA_KEY}", "SYSTEM")
        if not self._click_region_safe(
            regions,
            HONOR_EXCHANGE_TO_GACHA_KEY,
            use_foreground,
        ):
            return False
        if not self._click_region_safe(regions, "荣誉兑换.打开", use_foreground):
            return False
        if not self._wait_region_honor_white(
            regions,
            "荣誉兑换.白色探针零",
            timeout_s=12.0,
            log_tag=f"{tag}·打开",
        ):
            return False
        if not self._wait_honor_right_state_by_clicking(
            regions,
            "荣誉兑换.其他",
            "银灰色",
            self._honor_rgb_is_silver_gray,
            use_foreground,
            timeout_s=12.0,
            log_tag=f"{tag}·其他",
        ):
            return False
        if not self._wait_honor_right_state_by_clicking(
            regions,
            "荣誉兑换.右",
            "偏蓝色",
            self._honor_rgb_is_blueish,
            use_foreground,
            timeout_s=20.0,
            log_tag=f"{tag}·右翻",
        ):
            return False
        if not self._click_honor_chip_until_left_1and1(
            regions,
            use_foreground,
            timeout_s=20.0,
            log_tag=tag,
        ):
            return False
        if not self._wait_1and1_clear(
            regions,
            use_foreground,
            timeout_s=20.0,
            min_confirm_clicks=1,
            log_tag=f"{tag}·普通1AND1",
        ):
            return False
        if not self._click_region_safe(regions, "荣誉兑换.关闭", use_foreground):
            return False
        time.sleep(0.3)
        self._emit(
            f"🎲 [{tag}] 兑换完成，开始扭蛋 {HONOR_EXCHANGE_GACHA_TIMES} 次",
            "SYSTEM",
        )
        if not self.run_gacha_probe_test(
            times=HONOR_EXCHANGE_GACHA_TIMES,
            background_mode=(not use_foreground),
        ):
            return False
        self._emit(f"✅ [{tag}] 兑换及扭蛋全部完成", "SUCCESS")
        return True

    @staticmethod
    def _chip_gold_probe_is_white(rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        return all(int(channel) >= 245 for channel in rgb)

    def _chip_gold_open_map_until_white(
        self,
        regions,
        use_foreground: bool,
        *,
        timeout_s: float = 12.0,
        log_tag: str = "一键周常",
    ) -> bool:
        """持续点击地图入口，直到 (304, 504) 的地图白色探针变白。"""
        self._emit(
            f"🗺️ [{log_tag}] 点击 地图.地图，等待 {CHIP_GOLD_MAP_WHITE_PROBE_KEY} 变白",
            "INFO",
        )
        t0 = time.time()
        last_click = 0.0
        last_log = 0.0
        click_count = 0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            now = time.time()
            if now - last_click >= 0.3:
                if not self._click_region_safe(regions, "地图.地图", use_foreground):
                    return False
                click_count += 1
                last_click = now
                time.sleep(0.08)
            rgb = mean_rgb_for_region_key(regions, CHIP_GOLD_MAP_WHITE_PROBE_KEY)
            if click_count > 0 and self._chip_gold_probe_is_white(rgb):
                self._emit(
                    f"✅ [{log_tag}] 地图已打开：{CHIP_GOLD_MAP_WHITE_PROBE_KEY} RGB={rgb}",
                    "SUCCESS",
                )
                return True
            if now - last_log >= 1.0:
                self._emit(
                    f"🔍 [{log_tag}] 等待地图白色探针：RGB={rgb}，点击={click_count}",
                    "DEBUG",
                )
                last_log = now
            time.sleep(0.04)
        rgb = mean_rgb_for_region_key(regions, CHIP_GOLD_MAP_WHITE_PROBE_KEY)
        self._emit(
            f"❌ [{log_tag}] 地图白色探针未变白：RGB={rgb}，点击={click_count}",
            "ERROR",
        )
        return False

    def _chip_gold_enter_map(
        self,
        regions,
        use_foreground: bool,
        *,
        target_key: str,
        map_id: int,
        newnpc_id: int,
        timeout_s: float = 25.0,
        log_tag: str = "一键周常",
    ) -> bool:
        """点击地图区域，等待对应的 map 与 newNpc 数字双门控。"""
        from core.logger import fetch_kernel_since, kernel_cursor

        npc_re = re.compile(
            rf"(?:resource[\\/]newNpc[\\/]multi[\\/]|^newNpc[\\/]multi[\\/]){int(newnpc_id)}\.swf",
            re.IGNORECASE,
        )
        cursor = kernel_cursor()
        t0 = time.time()
        last_click = 0.0
        last_log = 0.0
        map_seen = False
        gate_seen = False
        click_count = 0
        gate_name = f"newNpc/multi/{newnpc_id}.swf"
        self._emit(
            f"🖱️ [{log_tag}] 点击 {target_key}，等待 map{map_id} + {gate_name}",
            "INFO",
        )
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            now = time.time()
            if not map_seen and now - last_click >= 0.35:
                if not self._click_region_safe(regions, target_key, use_foreground):
                    return False
                click_count += 1
                last_click = now
            try:
                lines = fetch_kernel_since(cursor)
                if isinstance(lines, list):
                    for line in lines:
                        line_str = str(line)
                        if first_map_id_in_line(line_str) == int(map_id):
                            map_seen = True
                        if line_matches(npc_re, line_str):
                            gate_seen = True
                cursor = kernel_cursor()
            except Exception:
                pass

            if map_seen and gate_seen:
                self._emit(
                    f"✅ [{log_tag}] 已进入 map{map_id}，{gate_name} 门控通过（点击={click_count}）",
                    "SUCCESS",
                )
                return True
            if now - last_log >= 1.0:
                self._emit(
                    f"🔍 [{log_tag}] map{map_id}={map_seen}，gate={gate_seen}，"
                    f"等待={gate_name}，点击={click_count}",
                    "DEBUG",
                )
                last_log = now
            time.sleep(0.05)
        self._emit(
            f"❌ [{log_tag}] 进入 map{map_id} 超时：map={map_seen}，gate={gate_seen}，点击={click_count}",
            "ERROR",
        )
        return False

    def _chip_gold_open_shop(self, regions, use_foreground: bool, *, log_tag: str) -> bool:
        from core.logger import fetch_kernel_since, kernel_cursor

        cursor = kernel_cursor()
        t0 = time.time()
        last_click = 0.0
        click_count = 0
        self._emit(f"🛒 [{log_tag}] 点击 商店.商店，等待 buyPetProps.swf", "INFO")
        while time.time() - t0 < 15.0:
            if self._should_abort():
                return False
            now = time.time()
            if now - last_click >= 0.3:
                if not self._click_region_safe(regions, "商店.商店", use_foreground):
                    return False
                click_count += 1
                last_click = now
            try:
                lines = fetch_kernel_since(cursor)
                if isinstance(lines, list) and any(
                    line_matches(BUY_PET_PROPS_RE, str(line)) for line in lines
                ):
                    self._emit(
                        f"✅ [{log_tag}] 检测到 buyPetProps.swf（点击={click_count}）",
                        "SUCCESS",
                    )
                    return True
                cursor = kernel_cursor()
            except Exception:
                pass
            time.sleep(0.05)
        self._emit(f"❌ [{log_tag}] 未检测到 buyPetProps.swf", "ERROR")
        return False

    def _chip_gold_buy_chip(
        self,
        regions,
        use_foreground: bool,
        *,
        chip_key: str,
        log_tag: str,
    ) -> bool:
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner", "ERROR")
            return False
        dismiss_fn = getattr(drr, "_dismiss_specific_111_until_disappear", None)
        if not callable(dismiss_fn):
            self._emit(f"❌ [{log_tag}] 缺少高强度 1AND1 清理能力", "ERROR")
            return False
        stop_event = getattr(self.bot, "_stop_event", threading.Event())
        self._emit(f"🧩 [{log_tag}] 点击 {chip_key}", "SYSTEM")
        if not self._click_region_safe(regions, chip_key, use_foreground):
            return False
        self._emit(
            f"⚡ [{log_tag}] 高频追踪对话框蓝探针：左边1AND1（含中间确认探针）",
            "INFO",
        )
        if not dismiss_fn(
            checker=drr._check_left_1and1and1_probes,
            confirm_key="对话框.左边确认",
            use_foreground=use_foreground,
            stop_event=stop_event,
            timeout_s=20.0,
            log_tag=f"{log_tag}·左边111",
            poll_s=0.025,
            need_gone=2,
            click_while_waiting=False,
        ):
            return False
        self._emit(f"✅ [{log_tag}] 左边1AND1已消失，执行普通1AND1", "SUCCESS")
        return bool(
            dismiss_fn(
                checker=drr._check_normal_1and1and1_probes,
                confirm_key="对话框.普通确认",
                use_foreground=use_foreground,
                stop_event=stop_event,
                timeout_s=20.0,
                log_tag=f"{log_tag}·普通111",
                poll_s=0.04,
                need_gone=2,
                click_while_waiting=False,
            )
        )

    @staticmethod
    def _chip_gold_rgb_matches(
        rgb: Optional[Tuple[int, int, int]],
        target: Tuple[int, int, int],
        *,
        tolerance: int = CHIP_GOLD_SHOP_PROBE_TOLERANCE,
    ) -> bool:
        if rgb is None:
            return False
        return all(
            abs(int(actual) - int(expected)) <= int(tolerance)
            for actual, expected in zip(rgb, target)
        )

    def _chip_gold_wait_shop_probe(
        self,
        regions,
        *,
        probe_key: str,
        target_rgb: Tuple[int, int, int],
        timeout_s: float = 20.0,
        stable_scans: int = 2,
        log_tag: str,
    ) -> bool:
        """等待商店探针连续命中目标颜色，避免单帧误判。"""
        required_stable = max(1, int(stable_scans))
        stable_count = 0
        last_rgb = None
        last_log = 0.0
        started_at = time.time()
        self._emit(
            f"⏳ [{log_tag}] 等待 {probe_key} 变为 RGB={target_rgb}",
            "INFO",
        )
        while time.time() - started_at < timeout_s:
            if self._should_abort():
                return False
            self._wait_if_paused()
            last_rgb = mean_rgb_for_region_key(regions, probe_key)
            if self._chip_gold_rgb_matches(last_rgb, target_rgb):
                stable_count += 1
                if stable_count >= required_stable:
                    self._emit(
                        f"✅ [{log_tag}] {probe_key} 已稳定命中 RGB={last_rgb}",
                        "SUCCESS",
                    )
                    return True
            else:
                stable_count = 0
            now = time.time()
            if now - last_log >= 1.0:
                self._emit(
                    f"🔍 [{log_tag}] {probe_key} RGB={last_rgb}，"
                    f"稳定={stable_count}/{required_stable}",
                    "DEBUG",
                )
                last_log = now
            time.sleep(0.05)
        self._emit(
            f"❌ [{log_tag}] 等待 {probe_key} 变为 RGB={target_rgb} 超时，末次 RGB={last_rgb}",
            "ERROR",
        )
        return False

    def _chip_gold_type_text_direct(self, text: str, *, log_tag: str) -> bool:
        """前台直接键入商店数量，不执行全选或清空。"""
        try:
            from pynput.keyboard import Controller

            Controller().type(str(text))
            return True
        except Exception as exc:
            self._emit(f"❌ [{log_tag}] 商店数量直接输入失败：{exc}", "ERROR")
            return False

    def _chip_gold_buy_gacha_cards(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
    ) -> bool:
        """右翻商店并购买 999999 个扭蛋牌，输入后和左边确认后各处理一次普通 1A1。"""
        self._emit(f"➡️ [{log_tag}] 点击 商店.右", "SYSTEM")
        if not self._click_region_safe(regions, "商店.右", use_foreground):
            return False
        if not self._chip_gold_wait_shop_probe(
            regions,
            probe_key="商店.黄色探针",
            target_rgb=CHIP_GOLD_SHOP_YELLOW_RGB,
            log_tag=f"{log_tag}·右翻",
        ):
            return False

        self._emit(f"🎟️ [{log_tag}] 点击 商店.扭蛋牌", "SYSTEM")
        if not self._click_region_safe(regions, "商店.扭蛋牌", use_foreground):
            return False
        if not self._chip_gold_wait_shop_probe(
            regions,
            probe_key="商店.蓝色探针",
            target_rgb=CHIP_GOLD_SHOP_DIALOG_BLUE_RGB,
            log_tag=f"{log_tag}·数量框",
        ):
            return False

        if not self._click_region_safe(regions, "商店.输入框", use_foreground):
            return False
        time.sleep(0.2)
        if use_foreground:
            if not self._chip_gold_type_text_direct("999999", log_tag=log_tag):
                return False
        elif not self._pick_pet_exp_input_background("999999", log_tag=log_tag):
            return False
        self._emit(f"🔢 [{log_tag}] 已输入购买数量 999999", "SUCCESS")

        if not self._wait_1and1_clear(
            regions,
            use_foreground,
            timeout_s=20.0,
            min_confirm_clicks=1,
            log_tag=f"{log_tag}·输入后普通1A1",
        ):
            return False
        if not self._click_region_safe(regions, "商店.左边确认", use_foreground):
            return False
        time.sleep(0.8)
        return self._wait_1and1_clear(
            regions,
            use_foreground,
            timeout_s=20.0,
            min_confirm_clicks=1,
            log_tag=f"{log_tag}·确认后普通1A1",
        )

    def _chip_gold_wait_suke_white(
        self,
        regions,
        *,
        timeout_s: float = CHIP_GOLD_SUKE_WHITE_TIMEOUT_SEC,
        log_tag: str,
    ) -> bool:
        stable_count = 0
        last_rgb = None
        last_log = 0.0
        started_at = time.time()
        self._emit(
            f"⏳ [{log_tag}] 等待 {SUKE_EXCHANGE_WHITE_PROBE_KEY} 变白",
            "INFO",
        )
        while time.time() - started_at < timeout_s:
            if self._should_abort():
                return False
            self._wait_if_paused()
            last_rgb = mean_rgb_for_region_key(
                regions,
                SUKE_EXCHANGE_WHITE_PROBE_KEY,
            )
            if self._chip_gold_probe_is_white(last_rgb):
                stable_count += 1
                if stable_count >= 2:
                    self._emit(
                        f"✅ [{log_tag}] {SUKE_EXCHANGE_WHITE_PROBE_KEY} 已稳定变白：RGB={last_rgb}",
                        "SUCCESS",
                    )
                    return True
            else:
                stable_count = 0
            now = time.time()
            if now - last_log >= 1.0:
                self._emit(
                    f"🔍 [{log_tag}] {SUKE_EXCHANGE_WHITE_PROBE_KEY} RGB={last_rgb}，"
                    f"稳定={stable_count}/2",
                    "DEBUG",
                )
                last_log = now
            time.sleep(0.08)
        self._emit(
            f"❌ [{log_tag}] 等待 {SUKE_EXCHANGE_WHITE_PROBE_KEY} 变白超时，末次 RGB={last_rgb}",
            "ERROR",
        )
        return False

    def _chip_gold_run_suke_cycle(
        self,
        regions,
        use_foreground: bool,
        *,
        cycle: int,
        total: int,
        log_tag: str,
    ) -> bool:
        tag = f"{log_tag}·{cycle}/{total}"
        self._emit(f"🪨 [{tag}] 点击 {SUKE_EXCHANGE_CLICK0_KEY}", "SYSTEM")
        if not self._click_region_safe(
            regions,
            SUKE_EXCHANGE_CLICK0_KEY,
            use_foreground,
        ):
            return False
        if not self._chip_gold_wait_suke_white(regions, log_tag=tag):
            return False
        time.sleep(0.5)
        self._emit(f"🖱️ [{tag}] 点击 {SUKE_EXCHANGE_CLICK1_KEY}", "INFO")
        if not self._click_region_safe(
            regions,
            SUKE_EXCHANGE_CLICK1_KEY,
            use_foreground,
        ):
            return False
        time.sleep(0.8)
        self._emit(f"🖱️ [{tag}] 点击 {SUKE_EXCHANGE_CLICK2_KEY}", "INFO")
        if not self._click_region_safe(
            regions,
            SUKE_EXCHANGE_CLICK2_KEY,
            use_foreground,
        ):
            return False
        return self._wait_1and1_clear(
            regions,
            use_foreground,
            timeout_s=MINING_1AND1_TIMEOUT_SEC,
            min_confirm_clicks=1,
            log_tag=f"{tag}·1AND1",
        )

    def _chip_gold_run_crystal_suke_cycles(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
    ) -> bool:
        """Best-effort crystal-bubble/Suke bonus; only a manual stop aborts the main flow."""
        drr = getattr(self.bot, "dar_route_runner", None)
        stop_event = getattr(self.bot, "_stop_event", None) or self._new_daily_stop_event()
        required = (
            "刷新.基地",
            "刷新.基地右侧",
            CHIP_GOLD_CRYSTAL_WHITE_PROBE_KEY,
            SUKE_EXCHANGE_CLICK0_KEY,
            SUKE_EXCHANGE_CLICK1_KEY,
            SUKE_EXCHANGE_CLICK2_KEY,
            SUKE_EXCHANGE_WHITE_PROBE_KEY,
        )
        missing = [key for key in required if not regions.get(key)]
        if drr is None or missing:
            detail = "缺少 dar_route_runner" if drr is None else f"缺少区域：{', '.join(missing)}"
            self._emit(
                f"⚠️ [{log_tag}] {detail}，跳过晶化气泡苏克奖励，继续后续金豆流程",
                "WARN",
            )
            return not self._should_abort()

        if not self._new_daily_base_gate_and_confirm(
            regions,
            use_foreground,
            log_tag=f"{log_tag}·回基地",
        ):
            if self._should_abort():
                return False
            self._emit(
                f"⚠️ [{log_tag}] 回基地门控失败，跳过苏克奖励并继续后续流程",
                "WARN",
            )
            return True

        self._emit(f"🖱️ [{log_tag}] 基地门控后点击中间 → 右边", "SYSTEM")
        if not self._click_region_safe(regions, "刷新.基地", use_foreground):
            if self._should_abort():
                return False
            self._emit(f"⚠️ [{log_tag}] 点击基地中间失败，跳过苏克奖励", "WARN")
            return True
        time.sleep(0.3)
        if not self._click_region_safe(regions, "刷新.基地右侧", use_foreground):
            if self._should_abort():
                return False
            self._emit(f"⚠️ [{log_tag}] 点击基地右边失败，跳过苏克奖励", "WARN")
            return True
        time.sleep(0.3)

        from core.logger import kernel_cursor

        route_cursor = kernel_cursor()
        self._emit(
            f"📜 [{log_tag}] 执行 {CHIP_GOLD_CRYSTAL_SCRIPT_NAME}",
            "SYSTEM",
        )
        route_ok = self.run_single_script(
            CHIP_GOLD_CRYSTAL_SCRIPT_NAME,
            bg_mode=(not use_foreground),
        )
        if not route_ok:
            if self._should_abort():
                return False
            self._emit(
                f"⚠️ [{log_tag}] {CHIP_GOLD_CRYSTAL_SCRIPT_NAME} 返回失败，仍继续检查 map55 门控",
                "WARN",
            )

        gate_fn = getattr(drr, "_crystal_bubble_gate_after_to", None)
        gate_ok = False
        if callable(gate_fn):
            try:
                gate_ok = bool(
                    gate_fn(
                        route_cursor,
                        stop_event,
                        log_tag=f"{log_tag}·入口",
                    )
                )
            except Exception as exc:
                self._emit(f"⚠️ [{log_tag}] map55+白色探针门控异常：{exc}", "WARN")
        if not gate_ok:
            if self._should_abort():
                return False
            self._emit(
                f"⚠️ [{log_tag}] map55+白色探针门控未通过，跳过苏克十轮并继续后续流程",
                "WARN",
            )
            return True

        completed = 0
        failed = 0
        for cycle in range(1, CHIP_GOLD_SUKE_CYCLES + 1):
            if self._should_abort():
                return False
            try:
                cycle_ok = self._chip_gold_run_suke_cycle(
                    regions,
                    use_foreground,
                    cycle=cycle,
                    total=CHIP_GOLD_SUKE_CYCLES,
                    log_tag=log_tag,
                )
            except Exception as exc:
                cycle_ok = False
                self._emit(
                    f"⚠️ [{log_tag}·{cycle}/{CHIP_GOLD_SUKE_CYCLES}] 本轮异常：{exc}",
                    "WARN",
                )
            if self._should_abort():
                return False
            if cycle_ok:
                completed += 1
            else:
                failed += 1
                self._emit(
                    f"⚠️ [{log_tag}·{cycle}/{CHIP_GOLD_SUKE_CYCLES}] 本轮未完成，继续下一轮",
                    "WARN",
                )

        self._emit(
            f"📊 [{log_tag}] 苏克十轮结束：完成={completed}，异常/失败={failed}",
            "SUCCESS" if failed == 0 else "WARN",
        )
        return True

    def _chip_gold_run_gold_scripts(
        self,
        use_foreground: bool,
        *,
        log_tag: str,
        times: int = CHIP_GOLD_SCRIPT_TIMES,
    ) -> bool:
        """在瞭望露台执行金豆脚本；单次失败继续，手动停止才中止。"""
        total = max(1, int(times))
        completed = 0
        failed = 0
        for idx in range(1, total + 1):
            if self._should_abort():
                return False
            self._emit(
                f"💰 [{log_tag}] 执行 {CHIP_GOLD_SCRIPT_NAME}.json"
                f"（{idx}/{total}）",
                "SYSTEM",
            )
            try:
                script_ok = self.run_single_script(
                    CHIP_GOLD_SCRIPT_NAME,
                    bg_mode=(not use_foreground),
                )
            except Exception as exc:
                script_ok = False
                self._emit(
                    f"⚠️ [{log_tag}] 金豆脚本第 {idx} 次异常：{exc}",
                    "WARN",
                )
            if self._should_abort():
                return False
            if script_ok:
                completed += 1
            else:
                failed += 1
                self._emit(
                    f"⚠️ [{log_tag}] 金豆脚本第 {idx} 次执行失败，继续后续流程",
                    "WARN",
                )

        self._emit(
            f"📊 [{log_tag}] 金豆脚本{total}次结束：完成={completed}，"
            f"异常/失败={failed}",
            "SUCCESS" if failed == 0 else "WARN",
        )
        return True

    def _chip_gold_refresh_login(
        self,
        use_foreground: bool,
        *,
        log_tag: str,
    ) -> bool:
        """刷新登录并完成屏蔽，但不点击基地，供后续直接进入实验室。"""
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner，无法刷新重连", "ERROR")
            return False
        stop_event = getattr(self.bot, "_stop_event", None) or self._new_daily_stop_event()
        self._emit(f"🔄 [{log_tag}] 苏克十轮完成，刷新登录并屏蔽", "SYSTEM")
        if not drr.run_refresh_login_until_map(
            use_foreground,
            stop_event,
            include_base_and_map_gate=False,
        ):
            self._emit(f"❌ [{log_tag}] 刷新登录/屏蔽失败", "ERROR")
            return False
        return True

    def _chip_gold_follow_purple_from_closed_bag(
        self,
        use_foreground: bool,
        *,
        log_tag: str,
    ) -> bool:
        """在实验室从关闭背包状态打开背包并跟随紫色精灵。"""
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner，无法跟随紫色", "ERROR")
            return False
        stop_event = getattr(self.bot, "_stop_event", None) or self._new_daily_stop_event()
        self._emit(f"🐾 [{log_tag}] 打开背包并跟随紫色精灵", "SYSTEM")
        if not drr.set_follow_purple_jita_from_closed_bag(
            use_foreground,
            stop_event,
            log_tag=log_tag,
        ):
            self._emit(f"❌ [{log_tag}] 跟随紫色精灵失败", "ERROR")
            return False
        return True

    def run_gacha_reconnect_to_ready(
        self,
        use_foreground: bool,
        *,
        reconnect_round: int,
    ) -> bool:
        """重连屏蔽后进入瞭望露台、跟随紫色，并回到可再次扭蛋的入口。"""
        tag = f"扭蛋重连·第{int(reconnect_round)}轮"
        regions = getattr(self.bot, "regions", None)
        drr = getattr(self.bot, "dar_route_runner", None)
        if regions is None or drr is None:
            self._emit(f"❌ [{tag}] 缺少 regions 或 dar_route_runner", "ERROR")
            return False
        stop_event = getattr(self.bot, "_stop_event", None) or self._new_daily_stop_event()
        self._emit(f"🔄 [{tag}] 刷新登录并屏蔽，不点击基地", "SYSTEM")
        if not drr.run_refresh_login_until_map(
            use_foreground,
            stop_event,
            include_base_and_map_gate=False,
        ):
            self._emit(f"❌ [{tag}] 刷新登录/屏蔽失败", "ERROR")
            return False

        last_map_id = self._fusion_latest_map_id()
        if last_map_id != CHIP_GOLD_TERRACE_MAP_ID:
            if not self._chip_gold_open_map_until_white(
                regions,
                use_foreground,
                log_tag=f"{tag}·瞭望露台地图",
            ):
                return False
            if not self._chip_gold_enter_map(
                regions,
                use_foreground,
                target_key="地图.瞭望露台",
                map_id=CHIP_GOLD_TERRACE_MAP_ID,
                newnpc_id=CHIP_GOLD_TERRACE_NEWNPC_ID,
                log_tag=f"{tag}·瞭望露台",
            ):
                return False
        else:
            self._emit(
                f"✅ [{tag}] 重连后已在 map{CHIP_GOLD_TERRACE_MAP_ID}，跳过地图导航",
                "SUCCESS",
            )

        if not self._chip_gold_follow_purple_from_closed_bag(
            use_foreground,
            log_tag=f"{tag}·紫色跟随",
        ):
            return False
        self._emit(f"🎲 [{tag}] 点击 {HONOR_EXCHANGE_TO_GACHA_KEY}", "SYSTEM")
        if not self._click_region_safe(
            regions,
            HONOR_EXCHANGE_TO_GACHA_KEY,
            use_foreground,
        ):
            return False
        self._emit(f"⏳ [{tag}] to扭蛋后等待1秒，再次开启扭蛋", "INFO")
        time.sleep(1.0)
        return not self._should_abort()

    def run_chip_gold_honor_mode(
        self,
        use_foreground: bool = False,
        *,
        gacha_filled_times: int = 1,
    ) -> bool:
        """执行一键周常：露台跟随、金豆、苏克、实验室芯片和荣誉兑换。"""
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口", "ERROR")
            return False
        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ [一键周常] 缺少 bot.regions", "ERROR")
            return False
        required = (
            "地图.地图",
            CHIP_GOLD_MAP_WHITE_PROBE_KEY,
            "地图.实验室",
            "地图.瞭望露台",
            "精灵背包.打开精灵背包",
            "精灵背包.身边跟随",
            "商店.商店",
            "商店.专用芯片",
            "商店.通用芯片",
            "商店.关闭",
            "对话框.通用探针",
            "对话框.普通确认探针",
            "对话框.左边确认",
            "对话框.普通确认",
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ [一键周常] 缺少区域：{key}", "ERROR")
                return False
        script_path = os.path.join(self.script_dir, f"{CHIP_GOLD_SCRIPT_NAME}.json")
        if not os.path.isfile(script_path):
            self._emit(f"❌ [一键周常] 找不到脚本：{script_path}", "ERROR")
            return False

        tag = "一键周常"
        gold_runs = max(1, int(gacha_filled_times))
        self._emit(
            f"📋 [一键周常] map103瞭望露台→紫色跟随→金豆×{gold_runs}"
            "（扭蛋次数）→"
            "回基地→to晶化气泡→苏克×10→刷新→map5实验室→紫色跟随→"
            "专用/通用芯片→map103瞭望露台→荣誉兑换",
            "SYSTEM",
        )
        last_map_id = self._fusion_latest_map_id()
        self._emit(f"⬆️ [{tag}] 重连后向上扫描到的第一个 map#={last_map_id}", "INFO")
        if last_map_id != CHIP_GOLD_TERRACE_MAP_ID:
            if not self._chip_gold_open_map_until_white(
                regions,
                use_foreground,
                log_tag=f"{tag}·开局瞭望露台地图",
            ):
                return False
            if not self._chip_gold_enter_map(
                regions,
                use_foreground,
                target_key="地图.瞭望露台",
                map_id=CHIP_GOLD_TERRACE_MAP_ID,
                newnpc_id=CHIP_GOLD_TERRACE_NEWNPC_ID,
                log_tag=f"{tag}·开局瞭望露台",
            ):
                return False
        else:
            self._emit(
                f"✅ [{tag}] 已在 map{CHIP_GOLD_TERRACE_MAP_ID}，"
                "跳过开局瞭望露台导航",
                "SUCCESS",
            )

        time.sleep(0.5)
        self._emit(
            f"🐾 [{tag}] 在瞭望露台打开背包并跟随紫色精灵，不执行放生",
            "SYSTEM",
        )
        if not self._chip_gold_follow_purple_from_closed_bag(
            use_foreground,
            log_tag=f"{tag}·瞭望露台紫色跟随",
        ):
            return False
        if not self._chip_gold_run_gold_scripts(
            use_foreground,
            log_tag=f"{tag}·开局金豆",
            times=gold_runs,
        ):
            return False

        if not self._chip_gold_run_crystal_suke_cycles(
            regions,
            use_foreground,
            log_tag=f"{tag}·晶化气泡苏克",
        ):
            return False

        if not self._chip_gold_refresh_login(
            use_foreground,
            log_tag=f"{tag}·苏克后刷新",
        ):
            return False

        if not self._chip_gold_open_map_until_white(
            regions,
            use_foreground,
            log_tag=f"{tag}·实验室地图",
        ):
            return False
        if not self._chip_gold_enter_map(
            regions,
            use_foreground,
            target_key="地图.实验室",
            map_id=CHIP_GOLD_LAB_MAP_ID,
            newnpc_id=CHIP_GOLD_LAB_NEWNPC_ID,
            log_tag=f"{tag}·实验室",
        ):
            return False

        if not self._chip_gold_follow_purple_from_closed_bag(
            use_foreground,
            log_tag=f"{tag}·实验室紫色跟随",
        ):
            return False

        if not self._chip_gold_open_shop(regions, use_foreground, log_tag=tag):
            return False
        time.sleep(0.5)
        if not self._chip_gold_buy_chip(
            regions,
            use_foreground,
            chip_key="商店.专用芯片",
            log_tag=f"{tag}·专用芯片",
        ):
            return False
        if not self._chip_gold_buy_chip(
            regions,
            use_foreground,
            chip_key="商店.通用芯片",
            log_tag=f"{tag}·通用芯片",
        ):
            return False
        if not self._click_region_safe(regions, "商店.关闭", use_foreground):
            return False
        time.sleep(0.3)

        if not self._chip_gold_open_map_until_white(regions, use_foreground, log_tag=f"{tag}·瞭望露台地图"):
            return False
        if not self._chip_gold_enter_map(
            regions,
            use_foreground,
            target_key="地图.瞭望露台",
            map_id=CHIP_GOLD_TERRACE_MAP_ID,
            newnpc_id=CHIP_GOLD_TERRACE_NEWNPC_ID,
            log_tag=f"{tag}·瞭望露台",
        ):
            return False

        self._emit(
            f"🎖️ [{tag}] 已回到瞭望露台，开局金豆已经完成，"
            "不再重复执行金豆，开始荣誉兑换",
            "SYSTEM",
        )
        if not self.run_honor_exchange_mode(use_foreground=use_foreground):
            return False
        self._emit(f"✅ [{tag}] 全流程完成", "SUCCESS")
        return True

    def _mining_single_cycle(
        self,
        regions,
        spot_region_key: str,
        use_foreground: bool,
        *,
        cycle_idx: int,
        total: int,
        skip_initial_spot: bool = False,
    ) -> bool:
        tag = f"挖矿 {cycle_idx}/{total}"
        self._emit(f"⛏️ [{tag}] 开始", "SYSTEM")

        if not skip_initial_spot:
            self._emit(f"🖱 [{tag}] 点击矿点 {spot_region_key}", "INFO")
            if not self._click_region_safe(regions, spot_region_key, use_foreground):
                return False
            time.sleep(0.35)

            if not self._wait_suke_bw_probes(regions, log_tag=tag):
                return False

        self._emit(f"🖱 [{tag}] 点击 {MINING_START_KEY}", "INFO")
        if not self._click_region_safe(regions, MINING_START_KEY, use_foreground):
            return False
        time.sleep(0.25)

        if not self._wait_suke_bw_probes(regions, log_tag=f"{tag}·确认后"):
            return False

        if not self._wait_1and1_clear(regions, use_foreground, log_tag=tag):
            return False

        self._emit(f"✅ [{tag}] 单轮完成", "SUCCESS")
        return True

    def run_mining_cycles(
        self,
        spot_region_key: str,
        times: int,
        *,
        use_foreground: bool = False,
        skip_initial_spot: bool = False,
    ) -> bool:
        """
        挖矿循环：点击矿点 → 等苏克黑+白 → 点挖矿开始 → 再等黑+白 → 1AND1 至消失。

        Args:
            spot_region_key: 矿点区域 key（如 ``日常.11甲烷``）
            times: 执行轮数（>=1）
            skip_initial_spot: 为 True 时跳过首轮「点矿点 + 等苏克」，由调用方前置完成
        """
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口：无法挖矿", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions", "ERROR")
            return False

        try:
            n = max(1, int(times))
        except (TypeError, ValueError):
            n = 1

        spot = (spot_region_key or "").strip()
        if not spot:
            self._emit("❌ 矿点区域 key 为空", "ERROR")
            return False

        for req in (
            spot,
            MINING_SUKE_BLACK_KEY,
            MINING_SUKE_WHITE_KEY,
            MINING_START_KEY,
            "对话框.通用探针",
            "对话框.普通确认探针",
            "对话框.普通确认",
        ):
            if not regions.get(req):
                self._emit(f"❌ 缺少区域：{req}", "ERROR")
                return False

        self._emit(
            f"⛏️ 开始挖矿：矿点={spot}，共 {n} 轮（前台={use_foreground}）",
            "SYSTEM",
        )
        ok_all = True
        for i in range(1, n + 1):
            if self._should_abort():
                self._emit("⛔ 挖矿已中止", "SYSTEM")
                return False
            if not self._mining_single_cycle(
                regions,
                spot,
                use_foreground,
                cycle_idx=i,
                total=n,
                skip_initial_spot=skip_initial_spot and i == 1,
            ):
                ok_all = False
                self._emit(f"⚠️ 挖矿第 {i} 轮失败，停止后续", "WARN")
                break
            if i < n:
                time.sleep(0.4)

        if ok_all:
            self._emit(f"✅ 挖矿完成（{n} 轮）", "SUCCESS")
        return ok_all

    def _is_gacha_test_target_rgb(
        self,
        rgb: Optional[Tuple[int, int, int]],
        target: Tuple[int, int, int],
    ) -> bool:
        return bool(
            rgb is not None
            and self._rgb_distance(rgb, target) <= GACHA_TEST_TARGET_RGB_TOLERANCE
        )

    def _wait_gacha_test_probe_pair(
        self,
        regions,
        first_key: str,
        first_target: Tuple[int, int, int],
        second_key: str,
        second_target: Tuple[int, int, int],
        *,
        log_tag: str,
        desc: str,
        timeout_s: float = GACHA_TEST_PROBE_TIMEOUT_SEC,
        quiet: bool = False,
    ) -> bool:
        if not quiet:
            self._emit(
                f"⏳ [{log_tag}] 等待 {desc}（{timeout_s:.0f}s超时）",
                "INFO",
            )
        last_log = 0.0
        t0 = time.time()
        while not self._should_abort() and (time.time() - t0) < float(timeout_s):
            self._wait_if_paused()
            first_rgb = mean_rgb_for_region_key(regions, first_key)
            second_rgb = mean_rgb_for_region_key(regions, second_key)
            first_ready = self._is_gacha_test_target_rgb(first_rgb, first_target)
            second_ready = self._is_gacha_test_target_rgb(second_rgb, second_target)
            if first_ready and second_ready:
                if not quiet:
                    self._emit(
                        f"✅ [{log_tag}] 双探针命中："
                        f"{first_key}={self._format_rgb(first_rgb)}，"
                        f"{second_key}={self._format_rgb(second_rgb)}；{desc}",
                        "SUCCESS",
                    )
                return True
            now = time.time()
            if not quiet and now - last_log >= GACHA_TEST_PROBE_LOG_INTERVAL_SEC:
                self._emit(
                    f"🔎 [{log_tag}] 双探针未就绪："
                    f"{first_key}={self._format_rgb(first_rgb)}"
                    f"（目标={self._format_rgb(first_target)}，命中={first_ready}），"
                    f"{second_key}={self._format_rgb(second_rgb)}"
                    f"（目标={self._format_rgb(second_target)}，命中={second_ready}）",
                    "DEBUG",
                )
                last_log = now
            time.sleep(GACHA_TEST_PROBE_POLL_SEC)
        if not self._should_abort():
            first_rgb = mean_rgb_for_region_key(regions, first_key)
            second_rgb = mean_rgb_for_region_key(regions, second_key)
            self._emit(
                f"❌ [{log_tag}] 等待 {desc} 超时："
                f"{first_key}={self._format_rgb(first_rgb)}，"
                f"{second_key}={self._format_rgb(second_rgb)}",
                "ERROR",
            )
            return False
        self._emit(f"⛔ [{log_tag}] 已手动停止", "SYSTEM")
        return False

    def _wait_gacha_test_probe(
        self,
        regions,
        key: str,
        predicate: Callable[[Optional[Tuple[int, int, int]]], bool],
        *,
        log_tag: str,
        desc: str,
        timeout_s: float = GACHA_TEST_PROBE_TIMEOUT_SEC,
    ) -> bool:
        self._emit(f"⏳ [{log_tag}] 等待 {key} {desc}（{timeout_s:.0f}s超时）", "INFO")
        last_log = 0.0
        t0 = time.time()
        while not self._should_abort() and (time.time() - t0) < float(timeout_s):
            self._wait_if_paused()
            rgb = mean_rgb_for_region_key(regions, key)
            if predicate(rgb):
                self._emit(
                    f"✅ [{log_tag}] {key} 命中：RGB={self._format_rgb(rgb)}，{desc}",
                    "SUCCESS",
                )
                return True
            now = time.time()
            if now - last_log >= GACHA_TEST_PROBE_LOG_INTERVAL_SEC:
                if key == GACHA_TEST_KEY_3 and rgb is not None:
                    deep = self._rgb_distance(rgb, GACHA_TEST_DEEP_CYAN_RGB)
                    light = self._rgb_distance(rgb, GACHA_TEST_LIGHT_CYAN_RGB)
                    extra = f"，距深青={deep:.1f}，距浅青={light:.1f}"
                else:
                    extra = ""
                self._emit(
                    f"🔎 [{log_tag}] {key} RGB={self._format_rgb(rgb)} 未命中{extra}",
                    "DEBUG",
                )
                last_log = now
            time.sleep(GACHA_TEST_PROBE_POLL_SEC)
        if not self._should_abort():
            rgb = mean_rgb_for_region_key(regions, key)
            self._emit(
                f"❌ [{log_tag}] 等待 {key} {desc} 超时，最后RGB={self._format_rgb(rgb)}",
                "ERROR",
            )
            return False
        self._emit(f"⛔ [{log_tag}] 已手动停止", "SYSTEM")
        return False

    def run_gacha_probe_test(
        self,
        *,
        times: int = 1,
        background_mode: bool = True,
        failure_handoff: bool = True,
        initial_reconnect: bool = True,
    ) -> bool:
        total = max(1, int(times or 1))
        use_foreground = not background_mode
        completed_cycles = 0
        self._last_gacha_completed_cycles = 0
        self._last_gacha_failure_reason = ""
        session_after_reconnect = False

        if total > 10 and initial_reconnect:
            reconnect_round = 0
            while not self._should_abort():
                reconnect_round += 1
                self._emit(
                    f"🔄 [扭蛋首次重连] 计划{total}次，大于10；"
                    f"第1次扭蛋前先执行重连恢复（第{reconnect_round}轮）",
                    "SYSTEM",
                )
                if self.run_gacha_reconnect_to_ready(
                    use_foreground,
                    reconnect_round=reconnect_round,
                ):
                    session_after_reconnect = True
                    break
                if self._should_abort():
                    return False
                self._emit(
                    f"⚠️ [扭蛋首次重连] 第{reconnect_round}轮未恢复到扭蛋入口，继续重连",
                    "WARN",
                )
                time.sleep(1.0)
            if self._should_abort():
                return False

        def _fail(reason: str) -> bool:
            self._last_gacha_completed_cycles = int(completed_cycles)
            self._last_gacha_failure_reason = str(reason)
            failed_at = self._beijing_now()
            self._emit(
                f"❌ [扭蛋失败] 时间={failed_at.isoformat(timespec='milliseconds')}，"
                f"计划={total}，已完成={completed_cycles}，原因={reason}",
                "ERROR",
            )
            if (
                self._should_abort()
                or not failure_handoff
            ):
                return False
            handoff = getattr(
                self.bot,
                "request_gacha_recovery_after_failure",
                None,
            )
            if not callable(handoff):
                self._emit(
                    f"❌ [扭蛋重连] 扭蛋共{total}次且已失败，但缺少重连入口：{reason}",
                    "ERROR",
                )
                return False
            self._emit(
                f"🔄 [扭蛋重连] 扭蛋共{total}次，已完成{completed_cycles}次后失败，"
                f"开始扭蛋重连：{reason}",
                "SYSTEM",
            )
            try:
                return bool(
                    handoff(
                        total=total,
                        completed_cycles=completed_cycles,
                        session_after_reconnect=session_after_reconnect,
                        use_foreground=use_foreground,
                        reason=reason,
                    )
                )
            except Exception as exc:
                self._emit(f"❌ [扭蛋重连] 重连处理异常：{exc}", "ERROR")
            return False

        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口：无法执行扭蛋", "ERROR")
            return _fail("未检测到游戏窗口")

        regions = getattr(self.bot, "regions", None)
        if not regions:
            self._emit("❌ 扭蛋缺少 bot.regions", "ERROR")
            return _fail("缺少 bot.regions")
        for key in (
            GACHA_TEST_KEY_1,
            GACHA_TEST_KEY_2,
            GACHA_TEST_KEY_3,
            GACHA_TEST_KEY_4,
        ):
            if not regions.get(key):
                self._emit(f"❌ 扭蛋缺少区域：{key}", "ERROR")
                return _fail(f"缺少区域 {key}")

        session_id = self._new_gacha_session_id()
        session_started_at = self._beijing_now()
        previous_completed_at: Optional[datetime] = None
        previous_duration_seconds: Optional[float] = None
        session_duration_seconds: List[float] = []
        self._emit(
            f"🎲 [扭蛋会话] session={session_id}，计划={total}轮；"
            f"开始={session_started_at.isoformat(timespec='milliseconds')}；"
            f"双探针超时={GACHA_TEST_PROBE_TIMEOUT_SEC:.0f}s；刷新后会生成新session",
            "SYSTEM",
        )
        for cycle in range(1, total + 1):
            if self._should_abort():
                self._emit("⛔ 扭蛋已手动停止", "SYSTEM")
                return False
            run_id = self._new_gacha_run_id()
            tag = f"扭蛋·{cycle}/{total}·{run_id}"
            cycle_started_at = self._beijing_now()
            self._emit(
                f"▶️ [扭蛋轮次] session={session_id}，轮次={cycle}/{total}，"
                f"run={run_id}，开始={cycle_started_at.isoformat(timespec='milliseconds')}",
                "INFO",
            )

            if not self._click_region_safe(regions, GACHA_TEST_KEY_1, use_foreground):
                return _fail(f"{tag} 点击 {GACHA_TEST_KEY_1} 失败")
            if not self._wait_gacha_test_probe_pair(
                regions,
                GACHA_TEST_KEY_3,
                GACHA_TEST_DEEP_CYAN_RGB,
                GACHA_TEST_KEY_4,
                GACHA_TEST_YELLOW_RGB,
                log_tag=tag,
                desc="扭蛋.3=(14,99,133) 深蓝且扭蛋.4=(255,204,0) 橙黄",
                quiet=True,
            ):
                return _fail(f"{tag} 点击扭蛋.2前双探针未就绪")

            if not self._click_region_safe(regions, GACHA_TEST_KEY_2, use_foreground):
                return _fail(f"{tag} 点击 {GACHA_TEST_KEY_2} 失败")
            if not self._wait_gacha_test_probe_pair(
                regions,
                GACHA_TEST_KEY_3,
                GACHA_TEST_LIGHT_CYAN_RGB,
                GACHA_TEST_KEY_4,
                GACHA_TEST_OLIVE_RGB,
                log_tag=tag,
                desc="扭蛋.3=(25,167,190) 浅青且扭蛋.4=(152,142,41)",
                quiet=True,
            ):
                return _fail(f"{tag} 点击扭蛋.3前双探针未就绪")

            if not self._click_region_safe(regions, GACHA_TEST_KEY_3, use_foreground):
                return _fail(f"{tag} 点击 {GACHA_TEST_KEY_3} 失败")

            if not self._wait_1and1_clear(
                regions,
                use_foreground,
                timeout_s=GACHA_TEST_1AND1_TIMEOUT_SEC,
                min_confirm_clicks=1,
                log_tag=f"{tag}·1AND1",
                quiet=True,
            ):
                return _fail(f"{tag} 1AND1 清理失败")
            completed_at = self._beijing_now()
            duration_seconds = (
                (completed_at - previous_completed_at).total_seconds()
                if previous_completed_at is not None
                else None
            )
            trend_delta_seconds = (
                duration_seconds - previous_duration_seconds
                if duration_seconds is not None
                and previous_duration_seconds is not None
                else None
            )
            if duration_seconds is not None:
                session_duration_seconds.append(duration_seconds)
            rolling_average_seconds = (
                sum(session_duration_seconds) / len(session_duration_seconds)
                if session_duration_seconds
                else None
            )
            if not self._append_gacha_completion_record(
                run_id,
                session_id=session_id,
                session_cycle=cycle,
                session_total=total,
                session_started_at=session_started_at,
                completed_at=completed_at,
                previous_completed_at=previous_completed_at,
                duration_seconds=duration_seconds,
                rolling_average_seconds=rolling_average_seconds,
                trend_delta_seconds=trend_delta_seconds,
            ):
                self._emit(f"❌ [{tag}] 扭蛋已完成，但完成记录写入失败", "ERROR")
                return _fail(f"{tag} 完成记录写入失败")
            duration_text = (
                f"{duration_seconds:.3f}s"
                if duration_seconds is not None
                else "首轮"
            )
            average_text = (
                f"{rolling_average_seconds:.3f}s"
                if rolling_average_seconds is not None
                else "-"
            )
            trend_text = (
                (
                    f"{'变快' if trend_delta_seconds < 0 else '变慢' if trend_delta_seconds > 0 else '持平'}"
                    f"({trend_delta_seconds:+.3f}s)"
                )
                if trend_delta_seconds is not None
                else "-"
            )
            cycle_elapsed = (completed_at - cycle_started_at).total_seconds()
            self._emit(
                f"✅ [扭蛋完成] session={session_id}，轮次={cycle}/{total}，"
                f"run={run_id}，1AND1清空时间="
                f"{completed_at.isoformat(timespec='milliseconds')}，"
                f"本轮耗时={cycle_elapsed:.3f}s，距上次完成={duration_text}，"
                f"session均值={average_text}，趋势={trend_text}",
                "SUCCESS",
            )
            time.sleep(0.5)
            previous_completed_at = completed_at
            previous_duration_seconds = duration_seconds
            completed_cycles = cycle
            self._last_gacha_completed_cycles = int(completed_cycles)

        session_completed_at = self._beijing_now()
        session_elapsed = (session_completed_at - session_started_at).total_seconds()
        session_average_text = (
            f"{sum(session_duration_seconds) / len(session_duration_seconds):.3f}s"
            if session_duration_seconds
            else "-"
        )
        self._emit(
            f"🏁 [扭蛋会话完成] session={session_id}，完成={total}/{total}，"
            f"结束={session_completed_at.isoformat(timespec='milliseconds')}，"
            f"会话耗时={session_elapsed:.3f}s，完成间隔均值={session_average_text}",
            "SUCCESS",
        )
        return True

    def _master_cup_stop_event(self) -> threading.Event:
        stop_event = getattr(self.bot, "_stop_event", None)
        if stop_event is None:
            stop_event = self._new_daily_stop_event()
        return stop_event

    def _master_cup_open_bag_ready(
        self,
        regions,
        use_foreground: bool,
        stop_event: threading.Event,
        *,
        log_tag: str,
    ) -> bool:
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 DarRouteRunner，无法打开背包", "ERROR")
            return False
        bag_open_key = "精灵背包.打开精灵背包"
        bag_open_btn_key = "精灵背包.打开精灵背包按钮"
        try:
            self._emit(f"💼 [{log_tag}] 打开精灵背包", "INFO")
            try:
                drr._click_region(bag_open_btn_key, use_foreground)
            except KeyError:
                drr._click_region(bag_open_key, use_foreground)
            return bool(
                drr._ensure_pet_bag_ui_ready_after_open(
                    stop_event,
                    use_foreground,
                    bag_open_key,
                    bag_open_btn_key,
                    log_tag=log_tag,
                )
            )
        except Exception as exc:
            self._emit(f"❌ [{log_tag}] 打开精灵背包失败：{exc}", "ERROR")
            return False

    def _master_cup_close_bag(
        self,
        use_foreground: bool,
        stop_event: threading.Event,
        *,
        log_tag: str,
    ) -> bool:
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 DarRouteRunner，无法关闭精灵背包", "ERROR")
            return False
        try:
            drr._close_pet_bag_with_verify(
                use_foreground,
                stop_event,
                "精灵背包.打开精灵背包",
                "精灵背包.打开精灵背包按钮",
                log_tag=log_tag,
            )
            return True
        except Exception as exc:
            self._emit(f"⚠️ [{log_tag}] 关闭精灵背包异常：{exc}", "WARN")
            return False

    def _master_cup_close_warehouse_keep_bag_open(
        self,
        use_foreground: bool,
        *,
        log_tag: str,
    ) -> bool:
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 DarRouteRunner，无法关闭仓库", "ERROR")
            return False
        self._emit(
            f"📦 [{log_tag}] 关闭精灵仓库，保留背包页面；点击前RGB："
            f"仓库关闭={mean_rgb_for_region_key(getattr(self.bot, 'regions', None), '精灵仓库.关闭')}，"
            f"通用探针={mean_rgb_for_region_key(getattr(self.bot, 'regions', None), '对话框.通用探针')}，"
            f"普通确认探针={mean_rgb_for_region_key(getattr(self.bot, 'regions', None), '对话框.普通确认探针')}，"
            f"普通确认={mean_rgb_for_region_key(getattr(self.bot, 'regions', None), '对话框.普通确认')}",
            "INFO",
        )
        try:
            drr._click_pet_warehouse_close(use_foreground, log_tag=log_tag)
            time.sleep(0.5)
            self._emit(
                f"📦 [{log_tag}] 关闭仓库点击后RGB："
                f"仓库关闭={mean_rgb_for_region_key(getattr(self.bot, 'regions', None), '精灵仓库.关闭')}，"
                f"背包一={mean_rgb_for_region_key(getattr(self.bot, 'regions', None), '精灵背包.1')}，"
                f"背包打开按钮={mean_rgb_for_region_key(getattr(self.bot, 'regions', None), '精灵背包.打开精灵背包')}",
                "DEBUG",
            )
            return True
        except Exception as exc:
            self._emit(f"❌ [{log_tag}] 关闭精灵仓库失败：{exc}", "ERROR")
            return False

    def _master_cup_recover_cyan_follow_from_open_bag(
        self,
        use_foreground: bool,
        stop_event: threading.Event,
        *,
        log_tag: str,
        set_cyan_primary: bool = True,
        set_follow_purple: bool = True,
    ) -> bool:
        drr = getattr(self.bot, "dar_route_runner", None)
        fn = getattr(drr, "recover_cyan_and_follow_purple_from_open_bag", None)
        if not callable(fn):
            self._emit(f"❌ [{log_tag}] 缺少青色恢复/紫色跟随入口", "ERROR")
            return False
        return bool(
            fn(
                use_foreground,
                stop_event,
                log_tag,
                set_cyan_primary=set_cyan_primary,
                recover_pet_one=False,
                recover_cyan=True,
                set_follow_purple=set_follow_purple,
            )
        )

    def _master_cup_put_back_cyan_from_open_bag(
        self,
        use_foreground: bool,
        stop_event: threading.Event,
        *,
        log_tag: str,
        require_cyan: bool = True,
        max_slots: int = 1,
        verify_hp: bool = False,
    ) -> bool:
        drr = getattr(self.bot, "dar_route_runner", None)
        fn = getattr(drr, "put_back_cyan_slots_from_open_bag", None)
        if not callable(fn):
            self._emit(f"❌ [{log_tag}] 缺少青色放回入口", "ERROR")
            return False
        return bool(
            fn(
                use_foreground,
                stop_event,
                log_tag,
                require_cyan=require_cyan,
                max_slots=max_slots,
                verify_hp=verify_hp,
                require_all_six_tricolor=True,
                verify_slot_deep_blue=True,
            )
        )

    def _master_cup_replace_cyan_from_current_bag(
        self,
        regions,
        next_spec: Dict[str, Any],
        use_foreground: bool,
        *,
        log_tag: str,
        put_back_pet_one: bool = False,
        set_cyan_primary: bool = True,
        set_follow_purple: bool = True,
        recover_target_after_take: bool = True,
    ) -> bool:
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 DarRouteRunner，无法替换大师杯青色精灵", "ERROR")
            return False
        stop_event = self._master_cup_stop_event()
        if not self._master_cup_open_bag_ready(regions, use_foreground, stop_event, log_tag=log_tag):
            return False
        if put_back_pet_one:
            if not drr.put_back_bag_slot_from_open_bag("一", use_foreground, stop_event, log_tag):
                self._master_cup_close_bag(use_foreground, stop_event, log_tag=log_tag)
                return False
        elif not self._master_cup_put_back_cyan_from_open_bag(
            use_foreground,
            stop_event,
            log_tag=f"{log_tag}·放回青色",
            require_cyan=True,
        ):
            self._master_cup_close_bag(use_foreground, stop_event, log_tag=log_tag)
            return False
        if not drr.open_pickmode_bag_warehouse_from_ready_bag(
            use_foreground,
            stop_event,
            log_tag=f"{log_tag}·打开仓库",
        ):
            return False
        try:
            next_pet_id = int(next_spec.get("pet_id") or 0)
        except Exception:
            next_pet_id = 0
        if next_pet_id <= 0:
            self._emit(f"❌ [{log_tag}] 大师杯取宠配置缺少 pet_id：{next_spec}", "ERROR")
            return False
        if not drr.take_pickmode_pets_from_open_bag_warehouse(
            (next_pet_id,),
            use_foreground,
            stop_event,
            log_tag=f"{log_tag}·取青色",
        ):
            return False
        if not self._master_cup_close_warehouse_keep_bag_open(use_foreground, log_tag=log_tag):
            return False
        if not recover_target_after_take:
            self._emit(
                f"✅ [{log_tag}] 已取{next_pet_id}；模式黄色成功后跳过精灵恢复",
                "SUCCESS",
            )
            return self._master_cup_close_bag(
                use_foreground,
                stop_event,
                log_tag=f"{log_tag}·取宠后关闭背包",
            )
        if not self._master_cup_recover_cyan_follow_from_open_bag(
            use_foreground,
            stop_event,
            log_tag=f"{log_tag}·扫描恢复{'跟随' if set_follow_purple else ''}",
            set_cyan_primary=set_cyan_primary,
            set_follow_purple=set_follow_purple,
        ):
            return False
        if not set_follow_purple:
            self._master_cup_close_bag(
                use_foreground,
                stop_event,
                log_tag=f"{log_tag}·恢复后关闭背包",
            )
        return True

    def _master_cup_restore_67_after_run(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
        put_back_pet_one: bool = False,
    ) -> bool:
        return self._master_cup_replace_cyan_from_current_bag(
            regions,
            MASTER_CUP_RESTORE_67_SPEC,
            use_foreground,
            log_tag=log_tag,
            put_back_pet_one=put_back_pet_one,
            set_cyan_primary=False,
            set_follow_purple=False,
            recover_target_after_take=False,
        )

    def _master_cup_replace_cyan_with_positional_pet(
        self,
        regions,
        spec: Dict[str, Any],
        use_foreground: bool,
        *,
        log_tag: str,
    ) -> bool:
        """Replace the current cyan pet with a warehouse-position pet and lead it."""
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 DarRouteRunner，无法按位置替换大师杯精灵", "ERROR")
            return False
        stop_event = self._master_cup_stop_event()
        category = str(spec.get("warehouse_category") or "")
        mode_key = str(spec.get("warehouse_mode_key") or "")
        reverse_positions = tuple(int(x) for x in spec.get("reverse_positions") or ())
        right_clicks = max(1, int(spec.get("right_clicks") or 40))
        if not category or not reverse_positions:
            self._emit(f"❌ [{log_tag}] 按位置取宠配置不完整：{spec}", "ERROR")
            return False

        if not self._master_cup_open_bag_ready(
            regions,
            use_foreground,
            stop_event,
            log_tag=log_tag,
        ):
            return False
        if not self._master_cup_put_back_cyan_from_open_bag(
            use_foreground,
            stop_event,
            log_tag=f"{log_tag}·放回当前青色",
            require_cyan=True,
        ):
            self._master_cup_close_bag(use_foreground, stop_event, log_tag=log_tag)
            return False
        if not drr.open_pickmode_bag_warehouse_from_ready_bag(
            use_foreground,
            stop_event,
            log_tag=f"{log_tag}·打开仓库",
        ):
            return False
        if mode_key:
            self._emit(f"🗂️ [{log_tag}] 切换仓库模式：{mode_key}", "INFO")
            drr._click_region(mode_key, use_foreground)
            drr._sleep_abortable(stop_event, 0.4)
        if not drr._rotation_place_pets_same_category_by_reverse(
            category,
            reverse_positions,
            right_clicks,
            use_foreground,
            stop_event,
            log_tag=f"{log_tag}·取{category}倒数第{reverse_positions[0]}",
        ):
            return False
        if not self._master_cup_close_warehouse_keep_bag_open(
            use_foreground,
            log_tag=log_tag,
        ):
            return False

        self._emit(
            f"🔎 [{log_tag}] 取宠完成后重新扫描1-6号，以当前青色SWF定位本系专用宠",
            "INFO",
        )
        return bool(
            drr.recover_cyan_and_follow_purple_from_open_bag(
                use_foreground,
                stop_event,
                f"{log_tag}·重扫青色首发与紫色跟随",
                set_cyan_primary=True,
                recover_pet_one=False,
                recover_cyan=True,
                set_follow_purple=True,
            )
        )

    def _master_cup_replace_cyan_with_first_category_cyan(
        self,
        regions,
        spec: Dict[str, Any],
        use_foreground: bool,
        *,
        log_tag: str,
    ) -> bool:
        """Replace current cyan with the first cyan scanned forward from page one."""
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 DarRouteRunner，无法正向扫描大师杯青色精灵", "ERROR")
            return False
        stop_event = self._master_cup_stop_event()
        category = str(spec.get("warehouse_category") or "普通系").strip() or "普通系"
        category_key = f"精灵仓库.{category}"

        if not self._master_cup_open_bag_ready(
            regions,
            use_foreground,
            stop_event,
            log_tag=log_tag,
        ):
            return False
        if not self._master_cup_put_back_cyan_from_open_bag(
            use_foreground,
            stop_event,
            log_tag=f"{log_tag}·放回当前青色",
            require_cyan=True,
        ):
            self._master_cup_close_bag(use_foreground, stop_event, log_tag=log_tag)
            return False
        if not drr.open_pickmode_bag_warehouse_from_ready_bag(
            use_foreground,
            stop_event,
            log_tag=f"{log_tag}·打开仓库",
        ):
            return False
        if not self._hatch_exp_take_first_category_color_forward(
            regions,
            use_foreground,
            log_tag=f"{log_tag}·第一页正扫{category}青色",
            category_key=category_key,
            target_color="cyan",
        ):
            self._master_cup_close_warehouse_keep_bag_open(
                use_foreground,
                log_tag=log_tag,
            )
            return False
        if not self._master_cup_close_warehouse_keep_bag_open(
            use_foreground,
            log_tag=log_tag,
        ):
            return False

        self._emit(
            f"🔎 [{log_tag}] 已取{category}从第一页起正向扫描的第一只青色精灵，重扫背包并设为首发",
            "INFO",
        )
        return bool(
            drr.recover_cyan_and_follow_purple_from_open_bag(
                use_foreground,
                stop_event,
                f"{log_tag}·重扫568青色首发与紫色跟随",
                set_cyan_primary=True,
                recover_pet_one=False,
                recover_cyan=True,
                set_follow_purple=True,
            )
        )

    def _run_master_cup_pre_setup(
        self,
        regions,
        cup: str,
        use_foreground: bool,
        *,
        spec_override: Optional[Dict[str, Any]] = None,
    ) -> bool:
        tag = f"大师杯{cup}前置"
        spec = spec_override or MASTER_CUP_PRE_SETUP_SPECS.get(cup)
        if not spec:
            self._emit(f"❌ [{tag}] 暂未配置本系指定精灵前置", "ERROR")
            return False

        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{tag}] 缺少 DarRouteRunner，无法执行前置", "ERROR")
            return False

        warehouse_mode_key = str(spec.get("warehouse_mode_key") or "")
        warehouse_category = str(spec.get("warehouse_category") or "")
        required = (
            "精灵背包.打开精灵背包",
            "精灵背包.精灵仓库",
            "精灵背包.精灵一",
            "精灵背包.精灵恢复",
            "精灵背包.放回仓库",
            "精灵背包.设为首发",
            "精灵背包.身边跟随",
            "精灵仓库.关闭",
            "精灵仓库.右",
            "精灵仓库.左",
            "精灵仓库.放入背包",
            "对话框.普通确认",
            NEW_DAILY_SEQ8_MAP_BTN_KEY,
            NEW_DAILY_SEQ9_SPOT_90_KEY,
            LANLAN_TO_108_KEY,
            MASTER_CUP_MAP108_TO_111_KEY,
            MAP10_WHITE_PROBE_KEY_NIEO,
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ [{tag}] 缺少区域：{key}", "ERROR")
                return False
        for key in (warehouse_mode_key, f"精灵仓库.{warehouse_category}"):
            if key and not regions.get(key):
                self._emit(f"❌ [{tag}] 缺少区域：{key}", "ERROR")
                return False
        for slot in range(1, 10):
            if not regions.get(f"精灵仓库.{slot}"):
                self._emit(f"❌ [{tag}] 缺少区域：精灵仓库.{slot}", "ERROR")
                return False

        stop_event = self._master_cup_stop_event()
        self._emit(
            f"📋 [{tag}] 重连→按目标青色三分支组队（命中/普通换青/mismatch清包重取）→青色首发并恢复→紫色跟随→102→108→111",
            "SYSTEM",
        )

        if not drr.run_refresh_login_until_map(use_foreground, stop_event):
            self._emit(f"❌ [{tag}] 重连/屏蔽阶段失败", "ERROR")
            return False

        if spec.get("scan_first_cyan"):
            if not self._master_cup_replace_cyan_with_first_category_cyan(
                regions,
                spec,
                use_foreground,
                log_tag=f"{tag}·普通系第一页首只青色",
            ):
                return False
        elif spec.get("position_only"):
            if not self._master_cup_replace_cyan_with_positional_pet(
                regions,
                spec,
                use_foreground,
                log_tag=f"{tag}·位置专用宠前置",
            ):
                return False
        else:
            target_pet_id = int(spec.get("pet_id") or 0)
            if not drr.ensure_target_cyan_pick_party_from_bag_warehouse_or_rebuild(
                target_pet_id,
                use_foreground,
                stop_event,
                log_tag=f"{tag}·目标青色前置",
                base_pet_id=67,
            ):
                return False

        if not self._new_daily_click_map_then_delay(regions, use_foreground, log_tag=f"{tag}·去102"):
            return False
        self._emit(f"🖱️ [{tag}] 点击 {NEW_DAILY_SEQ9_SPOT_90_KEY}", "SYSTEM")
        if not self._click_region_safe(regions, NEW_DAILY_SEQ9_SPOT_90_KEY, use_foreground):
            return False
        if not self._wait_map_npc_then_delay(102, log_tag=f"{tag}·map102"):
            return False

        self._emit(f"🖱️ [{tag}] 点击 {LANLAN_TO_108_KEY}", "SYSTEM")
        if not self._click_region_safe(regions, LANLAN_TO_108_KEY, use_foreground):
            return False
        self._emit(f"⏳ [{tag}] 等待 map108 信号", "INFO")
        if not self._wait_for_map_kernel(108, timeout_s=NEW_DAILY_MAP_WAIT_TIMEOUT_SEC):
            self._emit(f"❌ [{tag}] 等待 map108 超时", "ERROR")
            return False
        if not self._wait_yilu_white_probe_disappear(
            regions,
            log_tag=f"{tag}·map108白探针",
            timeout_s=45.0,
        ):
            return False

        self._emit(f"🖱️ [{tag}] 点击 {MASTER_CUP_MAP108_TO_111_KEY}", "SYSTEM")
        if not self._click_region_safe(regions, MASTER_CUP_MAP108_TO_111_KEY, use_foreground):
            return False
        if not self._wait_map_npc_then_delay(111, log_tag=f"{tag}·map111"):
            return False

        self._emit(f"✅ [{tag}] 前置完成，开始大师杯", "SUCCESS")
        return True

    def _run_master_cup_reconnect_to_111(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str = "大师杯重连",
    ) -> bool:
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 DarRouteRunner，无法重连到大师杯", "ERROR")
            return False
        required = (
            NEW_DAILY_SEQ8_MAP_BTN_KEY,
            NEW_DAILY_SEQ9_SPOT_90_KEY,
            LANLAN_TO_108_KEY,
            MASTER_CUP_MAP108_TO_111_KEY,
            MAP10_WHITE_PROBE_KEY_NIEO,
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ [{log_tag}] 缺少区域：{key}", "ERROR")
                return False

        stop_event = self._new_daily_stop_event()
        self._emit(f"🔄 [{log_tag}] 重连并走 102→108→111", "SYSTEM")
        if not drr.run_refresh_login_until_map(use_foreground, stop_event):
            self._emit(f"❌ [{log_tag}] 重连/屏蔽阶段失败", "ERROR")
            return False
        if not self._new_daily_click_map_then_delay(regions, use_foreground, log_tag=f"{log_tag}·去102"):
            return False
        self._emit(f"🖱️ [{log_tag}] 点击 {NEW_DAILY_SEQ9_SPOT_90_KEY}", "SYSTEM")
        if not self._click_region_safe(regions, NEW_DAILY_SEQ9_SPOT_90_KEY, use_foreground):
            return False
        if not self._wait_map_npc_then_delay(102, log_tag=f"{log_tag}·map102"):
            return False
        self._emit(f"🖱️ [{log_tag}] 点击 {LANLAN_TO_108_KEY}", "SYSTEM")
        if not self._click_region_safe(regions, LANLAN_TO_108_KEY, use_foreground):
            return False
        self._emit(f"⏳ [{log_tag}] 等待 map108 信号", "INFO")
        if not self._wait_for_map_kernel(108, timeout_s=NEW_DAILY_MAP_WAIT_TIMEOUT_SEC):
            self._emit(f"❌ [{log_tag}] 等待 map108 超时", "ERROR")
            return False
        if not self._wait_yilu_white_probe_disappear(
            regions,
            log_tag=f"{log_tag}·map108白探针",
            timeout_s=45.0,
        ):
            return False
        self._emit(f"🖱️ [{log_tag}] 点击 {MASTER_CUP_MAP108_TO_111_KEY}", "SYSTEM")
        if not self._click_region_safe(regions, MASTER_CUP_MAP108_TO_111_KEY, use_foreground):
            return False
        return self._wait_map_npc_then_delay(111, log_tag=f"{log_tag}·map111")

    def _collect_master_cup_battle_pet_ids(
        self,
        start_cursor: int,
        *,
        timeout_s: float = 1.2,
        settle_s: float = 0.25,
    ) -> set:
        counts = self._collect_master_cup_battle_pet_id_counts(
            start_cursor,
            timeout_s=timeout_s,
            settle_s=settle_s,
        )
        return set(counts)

    def _collect_master_cup_battle_pet_id_counts(
        self,
        start_cursor: int,
        *,
        timeout_s: float = 1.2,
        settle_s: float = 0.25,
    ) -> Dict[int, int]:
        try:
            from core.logger import fetch_kernel_since
        except Exception:
            return {}

        pet_id_counts: Dict[int, int] = {}
        last_signature: Tuple[int, ...] = ()
        last_change_at = 0.0
        t0 = time.time()
        while time.time() - t0 < timeout_s and not self._should_abort():
            current_counts: Dict[int, int] = {}
            skill_seen = False
            for line in fetch_kernel_since(start_cursor):
                # The compatibility matcher can expose the same path twice
                # from one ``path=`` line.  Deduplicate within a line while
                # preserving the same pet id appearing on separate lines.
                for pet_id in set(iter_fight_pet_swf_ids_in_line(str(line))):
                    pid = int(pet_id)
                    current_counts[pid] = current_counts.get(pid, 0) + 1
                if line_matches(RE_FIGHT_SKILL_SWF, str(line)):
                    skill_seen = True
            signature = tuple(
                value
                for pair in sorted(current_counts.items())
                for value in pair
            )
            if signature != last_signature:
                pet_id_counts = current_counts
                last_signature = signature
                last_change_at = time.time()
            if pet_id_counts and skill_seen:
                return pet_id_counts
            if pet_id_counts and last_change_at > 0 and time.time() - last_change_at >= settle_s:
                return pet_id_counts
            time.sleep(0.05)
        return pet_id_counts

    @staticmethod
    def _master_cup_enemy_pet_ids_for_568(
        pet_id_counts: Dict[int, int],
        own_party_ids: Optional[set] = None,
    ) -> set:
        """Subtract one expected occurrence for each of our six pets."""
        expected_own_ids = set(
            MASTER_CUP_568_OWN_PARTY_IDS
            if own_party_ids is None
            else own_party_ids
        )
        enemy_ids = set()
        for raw_id, raw_count in dict(pet_id_counts or {}).items():
            pet_id = int(raw_id)
            count = max(0, int(raw_count or 0))
            own_count = 1 if pet_id in expected_own_ids else 0
            if count > own_count:
                enemy_ids.add(pet_id)
        return enemy_ids

    @classmethod
    def _master_cup_should_escape_568_only(
        cls,
        pet_id_counts: Dict[int, int],
        own_party_ids: Optional[set] = None,
    ) -> bool:
        return cls._master_cup_enemy_pet_ids_for_568(
            pet_id_counts,
            own_party_ids,
        ) == {568}

    def _run_master_cup_norm_mode(
        self,
        use_foreground: bool,
        pre_setup: bool,
    ) -> bool:
        setup_text = "勾选前置，使用火系倒数20；" if pre_setup else "未勾前置，不换宠；"
        self._emit(
            f"🏆 诺姆大师杯启动：{setup_text}只打568并固定击败10次；完成后恢复67并进入轮换",
            "SYSTEM",
        )
        return self.run_master_cup_mode(
            cup_type="火系",
            use_foreground=use_foreground,
            yellow_target_count=10,
            pre_setup=pre_setup,
            norm_mode=False,
            target_pet_id=568,
            escape_non_target=True,
            skill_sequence=(2, 2, 2, 3, 3, 3),
            restore_light_after_finish=pre_setup,
            pre_setup_spec=MASTER_CUP_NORM_FIRE_PRE_SETUP_SPEC,
            allow_568_battle=True,
        )

    @staticmethod
    def _master_cup_resolve_escape_pet_id(
        escape_pet_id: Optional[int],
        *,
        allow_568_battle: bool,
    ) -> Optional[int]:
        if escape_pet_id is not None:
            return int(escape_pet_id)
        if allow_568_battle:
            return None
        return 568

    @staticmethod
    def _master_cup_skill_action(
        cup: str,
        round_idx: int,
        *,
        skill_sequence: Optional[Tuple[int, ...]],
        escape_after_skill_sequence: bool,
    ) -> str:
        current_round = max(1, int(round_idx or 1))
        if skill_sequence:
            seq = tuple(max(1, min(4, int(x))) for x in skill_sequence)
            idx = current_round - 1
            if idx >= len(seq):
                return "escape" if escape_after_skill_sequence else "skill4"
            skill_num = seq[idx]
            return "skill" if skill_num == 1 else f"skill{skill_num}"
        if cup in ("草系", "飞行系", "机械系"):
            return "skill4"
        return "skill2" if current_round <= 1 else "skill4"

    def _master_cup_handle_escape_post_battle(
        self,
        regions,
        use_foreground: bool,
        config,
        *,
        tag: str,
        escape_round: int,
    ) -> bool:
        self._emit(
            f"🏃 [{tag}] 本场在第{escape_round}回合逃跑，不计胜负；"
            "仅执行战后清理，不再恢复精灵一",
            "INFO",
        )
        if not self._unified_framework.stage4_post_battle(config):
            self._emit(f"❌ [{tag}] 逃跑后清理失败，停止循环", "ERROR")
            return False
        self._emit(
            f"✅ [{tag}] 逃跑后已清理，不恢复精灵一，准备下一场",
            "SUCCESS",
        )
        return True

    def _master_cup_recover_after_result(
        self,
        regions,
        use_foreground: bool,
        *,
        probe_result: str,
        tag: str,
    ) -> bool:
        if probe_result == "yellow":
            self._emit(
                f"🧹 [{tag}] 黄胜确认后执行一次1A1；战斗循环内不再恢复精灵一",
                "INFO",
            )
            if not self._wait_for_1and1_cleanup(
                use_foreground,
                timeout_s=MASTER_CUP_1AND1_TIMEOUT_SEC,
            ):
                self._emit(
                    f"❌ [{tag}] 黄胜后1A1未完成，停止循环",
                    "ERROR",
                )
                return False
            self._emit(f"✅ [{tag}] 黄胜后1A1完成", "SUCCESS")
        else:
            time.sleep(1.0)

        self._emit(
            f"✅ [{tag}] 战后处理完成，不恢复精灵一，继续大师杯循环",
            "SUCCESS",
        )
        return True

    def run_master_cup_mode(
        self,
        cup_type: str = "水系",
        use_foreground: bool = False,
        yellow_target_count: int = 36,
        pre_setup: bool = False,
        norm_mode: bool = False,
        target_pet_id: Optional[int] = None,
        escape_non_target: bool = False,
        escape_pet_id: Optional[int] = None,
        skill_sequence: Optional[Tuple[int, ...]] = None,
        escape_after_skill_sequence: bool = False,
        restore_light_after_finish: bool = False,
        pre_setup_spec: Optional[Dict[str, Any]] = None,
        allow_568_battle: bool = False,
    ) -> bool:
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口：无法执行大师杯", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if not regions:
            self._emit("❌ 大师杯缺少 bot.regions", "ERROR")
            return False

        cup = str(cup_type or "水系").strip() or "水系"
        if cup == "诺姆" or norm_mode:
            return self._run_master_cup_norm_mode(use_foreground, pre_setup)
        uses_568_profile = cup in MASTER_CUP_568_TYPES
        expected_568_party_ids = set(
            MASTER_CUP_568_OWN_PARTY_IDS
            if pre_setup
            else MASTER_CUP_DEFAULT_OWN_PARTY_IDS
        )
        if pre_setup and target_pet_id is None and escape_pet_id is None and not skill_sequence:
            restore_light_after_finish = True
        if uses_568_profile and skill_sequence is None:
            skill_sequence = MASTER_CUP_568_SKILL_SEQUENCE
            escape_after_skill_sequence = True
        escape_pet_id = self._master_cup_resolve_escape_pet_id(
            escape_pet_id,
            allow_568_battle=(allow_568_battle or uses_568_profile),
        )

        type_key = f"大师杯.{cup}"
        for key in (MASTER_CUP_ENTRY_KEY, type_key, MASTER_CUP_START_KEY):
            if not regions.get(key):
                self._emit(f"❌ 大师杯缺少区域：{key}", "ERROR")
                return False

        if cup not in MASTER_CUP_SUPPORTED_TYPES:
            self._emit(f"⚠️ 大师杯「{cup}」暂未纳入执行列表", "WARN")
            return False

        required_battle_keys = ["对战.使用技能四", "对战.胜利探针", "对话框.对战胜利确认"]
        if (
            skill_sequence
            and 2 in tuple(int(x) for x in skill_sequence)
        ) or (
            not skill_sequence
            and cup not in ("草系", "飞行系", "机械系")
        ):
            required_battle_keys.append("对战.使用技能二")
        if skill_sequence and 3 in tuple(int(x) for x in skill_sequence):
            required_battle_keys.append("对战.使用技能三")
        for key in required_battle_keys:
            if not regions.get(key):
                self._emit(f"❌ 大师杯{cup}缺少区域：{key}", "ERROR")
                return False
        if (
            escape_non_target
            or escape_pet_id is not None
            or uses_568_profile
            or escape_after_skill_sequence
        ):
            escape_panel_ok = any(regions.get(k) for k in ("对战.逃跑.切换逃跑面板", "战斗.切换逃跑面板", "对战.切换逃跑面板"))
            escape_confirm_ok = any(regions.get(k) for k in ("对战.逃跑.确认逃跑", "对战.确认逃跑", "战斗.确认逃跑"))
            if not escape_panel_ok or not escape_confirm_ok:
                self._emit(f"❌ 大师杯{cup}缺少逃跑区域：切换逃跑面板/确认逃跑", "ERROR")
                return False

        if not self._ensure_unified_framework(regions):
            return False
        if pre_setup:
            if not self._run_master_cup_pre_setup(
                regions,
                cup,
                use_foreground,
                spec_override=pre_setup_spec,
            ):
                return False
        try:
            from config import TEMPLATES_PATH
        except Exception:
            TEMPLATES_PATH = os.path.join(BASE_PATH, "assets", "templates")
        cleaner = PostBattleCleaner(self.bot, regions, TEMPLATES_PATH)
        try:
            yellow_target = max(1, int(yellow_target_count))
        except (TypeError, ValueError):
            yellow_target = 36

        def abort_check() -> bool:
            return self._should_abort()

        if skill_sequence:
            skill_desc = "→".join(f"技能{int(x)}" for x in skill_sequence)
            skill_desc += "→仍未击败则逃跑" if escape_after_skill_sequence else "→技能4循环"
        else:
            skill_desc = "全程四技能" if cup in ("草系", "飞行系", "机械系") else "二技能一次→四技能循环"

        try:
            from core.logger import kernel_cursor

            attempts = 0
            yellow_wins = 0
            white_losses = 0
            self._emit(
                f"🏆 大师杯{cup}循环启动：入战前循环点击 1→{cup}→开始，"
                f"战斗技能顺序（{skill_desc}），目标黄胜 {yellow_target} 次；白色失利不计数",
                "SYSTEM",
            )
            while not self._should_abort() and yellow_wins < yellow_target:
                attempts += 1
                tag = f"大师杯{cup}·第{attempts}场·黄胜{yellow_wins}/{yellow_target}"

                start_cursor = kernel_cursor()
                click_stop = threading.Event()
                click_failed = threading.Event()
                battle_state = {
                    "pet_ids": None,
                    "pet_id_counts": None,
                    "escaped": False,
                    "escape_round": None,
                    "568_decision_logged": False,
                }

                def _battle_pet_id_counts() -> Dict[int, int]:
                    cached = battle_state.get("pet_id_counts")
                    if cached is not None:
                        return dict(cached)
                    counts = self._collect_master_cup_battle_pet_id_counts(start_cursor)
                    battle_state["pet_id_counts"] = dict(counts)
                    battle_state["pet_ids"] = set(counts)
                    self._emit(
                        f"🔎 [{tag}] 入战 pet id计数={dict(sorted(counts.items()))}",
                        "INFO",
                    )
                    return dict(counts)

                def _battle_pet_ids() -> set:
                    cached = battle_state.get("pet_ids")
                    if cached is not None:
                        return set(cached)
                    return set(_battle_pet_id_counts())

                def action_callback(round_idx: int) -> str:
                    pet_ids = _battle_pet_ids()
                    if target_pet_id is not None and escape_non_target and int(target_pet_id) not in pet_ids:
                        battle_state["escaped"] = True
                        battle_state["escape_round"] = int(round_idx or 1)
                        self._emit(f"🏃 [{tag}] 未遇到目标 {int(target_pet_id)}，逃跑", "INFO")
                        return "escape"
                    if escape_pet_id is not None and int(escape_pet_id) in pet_ids:
                        battle_state["escaped"] = True
                        battle_state["escape_round"] = int(round_idx or 1)
                        self._emit(f"🏃 [{tag}] 遇到 {int(escape_pet_id)}，按配置逃跑", "INFO")
                        return "escape"
                    if uses_568_profile:
                        counts = _battle_pet_id_counts()
                        enemy_ids = self._master_cup_enemy_pet_ids_for_568(
                            counts,
                            expected_568_party_ids,
                        )
                        if self._master_cup_should_escape_568_only(
                            counts,
                            expected_568_party_ids,
                        ):
                            battle_state["escaped"] = True
                            battle_state["escape_round"] = int(round_idx or 1)
                            self._emit(
                                f"🏃 [{tag}] 敌方ID仅568，立即逃跑；敌方ID={sorted(enemy_ids)}",
                                "INFO",
                            )
                            return "escape"
                        if not battle_state.get("568_decision_logged"):
                            battle_state["568_decision_logged"] = True
                            if 568 in enemy_ids and len(enemy_ids) > 1:
                                self._emit(
                                    f"⚔️ [{tag}] 敌方568与其他ID同时出现，不逃跑；敌方ID={sorted(enemy_ids)}",
                                    "INFO",
                                )
                            else:
                                self._emit(
                                    f"⚔️ [{tag}] 敌方并非仅568，正常战斗；敌方ID={sorted(enemy_ids)}",
                                    "INFO",
                                )
                    action = self._master_cup_skill_action(
                        cup,
                        round_idx,
                        skill_sequence=skill_sequence,
                        escape_after_skill_sequence=escape_after_skill_sequence,
                    )
                    if action == "escape":
                        battle_state["escaped"] = True
                        battle_state["escape_round"] = int(round_idx or 1)
                        self._emit(
                            f"🏃 [{tag}] 已执行完整技能序列仍未击败，回合{int(round_idx or 1)}逃跑",
                            "INFO",
                        )
                    return action

                config = BattleConfig(
                    mode=BattleMode.FIXED,
                    use_foreground=use_foreground,
                    action_callback=action_callback,
                    abort_check=abort_check,
                    skip_map10_white_end=True,
                )

                def _entry_click_loop() -> None:
                    loop_idx = 0
                    keys = (MASTER_CUP_ENTRY_KEY, type_key, MASTER_CUP_START_KEY)
                    while not click_stop.is_set() and not self._should_abort():
                        loop_idx += 1
                        self._emit(
                            f"🏆 [{tag}] 入战点击循环 {loop_idx}: 1 → {cup} → 开始",
                            "INFO" if loop_idx == 1 else "DEBUG",
                        )
                        for key in keys:
                            if click_stop.is_set() or self._should_abort():
                                return
                            if not self._click_region_safe(regions, key, use_foreground):
                                click_failed.set()
                                click_stop.set()
                                return
                            t0 = time.time()
                            while (
                                time.time() - t0 < MASTER_CUP_CLICK_GAP_SEC
                                and not click_stop.is_set()
                                and not self._should_abort()
                            ):
                                time.sleep(0.05)

                self._emit(
                    f"⏳ [{tag}] 循环点击并等待入战（{MASTER_CUP_ENTRY_TIMEOUT_SEC:.0f}s），{skill_desc}",
                    "INFO",
                )
                click_thread = threading.Thread(target=_entry_click_loop, daemon=True)
                click_thread.start()
                try:
                    success, _ = self._unified_framework.stage2_calibration_and_petitem(
                        trigger_callback=None,
                        use_foreground=use_foreground,
                        timeout_s=MASTER_CUP_ENTRY_TIMEOUT_SEC,
                        skip_stage1=True,
                        config=config,
                        initial_cursor=start_cursor,
                    )
                finally:
                    click_stop.set()
                    click_thread.join(timeout=1.0)
                if click_failed.is_set():
                    self._emit(f"❌ [{tag}] 入战点击循环失败，停止循环", "ERROR")
                    return False
                if not success:
                    self._emit(f"❌ [{tag}] 未检测到入战/PetItem，停止循环", "ERROR")
                    return False

                if not self._unified_framework.stage3_battle_loop(config):
                    if self._should_abort():
                        break
                    self._emit(f"❌ [{tag}] 战斗循环未正常结束，停止循环", "ERROR")
                    return False

                if battle_state.get("escaped"):
                    escape_round = int(battle_state.get("escape_round") or 1)
                    if not self._master_cup_handle_escape_post_battle(
                        regions,
                        use_foreground,
                        config,
                        tag=tag,
                        escape_round=escape_round,
                    ):
                        return False
                    time.sleep(0.6)
                    continue

                self._emit(f"⏳ [{tag}] 等待结算 UI 稳定（2.5秒）", "INFO")
                time.sleep(2.5)
                self._emit(f"🟡 [{tag}] 检测结算黄/白探针", "INFO")
                probe_result = self._detect_victory_probe_result(
                    cleaner,
                    use_foreground,
                    timeout_s=8.0,
                )
                if probe_result not in ("yellow", "white"):
                    self._emit(f"❌ [{tag}] 未检测到结算黄/白探针，停止循环", "ERROR")
                    return False

                if probe_result == "yellow":
                    yellow_wins += 1
                    result_text = f"黄胜 +1（黄胜 {yellow_wins}/{yellow_target}，白败 {white_losses}）"
                    result_level = "SUCCESS"
                else:
                    white_losses += 1
                    result_text = f"白色失利不计数（黄胜 {yellow_wins}/{yellow_target}，白败 {white_losses}）"
                    result_level = "INFO"
                self._emit(
                    f"✅ [{tag}] 结算探针={probe_result}，{result_text}，点击胜利确认后继续循环",
                    result_level,
                )
                if not self._click_region_safe(regions, "对话框.对战胜利确认", use_foreground):
                    self._emit(f"❌ [{tag}] 点击胜利确认失败，停止循环", "ERROR")
                    return False
                if not self._master_cup_recover_after_result(
                    regions,
                    use_foreground,
                    probe_result=probe_result,
                    tag=tag,
                ):
                    return False

                if yellow_wins >= yellow_target:
                    self._emit(
                        f"✅ [{tag}] 大师杯完成：黄胜 {yellow_wins}/{yellow_target}，白败 {white_losses}，总对战 {attempts}",
                        "SUCCESS",
                    )
                    if restore_light_after_finish:
                        if not self._master_cup_restore_67_after_run(
                            regions,
                            use_foreground,
                            log_tag=f"{tag}·青色换回67",
                            put_back_pet_one=(cup == "地面系"),
                        ):
                            return False
                    return True

                self._emit(f"✅ [{tag}] 完成，准备下一场", "SUCCESS")
                time.sleep(0.6)

            self._emit(
                f"⛔ 大师杯{cup}循环已停止（黄胜 {yellow_wins}/{yellow_target}，白败 {white_losses}，总对战 {attempts}）",
                "SYSTEM",
            )
            return yellow_wins >= yellow_target
        except Exception as e:
            self._emit(f"❌ 大师杯{cup}异常: {e}", "ERROR")
            return False

    @staticmethod
    def _hatch_rgb_is_gray(rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        return 35 <= min(r, g, b) and max(r, g, b) <= 235 and (max(r, g, b) - min(r, g, b)) <= 18

    @staticmethod
    def _hatch_color_name(rgb: Optional[Tuple[int, int, int]]) -> str:
        if DailyRunner._hatch_rgb_is_gray(rgb):
            return "gray"
        color = UnifiedBattleFramework._classify_calibration_cell_rgb(rgb)
        if color == "purple":
            return "purple" if DailyRunner._hatch_rgb_is_purple(rgb) else "unknown"
        if color == "unknown" and DailyRunner._hatch_rgb_is_purple(rgb):
            return "purple"
        return color or "unknown"

    @staticmethod
    def _hatch_rgb_is_purple(rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        return (
            45 <= r <= 100
            and 12 <= g <= 50
            and 58 <= b <= 112
            and r > g + 22
            and b > g + 35
        )

    def _hatch_wait_pet_distribution(
        self,
        regions,
        expected_counts: Dict[str, int],
        *,
        click_key: Optional[str] = None,
        use_foreground: bool = False,
        timeout_s: float = HATCH_COLOR_WAIT_TIMEOUT_SEC,
        log_tag: str = "孵化开始",
    ) -> Optional[str]:
        t0 = time.time()
        last_log = 0.0
        last_state = ""
        clicked = 0
        target_color = "purple" if expected_counts.get("purple") == 1 else "cyan"
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return None
            counts: Dict[str, int] = {}
            slots_by_color: Dict[str, List[str]] = {}
            snapshots = []
            for cn in HATCH_CN:
                key = f"孵化.精灵{cn}"
                rgb = mean_rgb_for_region_key(regions, key)
                color = self._hatch_color_name(rgb)
                counts[color] = counts.get(color, 0) + 1
                slots_by_color.setdefault(color, []).append(cn)
                snapshots.append(f"{cn}:{color}:{rgb}")
            last_state = " | ".join(snapshots)
            if (
                sum(counts.get(color, 0) for color in expected_counts) == len(HATCH_CN)
                and all(counts.get(color, 0) == expected for color, expected in expected_counts.items())
                and len(slots_by_color.get(target_color, [])) == 1
            ):
                target_slot = slots_by_color[target_color][0]
                self._emit(
                    f"✅ [{log_tag}] 槽位颜色达标：{expected_counts}，{target_color}=精灵{target_slot}；{last_state}",
                    "SUCCESS",
                )
                return target_slot
            now = time.time()
            if now - last_log >= 0.8:
                last_log = now
                self._emit(
                    f"⏳ [{log_tag}] 持续点击{click_key or '当前侧'}并等待槽位颜色 {expected_counts}；当前={counts}；点击={clicked}；{last_state}",
                    "DEBUG",
                )
            if click_key:
                if not self._click_region_safe(regions, click_key, use_foreground):
                    return None
                clicked += 1
                time.sleep(HATCH_SIDE_CLICK_GAP_SEC)
                continue
            time.sleep(0.08)
        self._emit(
            f"❌ [{log_tag}] 等待槽位颜色超时：目标={expected_counts}；点击={clicked}；最后={last_state}",
            "ERROR",
        )
        return None

    def _hatch_click_pet_until_selected(
        self,
        regions,
        cn: str,
        use_foreground: bool,
        *,
        color_label: str = "目标",
        timeout_s: float = HATCH_SELECT_TIMEOUT_SEC,
        log_tag: str = "孵化开始",
    ) -> bool:
        click_key = f"孵化.精灵{cn}"
        selected_key = f"孵化.选中{cn}"
        t0 = time.time()
        clicked = 0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            rgb = mean_rgb_for_region_key(regions, selected_key)
            if self._fusion_is_yellow(rgb):
                self._emit(f"✅ [{log_tag}] {selected_key} 已变黄，{color_label}精灵{cn}已选中", "SUCCESS")
                return True
            if not self._click_region_safe(regions, click_key, use_foreground):
                return False
            clicked += 1
            time.sleep(0.12)
        self._emit(
            f"❌ [{log_tag}] 点击{color_label}精灵{cn} {clicked} 次后仍未选中，{selected_key}={mean_rgb_for_region_key(regions, selected_key)}",
            "ERROR",
        )
        return False

    def _hatch_wait_probe_purple(
        self,
        regions,
        key: str,
        *,
        timeout_s: float = HATCH_PROBE_TIMEOUT_SEC,
        log_tag: str = "孵化开始",
    ) -> bool:
        t0 = time.time()
        last_log = 0.0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            rgb = mean_rgb_for_region_key(regions, key)
            if self._hatch_color_name(rgb) == "purple":
                self._emit(f"✅ [{log_tag}] {key} 已变紫色：RGB={rgb}", "SUCCESS")
                return True
            now = time.time()
            if now - last_log >= 0.8:
                last_log = now
                self._emit(f"⏳ [{log_tag}] 等待 {key} 变紫色，当前RGB={rgb}", "DEBUG")
            time.sleep(0.08)
        self._emit(f"❌ [{log_tag}] 等待 {key} 变紫色超时，最后RGB={mean_rgb_for_region_key(regions, key)}", "ERROR")
        return False

    @staticmethod
    def _hatch_rgb_is_light_white(rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        light_white = r >= 180 and g >= 215 and b >= 215
        light_yellow = r >= 220 and g >= 245 and b >= 145
        return light_white or light_yellow

    @staticmethod
    def _hatch_rgb_is_nearly_red(rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        return r >= 225 and g <= 45 and b <= 45 and (r - g) >= 180 and (r - b) >= 180

    @staticmethod
    def _hatch_rgb_is_end_gray(rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        channels = (r, g, b)
        return max(channels) - min(channels) <= 5 and all(abs(v - 78) <= 8 for v in channels)

    def _hatch_xls_path(self) -> str:
        return os.path.join(self._records_dir(), "孵化.xls")

    def _append_hatch_xls_record(self, phase: str) -> bool:
        now = self._beijing_now()
        path = self._hatch_xls_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write_header = not os.path.exists(path) or os.path.getsize(path) == 0
            with open(path, "a", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f, delimiter="\t")
                if write_header:
                    writer.writerow(("time", "phase"))
                writer.writerow((now.isoformat(timespec="seconds"), str(phase or "").strip()))
            self._emit(
                f"🧾 [孵化记录] 已写入 {phase}: {now.isoformat(timespec='seconds')} -> {path}",
                "INFO",
            )
            return True
        except Exception as e:
            self._emit(f"⚠️ [孵化记录] 写入失败: {e}", "WARN")
            return False

    def get_last_hatch_record(self) -> Optional[Dict[str, Any]]:
        path = self._hatch_xls_path()
        if not os.path.isfile(path):
            return None
        last: Optional[Dict[str, Any]] = None
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f, delimiter="\t"):
                    if not isinstance(row, dict):
                        continue
                    raw_time = str(row.get("time") or "").strip()
                    raw_phase = str(row.get("phase") or "").strip()
                    if not raw_time and not raw_phase:
                        continue
                    dt = self._parse_record_datetime(raw_time)
                    last = {"time": raw_time, "phase": raw_phase, "datetime": dt}
        except Exception as e:
            self._emit(f"⚠️ [孵化记录] 读取失败，按需要执行处理: {e}", "WARN")
            return None
        return last

    def hatch_one_click_daily_due_state(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or self._beijing_now()
        record = self.get_last_hatch_record()
        if not record:
            return {
                "due": True,
                "reason": "无孵化记录",
                "phase": "",
                "elapsed_hours": None,
                "last_time": "",
            }
        phase = str(record.get("phase") or "").strip().lower()
        dt = record.get("datetime")
        if phase in {"complete", "completed", "end", "ended"}:
            return {
                "due": True,
                "reason": f"上一条孵化记录为结束态({phase})",
                "phase": phase,
                "elapsed_hours": None,
                "last_time": str(record.get("time") or ""),
            }
        if dt is None:
            return {
                "due": True,
                "reason": "上一条孵化记录时间无法解析",
                "phase": phase,
                "elapsed_hours": None,
                "last_time": str(record.get("time") or ""),
            }
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        elapsed_hours = max(0.0, (now - dt.astimezone(now.tzinfo)).total_seconds() / 3600.0)
        if phase == "start" and elapsed_hours < 20.0:
            return {
                "due": False,
                "reason": f"上一条孵化 start 距今 {elapsed_hours:.2f}h，不足20h",
                "phase": phase,
                "elapsed_hours": elapsed_hours,
                "last_time": str(record.get("time") or ""),
            }
        if phase == "start":
            return {
                "due": True,
                "reason": f"上一条孵化 start 距今 {elapsed_hours:.2f}h，已满20h",
                "phase": phase,
                "elapsed_hours": elapsed_hours,
                "last_time": str(record.get("time") or ""),
            }
        return {
            "due": True,
            "reason": f"上一条孵化记录阶段为 {phase or '空'}，按需要执行处理",
            "phase": phase,
            "elapsed_hours": elapsed_hours,
            "last_time": str(record.get("time") or ""),
        }

    def _hatch_claim_end_if_ready(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
    ) -> Tuple[bool, bool]:
        """Return (ok, closed_running_panel) after the post-confirm red probe check."""
        rgb = mean_rgb_for_region_key(regions, HATCH_RED_PROBE_KEY)
        if self._hatch_rgb_is_nearly_red(rgb):
            self._emit(
                f"✅ [{log_tag}] 红探针已为红色，证明孵化已开始：RGB={rgb}；"
                "直接关闭面板并跳过后续流程",
                "SUCCESS",
            )
            if not self._click_region_safe(regions, HATCH_CLOSE_KEY, use_foreground):
                return False, False
            return True, True
        if not self._hatch_rgb_is_end_gray(rgb):
            self._emit(
                f"ℹ️ [{log_tag}] 红探针既不是78灰度也不是红色，跳过领取：RGB={rgb}",
                "INFO",
            )
            return True, False

        self._emit(f"✅ [{log_tag}] 红探针为78灰度，先执行 complete 孵化领取：RGB={rgb}", "SUCCESS")
        if not self._click_region_safe(regions, HATCH_CLAIM_KEY, use_foreground):
            return False, False
        if not self._wait_1and1_clear(
            regions,
            use_foreground,
            timeout_s=NEW_DAILY_SEQ2_1AND1_TIMEOUT_SEC,
            min_confirm_clicks=1,
            log_tag=f"{log_tag}·领取1AND1",
        ):
            return False, False
        self._append_hatch_xls_record("complete")
        return True, False

    def _hatch_close_if_already_running(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
        stable_scans: int = 3,
    ) -> Optional[bool]:
        """返回 None 表示未在孵化；True/False 表示命中后关闭成功/失败。"""
        stable_scans = max(1, int(stable_scans or 1))
        red_count = 0
        last_red_rgb = None
        for _ in range(stable_scans):
            if self._should_abort():
                return False
            last_red_rgb = mean_rgb_for_region_key(regions, HATCH_RED_PROBE_KEY)
            if not self._hatch_rgb_is_nearly_red(last_red_rgb):
                egg_rgb = mean_rgb_for_region_key(regions, HATCH_EGG_PROBE_KEY)
                self._emit(
                    f"ℹ️ [{log_tag}] 开场未处于孵化中：蛋探针={egg_rgb}，红探针={last_red_rgb}",
                    "INFO",
                )
                return None
            red_count += 1
            if red_count < stable_scans:
                time.sleep(0.08)

        egg_rgb = mean_rgb_for_region_key(regions, HATCH_EGG_PROBE_KEY)
        self._emit(
            f"✅ [{log_tag}] 开场红探针已稳定为红色({red_count}/{stable_scans})，"
            f"证明仍在孵化：蛋探针={egg_rgb}，红探针={last_red_rgb}；直接关闭并跳过后续流程",
            "SUCCESS",
        )
        return self._click_region_safe(regions, HATCH_CLOSE_KEY, use_foreground)

    def _hatch_wait_rgb_condition(
        self,
        regions,
        key: str,
        predicate: Callable[[Optional[Tuple[int, int, int]]], bool],
        desc: str,
        *,
        timeout_s: float = HATCH_EGG_PROBE_TIMEOUT_SEC,
        log_tag: str = "孵化开始",
        stable_scans: int = 1,
    ) -> bool:
        t0 = time.time()
        last_log = 0.0
        stable_scans = max(1, int(stable_scans or 1))
        stable_count = 0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            rgb = mean_rgb_for_region_key(regions, key)
            if predicate(rgb):
                stable_count += 1
                if stable_count >= stable_scans:
                    suffix = "" if stable_scans <= 1 else f"（稳定 {stable_count}/{stable_scans}）"
                    self._emit(f"✅ [{log_tag}] {key} 已变为{desc}{suffix}：RGB={rgb}", "SUCCESS")
                    return True
            else:
                stable_count = 0
            now = time.time()
            if now - last_log >= 0.8:
                last_log = now
                stable_text = "" if stable_scans <= 1 else f"，稳定={stable_count}/{stable_scans}"
                self._emit(f"⏳ [{log_tag}] 等待 {key} 变为{desc}{stable_text}，当前RGB={rgb}", "DEBUG")
            time.sleep(0.08)
        final_stable = "" if stable_scans <= 1 else f"，稳定={stable_count}/{stable_scans}"
        self._emit(
            f"❌ [{log_tag}] 等待 {key} 变为{desc}超时{final_stable}，最后RGB={mean_rgb_for_region_key(regions, key)}",
            "ERROR",
        )
        return False

    def _hatch_refresh_to_base_right(self, regions, use_foreground: bool, *, log_tag: str = "孵化开始") -> bool:
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner，无法刷新重连", "ERROR")
            return False
        stop_event = getattr(self.bot, "_stop_event", None) or self._new_daily_stop_event()
        self._emit(f"🔄 [{log_tag}] 刷新重连并回基地", "SYSTEM")
        if not drr.run_refresh_login_until_map(
            use_foreground,
            stop_event,
            include_base_and_map_gate=True,
        ):
            self._emit(f"❌ [{log_tag}] 刷新重连回基地失败", "ERROR")
            return False
        ensure_party = getattr(drr, "_rotation_clear_backpack_and_pick_or_skip", None)
        expected_party = getattr(drr, "_pickmode_expected_party_ids", None)
        if callable(ensure_party) and callable(expected_party):
            if not ensure_party(
                "nieo",
                use_foreground,
                stop_event,
                log_tag=f"{log_tag}·新六宠校验",
                expected_party_ids=expected_party(),
                verify_primary_166=False,
            ):
                self._emit(f"❌ [{log_tag}] 新六宠补取失败", "ERROR")
                return False
        else:
            self._emit(f"⚠️ [{log_tag}] 缺少新六宠校验入口，继续孵化流程", "WARN")
        time.sleep(0.3)
        self._emit(f"🖱️ [{log_tag}] 点击 {NEW_DAILY_SEQ2_BASE_RIGHT_KEY}", "INFO")
        if not self._click_region_safe(regions, NEW_DAILY_SEQ2_BASE_RIGHT_KEY, use_foreground):
            return False
        time.sleep(0.3)
        return True

    def run_hatch_start(self, use_foreground: bool = False) -> bool:
        tag = "孵化开始"
        regions = getattr(self.bot, "regions", None)
        if not regions:
            self._emit(f"❌ [{tag}] 缺少区域配置", "ERROR")
            return False
        required = [
            HATCH_OPEN_KEY,
            HATCH_LEFT_KEY,
            HATCH_RIGHT_KEY,
            HATCH_START_KEY,
            HATCH_CLAIM_KEY,
            HATCH_LEFT_PROBE_KEY,
            HATCH_RIGHT_PROBE_KEY,
            HATCH_EGG_PROBE_KEY,
            HATCH_EGG_CLICK_KEY,
            HATCH_RED_PROBE_KEY,
            HATCH_CLOSE_KEY,
            NEW_DAILY_SEQ2_BASE_RIGHT_KEY,
            "对话框.普通确认",
            "对话框.左边确认",
        ]
        for cn in HATCH_CN:
            required.append(f"孵化.精灵{cn}")
            required.append(f"孵化.选中{cn}")
        missing = [key for key in required if not regions.get(key)]
        if missing:
            self._emit(f"❌ [{tag}] 缺少区域：{', '.join(missing)}", "ERROR")
            return False

        self._emit(f"📋 [{tag}] 左侧持续点击直到4灰1紫1橙，右侧持续点击直到5灰1紫；完成后左确认一次、普通确认一次", "SYSTEM")
        if not self._hatch_refresh_to_base_right(regions, use_foreground, log_tag=tag):
            return False
        if not self._click_until_kernel_line_matches(
            regions,
            HATCH_OPEN_KEY,
            use_foreground,
            HATCH_PET_BREED_PANEL_RE,
            log_tag=f"{tag}·打开面板",
            timeout_s=20.0,
            click_gap_s=0.25,
        ):
            return False
        time.sleep(0.4)

        already_running_result = self._hatch_close_if_already_running(
            regions,
            use_foreground,
            log_tag=f"{tag}·开场探针",
        )
        if already_running_result is not None:
            if not already_running_result:
                self._emit(f"❌ [{tag}] 已在孵化中，但关闭孵化面板失败", "ERROR")
                return False
            self._emit(f"✅ [{tag}] 已在孵化中，本轮孵化流程跳过", "SUCCESS")
            return True

        left_purple = self._hatch_wait_pet_distribution(
            regions,
            {"gray": 4, "purple": 1, "orange": 1},
            click_key=HATCH_LEFT_KEY,
            use_foreground=use_foreground,
            log_tag=f"{tag}·左侧",
        )
        if not left_purple:
            return False
        if not self._hatch_click_pet_until_selected(regions, left_purple, use_foreground, color_label="紫色", log_tag=f"{tag}·左侧"):
            return False
        if not self._click_region_safe(regions, HATCH_START_KEY, use_foreground):
            return False
        if not self._hatch_wait_probe_purple(regions, HATCH_LEFT_PROBE_KEY, log_tag=f"{tag}·左探针"):
            return False

        right_purple = self._hatch_wait_pet_distribution(
            regions,
            {"gray": 5, "purple": 1},
            click_key=HATCH_RIGHT_KEY,
            use_foreground=use_foreground,
            log_tag=f"{tag}·右侧",
        )
        if not right_purple:
            return False
        if not self._hatch_click_pet_until_selected(regions, right_purple, use_foreground, color_label="紫色", log_tag=f"{tag}·右侧"):
            return False
        if not self._click_region_safe(regions, HATCH_START_KEY, use_foreground):
            return False
        if not self._hatch_wait_probe_purple(regions, HATCH_RIGHT_PROBE_KEY, log_tag=f"{tag}·右探针"):
            return False
        if not self._click_region_safe(regions, HATCH_START_KEY, use_foreground):
            return False
        time.sleep(0.2)

        if not self._wait_left_1and1_clear(
            regions,
            use_foreground,
            timeout_s=NEW_DAILY_SEQ2_1AND1_TIMEOUT_SEC,
            min_confirm_clicks=1,
            log_tag=f"{tag}·普通左确认",
        ):
            return False
        if not self._wait_1and1_clear(
            regions,
            use_foreground,
            timeout_s=NEW_DAILY_SEQ2_1AND1_TIMEOUT_SEC,
            min_confirm_clicks=1,
            log_tag=f"{tag}·普通右确认",
        ):
            return False
        self._emit(f"⏳ [{tag}·1AND1后complete检查] 普通1AND1结束，1.0s 后扫描红探针灰度", "INFO")
        time.sleep(1.0)
        post_confirm_ok, post_confirm_closed = self._hatch_claim_end_if_ready(
            regions,
            use_foreground,
            log_tag=f"{tag}·1AND1后complete检查",
        )
        if not post_confirm_ok:
            return False
        if post_confirm_closed:
            self._emit(f"✅ [{tag}] 红探针已确认孵化开始，面板已关闭，本轮后续流程跳过", "SUCCESS")
            return True
        if not self._hatch_wait_rgb_condition(
            regions,
            HATCH_EGG_PROBE_KEY,
            self._hatch_rgb_is_light_white,
            "高亮浅色（浅白/浅黄）",
            log_tag=f"{tag}·蛋探针",
        ):
            return False
        self._emit(f"🖱️ [{tag}] 点击 {HATCH_EGG_CLICK_KEY}", "INFO")
        if not self._click_region_safe(regions, HATCH_EGG_CLICK_KEY, use_foreground):
            return False
        if not self._wait_left_1and1_clear(
            regions,
            use_foreground,
            timeout_s=NEW_DAILY_SEQ2_1AND1_TIMEOUT_SEC,
            min_confirm_clicks=1,
            log_tag=f"{tag}·蛋后左确认",
        ):
            return False
        self._emit(f"⏳ [{tag}·红探针] 蛋后1AND1结束，0.5s 后开始稳定扫描", "INFO")
        time.sleep(0.5)
        if not self._hatch_wait_rgb_condition(
            regions,
            HATCH_RED_PROBE_KEY,
            self._hatch_rgb_is_nearly_red,
            "几乎纯红色",
            log_tag=f"{tag}·红探针",
            stable_scans=3,
        ):
            return False
        self._emit(f"📜 [{tag}] 执行固定脚本：亲密度", "SYSTEM")
        if not self.run_single_script("亲密度", bg_mode=not use_foreground):
            return False
        self._append_hatch_xls_record("start")
        self._emit(f"✅ [{tag}] 流程完成", "SUCCESS")
        return True

    def _nono_soul_fusion_csv_path(self) -> str:
        new_path = os.path.join(self._records_dir(), "nono_soul_fusion_schedule.csv")
        old_path = os.path.join(BASE_PATH, "data", "nono_soul_fusion_schedule.csv")
        if os.path.isfile(old_path) and not os.path.isfile(new_path):
            try:
                os.makedirs(os.path.dirname(new_path), exist_ok=True)
                os.replace(old_path, new_path)
            except Exception as e:
                self._emit(f"⚠️ [nono孵化记录] 迁移到 records 失败，暂用旧路径: {e}", "WARN")
                return old_path
        return new_path

    def _records_dir(self) -> str:
        return os.path.join(BASE_PATH, "data", "records")

    def _gacha_run_record_csv_path(self) -> str:
        return os.path.join(self._records_dir(), "gacha_runs.csv")

    def _yilu_daily_record_csv_path(self) -> str:
        return os.path.join(self._records_dir(), "yilu_daily.csv")

    def _lanlan_daily_record_csv_path(self) -> str:
        return os.path.join(self._records_dir(), "lanlan_daily.csv")

    def _light_mantis_weekly_record_csv_path(self) -> str:
        return os.path.join(self._records_dir(), "light_mantis_weekly.csv")

    def _one_click_release_weekly_record_csv_path(self) -> str:
        return os.path.join(self._records_dir(), "one_click_release_weekly.csv")

    def _master_cup_weekly_record_csv_path(self) -> str:
        return os.path.join(self._records_dir(), "master_cup_weekly.csv")

    def _one_click_daily_record_path(self) -> str:
        return os.path.join(self._records_dir(), "one_click_daily_status.json")

    def _one_click_daily_completion_csv_path(self) -> str:
        return os.path.join(self._records_dir(), "one_click_daily_completed.csv")

    def _one_click_daily_completed_key_from_state(self, state: Dict[str, Any]) -> str:
        if not isinstance(state, dict) or state.get("status") != "complete":
            return ""
        completed_at = self._parse_record_datetime(state.get("completed_at", ""))
        if completed_at is not None:
            return self._yilu_daily_key(completed_at)
        return str(state.get("business_day") or "")

    def _has_one_click_daily_completion_record(self, business_day: str) -> bool:
        target_day = str(business_day or "").strip()
        if not target_day:
            return False
        path = self._one_click_daily_completion_csv_path()
        if not os.path.isfile(path):
            return False
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    row_day = str(row.get("business_day") or "").strip()
                    if not row_day:
                        completed_at = self._parse_record_datetime(row.get("time", ""))
                        if completed_at is not None:
                            row_day = self._yilu_daily_key(completed_at)
                    if row_day == target_day:
                        return True
        except Exception as e:
            self._emit(f"⚠️ [一键日常完成台账] 读取失败，继续检查进度记录: {e}", "WARN")
        return False

    def _append_one_click_daily_completion_record(
        self,
        completed_at: datetime,
        *,
        note: str = "",
    ) -> bool:
        business_day = self._yilu_daily_key(completed_at)
        if self._has_one_click_daily_completion_record(business_day):
            return True
        path = self._one_click_daily_completion_csv_path()
        exists = os.path.isfile(path) and os.path.getsize(path) > 0
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=("time", "business_day", "phase", "note"),
                )
                if not exists:
                    writer.writeheader()
                writer.writerow(
                    {
                        "time": completed_at.isoformat(timespec="milliseconds"),
                        "business_day": business_day,
                        "phase": "complete",
                        "note": str(note or ""),
                    }
                )
            return True
        except Exception as e:
            self._emit(f"⚠️ [一键日常完成台账] 写入失败: {e}", "WARN")
            return False

    def has_one_click_daily_complete_today(self, now: Optional[datetime] = None) -> bool:
        today_key = self._yilu_daily_key(now or self._beijing_now())
        path = self._one_click_daily_record_path()
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                if self._one_click_daily_completed_key_from_state(state) == today_key:
                    return True
            except Exception as e:
                self._emit(f"⚠️ [一键日常记录] 读取失败，继续检查完成台账: {e}", "WARN")
        return self._has_one_click_daily_completion_record(today_key)

    def _write_one_click_daily_progress(self) -> None:
        state = self._one_click_daily_progress
        if not isinstance(state, dict):
            return
        path = self._one_click_daily_record_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception as e:
            logger.warning("[OneClickDaily] progress write failed: %s", e)

    def begin_one_click_daily_progress(self, start_variant: str, start_step: int) -> None:
        now = self._beijing_now().isoformat(timespec="seconds")
        self._one_click_daily_progress = {
            "business_day": self._yilu_daily_key(self._beijing_now()),
            "status": "running",
            "started_at": now,
            "updated_at": now,
            "completed_at": "",
            "start_variant": str(start_variant),
            "start_step": int(start_step),
            "current_step": "",
            "last_completed_step": "",
            "reason": "",
        }
        self._write_one_click_daily_progress()

    def mark_one_click_daily_progress(
        self,
        step_label: str,
        *,
        variant: Optional[str] = None,
        step: Optional[int] = None,
        completed: bool = False,
    ) -> None:
        state = self._one_click_daily_progress
        if not isinstance(state, dict) or state.get("status") != "running":
            return
        state["current_step"] = str(step_label)
        if completed:
            state["last_completed_step"] = str(step_label)
        if variant is not None:
            state["active_variant"] = str(variant)
        if step is not None:
            state["active_step"] = int(step)
        state["updated_at"] = self._beijing_now().isoformat(timespec="seconds")
        self._write_one_click_daily_progress()

    def finish_one_click_daily_progress(self, status: str, reason: str = "") -> None:
        state = self._one_click_daily_progress
        if not isinstance(state, dict) or state.get("status") != "running":
            return
        now_dt = self._beijing_now()
        now = now_dt.isoformat(timespec="seconds")
        state["status"] = str(status)
        state["reason"] = str(reason)
        state["updated_at"] = now
        if status == "complete":
            state["completed_at"] = now
            state["business_day"] = self._yilu_daily_key(now_dt)
        self._write_one_click_daily_progress()
        if status == "complete":
            self._append_one_click_daily_completion_record(now_dt, note=reason)

    def _track_one_click_daily_step_from_log(self, text: str) -> None:
        state = self._one_click_daily_progress
        if not isinstance(state, dict) or state.get("status") != "running":
            return
        variant = state.get("active_variant")
        if not variant:
            return
        markers = {"①": 1, "②": 2, "③": 3, "④": 4, "⑤": 5, "⑥": 6, "⑦": 7, "⑧": 8, "⑨": 9}
        for marker, step in markers.items():
            if marker in str(text):
                label = f"方案{variant} 第{step}步"
                if state.get("current_step") != label:
                    previous_step = state.get("current_step")
                    if previous_step:
                        state["last_completed_step"] = str(previous_step)
                    self.mark_one_click_daily_progress(
                        label, variant=str(variant), step=step
                    )
                return

    @staticmethod
    def _beijing_now() -> datetime:
        return datetime.now(timezone(timedelta(hours=8)))

    def _new_gacha_run_id(self) -> str:
        return f"GACHA-{uuid.uuid4().hex}"

    def _new_gacha_session_id(self) -> str:
        return f"GACHA-SESSION-{uuid.uuid4().hex}"

    @staticmethod
    def _format_gacha_duration(duration_seconds: float) -> str:
        sign = "-" if duration_seconds < 0 else ""
        total_milliseconds = int(round(abs(duration_seconds) * 1000))
        hours, remainder = divmod(total_milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, milliseconds = divmod(remainder, 1000)
        return (
            f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}."
            f"{milliseconds:03d}"
        )

    def _append_gacha_completion_record(
        self,
        run_id: str,
        *,
        session_id: str,
        session_cycle: int,
        session_total: int,
        session_started_at: datetime,
        completed_at: Optional[datetime] = None,
        previous_completed_at: Optional[datetime] = None,
        duration_seconds: Optional[float] = None,
        rolling_average_seconds: Optional[float] = None,
        trend_delta_seconds: Optional[float] = None,
    ) -> bool:
        current = completed_at or self._beijing_now()
        beijing_tz = timezone(timedelta(hours=8))
        if current.tzinfo is None:
            current = current.replace(tzinfo=beijing_tz)
        else:
            current = current.astimezone(beijing_tz)

        path = self._gacha_run_record_csv_path()
        fieldnames = (
            "session_id",
            "session_cycle",
            "session_total",
            "session_started_at",
            "run_id",
            "timestamp",
            "one_and_one_cleared_at",
            "previous_timestamp",
            "duration_seconds",
            "duration",
            "rolling_average_seconds",
            "trend_delta_seconds",
            "trend",
        )
        try:
            with GACHA_RUN_RECORD_LOCK:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                exists = os.path.isfile(path) and os.path.getsize(path) > 0
                if exists:
                    with open(path, "r", encoding="utf-8-sig", newline="") as f:
                        reader = csv.DictReader(f)
                        existing_fieldnames = tuple(reader.fieldnames or ())
                        existing_rows = (
                            list(reader)
                            if existing_fieldnames != fieldnames
                            else []
                        )
                    if existing_rows or existing_fieldnames != fieldnames:
                        temp_path = f"{path}.session-migration.tmp"
                        with open(temp_path, "w", encoding="utf-8", newline="") as f:
                            writer = csv.DictWriter(f, fieldnames=fieldnames)
                            writer.writeheader()
                            for row in existing_rows:
                                writer.writerow(
                                    {
                                        key: row.get(key, "")
                                        for key in fieldnames
                                    }
                                )
                        os.replace(temp_path, path)

                previous = previous_completed_at
                if previous is not None:
                    if previous.tzinfo is None:
                        previous = previous.replace(tzinfo=beijing_tz)
                    else:
                        previous = previous.astimezone(beijing_tz)
                started = session_started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=beijing_tz)
                else:
                    started = started.astimezone(beijing_tz)
                duration = (
                    self._format_gacha_duration(duration_seconds)
                    if duration_seconds is not None
                    else ""
                )
                if trend_delta_seconds is None:
                    trend = ""
                elif trend_delta_seconds < 0:
                    trend = "变快"
                elif trend_delta_seconds > 0:
                    trend = "变慢"
                else:
                    trend = "持平"

                with open(path, "a", encoding="utf-8", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    if not exists:
                        writer.writeheader()
                    writer.writerow(
                        {
                            "session_id": str(session_id),
                            "session_cycle": int(session_cycle),
                            "session_total": int(session_total),
                            "session_started_at": started.isoformat(
                                timespec="milliseconds"
                            ),
                            "run_id": str(run_id),
                            "timestamp": current.isoformat(timespec="milliseconds"),
                            "one_and_one_cleared_at": current.isoformat(
                                timespec="milliseconds"
                            ),
                            "previous_timestamp": (
                                previous.isoformat(timespec="milliseconds")
                                if previous is not None
                                else ""
                            ),
                            "duration_seconds": (
                                f"{duration_seconds:.3f}"
                                if duration_seconds is not None
                                else ""
                            ),
                            "duration": duration,
                            "rolling_average_seconds": (
                                f"{rolling_average_seconds:.3f}"
                                if rolling_average_seconds is not None
                                else ""
                            ),
                            "trend_delta_seconds": (
                                f"{trend_delta_seconds:.3f}"
                                if trend_delta_seconds is not None
                                else ""
                            ),
                            "trend": trend,
                        }
                    )

            return True
        except Exception as e:
            self._emit(
                f"⚠️ [扭蛋记录] run_id={run_id} 写入失败: {e}",
                "WARN",
            )
            return False

    @staticmethod
    def _yilu_daily_key(value: datetime) -> str:
        """依卢日界线：北京时间 06:00 到次日 05:59:59 算同一天。"""
        bj_tz = timezone(timedelta(hours=8))
        if value.tzinfo is None:
            bj = value.replace(tzinfo=bj_tz)
        else:
            bj = value.astimezone(bj_tz)
        if bj.hour < 6:
            bj = bj - timedelta(days=1)
        return bj.date().isoformat()

    @staticmethod
    def _parse_record_datetime(raw: str) -> Optional[datetime]:
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None

    def _yilu_has_daily_record_today(self, now: Optional[datetime] = None) -> bool:
        path = self._yilu_daily_record_csv_path()
        if not os.path.isfile(path):
            return False
        today_key = self._yilu_daily_key(now or self._beijing_now())
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    dt = self._parse_record_datetime(row.get("time", ""))
                    if dt is not None and self._yilu_daily_key(dt) == today_key:
                        return True
        except Exception as e:
            self._emit(f"⚠️ [依卢记录] 读取失败，按未执行处理: {e}", "WARN")
        return False

    def _append_yilu_daily_record(self, *, phase: str, note: str = "") -> bool:
        now = self._beijing_now()
        path = self._yilu_daily_record_csv_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            exists = os.path.isfile(path) and os.path.getsize(path) > 0
            with open(path, "a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=("time", "business_day", "phase", "note"),
                )
                if not exists:
                    writer.writeheader()
                writer.writerow(
                    {
                        "time": now.isoformat(timespec="milliseconds"),
                        "business_day": self._yilu_daily_key(now),
                        "phase": str(phase or ""),
                        "note": str(note or ""),
                    }
                )
            self._emit(
                f"🧾 [依卢记录] 已写入 {phase}: {now.isoformat(timespec='milliseconds')}",
                "INFO",
            )
            return True
        except Exception as e:
            self._emit(f"⚠️ [依卢记录] 写入失败: {e}", "WARN")
            return False

    def _lanlan_has_daily_record_today(self, now: Optional[datetime] = None) -> bool:
        path = self._lanlan_daily_record_csv_path()
        if not os.path.isfile(path):
            return False
        today_key = self._yilu_daily_key(now or self._beijing_now())
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    dt = self._parse_record_datetime(row.get("time", ""))
                    if dt is not None and self._yilu_daily_key(dt) == today_key:
                        return True
        except Exception as e:
            self._emit(f"⚠️ [岚岚记录] 读取失败，按未执行处理: {e}", "WARN")
        return False

    def _append_lanlan_daily_record(self, *, note: str = "") -> bool:
        now = self._beijing_now()
        path = self._lanlan_daily_record_csv_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            exists = os.path.isfile(path) and os.path.getsize(path) > 0
            with open(path, "a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=("time", "business_day", "phase", "note"),
                )
                if not exists:
                    writer.writeheader()
                writer.writerow(
                    {
                        "time": now.isoformat(timespec="milliseconds"),
                        "business_day": self._yilu_daily_key(now),
                        "phase": "yellow_complete",
                        "note": str(note or ""),
                    }
                )
            self._emit(
                f"📋 [岚岚记录] 已写入 yellow_complete: {now.isoformat(timespec='milliseconds')}",
                "INFO",
            )
            return True
        except Exception as e:
            self._emit(f"⚠️ [岚岚记录] 写入失败: {e}", "WARN")
            return False

    def has_yilu_daily_record_today(self, now: Optional[datetime] = None) -> bool:
        return self._yilu_has_daily_record_today(now)

    def has_lanlan_daily_record_today(self, now: Optional[datetime] = None) -> bool:
        return self._lanlan_has_daily_record_today(now)

    def _light_mantis_has_weekly_record(self, now: Optional[datetime] = None) -> bool:
        path = self._light_mantis_weekly_record_csv_path()
        if not os.path.isfile(path):
            return False
        current_week = self._nono_soul_fusion_week_key(now or self._beijing_now())
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    dt = self._parse_record_datetime(row.get("time", ""))
                    if dt is not None and self._nono_soul_fusion_week_key(dt) == current_week:
                        return True
        except Exception as e:
            self._emit(f"⚠️ [光螳螂记录] 读取失败，按未执行处理: {e}", "WARN")
        return False

    def _append_light_mantis_weekly_record(self, *, note: str = "") -> bool:
        now = self._beijing_now()
        path = self._light_mantis_weekly_record_csv_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            exists = os.path.isfile(path) and os.path.getsize(path) > 0
            week_year, week_no = self._nono_soul_fusion_week_key(now)
            with open(path, "a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=("time", "business_week", "phase", "note"),
                )
                if not exists:
                    writer.writeheader()
                writer.writerow(
                    {
                        "time": now.isoformat(timespec="milliseconds"),
                        "business_week": f"{week_year}-W{week_no:02d}",
                        "phase": "yellow_complete",
                        "note": str(note or ""),
                    }
                )
            self._emit(
                f"📋 [光螳螂记录] 已写入 yellow_complete: {now.isoformat(timespec='milliseconds')}",
                "INFO",
            )
            return True
        except Exception as e:
            self._emit(f"⚠️ [光螳螂记录] 写入失败: {e}", "WARN")
            return False

    def has_light_mantis_weekly_record(self, now: Optional[datetime] = None) -> bool:
        return self._light_mantis_has_weekly_record(now)

    def has_one_click_release_weekly_record(
        self, now: Optional[datetime] = None
    ) -> bool:
        path = self._one_click_release_weekly_record_csv_path()
        if not os.path.isfile(path):
            return False
        current_week = self._nono_soul_fusion_week_key(now or self._beijing_now())
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    completed_at = self._parse_record_datetime(row.get("time", ""))
                    if (
                        completed_at is not None
                        and self._nono_soul_fusion_week_key(completed_at)
                        == current_week
                    ):
                        return True
        except Exception as e:
            self._emit(f"⚠️ [周末放生记录] 读取失败，按未执行处理: {e}", "WARN")
        return False

    def append_one_click_release_weekly_record(self, *, note: str = "") -> bool:
        now = self._beijing_now()
        if self.has_one_click_release_weekly_record(now):
            return True
        path = self._one_click_release_weekly_record_csv_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            exists = os.path.isfile(path) and os.path.getsize(path) > 0
            week_year, week_no = self._nono_soul_fusion_week_key(now)
            with open(path, "a", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=("time", "business_week", "phase", "note"),
                )
                if not exists:
                    writer.writeheader()
                writer.writerow(
                    {
                        "time": now.isoformat(timespec="milliseconds"),
                        "business_week": f"{week_year}-W{week_no:02d}",
                        "phase": "complete",
                        "note": str(note or ""),
                    }
                )
            self._emit(
                f"📋 [周末放生记录] 已写入本周完成: {now.isoformat(timespec='milliseconds')}",
                "INFO",
            )
            return True
        except Exception as e:
            self._emit(f"⚠️ [周末放生记录] 写入失败: {e}", "WARN")
            return False

    def get_master_cup_weekly_record(
        self, now: Optional[datetime] = None
    ) -> Optional[Dict[str, str]]:
        path = self._master_cup_weekly_record_csv_path()
        if not os.path.isfile(path):
            return None
        current_week = self._nono_soul_fusion_week_key(now or self._beijing_now())
        latest: Optional[Dict[str, str]] = None
        latest_time: Optional[datetime] = None
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    if str(row.get("phase") or "complete").strip() != "complete":
                        continue
                    dt = self._parse_record_datetime(row.get("time", ""))
                    if (
                        dt is None
                        or self._nono_soul_fusion_week_key(dt) != current_week
                    ):
                        continue
                    if latest_time is None or dt > latest_time:
                        latest = dict(row)
                        latest_time = dt
        except Exception as e:
            self._emit(
                f"⚠️ [大师杯周记录] 读取失败，按本周未执行处理: {e}",
                "WARN",
            )
            return None
        return latest

    def has_master_cup_weekly_record(self, now: Optional[datetime] = None) -> bool:
        return self.get_master_cup_weekly_record(now) is not None

    def append_master_cup_weekly_record(
        self,
        *,
        cup_type: str,
        norm_ran: bool,
        yellow_target: int,
        pre_setup: bool,
        note: str = "",
    ) -> bool:
        now = self._beijing_now()
        path = self._master_cup_weekly_record_csv_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            exists = os.path.isfile(path) and os.path.getsize(path) > 0
            week_year, week_no = self._nono_soul_fusion_week_key(now)
            with open(path, "a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=(
                        "time",
                        "business_week",
                        "phase",
                        "cup_type",
                        "norm_ran",
                        "yellow_target",
                        "pre_setup",
                        "note",
                    ),
                )
                if not exists:
                    writer.writeheader()
                writer.writerow(
                    {
                        "time": now.isoformat(timespec="milliseconds"),
                        "business_week": f"{week_year}-W{week_no:02d}",
                        "phase": "complete",
                        "cup_type": str(cup_type or ""),
                        "norm_ran": str(bool(norm_ran)).lower(),
                        "yellow_target": max(1, int(yellow_target)),
                        "pre_setup": str(bool(pre_setup)).lower(),
                        "note": str(note or ""),
                    }
                )
            self._emit(
                "📋 [大师杯周记录] 已写入："
                f"时间={now.isoformat(timespec='milliseconds')}，"
                f"系别={cup_type}，诺姆={'是' if norm_ran else '否'}",
                "INFO",
            )
            return True
        except Exception as e:
            self._emit(f"⚠️ [大师杯周记录] 写入失败: {e}", "WARN")
            return False

    @staticmethod
    def _nono_soul_fusion_wait_hours(fusion_count: int, phase: str = "complete") -> float:
        _ = phase
        try:
            n = max(1, int(fusion_count))
        except (TypeError, ValueError):
            n = 1
        return min(0.5 + 1.5 * (n - 1), 11.0)

    @staticmethod
    def _nono_soul_fusion_week_key(value: datetime) -> Tuple[int, int]:
        """业务周从北京时间周一 06:00 开始；周一 00:00-05:59:59 仍算上一周。"""
        if value.tzinfo is None:
            bj = value.replace(tzinfo=timezone(timedelta(hours=8)))
        else:
            bj = value.astimezone(timezone(timedelta(hours=8)))
        if bj.weekday() == 0 and bj.hour < 6:
            bj = bj - timedelta(days=1)
        iso = bj.isocalendar()
        return int(iso.year), int(iso.week)

    def _nono_soul_fusion_same_business_week(self, a: datetime, b: datetime) -> bool:
        return self._nono_soul_fusion_week_key(a) == self._nono_soul_fusion_week_key(b)

    def _nono_soul_fusion_count_for_start(self, recorded_time: datetime, fusion_count: int, now: datetime) -> int:
        if not self._nono_soul_fusion_same_business_week(recorded_time, now):
            self._emit("ℹ️ [nono孵化] 开始时已进入新的一周，本次 start 记录次数写入1", "INFO")
            return 1
        return max(1, int(fusion_count or 1))

    def _nono_soul_fusion_count_after_complete(self, recorded_time: datetime, fusion_count: int, completed_at: datetime) -> int:
        if not self._nono_soul_fusion_same_business_week(recorded_time, completed_at):
            self._emit("ℹ️ [nono孵化] 完成时已进入新的一周，本次 complete 记录次数写入1", "INFO")
            return 1
        return max(1, int(fusion_count or 1)) + 1

    def _load_nono_soul_fusion_state(self) -> Tuple[datetime, int, str]:
        path = self._nono_soul_fusion_csv_path()
        now = self._beijing_now()
        default_time = now - timedelta(minutes=30)
        default_count = 0
        default_phase = "start"
        if not os.path.exists(path):
            self._append_nono_soul_fusion_record(default_time, default_count, default_phase)
            return default_time, default_count, default_phase

        try:
            with open(path, "r", newline="", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            if not rows:
                raise ValueError("empty csv")
            row = rows[-1]
            recorded_time = datetime.fromisoformat(str(row.get("time") or ""))
            if recorded_time.tzinfo is None:
                recorded_time = recorded_time.replace(tzinfo=timezone(timedelta(hours=8)))
            fusion_count = max(0, int(row.get("fusion_count") or default_count))
            phase = str(row.get("phase") or "complete").strip() or "complete"
            recorded_time = recorded_time.astimezone(timezone(timedelta(hours=8)))
            return recorded_time, fusion_count, phase
        except Exception as e:
            self._emit(f"⚠️ [nono孵化] 读取CSV失败，重新初始化：{e}", "WARN")
            self._append_nono_soul_fusion_record(default_time, default_count, default_phase)
            return default_time, default_count, default_phase

    def _append_nono_soul_fusion_record(self, recorded_time: datetime, fusion_count: int, phase: str) -> None:
        path = self._nono_soul_fusion_csv_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if recorded_time.tzinfo is None:
            recorded_time = recorded_time.replace(tzinfo=timezone(timedelta(hours=8)))
        fieldnames = ["time", "fusion_count", "phase"]
        write_header = not os.path.exists(path) or os.path.getsize(path) == 0
        if not write_header:
            with open(path, "r", newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                old_fieldnames = reader.fieldnames or []
                old_rows = list(reader)
            if old_fieldnames != fieldnames:
                with open(path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    for row in old_rows:
                        writer.writerow({
                            "time": row.get("time") or "",
                            "fusion_count": row.get("fusion_count") or 0,
                            "phase": row.get("phase") or "complete",
                        })
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerow({
                "time": recorded_time.astimezone(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
                "fusion_count": int(fusion_count),
                "phase": str(phase or "").strip() or "complete",
            })

    def _nono_soul_fusion_reconnect_reason(self) -> Optional[str]:
        """Return why a mode start must reconnect for the pending Nono fusion."""
        if not NONO_SOUL_FUSION_GLOBAL_ENABLED:
            return None
        recorded_time, fusion_count, phase = self._load_nono_soul_fusion_state()
        normalized_phase = str(phase or "").strip().lower()
        wait_hours = self._nono_soul_fusion_wait_hours(fusion_count, normalized_phase)
        due_time = recorded_time + timedelta(hours=wait_hours)

        if normalized_phase == "complete":
            return (
                f"上一条融合记录为{normalized_phase} "
                f"(time={recorded_time.isoformat(timespec='seconds')})"
            )
        if normalized_phase == "start" and self._beijing_now() >= due_time:
            return (
                "上一条融合 start 已到期 "
                f"(time={recorded_time.isoformat(timespec='seconds')}, "
                f"wait={wait_hours:g}h)"
            )
        return None

    def _enter_nono_fusion_guard(
        self,
        tag: str,
        *,
        wait_timeout_s: float = 300.0,
    ) -> Tuple[bool, bool]:
        """Enter the shared Nono fusion guard.

        Returns ``(ok, acquired_by_this_call)``. Same-thread nested calls are
        allowed and must not release the guard.
        """
        ident = threading.get_ident()
        if self._nono_fusion_guard_owner == ident:
            return True, False

        deadline = time.time() + max(0.0, float(wait_timeout_s))
        warned = False
        while not self._should_abort():
            if self._nono_fusion_guard.acquire(blocking=False):
                self._nono_fusion_guard_owner = ident
                self._nono_fusion_guard_tag = tag
                return True, True
            if not warned:
                owner_tag = self._nono_fusion_guard_tag or "未知入口"
                self._emit(
                    f"⏳ [{tag}] 已有Nono融合检查执行中：{owner_tag}，等待其结束以避免双层重连",
                    "WARN",
                )
                warned = True
            if time.time() >= deadline:
                self._emit(
                    f"⚠️ [{tag}] 等待Nono融合互斥超时，跳过本次重复融合入口",
                    "WARN",
                )
                return False, False
            time.sleep(0.25)
        return False, False

    def _exit_nono_fusion_guard(self, acquired_by_this_call: bool) -> None:
        if not acquired_by_this_call:
            return
        self._nono_fusion_guard_owner = None
        self._nono_fusion_guard_tag = ""
        try:
            self._nono_fusion_guard.release()
        except RuntimeError:
            pass

    def _restore_base_gate_after_nono_guard_wait(
        self,
        tag: str,
        use_foreground: bool,
    ) -> bool:
        """Rebuild the base gate before a waiting reconnect path continues."""
        drr = getattr(self.bot, "dar_route_runner", None)
        stop_event = getattr(self.bot, "_stop_event", None) or self._new_daily_stop_event()
        if drr is None:
            self._emit(f"❌ [{tag}] 等待融合互斥后缺少 dar_route_runner，无法恢复基地门控", "ERROR")
            return False
        if stop_event.is_set() or self._should_abort():
            return False
        self._emit(f"🔄 [{tag}] 等待融合互斥后重建基地门控，再继续后续流程", "SYSTEM")
        return bool(
            drr.run_refresh_login_until_map(
                use_foreground,
                stop_event,
                include_base_and_map_gate=True,
            )
        )

    def run_nono_soul_fusion_pre_mode_check(
        self,
        use_foreground: bool = False,
        *,
        mode_name: str,
    ) -> Tuple[bool, bool]:
        """Handle a due fusion before a rare/Nieo/resource mode starts.

        Returns ``(ok, fusion_handled)``.  Callers must run their normal
        backpack/pet/to-script preparation after ``fusion_handled`` is true.
        """
        if not NONO_SOUL_FUSION_GLOBAL_ENABLED:
            self._emit(f"⏸️ [{mode_name}] nono融合已全局禁用，跳过模式启动前检查", "INFO")
            return True, False
        if "轮换" in str(mode_name or ""):
            self._emit(f"[轮换模式] 已禁用 Nono 融合前检查：{mode_name}", "INFO")
            return True, False
        reason = self._nono_soul_fusion_reconnect_reason()
        if reason is None:
            return True, False
        tag = f"{mode_name}-融合前检查"

        regions = getattr(self.bot, "regions", None)
        drr = getattr(self.bot, "dar_route_runner", None)
        if not regions or drr is None:
            self._emit(f"❌ [{tag}] 缺少 regions 或 dar_route_runner", "ERROR")
            return False, False
        for key in ("刷新.基地", "刷新.基地右侧"):
            if not regions.get(key):
                self._emit(f"❌ [{tag}] 缺少区域：{key}", "ERROR")
                return False, False

        stop_event = getattr(self.bot, "_stop_event", None) or self._new_daily_stop_event()
        if stop_event.is_set() or self._should_abort():
            return False, False

        guard_ok, guard_acquired = self._enter_nono_fusion_guard(tag)
        if not guard_ok:
            return False, False
        try:
            reason = self._nono_soul_fusion_reconnect_reason()
            if reason is None:
                self._emit(f"✅ [{tag}] 互斥等待后复查：已无到期融合，跳过重复入口", "SUCCESS")
                return True, False

            retry_round = 0
            while not stop_event.is_set() and not self._should_abort():
                retry_round += 1
                self._emit(
                    f"🔄 [{tag}] {reason}，刷新重连后先执行融合（第 {retry_round} 轮）",
                    "SYSTEM",
                )
                if not drr.run_refresh_login_until_map(
                    use_foreground,
                    stop_event,
                    include_base_and_map_gate=True,
                ):
                    if stop_event.is_set() or self._should_abort():
                        break
                    self._emit(f"⚠️ [{tag}] 刷新重连/基地门控失败，保留任务并继续重试", "WARN")
                    time.sleep(1.0)
                    continue

                ok, handled = self.run_nono_soul_fusion_after_reconnect_check(
                    use_foreground=use_foreground,
                    mode_name=mode_name,
                )
                if ok:
                    if handled:
                        mark_ready = getattr(drr, "mark_nono_fusion_connection_ready", None)
                        if callable(mark_ready):
                            mark_ready()
                    return True, handled
                if stop_event.is_set() or self._should_abort():
                    break
                self._emit(f"⚠️ [{tag}] 融合未完成，保留任务并重新完整刷新后重试", "WARN")
                time.sleep(1.0)

            self._emit(f"⛔ [{tag}] 检测到手动停止，结束融合前检查", "SYSTEM")
            return False, False
        finally:
            self._exit_nono_fusion_guard(guard_acquired)

    def run_nono_soul_fusion_after_reconnect_check(
        self,
        use_foreground: bool = False,
        *,
        mode_name: str,
    ) -> Tuple[bool, bool]:
        """Run due fusion after a rare/Nieo reconnect has reached the base gate."""
        if not NONO_SOUL_FUSION_GLOBAL_ENABLED:
            self._emit(f"⏸️ [{mode_name}] nono融合已全局禁用，跳过重连检查", "INFO")
            return True, False
        drr = getattr(self.bot, "dar_route_runner", None)
        if bool(getattr(drr, "_is_rotation_mode", False)):
            self._emit(f"[轮换模式] 已禁用 Nono 融合重连检查：{mode_name}", "INFO")
            return True, False
        reason = self._nono_soul_fusion_reconnect_reason()
        if reason is None:
            return True, False
        tag = f"{mode_name}-融合重连检查"

        regions = getattr(self.bot, "regions", None)
        if not regions:
            self._emit(f"❌ [{tag}] 缺少 regions", "ERROR")
            return False, False
        for key in ("刷新.基地", "刷新.基地右侧"):
            if not regions.get(key):
                self._emit(f"❌ [{tag}] 缺少区域：{key}", "ERROR")
                return False, False

        guard_ok, guard_acquired = self._enter_nono_fusion_guard(tag)
        if not guard_ok:
            return False, False
        try:
            reason = self._nono_soul_fusion_reconnect_reason()
            if reason is None:
                self._emit(f"✅ [{tag}] 互斥等待后复查：已无到期融合，跳过重复入口", "SUCCESS")
                if guard_acquired and not self._restore_base_gate_after_nono_guard_wait(tag, use_foreground):
                    return False, False
                return True, False

            self._emit(f"🔄 [{tag}] {reason}，本次重连先执行融合", "SYSTEM")
            self._emit(f"🖱️ [{tag}] 基地门控后点击中间 → 右侧", "INFO")
            if not self._click_region_safe(regions, "刷新.基地", use_foreground):
                self._emit(f"❌ [{tag}] 点击刷新.基地失败", "ERROR")
                return False, False
            time.sleep(0.3)
            if not self._click_region_safe(regions, "刷新.基地右侧", use_foreground):
                self._emit(f"❌ [{tag}] 点击刷新.基地右侧失败", "ERROR")
                return False, False
            time.sleep(0.3)

            if not self.run_nono_soul_fusion_check(use_foreground=use_foreground):
                self._emit(f"❌ [{tag}] 融合流程失败，停止进入{mode_name}", "ERROR")
                return False, False
            self._emit(f"✅ [{tag}] 融合流程完成；继续模式前置清包、取宠和 to 脚本", "SUCCESS")
            return True, True
        finally:
            self._exit_nono_fusion_guard(guard_acquired)

    def _nono_rgb_is_pure_white(self, rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        return min(r, g, b) >= 240 and ((r + g + b) / 3.0) >= 248.0

    def _nono_rgb_is_start_confirm_blue(self, rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        target = (17, 115, 212)
        return (
            self._rgb_distance((r, g, b), target) <= 70
            or (r <= 60 and 80 <= g <= 155 and 165 <= b <= 245 and (b - r) >= 120 and (b - g) >= 45)
        )

    def _nono_rgb_close(self, a: Optional[Tuple[int, int, int]], b: Optional[Tuple[int, int, int]], tolerance: int = 3) -> bool:
        if a is None or b is None:
            return False
        return max(abs(int(a[i]) - int(b[i])) for i in range(3)) <= tolerance

    def _nono_wait_fusion_probes_stable(
        self,
        regions,
        *,
        timeout_s: float = NONO_FUSION_PROBE_STABLE_TIMEOUT_SEC,
        log_tag: str = "nono孵化·探针",
    ) -> Optional[Tuple[Tuple[int, int, int], Tuple[int, int, int]]]:
        self._emit(f"⏳ [{log_tag}] 扫描两个融合探针，等待颜色稳定…", "INFO")
        start = time.time()
        prev1 = None
        prev2 = None
        stable_hits = 0
        last_pair = None
        while (time.time() - start) < timeout_s:
            if self._should_abort():
                return None
            rgb1 = mean_rgb_for_region_key(regions, NONO_FUSION_PROBE_1_KEY)
            rgb2 = mean_rgb_for_region_key(regions, NONO_FUSION_PROBE_2_KEY)
            if rgb1 is not None and rgb2 is not None:
                if self._nono_rgb_close(prev1, rgb1) and self._nono_rgb_close(prev2, rgb2):
                    stable_hits += 1
                else:
                    stable_hits = 0
                prev1 = rgb1
                prev2 = rgb2
                last_pair = (rgb1, rgb2)
                if stable_hits >= 3:
                    self._emit(
                        f"✅ [{log_tag}] 探针稳定：探针一={self._format_rgb(rgb1)}，探针二={self._format_rgb(rgb2)}",
                        "SUCCESS",
                    )
                    return rgb1, rgb2
            time.sleep(0.08)

        if last_pair:
            self._emit(
                f"❌ [{log_tag}] 探针未稳定，最后读数：探针一={self._format_rgb(last_pair[0])}，探针二={self._format_rgb(last_pair[1])}",
                "ERROR",
            )
        else:
            self._emit(f"❌ [{log_tag}] 探针读色失败", "ERROR")
        return None

    def _nono_click_sweep_between(
        self,
        regions,
        left_key: str,
        confirm_key: str,
        use_foreground: bool,
        *,
        log_tag: str,
    ) -> bool:
        left = regions.get(left_key)
        confirm = regions.get(confirm_key)
        if not left or not confirm:
            self._emit(f"❌ [{log_tag}] 缺少 {left_key} 或 {confirm_key} 区域", "ERROR")
            return False
        try:
            start_x, y = left.sample_click_point()
            end_x, _ = confirm.sample_click_point()
            start_x = int(round(start_x))
            end_x = int(round(end_x))
            y = int(round(y))
            if end_x < start_x:
                start_x, end_x = end_x, start_x
            positions = list(range(start_x, end_x + 1, 5))
            if not positions or positions[-1] != end_x:
                positions.append(end_x)
            self._emit(
                f"🖱️ [{log_tag}] 横扫点击确认：x={start_x}->{end_x}, y={y}, 点数={len(positions)}",
                "INFO",
            )
            for x in positions:
                if self._should_abort():
                    return False
                if use_foreground:
                    window_manager.click(x, y)
                else:
                    window_manager.click_background(x, y)
                time.sleep(0.02)
            return True
        except Exception as e:
            self._emit(f"❌ [{log_tag}] 横扫点击确认失败：{e}", "ERROR")
            return False

    def _nono_click_fusion_confirm_sweep(self, regions, use_foreground: bool) -> bool:
        return self._nono_click_sweep_between(
            regions,
            "nono.融合完毕确认左",
            "nono.融合完毕确认",
            use_foreground,
            log_tag="nono孵化·完成确认",
        )

    def _nono_click_start_confirm_sweep(self, regions, use_foreground: bool) -> bool:
        return self._nono_click_sweep_between(
            regions,
            NONO_FUSION_START_CONFIRM_LEFT_KEY,
            NONO_FUSION_START_CONFIRM_KEY,
            use_foreground,
            log_tag="nono孵化·开始确认",
        )

    def _nono_open_soul_transform_panel(self, regions, use_foreground: bool) -> bool:
        from core.logger import kernel_cursor

        start_cursor = kernel_cursor()
        self._emit("🖱️ [nono孵化] 点击 nono.打开，等待 MachineDogPanel.swf", "INFO")
        if not self._click_region_safe(regions, "nono.打开", use_foreground):
            self._emit("❌ [nono孵化] 点击 nono.打开失败", "ERROR")
            return False
        if not self._wait_kernel_line_matches(
            PSYCHIC_EXP_MACHINE_DOG_PANEL_RE,
            log_tag="nono孵化·MachineDogPanel",
            timeout_s=5.0,
            success_msg="✅ [nono孵化] 已检测到 MachineDogPanel.swf",
            start_cursor=start_cursor,
        ):
            return False

        self._emit("🖱️ [nono孵化] 点击 nono.展开", "INFO")
        if not self._click_region_safe(regions, "nono.展开", use_foreground):
            self._emit("❌ [nono孵化] 点击 nono.展开失败", "ERROR")
            return False
        time.sleep(0.3)

        start_cursor = kernel_cursor()
        self._emit("🖱️ [nono孵化] 点击 nono.融合，等待 SoulTransformPanel.swf", "INFO")
        if not self._click_region_safe(regions, "nono.融合", use_foreground):
            self._emit("❌ [nono孵化] 点击 nono.融合失败", "ERROR")
            return False
        if not self._wait_kernel_line_matches(
            NONO_SOUL_TRANSFORM_PANEL_RE,
            log_tag="nono孵化·SoulTransformPanel",
            timeout_s=5.0,
            success_msg="✅ [nono孵化] 已检测到 SoulTransformPanel.swf",
            start_cursor=start_cursor,
        ):
            return False
        return True

    def _nono_close_soul_transform_panel(self, regions, use_foreground: bool, *, reason: str) -> None:
        """Best-effort close for an open SoulTransformPanel before abandoning a run."""
        if not regions.get("nono.融合关闭"):
            self._emit(f"⚠️ [nono孵化] {reason}：缺少 nono.融合关闭，无法关闭融合面板", "WARN")
            return
        if self._click_region_safe(regions, "nono.融合关闭", use_foreground):
            self._emit(f"🖱️ [nono孵化] {reason}：已点击 nono.融合关闭", "INFO")
        else:
            self._emit(f"⚠️ [nono孵化] {reason}：点击 nono.融合关闭失败", "WARN")

    def _nono_reconnect_base_right_and_reopen_panel(
        self,
        regions,
        use_foreground: bool,
        *,
        attempt: int,
    ) -> Optional[Tuple[Tuple[int, int, int], Tuple[int, int, int]]]:
        tag = f"nono孵化·探针二白重连{attempt}"
        self._emit(
            f"🔁 [{tag}] 探针二仍为纯白：背包恢复 → 回基地门控 → 检查融合记录 → 基地右侧 → 重启融合面板",
            "INFO",
        )
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{tag}] 缺少 dar_route_runner，无法执行基地门控", "ERROR")
            return None

        stop_event = getattr(self.bot, "_stop_event", None) or self._new_daily_stop_event()
        # 白色重连先只做刷新登录和屏蔽，停在“地图已出现、尚未点击基地”的位置。
        # 背包流程必须插在这里；不能像普通重连一样先点击基地/做基地门控。
        self._emit(f"🔄 [{tag}] 白色重连：刷新登录并完成屏蔽，暂不点击基地", "SYSTEM")
        if not drr.run_refresh_login_until_map(
            use_foreground,
            stop_event,
            include_base_and_map_gate=False,
        ):
            self._emit(f"❌ [{tag}] 白色重连的刷新登录/屏蔽失败", "ERROR")
            return None

        if not self._nono_white_reconnect_bag_reset(
            regions,
            use_foreground,
            drr=drr,
            stop_event=stop_event,
            log_tag=tag,
        ):
            return None

        reason = self._nono_soul_fusion_reconnect_reason()
        if reason:
            self._emit(f"🧪 [{tag}] 背包流程和基地门控后复查融合记录：{reason}；继续本轮融合开始阶段", "INFO")
        else:
            self._emit(f"🧪 [{tag}] 背包流程和基地门控后复查融合记录：未到下一轮时间；继续当前已开始的融合阶段", "INFO")

        self._emit(f"🖱️ [{tag}] 补点 对话框.普通确认", "INFO")
        self._click_region_safe(regions, "对话框.普通确认", use_foreground)
        time.sleep(0.3)

        self._emit(f"🖱️ [{tag}] 点击 {NEW_DAILY_SEQ2_BASE_RIGHT_KEY}", "INFO")
        if not self._click_region_safe(regions, NEW_DAILY_SEQ2_BASE_RIGHT_KEY, use_foreground):
            return None
        time.sleep(0.5)

        if not self._nono_open_soul_transform_panel(regions, use_foreground):
            return None
        return self._nono_wait_fusion_probes_stable(regions, log_tag=f"{tag}·探针复扫")

    def _nono_white_reconnect_bag_reset(
        self,
        regions,
        use_foreground: bool,
        *,
        drr,
        stop_event: threading.Event,
        log_tag: str,
    ) -> bool:
        """白色重连后固定执行背包/元神珠恢复，再重新回到基地。"""
        required = (
            "背包.背包",
            "背包.白色探针一",
            "背包.白色探针二",
            "背包.元神珠",
            "背包.右",
            NEW_DAILY_SEQ2_RETURN_BASE_KEY,
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ [{log_tag}·背包恢复] 缺少区域：{key}", "ERROR")
                return False

        self._emit(f"🎒 [{log_tag}·背包恢复] 点击 背包.背包", "INFO")
        if not self._click_region_safe(regions, "背包.背包", use_foreground):
            return False

        self._emit(f"⏳ [{log_tag}·背包恢复] 等待两个背包白色探针均为白色", "INFO")
        last_log = 0.0
        while not stop_event.is_set() and not self._should_abort():
            rgb1 = mean_rgb_for_region_key(regions, "背包.白色探针一")
            rgb2 = mean_rgb_for_region_key(regions, "背包.白色探针二")
            if self._nono_rgb_is_pure_white(rgb1) and self._nono_rgb_is_pure_white(rgb2):
                self._emit(
                    f"✅ [{log_tag}·背包恢复] 两个白色探针已就绪：一={self._format_rgb(rgb1)}，二={self._format_rgb(rgb2)}",
                    "SUCCESS",
                )
                break
            if time.time() - last_log >= 1.0:
                self._emit(
                    f"⏳ [{log_tag}·背包恢复] 等待白色探针：一={self._format_rgb(rgb1)}，二={self._format_rgb(rgb2)}",
                    "DEBUG",
                )
                last_log = time.time()
            time.sleep(0.1)
        else:
            return False

        self._emit(f"🖱️ [{log_tag}·背包恢复] 点击 背包.元神珠", "INFO")
        if not self._click_region_safe(regions, "背包.元神珠", use_foreground):
            return False
        time.sleep(0.5)

        self._emit(f"⚡ [{log_tag}·背包恢复] 点击 背包.右 50 次（间隔 0.05s）", "INFO")
        for _ in range(50):
            if stop_event.is_set() or self._should_abort():
                return False
            if not self._click_region_safe(regions, "背包.右", use_foreground):
                return False
            time.sleep(0.05)
        time.sleep(2.0)

        self._emit(f"🏠 [{log_tag}·背包恢复] 点击 {NEW_DAILY_SEQ2_RETURN_BASE_KEY}，重新执行基地门控", "INFO")
        if not self._click_region_btn_fallback(regions, NEW_DAILY_SEQ2_RETURN_BASE_KEY, use_foreground):
            return False
        gate_fn = getattr(drr, "_gate_map500001_after_refresh_base", None)
        if not callable(gate_fn):
            self._emit(f"❌ [{log_tag}·背包恢复] 缺少基地门控方法", "ERROR")
            return False
        return bool(
            gate_fn(
                use_foreground,
                stop_event,
                f"{log_tag}·背包恢复",
                skip_molecule_converter=True,
            )
        )

    def _nono_normalize_start_probe_state(
        self,
        regions,
        use_foreground: bool,
        probe1_rgb: Tuple[int, int, int],
        probe2_rgb: Tuple[int, int, int],
    ) -> Optional[Tuple[Tuple[int, int, int], Tuple[int, int, int]]]:
        attempt = 0
        while not self._should_abort():
            probe1_white = self._nono_rgb_is_pure_white(probe1_rgb)
            probe2_white = self._nono_rgb_is_pure_white(probe2_rgb)
            self._emit(
                f"🧪 [nono孵化] 开始阶段探针校验：探针一={self._format_rgb(probe1_rgb)} "
                f"({'白' if probe1_white else '非白'})，探针二={self._format_rgb(probe2_rgb)} "
                f"({'白' if probe2_white else '非白'})",
                "INFO",
            )

            if not probe1_white:
                self._emit(
                    f"❌ [nono孵化] 重开后探针一不是纯白：RGB={self._format_rgb(probe1_rgb)}，退出",
                    "ERROR",
                )
                return None
            if not probe2_white:
                self._emit("✅ [nono孵化] 已归一化到 探针一白 / 探针二非白，继续开始 oracle", "SUCCESS")
                return probe1_rgb, probe2_rgb

            if attempt >= NONO_FUSION_NORMALIZE_MAX_REOPENS:
                self._nono_close_soul_transform_panel(
                    regions,
                    use_foreground,
                    reason="探针二连续纯白，放弃本轮融合",
                )
                self._emit(
                    f"❌ [nono孵化] 探针二连续 {attempt} 次重开后仍为纯白，退出避免卡死",
                    "ERROR",
                )
                return None
            attempt += 1
            stable = self._nono_reconnect_base_right_and_reopen_panel(
                regions,
                use_foreground,
                attempt=attempt,
            )
            if stable is None:
                return None
            probe1_rgb, probe2_rgb = stable

        return None

    def _nono_run_complete_phase(
        self,
        regions,
        use_foreground: bool,
        recorded_time: datetime,
        fusion_count: int,
    ) -> Optional[Tuple[datetime, int]]:
        self._emit("⏳ [nono孵化] 当前为融合结束阶段，先执行完成确认流程", "INFO")
        time.sleep(1.0)
        if not self._nono_click_fusion_confirm_sweep(regions, use_foreground):
            return None
        if not self._wait_1and1_clear(
            regions,
            use_foreground,
            timeout_s=20.0,
            min_confirm_clicks=1,
            log_tag="nono孵化·完成确认",
        ):
            self._emit("❌ [nono孵化] 未检测并清理完成 1AND1，不写入 complete 记录", "ERROR")
            return None

        completed_at = self._beijing_now()
        next_count = self._nono_soul_fusion_count_after_complete(recorded_time, fusion_count, completed_at)
        self._append_nono_soul_fusion_record(completed_at, next_count, "complete")
        next_wait_hours = self._nono_soul_fusion_wait_hours(next_count, "complete")
        self._emit(
            f"✅ [nono孵化] 已追加CSV完成记录：time={completed_at.isoformat(timespec='seconds')}, "
            f"fusion_count={next_count}, phase=complete，下次等待={next_wait_hours:g}h",
            "SUCCESS",
        )
        return completed_at, next_count

    def _nono_run_start_phase_oracle(
        self,
        regions,
        use_foreground: bool,
        recorded_time: datetime,
        fusion_count: int,
        probe2_rgb: Tuple[int, int, int],
    ) -> bool:
        self._emit(
            f"🧪 [nono孵化] 执行开始阶段 oracle：初始探针二={self._format_rgb(probe2_rgb)}",
            "INFO",
        )
        if not self._nono_rgb_is_pure_white(probe2_rgb):
            self._emit("🖱️ [nono孵化] 探针二非纯白，持续点击探针二直到开始确认区域由深灰转为偏蓝", "INFO")
            start_time = time.time()
            while (time.time() - start_time) < NONO_FUSION_START_ORACLE_TIMEOUT_SEC:
                if self._should_abort():
                    return False
                if not self._click_region_safe(regions, NONO_FUSION_PROBE_2_KEY, use_foreground):
                    return False
                time.sleep(0.12)
                confirm_rgb = mean_rgb_for_region_key(regions, NONO_FUSION_START_CONFIRM_KEY)
                if self._nono_rgb_is_start_confirm_blue(confirm_rgb):
                    self._emit(
                        f"✅ [nono孵化] 开始确认区域已偏蓝：RGB={self._format_rgb(confirm_rgb)}",
                        "SUCCESS",
                    )
                    break
            else:
                confirm_rgb = mean_rgb_for_region_key(regions, NONO_FUSION_START_CONFIRM_KEY)
                self._emit(
                    f"❌ [nono孵化] 点击探针二后开始确认区域未变偏蓝，最后RGB={self._format_rgb(confirm_rgb)}",
                    "ERROR",
                )
                return False
        else:
            self._emit("✅ [nono孵化] 探针二已纯白，跳过探针二点击", "INFO")

        self._emit("🖱️ [nono孵化] 重复横扫开始确认，直到探针一由白色变为非白", "INFO")
        start_time = time.time()
        while (time.time() - start_time) < 20.0:
            if self._should_abort():
                return False
            if not self._nono_click_start_confirm_sweep(regions, use_foreground):
                return False
            time.sleep(0.15)
            probe1_rgb = mean_rgb_for_region_key(regions, NONO_FUSION_PROBE_1_KEY)
            if not self._nono_rgb_is_pure_white(probe1_rgb):
                self._emit(
                    f"✅ [nono孵化] 探针一已由白转非白：RGB={self._format_rgb(probe1_rgb)}",
                    "SUCCESS",
                )
                break
        else:
            probe1_rgb = mean_rgb_for_region_key(regions, NONO_FUSION_PROBE_1_KEY)
            self._emit(
                f"❌ [nono孵化] 开始确认后探针一未转非白，最后RGB={self._format_rgb(probe1_rgb)}，不写入 start 记录",
                "ERROR",
            )
            return False

        started_at = self._beijing_now()
        start_count = self._nono_soul_fusion_count_for_start(recorded_time, fusion_count, started_at)
        self._append_nono_soul_fusion_record(started_at, start_count, "start")
        next_wait_hours = self._nono_soul_fusion_wait_hours(start_count, "start")
        self._emit(
            f"✅ [nono孵化] 已追加CSV开始记录：time={started_at.isoformat(timespec='seconds')}, "
            f"fusion_count={start_count}, phase=start，下次等待={next_wait_hours:g}h",
            "SUCCESS",
        )
        if self._click_region_safe(regions, "nono.融合关闭", use_foreground):
            self._emit("🖱️ [nono孵化] 已点击 nono.融合关闭", "INFO")
        return True

    def run_nono_soul_fusion_check(self, use_foreground: bool = False) -> bool:
        if not NONO_SOUL_FUSION_GLOBAL_ENABLED:
            self._emit("⏸️ [nono孵化] nono融合已全局禁用，保留功能但不执行", "INFO")
            return True
        if not window_manager.find_window():
            self._emit("❌ [nono孵化] 未检测到游戏窗口", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if not regions:
            self._emit("❌ [nono孵化] 缺少 bot.regions", "ERROR")
            return False

        required = (
            "nono.打开",
            "nono.展开",
            "nono.融合",
            "nono.融合完毕确认左",
            "nono.融合完毕确认",
            NONO_FUSION_PROBE_1_KEY,
            NONO_FUSION_PROBE_2_KEY,
            NONO_FUSION_START_CONFIRM_LEFT_KEY,
            NONO_FUSION_START_CONFIRM_KEY,
            NEW_DAILY_SEQ2_BASE_RIGHT_KEY,
            "对话框.普通确认",
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ [nono孵化] 缺少区域：{key}", "ERROR")
                return False

        recorded_time, fusion_count, phase = self._load_nono_soul_fusion_state()
        now = self._beijing_now()
        normalized_phase = str(phase or "").strip().lower()
        wait_hours = self._nono_soul_fusion_wait_hours(fusion_count, normalized_phase)
        due_time = recorded_time + timedelta(hours=wait_hours)
        self._emit(
            f"🧪 [nono孵化] CSV最后记录: time={recorded_time.isoformat(timespec='seconds')}, "
            f"fusion_count={fusion_count}, phase={phase}, 本次等待={wait_hours:g}h, 到期={due_time.isoformat(timespec='seconds')}",
            "INFO",
        )

        # complete 表示可以立刻进入下一次融合开始阶段；只有上一次
        # 是 start 时，才需要等待该 start 记录对应的融合时长。
        if normalized_phase == "complete":
            self._emit("yes [nono孵化] 上一条为完成记录，不等待时间，立即执行下一步融合", "SUCCESS")
        elif now < due_time:
            remaining = due_time - now
            remaining_min = max(0.0, remaining.total_seconds() / 60.0)
            self._emit(f"no [nono孵化] 未到时间，剩余约 {remaining_min:.1f} 分钟", "INFO")
            return True
        else:
            self._emit("yes [nono孵化] 上一条 start 已到时间，开始执行 nono 孵化流程", "SUCCESS")

        if not self._nono_open_soul_transform_panel(regions, use_foreground):
            return False

        stable = self._nono_wait_fusion_probes_stable(regions)
        if stable is None:
            return False
        probe1_rgb, probe2_rgb = stable

        if not self._nono_rgb_is_pure_white(probe1_rgb):
            finished = self._nono_run_complete_phase(
                regions,
                use_foreground,
                recorded_time,
                fusion_count,
            )
            if finished is None:
                return False
            recorded_time, fusion_count = finished

            self._emit("🔁 [nono孵化] 完成阶段已处理，重新打开融合面板进入开始阶段校验", "INFO")
            if not self._nono_open_soul_transform_panel(regions, use_foreground):
                return False
            stable = self._nono_wait_fusion_probes_stable(regions, log_tag="nono孵化·探针复检")
            if stable is None:
                return False
            probe1_rgb, probe2_rgb = stable
            if not self._nono_rgb_is_pure_white(probe1_rgb):
                self._emit(
                    f"❌ [nono孵化] 完成后复检探针一仍非纯白：RGB={self._format_rgb(probe1_rgb)}，退出",
                    "ERROR",
                )
                return False
        else:
            self._emit("✅ [nono孵化] 探针一纯白，当前为融合开始阶段", "SUCCESS")

        normalized = self._nono_normalize_start_probe_state(
            regions,
            use_foreground,
            probe1_rgb,
            probe2_rgb,
        )
        if normalized is None:
            return False
        probe1_rgb, probe2_rgb = normalized

        return self._nono_run_start_phase_oracle(
            regions,
            use_foreground,
            recorded_time,
            fusion_count,
            probe2_rgb,
        )

    def _prepare_shanni_energy_drain(self, regions, use_foreground: bool) -> bool:
        """Prepare Shanni SWF resources without running a reconnect or route prelude."""
        _ = (regions, use_foreground)
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit("❌ [闪尼吸能启动] 缺少 dar_route_runner，无法删除 SWF 89、90", "ERROR")
            return False

        from core.dar_route_runner import WildCaptureProfile

        self._emit("🧹 [闪尼吸能启动] 删除 pet SWF：89、90", "SYSTEM")
        swf_profile = WildCaptureProfile(
            name="闪尼吸能",
            route_hint="to闪尼吸能",
            map_swf_id=SHANNI_MAP_105,
            target_mp3_id=0,
            target_pet_id=0,
            delete_swf_ids=SHANNI_DELETE_SWF_IDS,
        )
        drr._check_and_delete_swf_files(swf_profile)
        return True

    def _click_shanni_drain_and_watch_change(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
    ) -> Optional[bool]:
        """Click drain and watch the 2x2 probe centered at (390, 200) for one second."""
        x = SHANNI_CHANGE_PROBE_X
        y = SHANNI_CHANGE_PROBE_Y

        def _grab_probe():
            return window_manager.grab_game_bbox(
                x - 1,
                y - 1,
                x + 1,
                y + 1,
                min_size_px=2,
            )

        baseline = _grab_probe()
        if baseline is None:
            self._emit(f"❌ [{log_tag}] 无法截图变化探针 ({x}, {y})", "ERROR")
            return None
        baseline_pixels = tuple(baseline.convert("RGB").getdata())
        if not baseline_pixels:
            self._emit(f"❌ [{log_tag}] 变化探针 ({x}, {y}) 截图为空", "ERROR")
            return None

        self._emit(
            f"🖱️ [{log_tag}] 点击 {SHANNI_DRAIN_KEY}，观察 ({x}, {y}) 区域 {SHANNI_CHANGE_WATCH_SEC:.0f}s",
            "INFO",
        )
        if not self._click_region_safe(regions, SHANNI_DRAIN_KEY, use_foreground):
            return None

        changed = False
        peak_delta = 0
        deadline = time.monotonic() + SHANNI_CHANGE_WATCH_SEC
        while time.monotonic() < deadline:
            if self._should_abort():
                return None
            self._wait_if_paused()
            current = _grab_probe()
            if current is None:
                self._emit(f"❌ [{log_tag}] 观察期间无法截图变化探针 ({x}, {y})", "ERROR")
                return None
            current_pixels = tuple(current.convert("RGB").getdata())
            if len(current_pixels) != len(baseline_pixels):
                self._emit(f"❌ [{log_tag}] 变化探针截图尺寸不一致", "ERROR")
                return None
            frame_delta = max(
                abs(int(current_channel) - int(base_channel))
                for current_rgb, baseline_rgb in zip(current_pixels, baseline_pixels)
                for current_channel, base_channel in zip(current_rgb, baseline_rgb)
            )
            peak_delta = max(peak_delta, frame_delta)
            if frame_delta >= SHANNI_CHANGE_MIN_CHANNEL_DELTA:
                changed = True
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(SHANNI_CHANGE_POLL_SEC, remaining))

        state = "有变化" if changed else "无变化"
        self._emit(
            f"🔎 [{log_tag}] ({x}, {y}) 区域 {state}（最大通道差={peak_delta}）",
            "SUCCESS" if changed else "INFO",
        )
        return changed

    def run_shanni_energy_drain_loop(self, use_foreground: bool = False) -> bool:
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口：无法执行闪尼吸能", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions", "ERROR")
            return False

        for key in (
            SHANNI_TO_106_KEY,
            SHANNI_TO_105_KEY,
            SHANNI_DRAIN_KEY,
            "对话框.通用探针",
            "对话框.普通确认探针",
            "对话框.普通确认",
        ):
            if not regions.get(key):
                self._emit(f"❌ 闪尼吸能缺少区域：{key}", "ERROR")
                return False

        if not self._ensure_unified_framework(regions):
            return False

        if not self._prepare_shanni_energy_drain(regions, use_foreground):
            return False

        self._emit(
            "⚡ 闪尼吸能：跳过前置重连，开局先走 105→106→105，再点击吸能并观察 (390,200) 区域 1s",
            "SYSTEM",
        )
        cycle = 0
        drain_succeeded = False
        while not self._should_abort():
            cycle += 1
            tag = f"闪尼吸能·{cycle}"

            self._emit(f"🖱️ [{tag}] 点击 {SHANNI_TO_106_KEY}", "INFO")
            if not self._click_region_safe(regions, SHANNI_TO_106_KEY, use_foreground):
                return False
            if not self._wait_map_npc_then_delay(
                SHANNI_MAP_106,
                log_tag=tag,
                post_delay_s=0.0,
            ):
                return False

            self._emit(f"🖱️ [{tag}] 点击 {SHANNI_TO_105_KEY}", "INFO")
            if not self._click_region_safe(regions, SHANNI_TO_105_KEY, use_foreground):
                return False
            if not self._wait_map_npc_then_delay(
                SHANNI_MAP_105,
                log_tag=tag,
                post_delay_s=0.0,
            ):
                return False

            changed = self._click_shanni_drain_and_watch_change(
                regions,
                use_foreground,
                log_tag=tag,
            )
            if changed is None:
                if self._should_abort():
                    self._emit("⛔ 闪尼吸能已手动停止", "SYSTEM")
                return False

            if changed:
                if not self._wait_1and1_clear(
                    regions,
                    use_foreground,
                    timeout_s=SHANNI_1AND1_TIMEOUT_SEC,
                    min_confirm_clicks=1,
                    log_tag=f"{tag}·吸能1AND1",
                ):
                    if self._should_abort():
                        self._emit("⛔ 闪尼吸能已手动停止", "SYSTEM")
                    else:
                        self._emit(f"❌ [{tag}] 吸能后 1AND1 未在时限内执行完毕", "ERROR")
                    return False
                drain_succeeded = True
            elif drain_succeeded:
                self._emit(
                    f"✅ [{tag}] 已成功吸能，下一次点击后区域无变化，按规则结束循环",
                    "SUCCESS",
                )
                return True
            else:
                self._emit(f"ℹ️ [{tag}] 点击后区域无变化，首次成功前切图重试", "INFO")

        self._emit("⛔ 闪尼吸能已手动停止", "SYSTEM")
        return False

    def _new_daily_step_gap(self) -> bool:
        if self._should_abort():
            return False
        time.sleep(NEW_DAILY_STEP_GAP_SEC)
        return True

    def _wait_map_npc_then_delay(
        self,
        map_id: int,
        *,
        log_tag: str = "新日常",
        post_delay_s: float = NEW_DAILY_MAP_NPC_POST_DELAY_SEC,
        timeout_s: float = NEW_DAILY_MAP_WAIT_TIMEOUT_SEC,
        start_cursor=None,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> bool:
        if map_id == 10:
            self._emit(
                f"⏳ [{log_tag}] 等待 map10 + {MAP10_WHITE_PROBE_KEY_NIEO} 先白后非白…",
                "INFO",
            )
        else:
            self._emit(f"⏳ [{log_tag}] 等待 map{map_id} + newNpc…", "INFO")
        if not self._wait_for_map_and_npc(
            map_id,
            timeout_s=timeout_s,
            start_cursor=start_cursor,
            stop_check=stop_check,
        ):
            npc_hint = (
                f"{MAP10_WHITE_PROBE_KEY_NIEO} 先白后非白"
                if map_id == 10
                else "newNpc"
            )
            self._emit(f"❌ [{log_tag}] 等待 map{map_id}+{npc_hint} 超时", "ERROR")
            return False
        if post_delay_s > 0:
            should_stop = stop_check or self._should_abort
            end_at = time.monotonic() + post_delay_s
            while time.monotonic() < end_at:
                if should_stop():
                    return False
                time.sleep(min(0.05, max(0.0, end_at - time.monotonic())))
        return True

    def _format_rgb(self, rgb: Optional[Tuple[int, int, int]]) -> str:
        if rgb is None:
            return "None"
        return f"({rgb[0]},{rgb[1]},{rgb[2]})"

    def _rgb_distance(self, a: Tuple[int, int, int], b: Tuple[int, int, int]) -> float:
        return (
            (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2
        ) ** 0.5

    def _is_new_daily_hp_bar_blue(self, rgb: Tuple[int, int, int]) -> bool:
        return (
            self._rgb_distance(rgb, NEW_DAILY_BAG_HP_BLUE_RGB)
            <= NEW_DAILY_BAG_HP_BLUE_TOLERANCE
        )

    def _wait_new_daily_hp_bar_blue(
        self,
        regions,
        hp_bar_key: str,
        *,
        log_tag: str = "新日常",
        bar_label: str = "",
        put_back_key: Optional[str] = None,
        use_foreground: bool = False,
    ) -> bool:
        """放回仓库后扫描指定血条；未变蓝则每 5s 补点放回仓库，直至 #174991 蓝色。"""
        label = bar_label or hp_bar_key.split(".")[-1]
        if not regions.get(hp_bar_key):
            self._emit(f"❌ [{log_tag}] 缺少区域：{hp_bar_key}", "ERROR")
            return False

        can_retry_putback = bool(put_back_key and regions.get(put_back_key))
        self._emit(
            f"⏳ [{log_tag}] 扫描{label}，等待蓝色(#174991)；"
            f"{'每' + str(NEW_DAILY_BAG_PUTBACK_RETRY_INTERVAL_SEC) + 's未变蓝补点放回仓库，' if can_retry_putback else ''}"
            f"{NEW_DAILY_BAG_HP_WAIT_TIMEOUT_SEC}s总超时",
            "INFO",
        )
        t0 = time.time()
        last_putback_click = t0
        while time.time() - t0 < NEW_DAILY_BAG_HP_WAIT_TIMEOUT_SEC:
            if self._should_abort():
                return False
            rgb = mean_rgb_for_region_key(regions, hp_bar_key)
            if rgb and self._is_new_daily_hp_bar_blue(rgb):
                r, g, b = rgb
                self._emit(
                    f"✅ [{log_tag}] {label}已为蓝色（RGB=({r},{g},{b})）",
                    "SUCCESS",
                )
                return True
            if (
                can_retry_putback
                and (time.time() - last_putback_click)
                >= NEW_DAILY_BAG_PUTBACK_RETRY_INTERVAL_SEC
            ):
                self._emit(
                    f"📦 [{log_tag}] {label}仍未变蓝，补点放回仓库",
                    "WARN",
                )
                if not self._click_region_btn_fallback(
                    regions, put_back_key, use_foreground
                ):
                    return False
                last_putback_click = time.time()
            time.sleep(BAG_OPEN_READY_POLL_SEC)

        self._emit(
            f"❌ [{log_tag}] 等待{label}变蓝超时({NEW_DAILY_BAG_HP_WAIT_TIMEOUT_SEC}s)",
            "ERROR",
        )
        return False

    def _new_daily_bag_return_and_follow(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str = "新日常",
        verify_pet_pos: str = "六",
        expected_pet_count: Optional[int] = None,
        expected_pet_count_wait_timeout_s: float = 0.0,
    ) -> bool:
        """打开背包并数宠 → 3s 后放回 → 原尾槽变深蓝 → 身边跟随。"""
        bag_open_key = "精灵背包.打开精灵背包"
        put_back_key = "精灵背包.放回仓库"
        bag_scan_result: Dict[str, Any] = {}

        self._emit(f"💼 [{log_tag}] 打开精灵背包", "INFO")
        if not self._click_region_btn_fallback(regions, bag_open_key, use_foreground):
            return False

        if not wait_pet_bag_ui_ready_after_open(
            regions,
            emit_fn=self._emit,
            stop_check=self._should_abort,
            log_tag=log_tag,
            probe_key=NEW_DAILY_BAG_PET_ONE_PROBE_KEY,
            bag_scan_callback=bag_scan_result.update,
        ):
            self._emit(
                f"❌ [{log_tag}] 精灵一探针未就绪（需橙色 #FF9900/#FF9901）",
                "ERROR",
            )
            return False

        count_to_pos = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六"}
        detected_count = bag_scan_result.get("count")
        expected_count = (
            int(expected_pet_count) if expected_pet_count is not None else None
        )
        expected_wait_s = max(0.0, float(expected_pet_count_wait_timeout_s))
        if (
            expected_count is not None
            and bag_scan_result.get("ok")
            and detected_count != expected_count
            and expected_wait_s > 0.0
        ):
            self._emit(
                f"⏳ [{log_tag}] 当前检测到 {detected_count} 只，等待奖励精灵入包至 "
                f"{expected_count} 只（{expected_wait_s:g}s超时）",
                "INFO",
            )
            deadline = time.time() + expected_wait_s
            while time.time() < deadline and not self._should_abort():
                remaining = max(0.0, deadline - time.time())
                refreshed = scan_pet_bag_count(
                    regions,
                    stop_check=self._should_abort,
                    log_tag=f"{log_tag}·奖励入包",
                    timeout_s=min(1.0, remaining),
                    poll_s=BAG_OPEN_READY_POLL_SEC,
                )
                if refreshed.get("ok"):
                    bag_scan_result.clear()
                    bag_scan_result.update(refreshed)
                    detected_count = refreshed.get("count")
                    if detected_count == expected_count:
                        self._emit(
                            f"✅ [{log_tag}] 奖励精灵已入包，背包数量={expected_count}",
                            "SUCCESS",
                        )
                        break
                time.sleep(BAG_OPEN_READY_POLL_SEC)

        if expected_pet_count is not None and (
            not bag_scan_result.get("ok")
            or detected_count != expected_count
        ):
            self._emit(
                f"❌ [{log_tag}] 开包应有 {expected_count} 只精灵，"
                f"实际检测={detected_count}；停止放回，避免错误跟随",
                "ERROR",
            )
            return False
        detected_verify_pos = count_to_pos.get(detected_count)
        fallback_verify_pos = str(verify_pet_pos)
        if bag_scan_result.get("ok") and detected_verify_pos:
            verify_pet_pos = detected_verify_pos
            self._emit(
                f"🎒 [{log_tag}] 开包检测到 {detected_count} 只精灵；"
                f"放回后验证尾槽精灵{verify_pet_pos}变深蓝",
                "INFO",
            )
            if verify_pet_pos != fallback_verify_pos:
                self._emit(
                    f"⚠️ [{log_tag}] 自动数量与预设尾槽精灵{fallback_verify_pos}不同，"
                    f"以连续槽位扫描结果精灵{verify_pet_pos}为准",
                    "WARN",
                )
        else:
            verify_pet_pos = fallback_verify_pos
            self._emit(
                f"⚠️ [{log_tag}] 未能自动确认背包数量，"
                f"回退验证预设尾槽精灵{verify_pet_pos}",
                "WARN",
            )

        self._emit(
            f"⏳ [{log_tag}] 精灵一探针已橙色，等待 {NEW_DAILY_BAG_POST_ORANGE_DELAY_SEC}s 后点击放回仓库",
            "INFO",
        )
        time.sleep(NEW_DAILY_BAG_POST_ORANGE_DELAY_SEC)

        self._emit(f"📦 [{log_tag}] 点击放回仓库", "INFO")
        if not self._click_region_btn_fallback(regions, put_back_key, use_foreground):
            return False

        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner，无法扫描精灵{verify_pet_pos}槽位", "ERROR")
            return False
        if not drr.wait_bag_putback_slot_deep_blue(
            str(verify_pet_pos),
            self._new_daily_stop_event(),
            f"{log_tag}·精灵{verify_pet_pos}放回确认",
        ):
            return False

        self._emit(
            f"⏳ [{log_tag}] 精灵{verify_pet_pos}槽位已深蓝，等待 {NEW_DAILY_BAG_POST_BLUE_DELAY_SEC}s 后点击身边跟随",
            "INFO",
        )
        time.sleep(NEW_DAILY_BAG_POST_BLUE_DELAY_SEC)

        self._emit(f"🔄 [{log_tag}] 点击身边跟随", "INFO")
        if not self._click_region_safe(regions, "精灵背包.身边跟随", use_foreground):
            return False
        time.sleep(0.8)
        return True

    def _new_daily_bag_follow_after_orange(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str = "新日常",
    ) -> bool:
        """打开背包 → 等精灵一探针橙色（此时已选中）→ 身边跟随。"""
        bag_open_key = "精灵背包.打开精灵背包"
        follow_key = "精灵背包.身边跟随"

        self._emit(f"💼 [{log_tag}] 打开精灵背包", "INFO")
        if not self._click_region_btn_fallback(regions, bag_open_key, use_foreground):
            return False

        if not wait_pet_bag_ui_ready_after_open(
            regions,
            emit_fn=self._emit,
            stop_check=self._should_abort,
            log_tag=log_tag,
            probe_key=NEW_DAILY_BAG_PET_ONE_PROBE_KEY,
        ):
            self._emit(
                f"❌ [{log_tag}] 精灵一探针未就绪（需橙色 #FF9900/#FF9901）",
                "ERROR",
            )
            return False

        self._emit(
            f"⏳ [{log_tag}] 精灵一探针已橙色且精灵一已选中，"
            f"等待 {NEW_DAILY_BAG_POST_ORANGE_DELAY_SEC}s 后跟随",
            "INFO",
        )
        time.sleep(NEW_DAILY_BAG_POST_ORANGE_DELAY_SEC)

        self._emit(f"🔄 [{log_tag}] 不重复选择精灵一，直接点击身边跟随", "INFO")
        if not self._click_region_safe(regions, follow_key, use_foreground):
            return False
        self._emit(
            f"⏳ [{log_tag}] 跟随自动关包，等待 {FOLLOW_TO_NEXT_UI_DELAY_SEC:g}s 后继续",
            "INFO",
        )
        time.sleep(FOLLOW_TO_NEXT_UI_DELAY_SEC)
        self._emit(f"✅ [{log_tag}] 已跟随精灵一", "SUCCESS")
        return True

    def _wait_after_follow_before_next_ui(self, log_tag: str) -> bool:
        self._emit(
            f"⏳ [{log_tag}] 跟随流程已完成，等待 {FOLLOW_TO_NEXT_UI_DELAY_SEC:g}s 让背包关闭稳定",
            "INFO",
        )
        time.sleep(FOLLOW_TO_NEXT_UI_DELAY_SEC)
        return not self._should_abort()

    def _new_daily_stop_event(self) -> threading.Event:
        ev = threading.Event()

        def _watch():
            while not ev.is_set():
                if self._should_abort():
                    ev.set()
                    return
                time.sleep(0.05)

        threading.Thread(target=_watch, daemon=True).start()
        return ev

    def _new_daily_base_gate_and_confirm(
        self, regions, use_foreground: bool, *, log_tag: str = "新日常"
    ) -> bool:
        """点击回到基地 → map500001+newNpc 门控（含普通确认，跳过分子仪）。"""
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner，无法基地门控", "ERROR")
            return False
        if not regions.get(NEW_DAILY_SEQ2_RETURN_BASE_KEY):
            self._emit(
                f"❌ [{log_tag}] 缺少区域：{NEW_DAILY_SEQ2_RETURN_BASE_KEY}",
                "ERROR",
            )
            return False

        self._emit(
            f"🏠 [{log_tag}] 点击回到基地（{NEW_DAILY_SEQ2_RETURN_BASE_KEY}）",
            "INFO",
        )
        if not self._click_region_btn_fallback(
            regions, NEW_DAILY_SEQ2_RETURN_BASE_KEY, use_foreground
        ):
            return False

        stop_event = self._new_daily_stop_event()
        gate_fn = getattr(drr, "_gate_map500001_after_refresh_base", None)
        if not callable(gate_fn):
            self._emit(f"❌ [{log_tag}] dar_route_runner 无基地门控方法", "ERROR")
            return False

        self._emit(f"⏳ [{log_tag}] 基地门控（map500001 + newNpc + 普通确认）", "INFO")
        if not gate_fn(
            use_foreground,
            stop_event,
            log_tag,
            skip_molecule_converter=True,
        ):
            self._emit(f"❌ [{log_tag}] 基地门控失败", "ERROR")
            return False
        return True

    def _new_daily_gap_before_step(self, start_step: int, step_num: int) -> bool:
        """从中间步开始时，仅在已执行过的大步骤与下一步之间插入间隔。"""
        if step_num <= start_step:
            return True
        return self._new_daily_step_gap()

    def _wait_kernel_line_matches(
        self,
        needle,
        *,
        log_tag: str = "新日常",
        timeout_s: float = NEW_DAILY_SEQ3_KERNEL_WAIT_TIMEOUT_SEC,
        success_msg: str = "",
        start_cursor: Optional[int] = None,
    ) -> bool:
        """轮询内核日志，直至某行匹配 needle（子串或 RE）。"""
        from core.logger import fetch_kernel_since, kernel_cursor

        self._emit(f"⏳ [{log_tag}] 等待内核信号…", "INFO")
        start_time = time.time()
        cursor = kernel_cursor() if start_cursor is None else start_cursor
        while (time.time() - start_time) < timeout_s:
            if self._should_abort():
                return False
            try:
                lines = fetch_kernel_since(cursor)
                if isinstance(lines, list):
                    for line in lines:
                        if line_matches(needle, str(line)):
                            self._emit(
                                success_msg or f"✅ [{log_tag}] 已检测到内核信号",
                                "SUCCESS",
                            )
                            return True
                cursor = kernel_cursor()
            except Exception:
                pass
            time.sleep(0.05)
        self._emit(f"❌ [{log_tag}] 等待内核信号超时（{timeout_s}s）", "ERROR")
        return False

    def _new_daily_seq5_vines_interact_and_mine(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str = "新日常·5",
    ) -> bool:
        """点击 51藤蔓 → iris.swf → 普通确认 → 苏克黑白 → 挖矿 ×1。"""
        spot = NEW_DAILY_SEQ5_SPOT_51_VINES_KEY
        self._emit(f"🖱️ [{log_tag}] 点击 {spot}", "SYSTEM")
        if not self._click_region_safe(regions, spot, use_foreground):
            return False
        if not self._wait_kernel_line_matches(
            RE_NPC_IRIS_SWF,
            log_tag=f"{log_tag}·iris",
            timeout_s=NEW_DAILY_SEQ3_KERNEL_WAIT_TIMEOUT_SEC,
            success_msg=f"✅ [{log_tag}] 检测到 iris.swf",
        ):
            return False
        time.sleep(NEW_DAILY_SEQ5_POST_IRIS_DELAY_SEC)
        self._emit(f"🖱️ [{log_tag}] 点击 对话框.普通确认", "SYSTEM")
        if not self._click_region_safe(regions, "对话框.普通确认", use_foreground):
            return False
        if not self._wait_suke_bw_probes(regions, log_tag=f"{log_tag}·确认后"):
            return False
        return self.run_mining_cycles(
            spot,
            NEW_DAILY_SEQ5_MINING_TIMES,
            use_foreground=use_foreground,
            skip_initial_spot=True,
        )

    def _wait_kernel_map_and_bgm(
        self,
        map_id: int,
        bgm_id: int,
        *,
        log_tag: str = "新日常",
        timeout_s: float = NEW_DAILY_MAP_WAIT_TIMEOUT_SEC,
    ) -> bool:
        """固定脚本结束后立即取 cursor，只等新内核行中的 map swf + BGM mp3。"""
        from core.logger import fetch_kernel_since, kernel_cursor

        self._emit(
            f"⏳ [{log_tag}] 等待 map{map_id} + BGM_{bgm_id}（脚本结束后新内核行）…",
            "INFO",
        )
        t0 = time.time()
        cursor = kernel_cursor()
        seen_map = False
        seen_bgm = False
        while (time.time() - t0) < timeout_s:
            if self._should_abort():
                return False
            self._wait_if_paused()
            try:
                lines = fetch_kernel_since(cursor)
                if isinstance(lines, list):
                    for line in lines:
                        s = str(line)
                        if not seen_map and _kernel_line_has_map_id(s, map_id):
                            seen_map = True
                            self._emit(
                                f"✅ [{log_tag}] 检测到 map{map_id}",
                                "SUCCESS",
                            )
                        if not seen_bgm and line_has_target_map_bgm_id(s, bgm_id):
                            seen_bgm = True
                            self._emit(
                                f"✅ [{log_tag}] 检测到 BGM_{bgm_id}",
                                "SUCCESS",
                            )
                        if seen_map and seen_bgm:
                            self._emit(
                                f"✅ [{log_tag}] map+BGM 就绪",
                                "SUCCESS",
                            )
                            return True
                cursor = kernel_cursor()
            except Exception:
                pass
            time.sleep(0.05)
        missing = []
        if not seen_map:
            missing.append(f"map{map_id}")
        if not seen_bgm:
            missing.append(f"BGM_{bgm_id}")
        self._emit(
            f"❌ [{log_tag}] 等待 map+BGM 超时（缺：{', '.join(missing)}）",
            "ERROR",
        )
        return False

    def _kernel_batch_has_map_and_npc(self, lines, map_id: int) -> bool:
        has_map = False
        has_npc = False
        for line in lines:
            s = str(line)
            if _kernel_line_has_map_id(s, map_id):
                has_map = True
            if line_matches(RE_NEWNPC_MULTI, s):
                has_npc = True
            if has_map and has_npc:
                return True
        return False

    def _click_until_kernel_line_matches(
        self,
        regions,
        click_key: str,
        use_foreground: bool,
        needle,
        *,
        log_tag: str = "新日常",
        timeout_s: float = NEW_DAILY_SEQ3_KERNEL_WAIT_TIMEOUT_SEC,
        click_gap_s: float = 0.35,
    ) -> bool:
        """连点指定区域，直至内核日志命中 needle。"""
        from core.logger import fetch_kernel_since, kernel_cursor

        if not regions.get(click_key):
            self._emit(f"❌ [{log_tag}] 缺少区域：{click_key}", "ERROR")
            return False
        self._emit(
            f"⏳ [{log_tag}] 连点 {click_key} 直至内核信号…",
            "INFO",
        )
        t0 = time.time()
        cursor = kernel_cursor()
        while (time.time() - t0) < timeout_s:
            if self._should_abort():
                return False
            self._wait_if_paused()
            try:
                lines = fetch_kernel_since(cursor)
                if isinstance(lines, list):
                    for line in lines:
                        if line_matches(needle, str(line)):
                            self._emit(
                                f"✅ [{log_tag}] 已检测到内核信号",
                                "SUCCESS",
                            )
                            return True
                cursor = kernel_cursor()
            except Exception:
                pass
            if not self._click_region_safe(regions, click_key, use_foreground):
                return False
            time.sleep(max(0.05, click_gap_s))
        self._emit(f"❌ [{log_tag}] 等待内核信号超时（{timeout_s}s）", "ERROR")
        return False

    def _new_daily_seq7_summon_until_action(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str = "新日常·7",
    ) -> bool:
        """双击 71右下 → 连点 71召唤 直至内核出现 action\\。"""
        self._emit(
            f"🖱️ [{log_tag}] 双击 {NEW_DAILY_SEQ7_71_BOTTOM_RIGHT_KEY}",
            "SYSTEM",
        )
        if not self._click_region_safe_twice(
            regions, NEW_DAILY_SEQ7_71_BOTTOM_RIGHT_KEY, use_foreground
        ):
            return False
        return self._click_until_kernel_line_matches(
            regions,
            NEW_DAILY_SEQ7_71_SUMMON_KEY,
            use_foreground,
            RE_NONO_SUPER_ACTION_PATH,
            log_tag=f"{log_tag}·召唤",
            timeout_s=NEW_DAILY_SEQ7_SUMMON_ACTION_TIMEOUT_SEC,
            click_gap_s=NEW_DAILY_SEQ7_SUMMON_CLICK_GAP_SEC,
        )

    def _rapid_click_region_for_duration(
        self,
        regions,
        region_key: str,
        use_foreground: bool,
        duration_s: float,
        *,
        log_tag: str = "新日常",
        click_gap_s: float = NEW_DAILY_SEQ3_RAPID_CLICK_GAP_SEC,
    ) -> bool:
        """在指定时长内连续快速点击区域。"""
        if not regions.get(region_key):
            self._emit(f"❌ [{log_tag}] 缺少区域：{region_key}", "ERROR")
            return False
        self._emit(
            f"🖱️ [{log_tag}] 连续快速点击 {region_key}（{duration_s}s）",
            "INFO",
        )
        t0 = time.time()
        while (time.time() - t0) < duration_s:
            if self._should_abort():
                return False
            if not self._click_region_safe(regions, region_key, use_foreground):
                return False
            time.sleep(max(0.01, click_gap_s))
        return True

    def _new_daily_clear_backpack(
        self,
        use_foreground: bool,
        *,
        log_tag: str = "新日常",
        close_after: bool = True,
    ) -> bool:
        """清空背包（复用轮换 Step2）。"""
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner，无法清空背包", "ERROR")
            return False
        self._emit(f"🔄 [{log_tag}] 开始清空背包", "SYSTEM")
        return drr._rotation_step2_clear_backpack(
            use_foreground,
            self._new_daily_stop_event(),
            log_tag=log_tag,
            close_after=close_after,
        )

    def _new_daily_warehouse_take_reverse_positions(
        self,
        use_foreground: bool,
        *,
        category: str,
        reverse_positions: Tuple[int, ...],
        right_clicks: int,
        log_tag: str = "新日常",
        reverse_order: bool = False,
        include_jita: bool = False,
        jita_first: bool = False,
        from_open_bag: bool = False,
    ) -> bool:
        """打开仓库按倒数格位取宠，并可按入口要求先取或后取机塔。"""
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner，无法取宠", "ERROR")
            return False
        regions = getattr(self.bot, "regions", None)
        if regions is None:
            return False

        category_key = f"精灵仓库.{category}"
        required = (
            "精灵仓库.关闭",
            "精灵仓库.ALL",
            NEW_DAILY_SEQ4_WAREHOUSE_SINGLE_ATTR_KEY,
            category_key,
            "精灵仓库.右",
            "精灵仓库.左",
            "精灵仓库.放入背包",
            "对话框.普通确认",
        )
        required = required + (
            ("精灵背包.精灵仓库",)
            if from_open_bag
            else ("精灵仓库.打开",)
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ [{log_tag}] 缺少区域：{key}", "ERROR")
                return False
        for slot in range(1, 10):
            if not regions.get(f"精灵仓库.{slot}"):
                self._emit(f"❌ [{log_tag}] 缺少区域：精灵仓库.{slot}", "ERROR")
                return False

        stop_event = self._new_daily_stop_event()
        if from_open_bag:
            self._emit(
                f"📂 [{log_tag}] 非基地重连：从已打开的精灵背包进入精灵仓库",
                "INFO",
            )
            if not drr.open_pickmode_bag_warehouse_from_ready_bag(
                use_foreground,
                stop_event,
                log_tag=log_tag,
                initialize_all=False,
            ):
                return False
        else:
            self._emit(f"📂 [{log_tag}] 打开精灵仓库", "INFO")
            try:
                drr._click_pet_warehouse_open(use_foreground, log_tag=log_tag)
            except Exception as e:
                self._emit(f"❌ [{log_tag}] 打开精灵仓库失败: {e}", "ERROR")
                return False
            time.sleep(0.5)

        self._emit(
            f"📂 [{log_tag}] 点击 精灵仓库.ALL，等待 精灵仓库.右 变亮灰",
            "INFO",
        )
        if not self._warehouse_click_all_until_tail_color_ready(
            regions,
            use_foreground,
            log_tag=f"{log_tag}·仓库ALL",
        ):
            return False
        time.sleep(0.2)

        if include_jita and jita_first:
            self._emit(
                f"🐾 [{log_tag}] 先取机塔到精灵一（双属性→机械龙系）",
                "INFO",
            )
            if not drr._pickmode_place_jita_dual_mechanical(
                use_foreground,
                stop_event,
                log_tag=log_tag,
            ):
                return False

        self._emit(
            f"📂 [{log_tag}] 点击 {NEW_DAILY_SEQ4_WAREHOUSE_SINGLE_ATTR_KEY}",
            "INFO",
        )
        try:
            drr._click_region(
                NEW_DAILY_SEQ4_WAREHOUSE_SINGLE_ATTR_KEY, use_foreground
            )
        except Exception as e:
            self._emit(
                f"❌ [{log_tag}] 点击 {NEW_DAILY_SEQ4_WAREHOUSE_SINGLE_ATTR_KEY} 失败: {e}",
                "ERROR",
            )
            return False
        time.sleep(0.5)

        if not drr._rotation_place_pets_same_category_by_reverse(
            category,
            reverse_positions,
            right_clicks,
            use_foreground,
            stop_event,
            reverse_order=reverse_order,
            log_tag=log_tag,
        ):
            return False

        if include_jita and not jita_first:
            self._emit(f"🐾 [{log_tag}] 追加机塔（双属性→机械龙系）", "INFO")
            if not drr._pickmode_place_jita_dual_mechanical(
                use_foreground,
                stop_event,
                log_tag=log_tag,
            ):
                return False

        self._emit(f"📦 [{log_tag}] 关闭精灵仓库", "INFO")
        try:
            drr._click_pet_warehouse_close(use_foreground, log_tag=log_tag)
        except Exception as e:
            self._emit(f"❌ [{log_tag}] 关闭精灵仓库失败: {e}", "ERROR")
            return False
        time.sleep(0.3)
        return True

    def _new_daily_follow_then_return_pet_one(
        self,
        use_foreground: bool,
        *,
        log_tag: str,
        bag_already_open: bool = False,
    ) -> bool:
        """固定跟随精灵一，再放回精灵一并验证原尾槽精灵四为空。"""
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner", "ERROR")
            return False
        stop_event = self._new_daily_stop_event()
        bag_open_key = "精灵背包.打开精灵背包"
        bag_open_btn_key = "精灵背包.打开精灵背包按钮"

        if bag_already_open:
            self._emit(
                f"[{log_tag}] 仓库关闭后背包仍打开；机塔固定在精灵一",
                "INFO",
            )
            drr._sleep_abortable(stop_event, 0.3)
        else:
            self._emit(f"[{log_tag}] 打开精灵背包", "INFO")
            try:
                drr._click_region(bag_open_btn_key, use_foreground)
            except KeyError:
                drr._click_region(bag_open_key, use_foreground)
            except Exception as exc:
                self._emit(f"❌ [{log_tag}] 打开精灵背包失败：{exc}", "ERROR")
                return False
            drr._sleep_abortable(stop_event, 0.5)

        self._emit(f"[{log_tag}] 明确选择并跟随精灵一机塔", "SYSTEM")
        if not drr._click_pet_with_selection_check(
            "一",
            use_foreground,
            stop_event,
        ):
            self._emit(f"❌ [{log_tag}] 精灵一选中检测失败", "ERROR")
            return False
        try:
            drr._click_region("精灵背包.身边跟随", use_foreground)
        except Exception as exc:
            self._emit(f"❌ [{log_tag}] 点击身边跟随失败：{exc}", "ERROR")
            return False
        drr._sleep_abortable(stop_event, 0.5)
        self._emit(
            f"✅ [{log_tag}] 精灵一机塔已跟随，背包应由跟随动作自动关闭",
            "SUCCESS",
        )

        if not self._wait_after_follow_before_next_ui(f"{log_tag}·重新开包前"):
            return False
        self._emit(f"[{log_tag}] 重新打开精灵背包，不做颜色扫描", "INFO")
        try:
            drr._click_region(bag_open_btn_key, use_foreground)
        except KeyError:
            drr._click_region(bag_open_key, use_foreground)
        except Exception as exc:
            self._emit(f"❌ [{log_tag}] 重新打开精灵背包失败：{exc}", "ERROR")
            return False
        drr._sleep_abortable(stop_event, 0.5)

        try:
            self._emit(
                f"📦 [{log_tag}] 放回精灵一机塔；验证精灵四变深蓝",
                "INFO",
            )
            if not drr.put_back_bag_slot_from_open_bag(
                "一",
                use_foreground,
                stop_event,
                log_tag,
                verify_hp=False,
                verify_slot_deep_blue=True,
                deep_blue_verify_pos="四",
            ):
                return False
            drr._jita_pos = None
            drr._yameisi_pos = None
            self._emit(
                f"✅ [{log_tag}] 精灵一机塔已跟随并放回；背包仅余三只普通系",
                "SUCCESS",
            )
            return True
        finally:
            drr._close_pet_bag_with_verify(
                use_foreground,
                stop_event,
                bag_open_key,
                bag_open_btn_key,
                log_tag=log_tag,
            )

    def _new_daily_follow_then_return_purple(
        self,
        use_foreground: bool,
        *,
        log_tag: str,
        bag_already_open: bool = False,
    ) -> bool:
        """跟随背包中的紫色机塔，然后重新开包并只放回该紫色精灵。"""
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner", "ERROR")
            return False
        stop_event = self._new_daily_stop_event()

        if not drr.set_follow_purple_jita_from_closed_bag(
            use_foreground,
            stop_event,
            log_tag=f"{log_tag}·跟随",
            bag_already_open=bag_already_open,
        ):
            return False
        if not self._wait_after_follow_before_next_ui(f"{log_tag}·重新开包前"):
            return False
        if not drr._pickmode_open_bag_ready_for_target(
            use_foreground,
            stop_event,
            log_tag=f"{log_tag}·重新开包",
        ):
            return False

        bag_open_key = "精灵背包.打开精灵背包"
        bag_open_btn_key = "精灵背包.打开精灵背包按钮"
        try:
            scan = drr.scan_pick_bag_party_color_slots_any(
                stop_event,
                f"{log_tag}·定位紫色",
                timeout_s=10.0,
                min_cyan=0,
                min_purple=1,
            )
            purple = scan.get("purple") if isinstance(scan, dict) else None
            if not isinstance(scan, dict) or not scan.get("ok") or not purple:
                self._emit(f"❌ [{log_tag}] 跟随后未识别到紫色机塔，无法放回", "ERROR")
                return False
            self._emit(f"📦 [{log_tag}] 放回紫色机塔：精灵{purple}", "INFO")
            if not drr.put_back_bag_slot_from_open_bag(
                str(purple),
                use_foreground,
                stop_event,
                log_tag,
                verify_hp=False,
            ):
                return False
            drr._jita_pos = None
            drr._yameisi_pos = None
            self._emit(f"✅ [{log_tag}] 紫色机塔已跟随并放回仓库", "SUCCESS")
            return True
        finally:
            drr._close_pet_bag_with_verify(
                use_foreground,
                stop_event,
                bag_open_key,
                bag_open_btn_key,
                log_tag=log_tag,
            )

    def _new_daily_sequence4_first_step(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
        reconnect_before_setup: bool = True,
        inherit_jita_follow: bool = False,
    ) -> bool:
        """方案4第一步：顺接继承机塔跟随；切入则固定槽位建立跟随。"""
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner", "ERROR")
            return False
        stop_event = self._new_daily_stop_event()

        if reconnect_before_setup:
            self._emit(
                f"🔄 [{log_tag}] 方案4作为起点：刷新登录并屏蔽；"
                "清包后先取机塔到精灵一，再取普通系3/5/12",
                "SYSTEM",
            )
            if not drr.run_refresh_login_until_map(
                use_foreground,
                stop_event,
                include_base_and_map_gate=False,
            ):
                self._emit(f"❌ [{log_tag}] 刷新登录/屏蔽失败", "ERROR")
                return False
        else:
            self._emit(
                f"➡️ [{log_tag}] 从前序方案顺接到方案4：跳过刷新重连，"
                "继承方案3的机塔跟随；清包后只取普通系3/5/12",
                "SYSTEM",
            )
        if not self._new_daily_clear_backpack(
            use_foreground,
            log_tag=f"{log_tag}·清空背包",
            close_after=False,
        ):
            return False
        if not self._new_daily_warehouse_take_reverse_positions(
            use_foreground,
            category=NEW_DAILY_SEQ4_WAREHOUSE_CATEGORY,
            reverse_positions=NEW_DAILY_SEQ4_WAREHOUSE_REVERSE_POSITIONS,
            right_clicks=NEW_DAILY_SEQ4_WAREHOUSE_RIGHT_CLICKS,
            log_tag=f"{log_tag}·取宠",
            reverse_order=False,
            include_jita=not inherit_jita_follow,
            jita_first=not inherit_jita_follow,
            from_open_bag=True,
        ):
            return False
        if inherit_jita_follow:
            self._emit(
                f"✅ [{log_tag}] 沿用方案3的机塔跟随；三只普通系已入包，关闭背包",
                "SUCCESS",
            )
            drr._close_pet_bag_with_verify(
                use_foreground,
                stop_event,
                "精灵背包.打开精灵背包",
                "精灵背包.打开精灵背包按钮",
                log_tag=f"{log_tag}·顺接收尾",
            )
        elif not self._new_daily_follow_then_return_pet_one(
            use_foreground,
            log_tag=f"{log_tag}·精灵一机塔",
            bag_already_open=True,
        ):
            return False
        if not self._new_daily_base_gate_and_confirm(
            regions,
            use_foreground,
            log_tag=f"{log_tag}·基地",
        ):
            return False
        time.sleep(0.3)
        self._emit(
            f"🖱️ [{log_tag}] 点击 {NEW_DAILY_SEQ2_BASE_RIGHT_KEY}",
            "SYSTEM",
        )
        if not self._click_region_safe(
            regions,
            NEW_DAILY_SEQ2_BASE_RIGHT_KEY,
            use_foreground,
        ):
            return False
        return True

    def run_new_daily_sequence_1(
        self, use_foreground: bool = False, start_step: int = 1
    ) -> bool:
        """
        新日常方案「1」（步数说明见 new_daily/步骤说明.md）：
        ① 11甲烷挖矿×2 → ② 11切换→map21→12黄金矿×5 → ③ 12切换→map22→背包等橙色后跟随
        → ④ 13伊优脚本 → ⑤ 1AND1 至消失。大步骤之间间隔 0.5s。
        """
        start_step = max(1, int(start_step or 1))
        if start_step > NEW_DAILY_SEQ1_MAX_STEP:
            self._emit(
                f"❌ 新日常方案1 仅有 {NEW_DAILY_SEQ1_MAX_STEP} 步，无法从第 {start_step} 步开始",
                "ERROR",
            )
            return False
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions", "ERROR")
            return False

        required = (
            NEW_DAILY_MINING_SPOT_KEY,
            NEW_DAILY_SEQ1_SWITCH_11_KEY,
            NEW_DAILY_SEQ1_SWITCH_12_KEY,
            NEW_DAILY_SEQ1_GOLD_SPOT_KEY,
            MINING_SUKE_BLACK_KEY,
            MINING_SUKE_WHITE_KEY,
            MINING_START_KEY,
            "精灵背包.打开精灵背包",
            "精灵背包.清空精灵一",
            "精灵背包.身边跟随",
            "对话框.通用探针",
            "对话框.普通确认探针",
            "对话框.普通确认",
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ 新日常方案1 缺少区域：{key}", "ERROR")
                return False

        script_path = os.path.join(self.script_dir, f"{NEW_DAILY_SEQ1_SCRIPT_NAME}.json")
        if not os.path.isfile(script_path):
            self._emit(f"❌ 找不到脚本：{script_path}", "ERROR")
            return False

        tag = "新日常·1"
        if start_step > 1:
            self._emit(
                f"📋 [{tag}] 从第 {start_step} 步开始（前台={use_foreground}）",
                "SYSTEM",
            )
        else:
            self._emit(f"📋 [{tag}] 开始执行（前台={use_foreground}）", "SYSTEM")

        # ① 11甲烷 ×2
        if start_step <= 1:
            self._emit(f"① [{tag}] 11甲烷挖矿×{NEW_DAILY_MINING_TIMES}", "SYSTEM")
            if not self.run_mining_cycles(
                NEW_DAILY_MINING_SPOT_KEY,
                NEW_DAILY_MINING_TIMES,
                use_foreground=use_foreground,
            ):
                return False

        # ② 11切换 → map21 → 12黄金矿 ×5
        if start_step <= 2:
            if not self._new_daily_gap_before_step(start_step, 2):
                return False
            self._emit(f"② [{tag}] 点击 {NEW_DAILY_SEQ1_SWITCH_11_KEY}", "SYSTEM")
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ1_SWITCH_11_KEY, use_foreground
            ):
                return False
            if not self._wait_map_npc_then_delay(
                NEW_DAILY_SEQ1_MAP_AFTER_11, log_tag=f"{tag}·map21"
            ):
                return False
            if not self.run_mining_cycles(
                NEW_DAILY_SEQ1_GOLD_SPOT_KEY,
                NEW_DAILY_SEQ1_GOLD_TIMES,
                use_foreground=use_foreground,
            ):
                return False

        # ③ 12切换 → map22 → 背包
        if start_step <= 3:
            if not self._new_daily_gap_before_step(start_step, 3):
                return False
            self._emit(f"③ [{tag}] 点击 {NEW_DAILY_SEQ1_SWITCH_12_KEY}", "SYSTEM")
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ1_SWITCH_12_KEY, use_foreground
            ):
                return False
            if not self._wait_map_npc_then_delay(
                NEW_DAILY_SEQ1_MAP_AFTER_12, log_tag=f"{tag}·map22"
            ):
                return False
            if not self._new_daily_bag_follow_after_orange(
                regions, use_foreground, log_tag=f"{tag}·背包"
            ):
                return False

        # ④ 13伊优 脚本
        if start_step <= 4:
            if not self._new_daily_gap_before_step(start_step, 4):
                return False
            self._emit(f"④ [{tag}] 执行脚本 {NEW_DAILY_SEQ1_SCRIPT_NAME}", "SYSTEM")
            if not self.run_single_script(
                NEW_DAILY_SEQ1_SCRIPT_NAME, bg_mode=(not use_foreground)
            ):
                self._emit(
                    f"⚠️ [{tag}] 脚本 {NEW_DAILY_SEQ1_SCRIPT_NAME} 未完全成功", "WARN"
                )

        # ⑤ 1AND1
        if start_step <= 5:
            if not self._new_daily_gap_before_step(start_step, 5):
                return False
            self._emit(f"⑤ [{tag}] 普通 1AND1 直到消失", "SYSTEM")
            if not self._wait_1and1_clear(regions, use_foreground, log_tag=tag):
                return False

        self._emit(f"✅ [{tag}] 全部步骤完成", "SUCCESS")
        return True

    def run_new_daily_sequence_2(
        self, use_foreground: bool = False, start_step: int = 1
    ) -> bool:
        """
        新日常方案「2」（步数说明见 new_daily/步骤说明.md）：
        ① 回到基地 + 基地门控 + 基地右侧 → ② 背包放回+跟随（精灵六槽位深蓝）
        → ③ 20布布 → map11 → 21布布 → 1AND1
        → ④ 背包放回+跟随（精灵五槽位深蓝）→ 21布布 → 1AND1
        → ⑤ 背包放回+跟随（精灵四槽位深蓝）→ ⑥ 21切换 → map10 + 白色探针变非白 → 22黄金矿 ×5。
        """
        start_step = max(1, int(start_step or 1))
        if start_step > NEW_DAILY_SEQ2_MAX_STEP:
            self._emit(
                f"❌ 新日常方案2 仅有 {NEW_DAILY_SEQ2_MAX_STEP} 步，无法从第 {start_step} 步开始",
                "ERROR",
            )
            return False
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions", "ERROR")
            return False

        required = (
            NEW_DAILY_SEQ2_RETURN_BASE_KEY,
            NEW_DAILY_SEQ2_BASE_RIGHT_KEY,
            "对话框.普通确认",
            "精灵背包.打开精灵背包",
            "精灵背包.放回仓库",
            "精灵背包.清空精灵一",
            "精灵背包.6",
            "精灵背包.5",
            NEW_DAILY_SEQ2_SPOT_21_KEY,
            "对话框.通用探针",
            "对话框.普通确认探针",
            "对话框.普通确认",
            "精灵背包.4",
            "精灵背包.身边跟随",
            NEW_DAILY_SEQ2_SWITCH_21_KEY,
            NEW_DAILY_SEQ2_GOLD_SPOT_22_KEY,
            MAP10_WHITE_PROBE_KEY_NIEO,
            MINING_SUKE_BLACK_KEY,
            MINING_SUKE_WHITE_KEY,
            MINING_START_KEY,
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ 新日常方案2 缺少区域：{key}", "ERROR")
                return False

        for script_name in (NEW_DAILY_SEQ2_SCRIPT_NAME,):
            script_path = os.path.join(self.script_dir, f"{script_name}.json")
            if not os.path.isfile(script_path):
                self._emit(f"❌ 找不到脚本：{script_path}", "ERROR")
                return False

        tag = "新日常·2"
        if start_step > 1:
            self._emit(
                f"📋 [{tag}] 从第 {start_step} 步开始（前台={use_foreground}）",
                "SYSTEM",
            )
        else:
            self._emit(f"📋 [{tag}] 开始执行（前台={use_foreground}）", "SYSTEM")

        # ① 回到基地 → 门控 → 基地右侧
        if start_step <= 1:
            if not self._new_daily_base_gate_and_confirm(
                regions, use_foreground, log_tag=f"{tag}·基地"
            ):
                return False
            if not self._new_daily_step_gap():
                return False
            self._emit(
                f"🖱️ [{tag}] 点击 {NEW_DAILY_SEQ2_BASE_RIGHT_KEY}",
                "SYSTEM",
            )
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ2_BASE_RIGHT_KEY, use_foreground
            ):
                return False

        # ② 背包：放回 + 跟随（精灵六槽位深蓝）
        if start_step <= 2:
            if not self._new_daily_gap_before_step(start_step, 2):
                return False
            self._emit(f"② [{tag}] 打开背包 → 放回仓库 → 身边跟随", "SYSTEM")
            if not self._new_daily_bag_return_and_follow(
                regions,
                use_foreground,
                log_tag=f"{tag}·背包",
                verify_pet_pos="六",
            ):
                return False

        # ③ 20布布 → map11 → 21布布 → 1AND1
        if start_step <= 3:
            if not self._new_daily_gap_before_step(start_step, 3):
                return False
            self._emit(
                f"③ [{tag}] 执行脚本 {NEW_DAILY_SEQ2_SCRIPT_NAME}",
                "SYSTEM",
            )
            if not self.run_single_script(
                NEW_DAILY_SEQ2_SCRIPT_NAME, bg_mode=(not use_foreground)
            ):
                self._emit(
                    f"⚠️ [{tag}] 脚本 {NEW_DAILY_SEQ2_SCRIPT_NAME} 未完全成功",
                    "WARN",
                )
            if not self._wait_map_npc_then_delay(
                NEW_DAILY_SEQ2_MAP_AFTER_20, log_tag=f"{tag}·map11"
            ):
                return False
            self._emit(
                f"🖱️ [{tag}] 点击 {NEW_DAILY_SEQ2_SPOT_21_KEY}",
                "SYSTEM",
            )
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ2_SPOT_21_KEY, use_foreground
            ):
                return False
            if not self._wait_1and1_clear(
                regions,
                use_foreground,
                timeout_s=NEW_DAILY_SEQ2_1AND1_TIMEOUT_SEC,
                log_tag=f"{tag}·1AND1",
            ):
                return False

        # ④ 背包：放回 + 跟随（精灵五槽位深蓝）→ 21布布 → 1AND1
        if start_step <= 4:
            if not self._new_daily_gap_before_step(start_step, 4):
                return False
            self._emit(
                f"④ [{tag}] 打开背包 → 放回仓库 → 精灵五槽位深蓝 → 身边跟随 → 21布布 → 1AND1",
                "SYSTEM",
            )
            if not self._new_daily_bag_return_and_follow(
                regions,
                use_foreground,
                log_tag=f"{tag}·背包五",
                verify_pet_pos="五",
            ):
                return False
            self._emit(
                f"🖱️ [{tag}] 再次点击 {NEW_DAILY_SEQ2_SPOT_21_KEY}",
                "SYSTEM",
            )
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ2_SPOT_21_KEY, use_foreground
            ):
                return False
            if not self._wait_1and1_clear(
                regions,
                use_foreground,
                timeout_s=NEW_DAILY_SEQ2_1AND1_TIMEOUT_SEC,
                log_tag=f"{tag}·1AND1二",
            ):
                return False

        # ⑤ 背包：放回 + 跟随（精灵四槽位深蓝）
        if start_step <= 5:
            if not self._new_daily_gap_before_step(start_step, 5):
                return False
            self._emit(f"⑤ [{tag}] 打开背包 → 放回仓库 → 精灵四槽位深蓝 → 身边跟随", "SYSTEM")
            if not self._new_daily_bag_return_and_follow(
                regions,
                use_foreground,
                log_tag=f"{tag}·背包四",
                verify_pet_pos="四",
            ):
                return False

        # ⑥ 21切换 → map10 + 白色探针 → 22黄金矿 ×5
        if start_step <= 6:
            if not self._new_daily_gap_before_step(start_step, 6):
                return False
            self._emit(f"⑥ [{tag}] 点击 {NEW_DAILY_SEQ2_SWITCH_21_KEY}", "SYSTEM")
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ2_SWITCH_21_KEY, use_foreground
            ):
                return False
            if not self._wait_map_npc_then_delay(
                NEW_DAILY_SEQ2_MAP_AFTER_21_SWITCH, log_tag=f"{tag}·map10"
            ):
                return False
            if not self.run_mining_cycles(
                NEW_DAILY_SEQ2_GOLD_SPOT_22_KEY,
                NEW_DAILY_SEQ2_GOLD_TIMES,
                use_foreground=use_foreground,
            ):
                return False

        self._emit(f"✅ [{tag}] 全部步骤完成", "SUCCESS")
        return True

    def run_new_daily_sequence_3(
        self, use_foreground: bool = False, start_step: int = 1
    ) -> bool:
        """
        新日常方案「3」（步数说明见 new_daily/步骤说明.md）：
        ① 基地门控+基地右侧 → ② 30猩猩→map16→31甲烷×2
        → ③ 31切换→map15→32黄金矿×5 → ④ 32猩猩→1AND1
        → ⑤ 背包精灵三槽位深蓝 → ⑥ 32猩猩→MonkeyKongfu→32确认2s+32鼓励27s→1AND1
        → ⑦ 开包放回精灵一猩猩，确认精灵三变深蓝后跟随机塔。
        """
        start_step = max(1, int(start_step or 1))
        if start_step > NEW_DAILY_SEQ3_MAX_STEP:
            self._emit(
                f"❌ 新日常方案3 仅有 {NEW_DAILY_SEQ3_MAX_STEP} 步，无法从第 {start_step} 步开始",
                "ERROR",
            )
            return False
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions", "ERROR")
            return False

        required = (
            NEW_DAILY_SEQ2_RETURN_BASE_KEY,
            NEW_DAILY_SEQ2_BASE_RIGHT_KEY,
            "对话框.普通确认",
            NEW_DAILY_SEQ3_SPOT_31_METHANE_KEY,
            NEW_DAILY_SEQ3_SWITCH_31_KEY,
            NEW_DAILY_SEQ3_GOLD_32_KEY,
            NEW_DAILY_SEQ3_SPOT_32_GORILLA_KEY,
            NEW_DAILY_SEQ3_CONFIRM_32_KEY,
            NEW_DAILY_SEQ3_ENCOURAGE_32_KEY,
            MINING_SUKE_BLACK_KEY,
            MINING_SUKE_WHITE_KEY,
            MINING_START_KEY,
            "对话框.通用探针",
            "对话框.普通确认探针",
            "对话框.普通确认",
            "精灵背包.打开精灵背包",
            "精灵背包.放回仓库",
            "精灵背包.清空精灵一",
            "精灵背包.3",
            "精灵背包.身边跟随",
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ 新日常方案3 缺少区域：{key}", "ERROR")
                return False

        script_path = os.path.join(
            self.script_dir, f"{NEW_DAILY_SEQ3_SCRIPT_NAME}.json"
        )
        if not os.path.isfile(script_path):
            self._emit(f"❌ 找不到脚本：{script_path}", "ERROR")
            return False

        tag = "新日常·3"
        if start_step > 1:
            self._emit(
                f"📋 [{tag}] 从第 {start_step} 步开始（前台={use_foreground}）",
                "SYSTEM",
            )
        else:
            self._emit(f"📋 [{tag}] 开始执行（前台={use_foreground}）", "SYSTEM")

        # ① 回到基地 → 门控 → 基地右侧
        if start_step <= 1:
            if not self._new_daily_base_gate_and_confirm(
                regions, use_foreground, log_tag=f"{tag}·基地"
            ):
                return False
            if not self._new_daily_step_gap():
                return False
            self._emit(
                f"🖱️ [{tag}] 点击 {NEW_DAILY_SEQ2_BASE_RIGHT_KEY}",
                "SYSTEM",
            )
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ2_BASE_RIGHT_KEY, use_foreground
            ):
                return False

        # ② 30猩猩 → map16 → 31甲烷 ×2
        if start_step <= 2:
            if not self._new_daily_gap_before_step(start_step, 2):
                return False
            self._emit(
                f"② [{tag}] 执行脚本 {NEW_DAILY_SEQ3_SCRIPT_NAME}",
                "SYSTEM",
            )
            if not self.run_single_script(
                NEW_DAILY_SEQ3_SCRIPT_NAME, bg_mode=(not use_foreground)
            ):
                self._emit(
                    f"⚠️ [{tag}] 脚本 {NEW_DAILY_SEQ3_SCRIPT_NAME} 未完全成功",
                    "WARN",
                )
            if not self._wait_map_npc_then_delay(
                NEW_DAILY_SEQ3_MAP_AFTER_30, log_tag=f"{tag}·map16"
            ):
                return False
            if not self.run_mining_cycles(
                NEW_DAILY_SEQ3_SPOT_31_METHANE_KEY,
                NEW_DAILY_SEQ3_METHANE_TIMES,
                use_foreground=use_foreground,
            ):
                return False

        # ③ 31切换 → map15 → 32黄金矿 ×5
        if start_step <= 3:
            if not self._new_daily_gap_before_step(start_step, 3):
                return False
            self._emit(f"③ [{tag}] 点击 {NEW_DAILY_SEQ3_SWITCH_31_KEY}", "SYSTEM")
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ3_SWITCH_31_KEY, use_foreground
            ):
                return False
            if not self._wait_map_npc_then_delay(
                NEW_DAILY_SEQ3_MAP_AFTER_31_SWITCH, log_tag=f"{tag}·map15"
            ):
                return False
            if not self.run_mining_cycles(
                NEW_DAILY_SEQ3_GOLD_32_KEY,
                NEW_DAILY_SEQ3_GOLD_TIMES,
                use_foreground=use_foreground,
            ):
                return False

        # ④ 32猩猩 → 1AND1
        if start_step <= 4:
            if not self._new_daily_gap_before_step(start_step, 4):
                return False
            self._emit(
                f"④ [{tag}] 点击 {NEW_DAILY_SEQ3_SPOT_32_GORILLA_KEY}",
                "SYSTEM",
            )
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ3_SPOT_32_GORILLA_KEY, use_foreground
            ):
                return False
            if not self._wait_1and1_clear(
                regions,
                use_foreground,
                timeout_s=NEW_DAILY_SEQ3_1AND1_TIMEOUT_SEC,
                log_tag=f"{tag}·1AND1",
            ):
                return False

        # ⑤ 背包：放回 + 跟随（精灵三槽位深蓝）
        if start_step <= 5:
            if not self._new_daily_gap_before_step(start_step, 5):
                return False
            self._emit(f"⑤ [{tag}] 打开背包 → 放回仓库 → 精灵三槽位深蓝 → 身边跟随", "SYSTEM")
            if not self._new_daily_bag_return_and_follow(
                regions,
                use_foreground,
                log_tag=f"{tag}·背包三",
                verify_pet_pos="三",
            ):
                return False

        # ⑥ 32猩猩 → MonkeyKongfu → 32确认2s + 32鼓励27s → 1AND1
        if start_step <= 6:
            if not self._new_daily_gap_before_step(start_step, 6):
                return False
            self._emit(
                f"⑥ [{tag}] 点击 {NEW_DAILY_SEQ3_SPOT_32_GORILLA_KEY}",
                "SYSTEM",
            )
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ3_SPOT_32_GORILLA_KEY, use_foreground
            ):
                return False
            if not self._wait_kernel_line_matches(
                RE_MONKEY_KUNGFU_TASK_SWF,
                log_tag=f"{tag}·MonkeyKongfu",
                timeout_s=NEW_DAILY_SEQ3_KERNEL_WAIT_TIMEOUT_SEC,
                success_msg=(
                    f"✅ [{tag}] 检测到 MonkeyKongfu.swf，开始连续点击确认/鼓励"
                ),
            ):
                return False
            if not self._rapid_click_region_for_duration(
                regions,
                NEW_DAILY_SEQ3_CONFIRM_32_KEY,
                use_foreground,
                NEW_DAILY_SEQ3_CONFIRM_BURST_SEC,
                log_tag=f"{tag}·32确认",
            ):
                return False
            if not self._rapid_click_region_for_duration(
                regions,
                NEW_DAILY_SEQ3_ENCOURAGE_32_KEY,
                use_foreground,
                NEW_DAILY_SEQ3_ENCOURAGE_BURST_SEC,
                log_tag=f"{tag}·32鼓励",
            ):
                return False
            if not self._wait_1and1_clear(
                regions,
                use_foreground,
                timeout_s=NEW_DAILY_SEQ3_1AND1_TIMEOUT_SEC,
                log_tag=f"{tag}·鼓励后1AND1",
            ):
                return False

        # ⑦ 第二次猩猩结束后共有猩猩、机塔、奖励闪尼三只：
        #    开包默认选中精灵一猩猩，放回后机塔前移到精灵一，再直接跟随。
        #    连续链随后由方案4负责清空背包并重取普通系目标组。
        if start_step <= 7:
            if not self._new_daily_gap_before_step(start_step, 7):
                return False
            self._emit(
                f"⑦ [{tag}] 打开背包 → 放回精灵一猩猩 → 精灵三槽位深蓝 → "
                "机塔前移到精灵一 → 直接身边跟随",
                "SYSTEM",
            )
            if not self._new_daily_bag_return_and_follow(
                regions,
                use_foreground,
                log_tag=f"{tag}·放回猩猩跟随机塔",
                verify_pet_pos="三",
                expected_pet_count=3,
                expected_pet_count_wait_timeout_s=(
                    NEW_DAILY_SEQ3_REWARD_BAG_WAIT_TIMEOUT_SEC
                ),
            ):
                return False

        self._emit(f"✅ [{tag}] 全部步骤完成", "SUCCESS")
        return True

    def run_new_daily_sequence_4(
        self,
        use_foreground: bool = False,
        start_step: int = 1,
        *,
        reconnect_first_step: bool = True,
        inherit_jita_follow: bool = False,
    ) -> bool:
        """
        新日常方案「4」：
        ① 顺接时继承机塔跟随并只取三普通；切入时机塔先取到精灵一、
        跟随并放回精灵一 → 基地门控 + 基地右侧
        → ②/③ 仅供从中间步骤启动时执行原清包/普通系取宠
        → ④ 40云霄→map25→41云霄 → ⑤ 41甲烷×2。
        """
        start_step = max(1, int(start_step or 1))
        if start_step > NEW_DAILY_SEQ4_MAX_STEP:
            self._emit(
                f"❌ 新日常方案4 仅有 {NEW_DAILY_SEQ4_MAX_STEP} 步，无法从第 {start_step} 步开始",
                "ERROR",
            )
            return False
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions", "ERROR")
            return False

        required = (
            NEW_DAILY_SEQ2_RETURN_BASE_KEY,
            NEW_DAILY_SEQ2_BASE_RIGHT_KEY,
            "对话框.普通确认",
            "精灵背包.打开精灵背包",
            "精灵背包.放回仓库",
            "精灵背包.清空精灵一",
            "精灵背包.1",
            "精灵背包.身边跟随",
            NEW_DAILY_SEQ4_WAREHOUSE_SINGLE_ATTR_KEY,
            f"精灵仓库.{NEW_DAILY_SEQ4_WAREHOUSE_CATEGORY}",
            "精灵仓库.打开",
            "精灵仓库.关闭",
            "精灵仓库.ALL",
            "精灵仓库.3",
            "精灵仓库.5",
            "精灵仓库.右",
            "精灵仓库.左",
            "精灵仓库.放入背包",
            NEW_DAILY_SEQ4_SPOT_41_METHANE_KEY,
            MINING_SUKE_BLACK_KEY,
            MINING_SUKE_WHITE_KEY,
            MINING_START_KEY,
            "对话框.通用探针",
            "对话框.普通确认探针",
            "对话框.普通确认",
        )
        if start_step <= 1 and not inherit_jita_follow:
            required = required + (
                "精灵背包.4",
                "精灵仓库.双属性",
                "精灵仓库.机械龙系",
            )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ 新日常方案4 缺少区域：{key}", "ERROR")
                return False

        for script_name in (
            NEW_DAILY_SEQ4_SCRIPT_40_NAME,
            NEW_DAILY_SEQ4_SCRIPT_41_NAME,
        ):
            script_path = os.path.join(self.script_dir, f"{script_name}.json")
            if not os.path.isfile(script_path):
                self._emit(f"❌ 找不到脚本：{script_path}", "ERROR")
                return False

        tag = "新日常·4"
        if start_step > 1:
            self._emit(
                f"📋 [{tag}] 从第 {start_step} 步开始（前台={use_foreground}）",
                "SYSTEM",
            )
        else:
            self._emit(f"📋 [{tag}] 开始执行（前台={use_foreground}）", "SYSTEM")

        # ① 直接切入：重连后机塔先取到精灵一，三普通随后，
        #    固定跟随并放回精灵一。方案3顺接：继承机塔跟随，
        #    清包后只取三普通。两条路径都不依赖紫色扫描。
        if start_step <= 1:
            if not self._new_daily_sequence4_first_step(
                regions,
                use_foreground,
                log_tag=f"{tag}·第一步",
                reconnect_before_setup=reconnect_first_step,
                inherit_jita_follow=inherit_jita_follow,
            ):
                return False

        # ② 从第2步启动时保留原清包；第1步已经完成清包，不重复执行。
        if start_step == 2:
            if not self._new_daily_gap_before_step(start_step, 2):
                return False
            if not self._new_daily_clear_backpack(
                use_foreground, log_tag=f"{tag}·清空背包"
            ):
                return False

        # ③ 从第2/3步启动时保留原普通系取宠；第1步已经完成取宠，不重复执行。
        if 1 < start_step <= 3:
            if not self._new_daily_gap_before_step(start_step, 3):
                return False
            if not self._new_daily_warehouse_take_reverse_positions(
                use_foreground,
                category=NEW_DAILY_SEQ4_WAREHOUSE_CATEGORY,
                reverse_positions=NEW_DAILY_SEQ4_WAREHOUSE_REVERSE_POSITIONS,
                right_clicks=NEW_DAILY_SEQ4_WAREHOUSE_RIGHT_CLICKS,
                log_tag=f"{tag}·仓库",
                reverse_order=False,
            ):
                return False

        # ④ 40云霄 → map25 → 41云霄
        if start_step <= 4:
            if not self._new_daily_gap_before_step(start_step, 4):
                return False
            self._emit(
                f"④ [{tag}] 执行脚本 {NEW_DAILY_SEQ4_SCRIPT_40_NAME}",
                "SYSTEM",
            )
            if not self.run_single_script(
                NEW_DAILY_SEQ4_SCRIPT_40_NAME, bg_mode=(not use_foreground)
            ):
                self._emit(
                    f"⚠️ [{tag}] 脚本 {NEW_DAILY_SEQ4_SCRIPT_40_NAME} 未完全成功",
                    "WARN",
                )
            if not self._wait_map_npc_then_delay(
                NEW_DAILY_SEQ4_MAP_AFTER_40, log_tag=f"{tag}·map25"
            ):
                return False
            self._emit(
                f"④ [{tag}] 执行脚本 {NEW_DAILY_SEQ4_SCRIPT_41_NAME}",
                "SYSTEM",
            )
            if not self.run_single_script(
                NEW_DAILY_SEQ4_SCRIPT_41_NAME, bg_mode=(not use_foreground)
            ):
                self._emit(
                    f"⚠️ [{tag}] 脚本 {NEW_DAILY_SEQ4_SCRIPT_41_NAME} 未完全成功",
                    "WARN",
                )

        # ⑤ 41甲烷 ×2
        if start_step <= 5:
            if not self._new_daily_gap_before_step(start_step, 5):
                return False
            if not self.run_mining_cycles(
                NEW_DAILY_SEQ4_SPOT_41_METHANE_KEY,
                NEW_DAILY_SEQ4_METHANE_TIMES,
                use_foreground=use_foreground,
            ):
                return False

        self._emit(f"✅ [{tag}] 全部步骤完成", "SUCCESS")
        return True

    def run_new_daily_sequence_5(
        self, use_foreground: bool = False, start_step: int = 1
    ) -> bool:
        """
        新日常方案「5」：
        ① 回到基地门控 → 双击基地右侧
        → ② 50赫尔卡 → map34 → 51藤蔓交互（iris→确认→苏克→挖矿×1）。
        """
        start_step = max(1, int(start_step or 1))
        if start_step > NEW_DAILY_SEQ5_MAX_STEP:
            self._emit(
                f"❌ 新日常方案5 仅有 {NEW_DAILY_SEQ5_MAX_STEP} 步，无法从第 {start_step} 步开始",
                "ERROR",
            )
            return False
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions", "ERROR")
            return False

        required = (
            NEW_DAILY_SEQ2_RETURN_BASE_KEY,
            NEW_DAILY_SEQ2_BASE_RIGHT_KEY,
            "对话框.普通确认",
            NEW_DAILY_SEQ5_SPOT_51_VINES_KEY,
            MINING_SUKE_BLACK_KEY,
            MINING_SUKE_WHITE_KEY,
            MINING_START_KEY,
            "对话框.通用探针",
            "对话框.普通确认探针",
            "对话框.普通确认",
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ 新日常方案5 缺少区域：{key}", "ERROR")
                return False

        script_path = os.path.join(
            self.script_dir, f"{NEW_DAILY_SEQ5_SCRIPT_50_NAME}.json"
        )
        if not os.path.isfile(script_path):
            self._emit(f"❌ 找不到脚本：{script_path}", "ERROR")
            return False

        tag = "新日常·5"
        if start_step > 1:
            self._emit(
                f"📋 [{tag}] 从第 {start_step} 步开始（前台={use_foreground}）",
                "SYSTEM",
            )
        else:
            self._emit(f"📋 [{tag}] 开始执行（前台={use_foreground}）", "SYSTEM")

        # ① 回到基地 → 门控 → 双击基地右侧
        if start_step <= 1:
            if not self._new_daily_base_gate_and_confirm(
                regions, use_foreground, log_tag=f"{tag}·基地"
            ):
                return False
            if not self._new_daily_step_gap():
                return False
            self._emit(
                f"🖱️ [{tag}] 双击 {NEW_DAILY_SEQ2_BASE_RIGHT_KEY}",
                "SYSTEM",
            )
            if not self._click_region_safe_twice(
                regions, NEW_DAILY_SEQ2_BASE_RIGHT_KEY, use_foreground
            ):
                return False

        # ② 50赫尔卡 → map34 → 51藤蔓
        if start_step <= 2:
            if not self._new_daily_gap_before_step(start_step, 2):
                return False
            self._emit(
                f"② [{tag}] 执行脚本 {NEW_DAILY_SEQ5_SCRIPT_50_NAME}",
                "SYSTEM",
            )
            if not self.run_single_script(
                NEW_DAILY_SEQ5_SCRIPT_50_NAME, bg_mode=(not use_foreground)
            ):
                self._emit(
                    f"⚠️ [{tag}] 脚本 {NEW_DAILY_SEQ5_SCRIPT_50_NAME} 未完全成功",
                    "WARN",
                )
            if not self._wait_map_npc_then_delay(
                NEW_DAILY_SEQ5_MAP_AFTER_50, log_tag=f"{tag}·map34"
            ):
                return False
            if not self._new_daily_seq5_vines_interact_and_mine(
                regions, use_foreground, log_tag=tag
            ):
                return False

        self._emit(f"✅ [{tag}] 全部步骤完成", "SUCCESS")
        return True

    def run_new_daily_sequence_6(
        self, use_foreground: bool = False, start_step: int = 1
    ) -> bool:
        """
        新日常方案「6」：
        ① 基地门控 → 双击基地右侧
        → ② 60阿尔法一 → 快速点 60向下 2s
        → ③ 60阿尔法二 → map105 → 61蘑菇结晶×2
        → ④ 61切换 → map106 → 62纳格晶体×1
        → ⑤ 62豆豆果实×1
        → ⑥ 62切换 → map46 → 双击 63双击一/二
        → ⑦ 63切换 → map49 → 64电能石×2。
        """
        start_step = max(1, int(start_step or 1))
        if start_step > NEW_DAILY_SEQ6_MAX_STEP:
            self._emit(
                f"❌ 新日常方案6 仅有 {NEW_DAILY_SEQ6_MAX_STEP} 步，无法从第 {start_step} 步开始",
                "ERROR",
            )
            return False
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions", "ERROR")
            return False

        required = (
            NEW_DAILY_SEQ2_RETURN_BASE_KEY,
            NEW_DAILY_SEQ2_BASE_RIGHT_KEY,
            "对话框.普通确认",
            NEW_DAILY_SEQ6_DOWN_KEY,
            NEW_DAILY_SEQ6_SPOT_61_MUSHROOM_KEY,
            NEW_DAILY_SEQ6_SWITCH_61_KEY,
            NEW_DAILY_SEQ6_SPOT_62_NAGA_KEY,
            NEW_DAILY_SEQ6_SPOT_62_BEAN_KEY,
            NEW_DAILY_SEQ6_SWITCH_62_KEY,
            NEW_DAILY_SEQ6_DOUBLE_63_1_KEY,
            NEW_DAILY_SEQ6_DOUBLE_63_2_KEY,
            NEW_DAILY_SEQ6_SWITCH_63_KEY,
            NEW_DAILY_SEQ6_SPOT_64_POWER_KEY,
            MINING_SUKE_BLACK_KEY,
            MINING_SUKE_WHITE_KEY,
            MINING_START_KEY,
            "对话框.通用探针",
            "对话框.普通确认探针",
            "对话框.普通确认",
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ 新日常方案6 缺少区域：{key}", "ERROR")
                return False

        for script_name in (
            NEW_DAILY_SEQ6_SCRIPT_60_1_NAME,
            NEW_DAILY_SEQ6_SCRIPT_60_2_NAME,
        ):
            script_path = os.path.join(self.script_dir, f"{script_name}.json")
            if not os.path.isfile(script_path):
                self._emit(f"❌ 找不到脚本：{script_path}", "ERROR")
                return False

        tag = "新日常·6"
        if start_step > 1:
            self._emit(
                f"📋 [{tag}] 从第 {start_step} 步开始（前台={use_foreground}）",
                "SYSTEM",
            )
        else:
            self._emit(f"📋 [{tag}] 开始执行（前台={use_foreground}）", "SYSTEM")

        # ① 回到基地 → 门控 → 双击基地右侧
        if start_step <= 1:
            if not self._new_daily_base_gate_and_confirm(
                regions, use_foreground, log_tag=f"{tag}·基地"
            ):
                return False
            if not self._new_daily_step_gap():
                return False
            self._emit(
                f"🖱️ [{tag}] 双击 {NEW_DAILY_SEQ2_BASE_RIGHT_KEY}",
                "SYSTEM",
            )
            if not self._click_region_safe_twice(
                regions, NEW_DAILY_SEQ2_BASE_RIGHT_KEY, use_foreground
            ):
                return False

        # ② 60阿尔法一 → 快速点 60向下 2s
        if start_step <= 2:
            if not self._new_daily_gap_before_step(start_step, 2):
                return False
            self._emit(
                f"② [{tag}] 执行脚本 {NEW_DAILY_SEQ6_SCRIPT_60_1_NAME}",
                "SYSTEM",
            )
            if not self.run_single_script(
                NEW_DAILY_SEQ6_SCRIPT_60_1_NAME, bg_mode=(not use_foreground)
            ):
                self._emit(
                    f"⚠️ [{tag}] 脚本 {NEW_DAILY_SEQ6_SCRIPT_60_1_NAME} 未完全成功",
                    "WARN",
                )
            if not self._rapid_click_region_for_duration(
                regions,
                NEW_DAILY_SEQ6_DOWN_KEY,
                use_foreground,
                NEW_DAILY_SEQ6_DOWN_RAPID_SEC,
                log_tag=f"{tag}·60向下",
            ):
                return False

        # ③ 60阿尔法二 → map105 → 61蘑菇结晶 ×2
        if start_step <= 3:
            if not self._new_daily_gap_before_step(start_step, 3):
                return False
            self._emit(
                f"③ [{tag}] 执行脚本 {NEW_DAILY_SEQ6_SCRIPT_60_2_NAME}",
                "SYSTEM",
            )
            if not self.run_single_script(
                NEW_DAILY_SEQ6_SCRIPT_60_2_NAME, bg_mode=(not use_foreground)
            ):
                self._emit(
                    f"⚠️ [{tag}] 脚本 {NEW_DAILY_SEQ6_SCRIPT_60_2_NAME} 未完全成功",
                    "WARN",
                )
            if not self._wait_map_npc_then_delay(
                NEW_DAILY_SEQ6_MAP_AFTER_60_2, log_tag=f"{tag}·map105"
            ):
                return False
            if not self.run_mining_cycles(
                NEW_DAILY_SEQ6_SPOT_61_MUSHROOM_KEY,
                NEW_DAILY_SEQ6_MUSHROOM_TIMES,
                use_foreground=use_foreground,
            ):
                return False

        # ④ 61切换 → map106 → 62纳格晶体 ×1
        if start_step <= 4:
            if not self._new_daily_gap_before_step(start_step, 4):
                return False
            self._emit(
                f"🖱️ [{tag}] 点击 {NEW_DAILY_SEQ6_SWITCH_61_KEY}",
                "SYSTEM",
            )
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ6_SWITCH_61_KEY, use_foreground
            ):
                return False
            if not self._wait_map_npc_then_delay(
                NEW_DAILY_SEQ6_MAP_AFTER_61_SWITCH, log_tag=f"{tag}·map106"
            ):
                return False
            if not self.run_mining_cycles(
                NEW_DAILY_SEQ6_SPOT_62_NAGA_KEY,
                NEW_DAILY_SEQ6_NAGA_TIMES,
                use_foreground=use_foreground,
            ):
                return False

        # ⑤ 62豆豆果实 ×1
        if start_step <= 5:
            if not self._new_daily_gap_before_step(start_step, 5):
                return False
            if not self.run_mining_cycles(
                NEW_DAILY_SEQ6_SPOT_62_BEAN_KEY,
                NEW_DAILY_SEQ6_BEAN_TIMES,
                use_foreground=use_foreground,
            ):
                return False

        # ⑥ 62切换 → map46 → 双击 63双击一、63双击二
        if start_step <= 6:
            if not self._new_daily_gap_before_step(start_step, 6):
                return False
            self._emit(
                f"🖱️ [{tag}] 点击 {NEW_DAILY_SEQ6_SWITCH_62_KEY}",
                "SYSTEM",
            )
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ6_SWITCH_62_KEY, use_foreground
            ):
                return False
            if not self._wait_map_npc_then_delay(
                NEW_DAILY_SEQ6_MAP_AFTER_62_SWITCH, log_tag=f"{tag}·map46"
            ):
                return False
            for dbl_key in (
                NEW_DAILY_SEQ6_DOUBLE_63_1_KEY,
                NEW_DAILY_SEQ6_DOUBLE_63_2_KEY,
            ):
                self._emit(f"🖱️ [{tag}] 双击 {dbl_key}", "SYSTEM")
                if not self._click_region_safe_twice(
                    regions, dbl_key, use_foreground
                ):
                    return False

        # ⑦ 63切换 → map49 → 64电能石 ×2
        if start_step <= 7:
            if not self._new_daily_gap_before_step(start_step, 7):
                return False
            self._emit(
                f"🖱️ [{tag}] 点击 {NEW_DAILY_SEQ6_SWITCH_63_KEY}",
                "SYSTEM",
            )
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ6_SWITCH_63_KEY, use_foreground
            ):
                return False
            if not self._wait_map_npc_then_delay(
                NEW_DAILY_SEQ6_MAP_AFTER_63_SWITCH, log_tag=f"{tag}·map49"
            ):
                return False
            if not self.run_mining_cycles(
                NEW_DAILY_SEQ6_SPOT_64_POWER_KEY,
                NEW_DAILY_SEQ6_POWER_TIMES,
                use_foreground=use_foreground,
            ):
                return False

        self._emit(f"✅ [{tag}] 全部步骤完成", "SUCCESS")
        return True

    def run_new_daily_sequence_7(
        self, use_foreground: bool = False, start_step: int = 1
    ) -> bool:
        """
        新日常方案「7」：
        ① 基地门控 → 双击基地右侧
        → ② 70露西欧 → map54 → 71一/二/三 各挖一次
        → ③ 双击 71右下 → 连点 71召唤 至 action\\
        → ④ 执行固定脚本 72珊瑚（原 5.json Step118–126）。
        """
        start_step = max(1, int(start_step or 1))
        if start_step > NEW_DAILY_SEQ7_MAX_STEP:
            self._emit(
                f"❌ 新日常方案7 仅有 {NEW_DAILY_SEQ7_MAX_STEP} 步，无法从第 {start_step} 步开始",
                "ERROR",
            )
            return False
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions", "ERROR")
            return False

        required = (
            NEW_DAILY_SEQ2_RETURN_BASE_KEY,
            NEW_DAILY_SEQ2_BASE_RIGHT_KEY,
            "对话框.普通确认",
            *NEW_DAILY_SEQ7_SPOT_71_KEYS,
            NEW_DAILY_SEQ7_71_BOTTOM_RIGHT_KEY,
            NEW_DAILY_SEQ7_71_SUMMON_KEY,
            MINING_SUKE_BLACK_KEY,
            MINING_SUKE_WHITE_KEY,
            MINING_START_KEY,
            "对话框.通用探针",
            "对话框.普通确认探针",
            "对话框.普通确认",
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ 新日常方案7 缺少区域：{key}", "ERROR")
                return False

        for script_name in (
            NEW_DAILY_SEQ7_SCRIPT_70_NAME,
            NEW_DAILY_SEQ7_SCRIPT_72_NAME,
        ):
            script_path = os.path.join(self.script_dir, f"{script_name}.json")
            if not os.path.isfile(script_path):
                self._emit(f"❌ 找不到脚本：{script_path}", "ERROR")
                return False

        tag = "新日常·7"
        if start_step > 1:
            self._emit(
                f"📋 [{tag}] 从第 {start_step} 步开始（前台={use_foreground}）",
                "SYSTEM",
            )
        else:
            self._emit(f"📋 [{tag}] 开始执行（前台={use_foreground}）", "SYSTEM")

        # ① 回到基地 → 门控 → 双击基地右侧
        if start_step <= 1:
            if not self._new_daily_base_gate_and_confirm(
                regions, use_foreground, log_tag=f"{tag}·基地"
            ):
                return False
            if not self._new_daily_step_gap():
                return False
            self._emit(
                f"🖱️ [{tag}] 双击 {NEW_DAILY_SEQ2_BASE_RIGHT_KEY}",
                "SYSTEM",
            )
            if not self._click_region_safe_twice(
                regions, NEW_DAILY_SEQ2_BASE_RIGHT_KEY, use_foreground
            ):
                return False

        # ② 70露西欧 → map54 → 71一/二/三 各挖一次
        if start_step <= 2:
            if not self._new_daily_gap_before_step(start_step, 2):
                return False
            self._emit(
                f"② [{tag}] 执行脚本 {NEW_DAILY_SEQ7_SCRIPT_70_NAME}",
                "SYSTEM",
            )
            if not self.run_single_script(
                NEW_DAILY_SEQ7_SCRIPT_70_NAME, bg_mode=(not use_foreground)
            ):
                self._emit(
                    f"⚠️ [{tag}] 脚本 {NEW_DAILY_SEQ7_SCRIPT_70_NAME} 未完全成功",
                    "WARN",
                )
            if not self._wait_map_npc_then_delay(
                NEW_DAILY_SEQ7_MAP_AFTER_70, log_tag=f"{tag}·map54"
            ):
                return False
            for spot_key in NEW_DAILY_SEQ7_SPOT_71_KEYS:
                if not self.run_mining_cycles(
                    spot_key, 1, use_foreground=use_foreground
                ):
                    return False

        # ③ 双击 71右下 → 连点 71召唤 至 action\
        if start_step <= 3:
            if not self._new_daily_gap_before_step(start_step, 3):
                return False
            if not self._new_daily_seq7_summon_until_action(
                regions, use_foreground, log_tag=tag
            ):
                return False

        # ④ 执行固定脚本 72珊瑚
        if start_step <= 4:
            if not self._new_daily_gap_before_step(start_step, 4):
                return False
            self._emit(
                f"④ [{tag}] 执行脚本 {NEW_DAILY_SEQ7_SCRIPT_72_NAME}",
                "SYSTEM",
            )
            if not self.run_single_script(
                NEW_DAILY_SEQ7_SCRIPT_72_NAME, bg_mode=(not use_foreground)
            ):
                self._emit(
                    f"⚠️ [{tag}] 脚本 {NEW_DAILY_SEQ7_SCRIPT_72_NAME} 未完全成功",
                    "WARN",
                )

        self._emit(f"✅ [{tag}] 全部步骤完成", "SUCCESS")
        return True

    def _new_daily_click_map_then_delay(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str = "新日常·8",
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """点击左下角地图按钮 → 固定等待。"""
        self._emit(f"🖱️ [{log_tag}] 点击 {NEW_DAILY_SEQ8_MAP_BTN_KEY}", "SYSTEM")
        if not self._click_region_safe(
            regions, NEW_DAILY_SEQ8_MAP_BTN_KEY, use_foreground
        ):
            return False
        should_stop = stop_check or self._should_abort
        end_at = time.monotonic() + NEW_DAILY_SEQ8_MAP_BTN_DELAY_SEC
        while time.monotonic() < end_at:
            if should_stop():
                return False
            time.sleep(min(0.05, max(0.0, end_at - time.monotonic())))
        return True

    def _seq8_collect_items_from_lines(
        self, lines, collected: set, *, log_tag: str
    ) -> None:
        if not isinstance(lines, list):
            return
        for line in lines:
            s = str(line)
            for item_id, pattern, label in NEW_DAILY_SEQ8_COLLECT_ITEM_SPECS:
                if item_id in collected:
                    continue
                if line_matches(pattern, s):
                    collected.add(item_id)
                    self._emit(
                        f"✅ [{log_tag}] 获得物品 {label}（{len(collected)}/3）",
                        "SUCCESS",
                    )

    def _new_daily_seq8_collect_84_items_loop(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str = "新日常·8",
    ) -> bool:
        """
        84一/二/三 循环：每点一次 → 必做完 1AND1 → 再点下一个；
        直至内核集齐三件物品（当次 1AND1 不可跳过）。
        """
        from core.logger import fetch_kernel_since, kernel_cursor

        collected: set = set()
        need = {spec[0] for spec in NEW_DAILY_SEQ8_COLLECT_ITEM_SPECS}
        self._emit(
            f"⏳ [{log_tag}] 84一/二/三 循环采集，目标 3 件物品…",
            "INFO",
        )
        for rnd in range(1, NEW_DAILY_SEQ8_COLLECT_MAX_ROUNDS + 1):
            if self._should_abort():
                return False
            if collected >= need:
                break
            for spot_key in NEW_DAILY_SEQ8_SPOT_84_KEYS:
                if self._should_abort():
                    return False
                if collected >= need:
                    break
                self._emit(
                    f"🖱️ [{log_tag}] 第 {rnd} 轮 · 点击 {spot_key}",
                    "INFO",
                )
                click_cursor = kernel_cursor()
                if not self._click_region_safe(regions, spot_key, use_foreground):
                    return False
                if not self._wait_1and1_clear(
                    regions,
                    use_foreground,
                    log_tag=f"{log_tag}·{spot_key.split('.')[-1]}",
                ):
                    return False
                try:
                    lines = fetch_kernel_since(click_cursor)
                    self._seq8_collect_items_from_lines(
                        lines, collected, log_tag=log_tag
                    )
                except Exception:
                    pass
        if collected >= need:
            self._emit(f"✅ [{log_tag}] 三件物品已集齐", "SUCCESS")
            return True
        missing = sorted(need - collected)
        self._emit(
            f"❌ [{log_tag}] 采集超时，仍缺：{', '.join(missing)}",
            "ERROR",
        )
        return False

    def run_new_daily_sequence_8(
        self, use_foreground: bool = False, start_step: int = 1
    ) -> bool:
        """
        新日常方案「8」：
        ① 80飞船 → ② 81星系 → map325+BGM → 81黄金矿×5
        → ③ 地图→82斯科尔→map328→83甲烷×2
        → ④ 地图→83普雷→map333→84切换→map339→84一/二/三采集。
        """
        start_step = max(1, int(start_step or 1))
        if start_step > NEW_DAILY_SEQ8_MAX_STEP:
            self._emit(
                f"❌ 新日常方案8 仅有 {NEW_DAILY_SEQ8_MAX_STEP} 步，无法从第 {start_step} 步开始",
                "ERROR",
            )
            return False
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions", "ERROR")
            return False

        required = (
            NEW_DAILY_SEQ8_SPOT_81_GOLD_KEY,
            NEW_DAILY_SEQ8_MAP_BTN_KEY,
            NEW_DAILY_SEQ8_SPOT_82_KEY,
            NEW_DAILY_SEQ8_SPOT_83_METHANE_KEY,
            NEW_DAILY_SEQ8_SPOT_83_PULEI_KEY,
            NEW_DAILY_SEQ8_SWITCH_84_KEY,
            *NEW_DAILY_SEQ8_SPOT_84_KEYS,
            MINING_SUKE_BLACK_KEY,
            MINING_SUKE_WHITE_KEY,
            MINING_START_KEY,
            "对话框.通用探针",
            "对话框.普通确认探针",
            "对话框.普通确认",
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ 新日常方案8 缺少区域：{key}", "ERROR")
                return False

        for script_name in (
            NEW_DAILY_SEQ8_SCRIPT_80_NAME,
            NEW_DAILY_SEQ8_SCRIPT_81_NAME,
        ):
            script_path = os.path.join(self.script_dir, f"{script_name}.json")
            if not os.path.isfile(script_path):
                self._emit(f"❌ 找不到脚本：{script_path}", "ERROR")
                return False

        tag = "新日常·8"
        if start_step > 1:
            self._emit(
                f"📋 [{tag}] 从第 {start_step} 步开始（前台={use_foreground}）",
                "SYSTEM",
            )
        else:
            self._emit(f"📋 [{tag}] 开始执行（前台={use_foreground}）", "SYSTEM")

        # ① 80飞船
        if start_step <= 1:
            self._emit(
                f"① [{tag}] 执行脚本 {NEW_DAILY_SEQ8_SCRIPT_80_NAME}",
                "SYSTEM",
            )
            if not self.run_single_script(
                NEW_DAILY_SEQ8_SCRIPT_80_NAME, bg_mode=(not use_foreground)
            ):
                self._emit(
                    f"⚠️ [{tag}] 脚本 {NEW_DAILY_SEQ8_SCRIPT_80_NAME} 未完全成功",
                    "WARN",
                )

        # ② 81星系 → map325+BGM_228 → 81黄金矿×5
        if start_step <= 2:
            if not self._new_daily_gap_before_step(start_step, 2):
                return False
            self._emit(
                f"② [{tag}] 执行脚本 {NEW_DAILY_SEQ8_SCRIPT_81_NAME}",
                "SYSTEM",
            )
            if not self.run_single_script(
                NEW_DAILY_SEQ8_SCRIPT_81_NAME, bg_mode=(not use_foreground)
            ):
                self._emit(
                    f"⚠️ [{tag}] 脚本 {NEW_DAILY_SEQ8_SCRIPT_81_NAME} 未完全成功",
                    "WARN",
                )
            if not self._wait_kernel_map_and_bgm(
                NEW_DAILY_SEQ8_MAP_AFTER_81,
                NEW_DAILY_SEQ8_BGM_AFTER_81,
                log_tag=f"{tag}·map325+BGM",
            ):
                return False
            if not self.run_mining_cycles(
                NEW_DAILY_SEQ8_SPOT_81_GOLD_KEY,
                NEW_DAILY_SEQ8_GOLD_TIMES,
                use_foreground=use_foreground,
            ):
                return False

        # ③ 地图 → 82斯科尔 → map328 → 83甲烷×2
        if start_step <= 3:
            if not self._new_daily_gap_before_step(start_step, 3):
                return False
            if not self._new_daily_click_map_then_delay(
                regions, use_foreground, log_tag=tag
            ):
                return False
            self._emit(f"🖱️ [{tag}] 点击 {NEW_DAILY_SEQ8_SPOT_82_KEY}", "SYSTEM")
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ8_SPOT_82_KEY, use_foreground
            ):
                return False
            if not self._wait_map_npc_then_delay(
                NEW_DAILY_SEQ8_MAP_AFTER_82, log_tag=f"{tag}·map328"
            ):
                return False
            if not self.run_mining_cycles(
                NEW_DAILY_SEQ8_SPOT_83_METHANE_KEY,
                NEW_DAILY_SEQ8_METHANE_TIMES,
                use_foreground=use_foreground,
            ):
                return False

        # ④ 地图 → 83普雷 → map333 → 84切换 → map339 → 84一/二/三采集
        if start_step <= 4:
            if not self._new_daily_gap_before_step(start_step, 4):
                return False
            if not self._new_daily_click_map_then_delay(
                regions, use_foreground, log_tag=tag
            ):
                return False
            self._emit(
                f"🖱️ [{tag}] 点击 {NEW_DAILY_SEQ8_SPOT_83_PULEI_KEY}",
                "SYSTEM",
            )
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ8_SPOT_83_PULEI_KEY, use_foreground
            ):
                return False
            if not self._wait_map_npc_then_delay(
                NEW_DAILY_SEQ8_MAP_AFTER_83_PULEI, log_tag=f"{tag}·map333"
            ):
                return False
            self._emit(
                f"🖱️ [{tag}] 点击 {NEW_DAILY_SEQ8_SWITCH_84_KEY}",
                "SYSTEM",
            )
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ8_SWITCH_84_KEY, use_foreground
            ):
                return False
            if not self._wait_map_npc_then_delay(
                NEW_DAILY_SEQ8_MAP_AFTER_84_SWITCH, log_tag=f"{tag}·map339"
            ):
                return False
            if not self._new_daily_seq8_collect_84_items_loop(
                regions, use_foreground, log_tag=tag
            ):
                return False

        self._emit(f"✅ [{tag}] 全部步骤完成", "SUCCESS")
        return True

    def _new_daily_run_chaos(
        self,
        use_foreground: bool,
        *,
        from_daily_chain: bool = False,
        log_tag: str = "新日常",
    ) -> bool:
        """新日常尾段：大乱斗×2。"""
        if self._should_abort():
            return False
        self._emit(f"⏱ [{log_tag}] 3s 后开始【大乱斗x2】…", "SYSTEM")
        time.sleep(3.0)
        if self._should_abort():
            return False
        return self.run_chaos_battle_x2(
            use_foreground=use_foreground,
            from_daily_chain=from_daily_chain,
        )

    def run_chaos_then_rotation(
        self,
        use_foreground: bool,
        *,
        rotation_runner: Callable[[], None],
    ) -> bool:
        """独立按钮链：大乱斗×2 → 轮换/自定义（各场 30 分钟保护）。"""
        if self._should_abort():
            return False
        self._emit("⏱ 3s 后开始【大乱斗x2】…", "SYSTEM")
        if not self._sleep_respecting_deadline(3.0, None):
            return False
        if not self.run_chaos_battle_x2(
            use_foreground=use_foreground,
            from_daily_chain=True,
        ):
            return False
        if self._should_abort():
            return False
        self._emit("🔄 大乱斗完成，启动轮换/自定义…", "SYSTEM")
        rotation_runner()
        return True

    def run_new_daily_sequence_9(
        self,
        use_foreground: bool = False,
        start_step: int = 1,
        *,
        skip_hero_tower: bool = False,
        from_daily_chain: bool = False,
    ) -> bool:
        """
        新日常方案「9」：
        ① 地图→90太空站→map102+NPC
           （跳过勇者之塔则直接大乱斗）
        ② 91勇者→勇者之塔×2→离开
        ③ map108→5s→92切换→map102+NPC→大乱斗
        """
        start_step = max(1, int(start_step or 1))
        if start_step > NEW_DAILY_SEQ9_MAX_STEP:
            self._emit(
                f"❌ 新日常方案9 仅有 {NEW_DAILY_SEQ9_MAX_STEP} 步，无法从第 {start_step} 步开始",
                "ERROR",
            )
            return False
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions", "ERROR")
            return False

        required = (
            NEW_DAILY_SEQ8_MAP_BTN_KEY,
            NEW_DAILY_SEQ9_SPOT_90_KEY,
            NEW_DAILY_SEQ9_SWITCH_92_KEY,
            NEW_DAILY_SEQ9_HERO_TOWER_LEAVE_KEY,
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ 新日常方案9 缺少区域：{key}", "ERROR")
                return False

        script_path = os.path.join(self.script_dir, f"{NEW_DAILY_SEQ9_SCRIPT_91_NAME}.json")
        if not os.path.isfile(script_path):
            self._emit(f"❌ 找不到脚本：{script_path}", "ERROR")
            return False

        tag = "新日常·9"
        if start_step > 1:
            self._emit(
                f"📋 [{tag}] 从第 {start_step} 步开始（前台={use_foreground}；"
                f"跳过勇者之塔={skip_hero_tower}）",
                "SYSTEM",
            )
        else:
            self._emit(
                f"📋 [{tag}] 开始执行（前台={use_foreground}；跳过勇者之塔={skip_hero_tower}）",
                "SYSTEM",
            )

        # ① 地图 → 90太空站 → map102+NPC
        if start_step <= 1:
            if not self._new_daily_click_map_then_delay(
                regions, use_foreground, log_tag=tag
            ):
                return False
            self._emit(f"🖱️ [{tag}] 点击 {NEW_DAILY_SEQ9_SPOT_90_KEY}", "SYSTEM")
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ9_SPOT_90_KEY, use_foreground
            ):
                return False
            if not self._wait_map_npc_then_delay(
                NEW_DAILY_SEQ9_MAP_AFTER_90, log_tag=f"{tag}·map102"
            ):
                return False
            if skip_hero_tower:
                self._emit(
                    f"⏭️ [{tag}] 已勾选跳过勇者之塔，直接执行大乱斗",
                    "SYSTEM",
                )
                return self._new_daily_run_chaos(
                    use_foreground,
                    from_daily_chain=from_daily_chain,
                    log_tag=tag,
                )

        # ② 91勇者 → 勇者之塔×2 → 离开
        if start_step <= 2 and not skip_hero_tower:
            if not self._new_daily_gap_before_step(start_step, 2):
                return False
            self._emit(
                f"② [{tag}] 执行脚本 {NEW_DAILY_SEQ9_SCRIPT_91_NAME}",
                "SYSTEM",
            )
            if not self.run_single_script(
                NEW_DAILY_SEQ9_SCRIPT_91_NAME, bg_mode=(not use_foreground)
            ):
                self._emit(
                    f"⚠️ [{tag}] 脚本 {NEW_DAILY_SEQ9_SCRIPT_91_NAME} 未完全成功",
                    "WARN",
                )
                return False
            self._emit(
                f"🗼 [{tag}] 勇者之塔×{DEFAULT_HERO_TOWER_BATTLES}",
                "SYSTEM",
            )
            if not self.run_hero_tower(
                times=DEFAULT_HERO_TOWER_BATTLES,
                background_mode=(not use_foreground),
                use_unified_framework=False,
            ):
                return False
            self._emit(
                f"🖱️ [{tag}] 点击 {NEW_DAILY_SEQ9_HERO_TOWER_LEAVE_KEY}",
                "INFO",
            )
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ9_HERO_TOWER_LEAVE_KEY, use_foreground
            ):
                return False

        # ③ map108 → 5s → 92切换 → map102+NPC → 大乱斗
        if start_step <= 3:
            if not self._new_daily_gap_before_step(start_step, 3):
                return False
            self._emit(
                f"⏳ [{tag}] 等待 map{NEW_DAILY_SEQ9_MAP_AFTER_LEAVE}…",
                "INFO",
            )
            if not self._wait_for_map_kernel(
                NEW_DAILY_SEQ9_MAP_AFTER_LEAVE,
                timeout_s=NEW_DAILY_MAP_WAIT_TIMEOUT_SEC,
            ):
                self._emit(
                    f"❌ [{tag}] 等待 map{NEW_DAILY_SEQ9_MAP_AFTER_LEAVE} 超时",
                    "ERROR",
                )
                return False
            self._emit(
                f"⏳ [{tag}] 检测到 map{NEW_DAILY_SEQ9_MAP_AFTER_LEAVE}，"
                f"等待 {NEW_DAILY_SEQ9_MAP108_WAIT_SEC}s…",
                "INFO",
            )
            time.sleep(NEW_DAILY_SEQ9_MAP108_WAIT_SEC)
            self._emit(
                f"🖱️ [{tag}] 点击 {NEW_DAILY_SEQ9_SWITCH_92_KEY}",
                "SYSTEM",
            )
            if not self._click_region_safe(
                regions, NEW_DAILY_SEQ9_SWITCH_92_KEY, use_foreground
            ):
                return False
            if not self._wait_map_npc_then_delay(
                NEW_DAILY_SEQ9_MAP_AFTER_92, log_tag=f"{tag}·map102"
            ):
                return False
            if not self._new_daily_run_chaos(
                use_foreground,
                from_daily_chain=from_daily_chain,
                log_tag=tag,
            ):
                return False

        self._emit(f"✅ [{tag}] 全部步骤完成", "SUCCESS")
        return True

    def _is_region_pure_white(
        self,
        regions,
        key: str,
        *,
        min_channel: int = 245,
    ) -> bool:
        rgb = mean_rgb_for_region_key(regions, key)
        return bool(rgb and all(int(v) >= min_channel for v in rgb))

    def _wait_region_pure_white(
        self,
        regions,
        key: str,
        *,
        log_tag: str,
        timeout_s: float = 30.0,
        poll_s: float = 0.08,
    ) -> bool:
        self._emit(f"⏳ [{log_tag}] 等待 {key} 变成纯白", "INFO")
        t0 = time.time()
        last_log = 0.0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            rgb = mean_rgb_for_region_key(regions, key)
            if rgb and all(int(v) >= 245 for v in rgb):
                self._emit(f"✅ [{log_tag}] {key} 已纯白：RGB={rgb}", "SUCCESS")
                return True
            now = time.time()
            if now - last_log >= 0.5:
                self._emit(f"🔍 [{log_tag}] 等待纯白：{key} RGB={rgb}", "DEBUG")
                last_log = now
            time.sleep(poll_s)
        self._emit(
            f"❌ [{log_tag}] 等待 {key} 纯白超时，最后RGB={mean_rgb_for_region_key(regions, key)}",
            "ERROR",
        )
        return False

    def _lanlan_click_npc_until_white_probe(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
        timeout_s: float = 45.0,
    ) -> bool:
        self._emit(
            f"⏳ [{log_tag}] 持续点击 {LANLAN_NPC_KEY} 直到 {LANLAN_WHITE_PROBE_KEY} 纯白",
            "INFO",
        )
        t0 = time.time()
        click_idx = 0
        last_log = 0.0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            if self._is_region_pure_white(regions, LANLAN_WHITE_PROBE_KEY):
                rgb = mean_rgb_for_region_key(regions, LANLAN_WHITE_PROBE_KEY)
                self._emit(
                    f"✅ [{log_tag}] 岚岚白色探针已出现：RGB={rgb}，点击次数={click_idx}",
                    "SUCCESS",
                )
                return True
            click_idx += 1
            if not self._click_region_safe(regions, LANLAN_NPC_KEY, use_foreground):
                return False
            now = time.time()
            if now - last_log >= 0.8:
                rgb = mean_rgb_for_region_key(regions, LANLAN_WHITE_PROBE_KEY)
                self._emit(
                    f"🔍 [{log_tag}] 等待岚岚白色探针：RGB={rgb}，点击次数={click_idx}",
                    "DEBUG",
                )
                last_log = now
            time.sleep(0.25)
        self._emit(
            f"❌ [{log_tag}] 持续点击后岚岚白色探针仍未出现，最后RGB={mean_rgb_for_region_key(regions, LANLAN_WHITE_PROBE_KEY)}",
            "ERROR",
        )
        return False

    def _lanlan_scan_switch_slots(
        self,
        dar_runner,
        *,
        mode: str = "rare",
    ) -> Tuple[
        bool,
        Tuple[Optional[str], ...],
        Dict[int, Optional[str]],
        Dict[int, Optional[Tuple[int, int, int]]],
        Dict[str, int],
        float,
    ]:
        colors: Dict[int, Optional[str]] = {}
        rgbs: Dict[int, Optional[Tuple[int, int, int]]] = {}
        for pet_num in range(1, 7):
            key = dar_runner._battle_switch_slot_key(pet_num)
            rgb = dar_runner._mean_rgb_for_region_key(key)
            rgbs[pet_num] = rgb
            color = dar_runner._classify_battle_switch_slot_rgb(rgb)
            if color is None and rgb is not None:
                r, g, b = rgb
                # 岚岚死亡换宠时会持续点击“切换精灵一”以使六槽面板
                # 对齐；对齐后的青色槽可能处于浅青高亮态。这里只在
                # 死亡换宠扫描中接受该变体，避免主动换宠把当前出战的
                # 高亮青色误判为可切换目标。
                if (
                    190 < r <= 230
                    and g >= 235
                    and b >= 245
                    and (g - r) >= 15
                    and (b - r) >= 20
                    and abs(b - g) <= 20
                ):
                    color = "blue"
            colors[pet_num] = color

        signature = tuple(colors.get(i) for i in range(1, 7))
        counts = {
            "orange": sum(1 for c in signature if c == "orange"),
            "blue": sum(1 for c in signature if c == "blue"),
            "purple": sum(1 for c in signature if c == "purple"),
        }

        reps: Dict[str, Optional[Tuple[int, int, int]]] = {}
        for pet_num in range(1, 7):
            color = colors.get(pet_num)
            if color in ("orange", "blue", "purple") and color not in reps:
                reps[color] = rgbs.get(pet_num)
        if {"orange", "blue", "purple"}.issubset(reps):
            distances = [
                dar_runner._battle_switch_rgb_distance(reps.get("orange"), reps.get("blue")),
                dar_runner._battle_switch_rgb_distance(reps.get("orange"), reps.get("purple")),
                dar_runner._battle_switch_rgb_distance(reps.get("blue"), reps.get("purple")),
            ]
            min_distance = min(distances)
        else:
            min_distance = 0.0

        min_required = float(getattr(dar_runner, "BATTLE_SWITCH_SLOT_COLOR_MIN_DISTANCE", 35.0))
        ready = counts == {"orange": 4, "blue": 1, "purple": 1} and min_distance >= min_required
        return ready, signature, colors, rgbs, counts, min_distance

    def _lanlan_click_switch_color(
        self,
        regions,
        dar_runner,
        colors: Dict[int, Optional[str]],
        target_color: str,
        use_foreground: bool,
        *,
        log_tag: str,
        remember_primary: bool = True,
        pet_type: Optional[str] = None,
    ) -> bool:
        target_slots = [slot for slot, color in colors.items() if color == target_color]
        if not target_slots:
            self._emit(f"❌ [{log_tag}] 六槽稳定但没有 {target_color} 槽，无法出战", "ERROR")
            return False

        pet_num = target_slots[0]
        if pet_type is None:
            pet_type = "aifeidesi" if target_color == "blue" else "jita"
        self._emit(
            f"🔄 [{log_tag}] 我方精灵被击败，选择{target_color}槽精灵{pet_num}出战",
            "WARN",
        )
        if not dar_runner._click_battle_switch_slot(
            pet_num,
            use_foreground,
            remember_primary=remember_primary,
            pet_type=pet_type,
        ):
            return False
        time.sleep(0.25)

        try:
            dar_runner._click_region("战斗.出战", use_foreground)
            return True
        except Exception:
            return self._click_region_safe(regions, "对战.切换精灵.出战", use_foreground)

    @staticmethod
    def _lanlan_skill_plan_for_now(now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or datetime.now()
        plan = LANLAN_SKILL_PLAN_BY_WEEKDAY.get(now.weekday())
        if plan and plan["start"] <= now.time() <= plan["end"]:
            return dict(plan)
        return {
            "key": LANLAN_SKILL_PLAN_DEFAULT,
            "label": "默认",
            "first_skill2_count": 3,
            "second_sequence": "",
            "second_repeat": "",
        }

    def _lanlan_wait_round_blue_or_switch(
        self,
        regions,
        dar_runner,
        config: BattleConfig,
        probe_model,
        *,
        target_switch_color: Optional[str],
        log_tag: str,
        round_idx: int,
        remember_primary: bool = True,
        switch_pet_type: Optional[str] = None,
        escape_after_target_switch: bool = False,
        on_poll: Optional[Callable[[], None]] = None,
    ) -> str:
        timeout_s = float(config.round_timeout_sec or 60.0)
        poll_s = float(getattr(dar_runner, "BATTLE_SWITCH_SLOT_COLOR_POLL_SEC", 0.08))
        required_stable = int(getattr(dar_runner, "BATTLE_SWITCH_SLOT_COLOR_STABLE_SCANS", 2))
        min_required = float(getattr(dar_runner, "BATTLE_SWITCH_SLOT_COLOR_MIN_DISTANCE", 35.0))
        t0 = time.time()
        last_signature: Optional[Tuple[Optional[str], ...]] = None
        stable_count = 0
        saw_gray = False
        last_log = 0.0
        switch_one_click_stop = threading.Event()
        switch_one_click_thread: Optional[threading.Thread] = None
        switch_one_click_count = 0

        def start_switch_one_click_loop() -> None:
            nonlocal switch_one_click_thread, switch_one_click_count
            if switch_one_click_thread is not None and switch_one_click_thread.is_alive():
                return
            switch_one_key = dar_runner._battle_switch_slot_key(1)
            self._emit(
                f"🖱️ [{log_tag}] 第{round_idx}回合灰色期：持续点击{switch_one_key}直到找到目标精灵位置",
                "INFO",
            )

            def _click_loop() -> None:
                nonlocal switch_one_click_count
                last_click_log = 0.0
                while not switch_one_click_stop.is_set() and not self._should_abort():
                    switch_one_click_count += 1
                    if not self._click_region_safe(regions, switch_one_key, config.use_foreground):
                        switch_one_click_stop.set()
                        return
                    now_click = time.time()
                    if now_click - last_click_log >= 1.5:
                        self._emit(
                            f"🔁 [{log_tag}] 持续点击切换1中：第{switch_one_click_count}次",
                            "DEBUG",
                        )
                        last_click_log = now_click
                    time.sleep(0.25)

            switch_one_click_thread = threading.Thread(target=_click_loop, daemon=True)
            switch_one_click_thread.start()

        def stop_switch_one_click_loop(reason: str) -> None:
            nonlocal switch_one_click_thread
            if switch_one_click_thread is None:
                return
            switch_one_click_stop.set()
            switch_one_click_thread.join(timeout=1.0)
            if switch_one_click_count > 0:
                self._emit(
                    f"✅ [{log_tag}] 已停止持续点击切换1：{reason}，累计{switch_one_click_count}次",
                    "DEBUG",
                )
            switch_one_click_thread = None

        try:
            while time.time() - t0 <= timeout_s:
                if self._should_abort():
                    return "abort"
                if on_poll is not None:
                    try:
                        on_poll()
                    except Exception:
                        pass
                map_seen, _npc_seen = self._unified_framework._check_battle_end()
                if map_seen:
                    self._emit(f"🏁 [{log_tag}] 第{round_idx}回合后检测到 map，战斗结束", "SUCCESS")
                    return "battle_end"

                state, s_blue, s_gray = self._unified_framework._detect_round_probe(probe_model)
                if state == "GRAY":
                    saw_gray = True

                if saw_gray and target_switch_color:
                    start_switch_one_click_loop()
                    ready, signature, colors, rgbs, counts, min_distance = self._lanlan_scan_switch_slots(
                        dar_runner,
                        mode="rare",
                    )
                    if ready and signature == last_signature:
                        stable_count += 1
                    elif ready:
                        stable_count = 1
                    else:
                        stable_count = 0
                    last_signature = signature

                    now = time.time()
                    if now - last_log >= 0.8 or stable_count >= required_stable:
                        self._emit(
                            f"🔍 [{log_tag}] 第{round_idx}回合灰色期扫六槽："
                            f"橙{counts['orange']}/蓝{counts['blue']}/紫{counts['purple']} "
                            f"minΔ={min_distance:.1f}/{min_required:.0f} "
                            f"稳定={stable_count}/{required_stable} colors={colors} rgbs={rgbs}",
                            "DEBUG" if stable_count < required_stable else "INFO",
                        )
                        last_log = now

                    if stable_count >= required_stable:
                        stop_switch_one_click_loop(f"已找到{target_switch_color}目标槽")
                        if not self._lanlan_click_switch_color(
                            regions,
                            dar_runner,
                            colors,
                            target_switch_color,
                            config.use_foreground,
                            log_tag=log_tag,
                            remember_primary=remember_primary,
                            pet_type=switch_pet_type,
                        ):
                            return "switch_failed"
                        deploy_t0 = time.time()
                        while time.time() - deploy_t0 <= 12.0:
                            if self._should_abort():
                                return "abort"
                            map_seen, _npc_seen = self._unified_framework._check_battle_end()
                            if map_seen:
                                return "battle_end"
                            state_after, _, _ = self._unified_framework._detect_round_probe(probe_model)
                            if state_after == "BLUE":
                                if escape_after_target_switch:
                                    return f"switched_{target_switch_color}_escape"
                                return f"switched_{target_switch_color}"
                            time.sleep(0.05)
                        self._emit(f"❌ [{log_tag}] 出战后未等到回合探针变蓝", "ERROR")
                        return "timeout"

                if saw_gray and state == "BLUE":
                    return "blue"

                if (not saw_gray) and state == "BLUE" and (time.time() - t0) >= 1.2:
                    self._emit(
                        f"⚠️ [{log_tag}] 第{round_idx}回合未捕获灰色但探针持续蓝色，按下一回合处理",
                        "DEBUG",
                    )
                    return "blue"

                time.sleep(max(0.03, min(0.12, poll_s)))
        finally:
            stop_switch_one_click_loop("回合等待结束")

        self._emit(f"❌ [{log_tag}] 第{round_idx}回合等待灰变蓝/换宠超时", "ERROR")
        return "timeout"

    def _run_lanlan_battle_loop(
        self,
        regions,
        config: BattleConfig,
        dar_runner,
        battle_state: Dict[str, Any],
        *,
        log_tag: str,
    ) -> str:
        skill_plan = dict(battle_state.get("skill_plan") or self._lanlan_skill_plan_for_now())
        plan_key = str(skill_plan.get("key") or LANLAN_SKILL_PLAN_DEFAULT)
        if plan_key == "saturday_683":
            return self._run_lanlan_683_battle_loop(
                regions,
                config,
                dar_runner,
                battle_state,
                log_tag=log_tag,
            )

        self._emit("⚔️ [岚岚] Stage 3: 专用战斗循环（死亡换宠版）", "INFO")
        self._unified_framework._stage3_exit_reason = "normal"
        self._unified_framework._battle_capsule_counts = {}
        self._unified_framework._capsule_cycle_index = 0
        self._unified_framework._capsule_cycle_tiers_override = getattr(
            config, "capsule_cycle_tiers_override", None
        )
        self._unified_framework._battle_start_time = time.time()
        self._unified_framework._battle_duration = 0.0
        self._unified_framework._start_kernel_listen(clear_queue=False)
        self._unified_framework._merge_kernel_buffer_after_stage2_gap()

        probe_model = self._unified_framework._load_probe_templates()
        round_idx = 1
        skill_plan = dict(battle_state.get("skill_plan") or self._lanlan_skill_plan_for_now())
        plan_key = str(skill_plan.get("key") or LANLAN_SKILL_PLAN_DEFAULT)
        plan_label = str(skill_plan.get("label") or plan_key)
        first_skill2_target = int(skill_plan.get("first_skill2_count") or 3)
        second_sequence = str(skill_plan.get("second_sequence") or "")
        second_repeat = str(skill_plan.get("second_repeat") or "")
        second_action_map = {
            "1": ("skill", "一技能"),
            "2": ("skill2", "二技能"),
            "3": ("skill3", "三技能"),
            "4": ("skill4", "四技能"),
        }

        def finish(result: str) -> str:
            self._unified_framework._round_idx = round_idx
            self._unified_framework._battle_duration = (
                time.time() - self._unified_framework._battle_start_time
            )
            self._unified_framework._stop_kernel_listen()
            return result

        def phase_target_color() -> Optional[str]:
            phase = str(battle_state.get("phase") or "first")
            if phase == "first":
                return "blue"
            if phase == "second":
                return "purple"
            return None

        while not self._should_abort():
            wait_result = self._lanlan_wait_round_blue_or_switch(
                regions,
                dar_runner,
                config,
                probe_model,
                target_switch_color=phase_target_color(),
                log_tag=log_tag,
                round_idx=round_idx,
            )
            if wait_result == "battle_end":
                return finish("ended")
            if wait_result in ("abort", "timeout", "switch_failed"):
                return finish(wait_result)
            if wait_result == "switched_blue":
                battle_state["phase"] = "second"
                battle_state["second_actions"] = 0
                self._emit(
                    f"🔵 [{log_tag}] 蓝色精灵已出战，进入{plan_label}出招逻辑",
                    "INFO",
                )
            elif wait_result == "switched_purple":
                battle_state["phase"] = "purple_escape"
                self._emit(f"🟣 [{log_tag}] 第二只精灵被击败，紫色精灵出战后逃跑", "WARN")
                self._unified_framework._execute_action("escape", config, round_idx=round_idx)
                return finish("retry_after_escape")

            round_idx += 1
            phase = str(battle_state.get("phase") or "first")
            if phase == "first":
                first_actions = int(battle_state.get("first_actions", 0))
                if first_actions < first_skill2_target:
                    battle_state["first_actions"] = first_actions + 1
                    self._emit(
                        f"🎯 [{log_tag}] 第{round_idx}回合：首发精灵二技能 "
                        f"{battle_state['first_actions']}/{first_skill2_target}",
                        "INFO",
                    )
                    self._unified_framework._execute_action("skill2", config, round_idx=round_idx)
                    continue

                self._emit(
                    f"🔄 [{log_tag}] 第{round_idx}回合：首发{first_skill2_target}次二技能完成，主动切蓝色精灵",
                    "INFO",
                )
                stop_event = getattr(self.bot, "_stop_event", threading.Event())
                if not dar_runner._switch_pet_for_rare_mode("aifeidesi", config.use_foreground, stop_event):
                    return finish("switch_failed")
                battle_state["phase"] = "second"
                battle_state["second_actions"] = 0
                continue

            if phase == "second":
                second_actions = int(battle_state.get("second_actions", 0)) + 1
                battle_state["second_actions"] = second_actions

                if second_sequence:
                    if second_actions > len(second_sequence):
                        if second_repeat:
                            action, skill_label = second_action_map.get(second_repeat, ("skill4", "四技能"))
                            desc = f"{plan_label}精灵二{skill_label}循环"
                            self._emit(f"🎯 [{log_tag}] 第{round_idx}回合：{desc}", "INFO")
                            self._unified_framework._execute_action(action, config, round_idx=round_idx)
                            continue
                        self._emit(
                            f"🏃 [{log_tag}] 第{round_idx}回合：{plan_label}精灵二技能串已耗尽，执行逃跑后重试",
                            "WARN",
                        )
                        self._unified_framework._execute_action("escape", config, round_idx=round_idx)
                        return finish("retry_after_escape")
                    skill_num = second_sequence[second_actions - 1]
                    action, skill_label = second_action_map.get(skill_num, ("skill", "一技能"))
                    desc = f"{plan_label}精灵二{skill_label} {second_actions}/{len(second_sequence)}"
                else:
                    if second_repeat:
                        action, skill_label = second_action_map.get(second_repeat, ("skill4", "四技能"))
                        desc = f"{plan_label}精灵二{skill_label}循环"
                        self._emit(f"🎯 [{log_tag}] 第{round_idx}回合：{desc}", "INFO")
                        self._unified_framework._execute_action(action, config, round_idx=round_idx)
                        continue
                    if second_actions <= 2:
                        action = "skill3"
                        desc = f"第二只精灵三技能 {second_actions}/2"
                    elif second_actions <= 5:
                        action = "skill2"
                        desc = f"第二只精灵二技能 {second_actions - 2}/3"
                    else:
                        action = "skill4"
                        desc = "第二只精灵四技能循环"
                self._emit(f"🎯 [{log_tag}] 第{round_idx}回合：{desc}", "INFO")
                self._unified_framework._execute_action(action, config, round_idx=round_idx)
                continue

            return finish("failed")

        return finish("abort")

    def _run_lanlan_683_battle_loop(
        self,
        regions,
        config: BattleConfig,
        dar_runner,
        battle_state: Dict[str, Any],
        *,
        log_tag: str,
    ) -> str:
        """Run the Saturday 683 plan without switching to the other Lanlan pets."""
        self._emit("⚔️ [岚岚683] Stage 3：三次二技能后按血量探针攻击", "INFO")
        self._unified_framework._stage3_exit_reason = "normal"
        self._unified_framework._battle_capsule_counts = {}
        self._unified_framework._capsule_cycle_index = 0
        self._unified_framework._capsule_cycle_tiers_override = getattr(
            config, "capsule_cycle_tiers_override", None
        )
        self._unified_framework._battle_start_time = time.time()
        self._unified_framework._battle_duration = 0.0
        self._unified_framework._start_kernel_listen(clear_queue=False)
        self._unified_framework._merge_kernel_buffer_after_stage2_gap()

        probe_model = self._unified_framework._load_probe_templates()
        round_idx = 1
        first_skill2_target = 3
        first_skill1_used = False
        attack_started = False
        skill4_count = 0
        skill3_count = 0
        escape_after_switch = False

        def finish(result: str) -> str:
            self._unified_framework._round_idx = round_idx
            self._unified_framework._battle_duration = (
                time.time() - self._unified_framework._battle_start_time
            )
            self._unified_framework._stop_kernel_listen()
            return result

        def read_hp_kind(region_key: str) -> Tuple[Optional[Tuple[int, int, int]], Optional[str]]:
            try:
                rgb = dar_runner._mean_rgb_for_region_key(region_key)
                kind = dar_runner._eit_classify_hp_color(rgb)
                return rgb, kind
            except Exception as exc:
                self._emit(f"⚠️ [{log_tag}] 读取血量探针失败 {region_key}: {exc}", "WARN")
                return None, None

        def escape_retry(reason: str) -> str:
            self._emit(f"🏃 [{log_tag}] {reason}，执行逃跑后重试", "WARN")
            self._unified_framework._execute_action("escape", config, round_idx=round_idx)
            return finish("retry_after_escape")

        while not self._should_abort():
            wait_result = self._lanlan_wait_round_blue_or_switch(
                regions,
                dar_runner,
                config,
                probe_model,
                target_switch_color="blue",
                log_tag=log_tag,
                round_idx=round_idx,
                escape_after_target_switch=True,
            )
            if wait_result == "battle_end":
                return finish("ended")
            if wait_result == "switched_blue_escape":
                escape_after_switch = True
                self._emit(
                    f"🔄 [{log_tag}] 683 被击败后已完成青色换宠，下一回合执行逃跑",
                    "WARN",
                )
            if wait_result in ("abort", "timeout", "switch_failed"):
                return finish(wait_result)

            round_idx += 1
            if escape_after_switch:
                return escape_retry("683 换宠完成后的下一回合")
            first_actions = int(battle_state.get("first_actions", 0))
            if first_actions < first_skill2_target:
                battle_state["first_actions"] = first_actions + 1
                self._emit(
                    f"🎯 [{log_tag}] 第{round_idx}回合：683二技能 "
                    f"{battle_state['first_actions']}/{first_skill2_target}",
                    "INFO",
                )
                self._unified_framework._execute_action("skill2", config, round_idx=round_idx)
                continue

            if not attack_started:
                own_rgb, own_kind = read_hp_kind("对战信息.我方中位血量探针")
                self._emit(
                    f"🩸 [{log_tag}] 第{round_idx}回合：检查我方血量RGB={own_rgb}，类型={own_kind or '非棕色/未知'}",
                    "INFO",
                )
                if own_kind == "brown" and not first_skill1_used:
                    first_skill1_used = True
                    attack_started = True
                    self._emit(
                        f"🎯 [{log_tag}] 我方血量为棕色，683只使用一次一技能后进入攻击阶段",
                        "INFO",
                    )
                    self._unified_framework._execute_action("skill", config, round_idx=round_idx)
                    continue

                attack_started = True
                self._emit(
                    f"⚔️ [{log_tag}] 683二技能阶段完成（共{first_actions}回合），进入攻击阶段",
                    "INFO",
                )

            if skill4_count < 3:
                skill4_count += 1
                self._emit(
                    f"🎯 [{log_tag}] 第{round_idx}回合：683使用四技能 "
                    f"{skill4_count}/3",
                    "INFO",
                )
                self._unified_framework._execute_action("skill4", config, round_idx=round_idx)
                continue

            skill3_count += 1
            self._emit(
                f"🎯 [{log_tag}] 第{round_idx}回合：683四技能已满3次，使用三技能 "
                f"{skill3_count}/5",
                "INFO",
            )
            self._unified_framework._execute_action("skill3", config, round_idx=round_idx)
            if skill3_count >= 5:
                return escape_retry("683 三技能已使用五回合")

        return finish("abort")

    def _recover_daily_pick_color_slots(
        self,
        use_foreground: bool,
        *,
        log_tag: str,
        recover_cyan: bool = False,
        recover_purple: bool = False,
        set_follow_purple: bool = False,
        before_close_callback: Optional[Callable[[], bool]] = None,
    ) -> bool:
        dar_runner = getattr(self.bot, "dar_route_runner", None)
        fn = getattr(dar_runner, "recover_pick_party_color_slots_from_closed_bag", None)
        if not callable(fn):
            self._emit(f"❌ [{log_tag}] 缺少按颜色恢复背包入口", "ERROR")
            return False
        stop_event = getattr(self.bot, "_stop_event", None)
        if stop_event is None:
            stop_event = threading.Event()
        return bool(
            fn(
                use_foreground,
                stop_event,
                log_tag,
                recover_cyan=recover_cyan,
                recover_purple=recover_purple,
                set_follow_purple=set_follow_purple,
                before_close_callback=before_close_callback,
            )
        )

    def _run_lanlan_skill2_until_yellow(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
    ) -> bool:
        if not self._ensure_unified_framework(regions):
            return False
        try:
            from config import TEMPLATES_PATH
        except Exception:
            TEMPLATES_PATH = os.path.join(BASE_PATH, "assets", "templates")
        cleaner = PostBattleCleaner(self.bot, regions, TEMPLATES_PATH)
        dar_runner = getattr(self.bot, "dar_route_runner", None)
        if dar_runner is None or not hasattr(dar_runner, "_switch_pet_for_rare_mode"):
            self._emit(f"❌ [{log_tag}] 缺少稀有切换精灵逻辑，无法执行岚岚死亡换宠", "ERROR")
            return False

        def abort_check() -> bool:
            return self._should_abort()

        from core.logger import kernel_cursor

        attempt = 0
        while not self._should_abort():
            attempt += 1
            tag = f"{log_tag}·第{attempt}场"
            skill_plan = self._lanlan_skill_plan_for_now()
            first_skill2_target = int(skill_plan.get("first_skill2_count") or 3)
            battle_state: Dict[str, Any] = {
                "phase": "first",
                "first_actions": 0,
                "second_actions": 0,
                "skill_plan": skill_plan,
            }

            def action_callback(round_idx: int) -> str:
                battle_state["first_actions"] = 1
                self._emit(
                    f"🎯 [{tag}] 第{round_idx}回合：首发精灵二技能 1/{first_skill2_target}",
                    "INFO",
                )
                return "skill2"

            config = BattleConfig(
                mode=BattleMode.FIXED,
                use_foreground=use_foreground,
                skill_key="对战.使用技能一",
                action_callback=action_callback,
                abort_check=abort_check,
                round_timeout_sec=60.0,
                skip_map10_white_end=True,
            )

            if not self._wait_region_pure_white(
                regions,
                LANLAN_WHITE_PROBE_KEY,
                log_tag=tag,
                timeout_s=45.0,
            ):
                return False

            start_cursor = kernel_cursor()
            click_stop = threading.Event()
            click_failed = threading.Event()
            click_saw_calibration = threading.Event()

            def _entry_click_loop() -> None:
                click_idx = 0
                while not click_stop.is_set() and not self._should_abort():
                    try:
                        if self._unified_framework._check_calibration_probes():
                            click_saw_calibration.set()
                            self._emit(
                                f"🧭 [{tag}] 点击岚岚开始后出现校准，停止外层点击并交给 Stage2",
                                "INFO",
                            )
                            click_stop.set()
                            return
                    except Exception:
                        pass
                    click_idx += 1
                    if click_idx == 1 or click_idx % 10 == 0:
                        self._emit(
                            f"🖱️ [{tag}] 持续点击 {LANLAN_START_KEY} 等待 PetItem/校准（第{click_idx}次）",
                            "INFO" if click_idx == 1 else "DEBUG",
                        )
                    if not self._click_region_safe(
                        regions, LANLAN_START_KEY, use_foreground
                    ):
                        click_failed.set()
                        click_stop.set()
                        return
                    t0 = time.time()
                    while time.time() - t0 < 0.25:
                        if click_stop.is_set() or self._should_abort():
                            return
                        try:
                            if self._unified_framework._check_calibration_probes():
                                click_saw_calibration.set()
                                self._emit(
                                    f"🧭 [{tag}] 岚岚入战触发校准，停止外层点击并等待 Stage2 完成校准",
                                    "INFO",
                                )
                                click_stop.set()
                                return
                        except Exception:
                            pass
                        time.sleep(0.05)

            click_thread = threading.Thread(target=_entry_click_loop, daemon=True)
            click_thread.start()
            try:
                success, _ = self._unified_framework.stage2_calibration_and_petitem(
                    trigger_callback=None,
                    use_foreground=use_foreground,
                    timeout_s=45.0,
                    skip_stage1=True,
                    config=config,
                    initial_cursor=start_cursor,
                )
            finally:
                click_stop.set()
                click_thread.join(timeout=1.0)

            if click_failed.is_set():
                self._emit(f"❌ [{tag}] 点击岚岚开始失败，退出", "ERROR")
                return False
            if not success:
                if click_saw_calibration.is_set():
                    self._emit(f"❌ [{tag}] 校准出现后仍未检测到 PetItem 入战，退出", "ERROR")
                else:
                    self._emit(f"❌ [{tag}] 未检测到 PetItem 入战，退出", "ERROR")
                return False

            repeat_text = str(skill_plan.get("second_repeat") or "")
            exhaust_text = (
                f"技能串后循环{repeat_text}技能"
                if repeat_text
                else "技能串耗尽则下一回合逃跑"
            )
            if str(skill_plan.get("key") or "") == "saturday_683":
                self._emit(
                    f"⚔️ [{tag}] 已入战：683首发二技能{first_skill2_target}次；"
                    "我方棕色时最多补一次一技能，四技能最多三次，三技能五回合后逃跑",
                    "SYSTEM",
                )
            else:
                self._emit(
                    f"⚔️ [{tag}] 已入战：首发二技能{first_skill2_target}次，主动/被击败切蓝后进入第二只；"
                    f"计划={skill_plan.get('label')} seq={skill_plan.get('second_sequence') or 'legacy'}"
                    f" repeat={skill_plan.get('second_repeat') or '-'}；"
                    f"第二只被击败则切紫逃跑，{exhaust_text}",
                    "SYSTEM",
                )
            battle_result = self._run_lanlan_battle_loop(
                regions,
                config,
                dar_runner,
                battle_state,
                log_tag=tag,
            )
            if battle_result == "retry_after_escape":
                self._emit(f"🧹 [{tag}] 紫色精灵已逃跑，清理1AND1后重新尝试入战", "WARN")
                if not self._unified_framework.stage4_post_battle(config, is_training_room=False):
                    self._emit(f"❌ [{tag}] 逃跑后清理1AND1失败，退出", "ERROR")
                    return False
                if not self._recover_daily_pick_color_slots(
                    use_foreground,
                    log_tag=f"{tag}·逃跑后恢复精灵一+青色",
                    recover_cyan=True,
                ):
                    return False
                if not self._lanlan_click_npc_until_white_probe(
                    regions,
                    use_foreground,
                    log_tag=f"{tag}·逃跑后恢复对战",
                ):
                    return False
                time.sleep(1.0)
                continue
            if battle_result != "ended":
                if self._should_abort():
                    return False
                self._emit(f"❌ [{tag}] 战斗循环未正常结束：{battle_result}", "ERROR")
                return False

            self._emit(f"🟡 [{tag}] 检测结算黄/白探针", "INFO")
            probe_result = self._detect_victory_probe_result(
                cleaner,
                use_foreground,
                timeout_s=8.0,
            )
            if probe_result == "yellow":
                # 黄色探针就是岚岚的业务完成点。后续胜利确认和1AND1
                # 仅是收尾，不能因收尾失败而漏记当天完成。
                if not self._lanlan_has_daily_record_today():
                    if self._append_lanlan_daily_record(note="黄色探针已检测"):
                        self._emit(f"📋 [{tag}] 黄色结束已立即写入当日记录", "SUCCESS")
                    else:
                        self._emit(f"⚠️ [{tag}] 黄色已检测但记录写入失败，收尾后将再次尝试", "WARN")
                self._emit(f"✅ [{tag}] 检测到黄色，点击胜利确认后清理1AND1", "SUCCESS")
                if not self._click_region_safe(regions, "对话框.对战胜利确认", use_foreground):
                    return False
                if not self._wait_1and1_clear(
                    regions,
                    use_foreground,
                    timeout_s=20.0,
                    min_confirm_clicks=1,
                    log_tag=f"{tag}·黄色结束",
                ):
                    return False
                self._emit(
                    f"✅ [{tag}] 黄色结束确认与1AND1清理完成；一次性模式结束，跳过精灵恢复",
                    "SUCCESS",
                )
                return True
            if probe_result == "white":
                self._emit(f"⚪ [{tag}] 检测到白色，确认后重复入战循环", "INFO")
                self._click_region_safe(regions, "对话框.对战胜利确认", use_foreground)
                time.sleep(1.0)
                if not self._recover_daily_pick_color_slots(
                    use_foreground,
                    log_tag=f"{tag}·白色后恢复精灵一+青色",
                    recover_cyan=True,
                ):
                    return False
                if not self._lanlan_click_npc_until_white_probe(
                    regions,
                    use_foreground,
                    log_tag=f"{tag}·白色后恢复对战",
                ):
                    return False
                continue

            self._emit(f"❌ [{tag}] 未检测到黄色或白色结算探针，退出", "ERROR")
            return False

        return False

    def _wait_light_mantis_white_after_click0(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
        timeout_s: float = 45.0,
    ) -> bool:
        self._emit(f"⏳ [{log_tag}] 点击 {LIGHT_MANTIS_CLICK0_KEY} 直到白色探针纯白", "INFO")
        t0 = time.time()
        click_idx = 0
        last_log = 0.0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            click_idx += 1
            if not self._click_region_safe(regions, LIGHT_MANTIS_CLICK0_KEY, use_foreground):
                return False
            rgb = mean_rgb_for_region_key(regions, LIGHT_MANTIS_WHITE_PROBE_KEY)
            if rgb and all(int(v) >= 245 for v in rgb):
                self._emit(f"✅ [{log_tag}] 光螳螂白色探针已纯白：RGB={rgb}", "SUCCESS")
                return True
            now = time.time()
            if now - last_log >= 0.8:
                self._emit(
                    f"🔍 [{log_tag}] 等待白色探针：RGB={rgb}，点击0次数={click_idx}",
                    "DEBUG",
                )
                last_log = now
            time.sleep(0.25)
        self._emit(
            f"❌ [{log_tag}] 等待光螳螂白色探针纯白超时，最后RGB={mean_rgb_for_region_key(regions, LIGHT_MANTIS_WHITE_PROBE_KEY)}",
            "ERROR",
        )
        return False

    def _run_light_mantis_entry_prefix(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
    ) -> Optional[int]:
        if not self._wait_light_mantis_white_after_click0(
            regions,
            use_foreground,
            log_tag=log_tag,
        ):
            return None
        for key in LIGHT_MANTIS_CLICK_KEYS:
            self._emit(f"🖱️ [{log_tag}] 点击 {key}", "INFO")
            if not self._click_region_safe(regions, key, use_foreground):
                return None
            time.sleep(0.8)
        if not self._wait_left_1and1_clear(
            regions,
            use_foreground,
            timeout_s=25.0,
            min_confirm_clicks=1,
            log_tag=f"{log_tag}·左边1AND1",
        ):
            return None
        if not self._wait_light_mantis_entry_orange_with_normal_confirm(
            regions,
            use_foreground,
            log_tag=f"{log_tag}·4点橙色门控",
        ):
            return None
        from core.logger import kernel_cursor

        entry_cursor = kernel_cursor()
        self._emit(f"🖱️ [{log_tag}] 点击 {LIGHT_MANTIS_ENTRY_KEY} 等待校准或PetItem", "INFO")
        if not self._click_region_safe(regions, LIGHT_MANTIS_ENTRY_KEY, use_foreground):
            return None
        return entry_cursor

    @staticmethod
    def _light_mantis_entry_is_orange(
        rgb: Optional[Tuple[int, int, int]],
    ) -> bool:
        if rgb is None:
            return False
        return max(
            abs(int(rgb[idx]) - LIGHT_MANTIS_ENTRY_ORANGE_RGB[idx])
            for idx in range(3)
        ) <= LIGHT_MANTIS_ENTRY_ORANGE_TOLERANCE

    def _wait_light_mantis_entry_orange_with_normal_confirm(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
        timeout_s: float = LIGHT_MANTIS_ENTRY_READY_TIMEOUT_SEC,
    ) -> bool:
        self._emit(
            f"⏳ [{log_tag}] 在 {LIGHT_MANTIS_RANDOM_CONFIRM_CENTER_KEY} 中心上下左右"
            f"±{LIGHT_MANTIS_RANDOM_CONFIRM_RADIUS_PX:.0f}px 方形内随机点击，"
            f"直到 {LIGHT_MANTIS_ENTRY_KEY} 变橙色 {LIGHT_MANTIS_ENTRY_ORANGE_RGB}",
            "INFO",
        )
        started_at = time.time()
        click_count = 0
        last_log_at = 0.0
        last_rgb: Optional[Tuple[int, int, int]] = None
        while time.time() - started_at < timeout_s:
            if self._should_abort():
                return False
            last_rgb = mean_rgb_for_region_key(regions, LIGHT_MANTIS_ENTRY_KEY)
            if self._light_mantis_entry_is_orange(last_rgb):
                self._emit(
                    f"✅ [{log_tag}] {LIGHT_MANTIS_ENTRY_KEY} 已变橙色："
                    f"RGB={last_rgb}，随机点击={click_count}次",
                    "SUCCESS",
                )
                return True
            click_xy = self._click_light_mantis_random_confirm_square(
                regions, use_foreground
            )
            if click_xy is None:
                return False
            click_count += 1
            now = time.time()
            if now - last_log_at >= 0.8:
                self._emit(
                    f"🔍 [{log_tag}] {LIGHT_MANTIS_ENTRY_KEY} 尚未变橙："
                    f"RGB={last_rgb}，本次随机点击=({click_xy[0]:.0f},{click_xy[1]:.0f})，"
                    f"累计={click_count}次",
                    "DEBUG",
                )
                last_log_at = now
            time.sleep(LIGHT_MANTIS_NORMAL_CONFIRM_CLICK_GAP_SEC)
        self._emit(
            f"❌ [{log_tag}] 等待 {LIGHT_MANTIS_ENTRY_KEY} 变橙色超时："
            f"最后RGB={last_rgb}，轮流点击={click_count}次",
            "ERROR",
        )
        return False

    def _click_light_mantis_random_confirm_square(
        self,
        regions,
        use_foreground: bool,
    ) -> Optional[Tuple[float, float]]:
        """在普通确认中心 ±100px 的方形内均匀随机点击。"""
        try:
            region = regions.get(LIGHT_MANTIS_RANDOM_CONFIRM_CENTER_KEY)
            if not region:
                self._emit(
                    f"❌ 找不到区域：{LIGHT_MANTIS_RANDOM_CONFIRM_CENTER_KEY}",
                    "ERROR",
                )
                return None
            x1, y1, x2, y2 = region.inner_bbox()
            center_x = (x1 + x2) / 2.0
            center_y = (y1 + y2) / 2.0
            click_x = random.uniform(
                center_x - LIGHT_MANTIS_RANDOM_CONFIRM_RADIUS_PX,
                center_x + LIGHT_MANTIS_RANDOM_CONFIRM_RADIUS_PX,
            )
            click_y = random.uniform(
                center_y - LIGHT_MANTIS_RANDOM_CONFIRM_RADIUS_PX,
                center_y + LIGHT_MANTIS_RANDOM_CONFIRM_RADIUS_PX,
            )
            if use_foreground:
                window_manager.click(click_x, click_y)
            else:
                window_manager.click_background(click_x, click_y)
            return click_x, click_y
        except Exception as exc:
            self._emit(f"❌ 光螳螂随机区域点击失败：{exc}", "ERROR")
            return None

    def _run_light_mantis_battle_loop(
        self,
        regions,
        config: BattleConfig,
        dar_runner,
        *,
        log_tag: str,
    ) -> str:
        self._emit(f"⚔️ [{log_tag}] Stage 3: 光螳螂专用战斗循环", "INFO")
        self._unified_framework._stage3_exit_reason = "normal"
        self._unified_framework._battle_capsule_counts = {}
        self._unified_framework._capsule_cycle_index = 0
        self._unified_framework._battle_start_time = time.time()
        self._unified_framework._battle_duration = 0.0
        self._unified_framework._start_kernel_listen(clear_queue=False)
        self._unified_framework._merge_kernel_buffer_after_stage2_gap()

        probe_model = self._unified_framework._load_probe_templates()
        round_idx = 1
        # 光螳螂前置已经把 197 青色设为首发。第一回合技能一由
        # stage2 的 action_callback 执行，这里直接沿用原“切青后”逻辑。
        second_turn = 1
        second_skill2_count = 0
        second_turn2_red = False
        second_turn3_not_red_skill1 = False

        def finish(result: str) -> str:
            self._unified_framework._round_idx = round_idx
            self._unified_framework._battle_duration = (
                time.time() - self._unified_framework._battle_start_time
            )
            self._unified_framework._stop_kernel_listen()
            return result

        def escape_retry(reason: str) -> str:
            self._emit(f"🏃 [{log_tag}] {reason}，执行逃跑后重试", "WARN")
            self._unified_framework._execute_action("escape", config, round_idx=round_idx)
            return finish("retry_after_escape")

        while not self._should_abort():
            wait_result = self._lanlan_wait_round_blue_or_switch(
                regions,
                dar_runner,
                config,
                probe_model,
                target_switch_color="purple",
                log_tag=log_tag,
                round_idx=round_idx,
                remember_primary=False,
                switch_pet_type=None,
            )
            if wait_result == "battle_end":
                return finish("ended")
            if wait_result in ("abort", "timeout", "switch_failed"):
                return finish(wait_result)
            if wait_result == "switched_purple":
                return escape_retry("197阵亡，紫色精灵已出战")

            round_idx += 1
            second_turn += 1
            fear_red = self._yilu_fear_probe_red()
            if not fear_red:
                if second_turn == 2:
                    return escape_retry("197第二回合害怕探针非红")
                if second_turn == 4 and second_turn2_red and second_turn3_not_red_skill1:
                    return escape_retry("197第2回合红、第3回合非红技能一后，第4回合仍非红")
                if second_turn == 3 and second_turn2_red:
                    second_turn3_not_red_skill1 = True
                self._emit(
                    f"⚪ [{log_tag}] 197第{second_turn}回合：害怕探针非红，使用一技能",
                    "INFO",
                )
                self._unified_framework._execute_action("skill", config, round_idx=round_idx)
                continue

            if second_turn == 2:
                second_turn2_red = True
            if second_skill2_count < 6:
                second_skill2_count += 1
                self._emit(
                    f"🔴 [{log_tag}] 197第{second_turn}回合：害怕红，二技能 {second_skill2_count}/6",
                    "INFO",
                )
                self._unified_framework._execute_action("skill2", config, round_idx=round_idx)
            else:
                self._emit(
                    f"🔴 [{log_tag}] 197第{second_turn}回合：害怕红且二技能已满6次，使用四技能",
                    "INFO",
                )
                self._unified_framework._execute_action("skill4", config, round_idx=round_idx)

        return finish("abort")

    def _light_mantis_first_action(self, round_idx: int, *, log_tag: str) -> str:
        self._emit(f"🎯 [{log_tag}] 第{round_idx}回合：197首发使用一技能", "INFO")
        return "skill"

    def _run_light_mantis_until_yellow(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
    ) -> bool:
        if not self._ensure_unified_framework(regions):
            return False
        try:
            from config import TEMPLATES_PATH
        except Exception:
            TEMPLATES_PATH = os.path.join(BASE_PATH, "assets", "templates")
        cleaner = PostBattleCleaner(self.bot, regions, TEMPLATES_PATH)
        dar_runner = getattr(self.bot, "dar_route_runner", None)
        if dar_runner is None:
            self._emit(f"❌ [{log_tag}] 缺少 DarRouteRunner，无法扫描切换槽", "ERROR")
            return False

        def abort_check() -> bool:
            return self._should_abort()

        attempt = 0
        while not self._should_abort():
            attempt += 1
            tag = f"{log_tag}·第{attempt}场"

            def action_callback(round_idx: int) -> str:
                return self._light_mantis_first_action(round_idx, log_tag=tag)

            config = BattleConfig(
                mode=BattleMode.FIXED,
                use_foreground=use_foreground,
                skill_key="对战.使用技能一",
                action_callback=action_callback,
                abort_check=abort_check,
                round_timeout_sec=60.0,
                skip_map10_white_end=True,
            )

            initial_cursor = self._run_light_mantis_entry_prefix(
                regions,
                use_foreground,
                log_tag=tag,
            )
            if initial_cursor is None:
                return False

            ok, _calib_result = self._unified_framework.stage2_calibration_and_petitem(
                trigger_callback=None,
                use_foreground=use_foreground,
                timeout_s=45.0,
                skip_stage1=True,
                config=config,
                initial_cursor=initial_cursor,
                check_calibration_after_fight_signal=True,
            )
            if not ok:
                self._emit(f"⚠️ [{tag}] 4点后未检测到校准/PetItem入战，重启0白123流程", "WARN")
                time.sleep(0.8)
                continue

            battle_result = self._run_light_mantis_battle_loop(
                regions,
                config,
                dar_runner,
                log_tag=tag,
            )
            if battle_result == "retry_after_escape":
                if not self._unified_framework.stage4_post_battle(config, is_training_room=False):
                    self._emit(f"❌ [{tag}] 逃跑后1AND1清理失败", "ERROR")
                    return False
                if not self._recover_daily_pick_color_slots(
                    use_foreground,
                    log_tag=f"{tag}·逃跑后恢复精灵一+青色",
                    recover_cyan=True,
                ):
                    return False
                time.sleep(1.0)
                continue
            if battle_result != "ended":
                self._emit(f"❌ [{tag}] 战斗循环未正常结束：{battle_result}", "ERROR")
                return False

            probe_result = self._detect_victory_probe_result(
                cleaner,
                use_foreground,
                timeout_s=8.0,
            )
            if probe_result == "yellow":
                self._emit(f"✅ [{tag}] 检测到黄色胜利探针，确认并清理1AND1", "SUCCESS")
                if not self._click_region_safe(regions, "对话框.对战胜利确认", use_foreground):
                    return False
                if not self._wait_1and1_clear(
                    regions,
                    use_foreground,
                    timeout_s=20.0,
                    min_confirm_clicks=1,
                    log_tag=f"{tag}·黄色结束",
                ):
                    return False
                self._emit(
                    f"✅ [{tag}] 黄色胜利收尾完成；一次性模式结束，跳过精灵恢复",
                    "SUCCESS",
                )
                return True
            if probe_result == "white":
                self._emit(f"⚪ [{tag}] 检测到白色结算，确认后重新尝试", "INFO")
                self._click_region_safe(regions, "对话框.对战胜利确认", use_foreground)
                time.sleep(1.0)
                if not self._recover_daily_pick_color_slots(
                    use_foreground,
                    log_tag=f"{tag}·白色后恢复精灵一+青色",
                    recover_cyan=True,
                ):
                    return False
                continue
            self._emit(f"❌ [{tag}] 未检测到黄色胜利探针，退出", "ERROR")
            return False
        return False

    def _wait_yilu_white_probe_disappear(
        self,
        regions,
        *,
        log_tag: str,
        timeout_s: float = 45.0,
        window_deadline: Optional[datetime] = None,
    ) -> bool:
        stop_check = lambda: self._should_abort() or self._yilu_window_expired(
            window_deadline
        )
        return wait_map10_white_probe_ready(
            regions,
            emit_fn=self._emit,
            stop_check=stop_check,
            white_probe_key=MAP10_WHITE_PROBE_KEY_NIEO,
            mode="nieo",
            log_tag=log_tag,
            timeout_s=timeout_s,
            two_phase=True,
        )

    def _yilu_window_expired(
        self,
        window_deadline: Optional[datetime],
    ) -> bool:
        return bool(
            window_deadline is not None
            and self._beijing_now() >= window_deadline
        )

    def _yilu_window_available(
        self,
        window_deadline: Optional[datetime],
        *,
        log_tag: str,
    ) -> bool:
        if not self._yilu_window_expired(window_deadline):
            return True
        deadline_text = (
            window_deadline.strftime("%H:%M:%S")
            if window_deadline is not None
            else "本小时10分"
        )
        self._emit(
            f"⏭️ [{log_tag}] 依卢出现窗口已于 {deadline_text} 结束，立即停止等待；"
            "不写完成记录，交还轮换外层",
            "WARN",
        )
        return False

    def _is_yilu_orange_rgb(self, rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        if tuple(int(v) for v in rgb) == YILU_GRAY_RGB:
            return False
        r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        return (
            self._rgb_distance((r, g, b), YILU_ORANGE_RGB) <= 75
            or (r >= 220 and 70 <= g <= 145 and b <= 55 and (r - g) >= 85 and (g - b) >= 45)
        )

    def _wait_yilu_orange_point(
        self,
        regions,
        *,
        log_tag: str,
        timeout_s: float = YILU_ORANGE_WAIT_TIMEOUT_SEC,
        window_deadline: Optional[datetime] = None,
    ) -> Optional[str]:
        self._emit(
            f"🔍 [{log_tag}] 扫描依卢1-6：严格灰 {YILU_GRAY_RGB} 不点，等待橙色 {YILU_ORANGE_RGB} 附近",
            "INFO",
        )
        t0 = time.time()
        last_log = 0.0
        last_samples: Dict[str, Optional[Tuple[int, int, int]]] = {}
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return None
            if self._yilu_window_expired(window_deadline):
                self._yilu_window_available(
                    window_deadline,
                    log_tag=log_tag,
                )
                return None
            for key in YILU_POINT_KEYS:
                rgb = mean_rgb_for_region_key(regions, key)
                last_samples[key] = rgb
                if self._is_yilu_orange_rgb(rgb):
                    self._emit(f"✅ [{log_tag}] 命中橙色点：{key} RGB={rgb}", "SUCCESS")
                    return key
            now = time.time()
            if now - last_log >= 0.5:
                summary = ", ".join(f"{k}={last_samples.get(k)}" for k in YILU_POINT_KEYS)
                self._emit(f"🔍 [{log_tag}] 依卢六点仍未橙：{summary}", "DEBUG")
                last_log = now
            time.sleep(0.08)
        summary = ", ".join(f"{k}={last_samples.get(k)}" for k in YILU_POINT_KEYS)
        self._emit(f"❌ [{log_tag}] 等待依卢橙色点超时：{summary}", "ERROR")
        return None

    def _yilu_fear_probe_red(self) -> bool:
        if not self._ensure_unified_framework(getattr(self.bot, "regions", None)):
            return False
        return self._unified_framework._check_color_strict(
            "对战信息.敌方害怕",
            (254, 0, 0),
            tolerance=5,
        )

    def _one_click_release_select_slot_until_yellow(
        self,
        regions,
        slot: int,
        use_foreground: bool,
        *,
        log_tag: str,
        timeout_s: Optional[float] = None,
    ) -> bool:
        """Select the scanned warehouse slot and require its yellow selection probe."""
        slot = int(slot)
        slot_cn = ("一", "二", "三", "四", "五", "六", "七", "八", "九")
        if not 1 <= slot <= len(slot_cn):
            self._emit(f"❌ [{log_tag}] 非法仓库槽位：{slot}", "ERROR")
            return False
        slot_key = f"精灵仓库.{slot}"
        selected_key = f"精灵仓库.选中{slot_cn[slot - 1]}"
        if not regions.get(slot_key) or not regions.get(selected_key):
            self._emit(f"❌ [{log_tag}] 缺少区域：{slot_key} 或 {selected_key}", "ERROR")
            return False
        selected_reg = regions.get(selected_key)
        center_x1, center_y1, center_x2, center_y2 = selected_reg.outer_bbox()
        center_x = int(round((center_x1 + center_x2) / 2.0))
        center_y = int(round((center_y1 + center_y2) / 2.0))

        t0 = time.time()
        attempts = 0
        last_log = 0.0
        while timeout_s is None or time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            attempts += 1
            if not self._click_region_safe(regions, slot_key, use_foreground):
                return False
            time.sleep(0.12)
            selected_rgb = None
            probe_img = window_manager.grab_game_bbox(
                center_x,
                center_y,
                center_x + 2,
                center_y + 2,
                min_size_px=2,
            )
            if probe_img is not None:
                rgb = probe_img.convert("RGB").resize((1, 1)).getpixel((0, 0))
                selected_rgb = int(rgb[0]), int(rgb[1]), int(rgb[2])
            if selected_rgb is not None:
                is_yellow_selected = _is_release_selection_yellow(selected_rgb)
                if is_yellow_selected:
                    self._emit(
                        f"✅ [{log_tag}] {slot_key} 已选中：{selected_key} RGB={selected_rgb} -> yellow",
                        "SUCCESS",
                    )
                    return True
            if time.time() - last_log >= 0.5:
                self._emit(
                    f"⏳ [{log_tag}] 点击 {slot_key} 等待 {selected_key} 黄色综合态(R/G高、B低)："
                    f"RGB={selected_rgb}，尝试 {attempts}",
                    "DEBUG",
                )
                last_log = time.time()
            time.sleep(0.05)
        self._emit(
            f"❌ [{log_tag}] {selected_key} 未变黄色，禁止点击放生",
            "ERROR",
        )
        return False

    def _one_click_release_wait_display_probe_light_blue(
        self,
        regions,
        *,
        log_tag: str,
    ) -> bool:
        """Wait for the warehouse display to leave its dark-blue loading state."""
        probe_key = "精灵仓库.展示探针"
        if not regions.get(probe_key):
            self._emit(f"❌ [{log_tag}] 缺少区域：{probe_key}", "ERROR")
            return False

        light_blue_reference = (163, 189, 216)
        attempts = 0
        last_log = 0.0
        while not self._should_abort():
            attempts += 1
            rgb = mean_rgb_for_region_key(regions, probe_key)
            is_light_blue = False
            if rgb is not None:
                is_light_blue = _is_release_display_light_blue(rgb)
            if is_light_blue:
                self._emit(
                    f"✅ [{log_tag}] {probe_key} 已为浅蓝：RGB={rgb}，"
                    f"基准={light_blue_reference}",
                    "SUCCESS",
                )
                return True
            if time.time() - last_log >= 0.5:
                self._emit(
                    f"⏳ [{log_tag}] 等待 {probe_key} 浅蓝，当前RGB={rgb}（基准{light_blue_reference}，"
                    f"深蓝如9,57,108不可放生），"
                    f"尝试 {attempts}",
                    "DEBUG",
                )
                last_log = time.time()
            time.sleep(0.05)
        return False

    def _one_click_release_dismiss_confirmations(
        self,
        use_foreground: bool,
        *,
        log_tag: str,
    ) -> bool:
        """Clear release left/normal confirms while tracking warehouse slot 1."""
        drr = getattr(self.bot, "dar_route_runner", None)
        regions = getattr(self.bot, "regions", None)
        if drr is None or regions is None:
            return False
        slot_key = "精灵仓库.1"
        required = (
            slot_key,
            "对话框.左边确认",
            "对话框.普通确认",
        )
        if any(not regions.get(key) for key in required):
            self._emit(f"❌ [{log_tag}] 缺少放生确认区域：{', '.join(required)}", "ERROR")
            return False

        blue_rgb = (47, 167, 238)
        four_colors = {
            "white": (255, 255, 255),
            "orange": (254, 103, 0),
            "cyan": (148, 223, 252),
            "purple": (71, 28, 83),
        }
        color_tolerance = 24
        slot_poll_s = 0.01
        confirm_probe_interval_s = 0.05
        click_interval_s = 0.10
        timeout_s = 20.0
        confirm_gone_required = 2
        phase = "left_11"
        left_seen = False
        normal_seen = False
        has_left_11 = False
        has_normal_11 = False
        left_gone_samples = 0
        normal_gone_samples = 0
        middle_four_seen = False
        middle_four_color = ""
        last_left_click = 0.0
        last_normal_click = 0.0
        last_left_probe = 0.0
        last_normal_probe = 0.0
        last_log = 0.0
        t0 = time.time()

        self._emit(
            f"⏳ [{log_tag}] 放生后高频追踪 {slot_key}；"
            "左边11期间记录四色；左边11完成→四色→对话框蓝→普通11完成后继续下一次放生",
            "INFO",
        )
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False

            now = time.time()
            slot_rgb = mean_rgb_for_region_key(regions, slot_key)
            slot_state = "unknown"
            if slot_rgb is not None:
                values = tuple(int(value) for value in slot_rgb)
                if max(abs(values[idx] - blue_rgb[idx]) for idx in range(3)) <= color_tolerance:
                    slot_state = "dialog_blue"
                else:
                    for color_name, target_rgb in four_colors.items():
                        if max(abs(values[idx] - target_rgb[idx]) for idx in range(3)) <= color_tolerance:
                            slot_state = color_name
                            break

            if phase == "left_11" and now - last_left_probe >= confirm_probe_interval_s:
                last_left_probe = now
                try:
                    has_left_11 = bool(drr._check_left_1and1and1_probes())
                except Exception as exc:
                    self._emit(f"⚠️ [{log_tag}] 左边11探针异常：{exc}", "DEBUG")
                    has_left_11 = False
                if has_left_11:
                    left_seen = True
                    left_gone_samples = 0
                    if now - last_left_click >= click_interval_s:
                        if not self._click_region_safe(regions, "对话框.左边确认", use_foreground):
                            return False
                        last_left_click = now
                elif left_seen:
                    left_gone_samples += 1
                    if left_gone_samples >= confirm_gone_required:
                        if middle_four_seen:
                            phase = "wait_second_blue"
                            self._emit(
                                f"✅ [{log_tag}] 左边11已完成；期间已捕捉到{middle_four_color}，"
                                "等待下一次对话框蓝后处理普通11",
                                "SUCCESS",
                            )
                        else:
                            phase = "wait_four_colors"
                            self._emit(f"✅ [{log_tag}] 左边11已完成，等待 精灵仓库.1 变四色之一", "SUCCESS")

            if phase == "left_11" and left_seen and slot_state in four_colors:
                if not middle_four_seen:
                    self._emit(
                        f"🎨 [{log_tag}] 左边11期间捕捉到 精灵仓库.1={slot_state}，"
                        "将作为中间四色推进依据",
                        "DEBUG",
                    )
                middle_four_seen = True
                middle_four_color = slot_state

            if phase == "wait_four_colors" and slot_state in four_colors:
                middle_four_seen = True
                middle_four_color = slot_state
                phase = "wait_second_blue"
                self._emit(
                f"🎨 [{log_tag}] {slot_key} 变为{slot_state}，等待下一次对话框蓝",
                    "SUCCESS",
                )
            elif phase == "wait_second_blue" and slot_state == "dialog_blue":
                phase = "normal_11"
                self._emit(f"🔵 [{log_tag}] {slot_key} 已变对话框蓝，开始处理普通11", "SUCCESS")

            if phase == "normal_11" and now - last_normal_probe >= confirm_probe_interval_s:
                last_normal_probe = now
                try:
                    has_normal_11 = bool(drr._check_normal_1and1and1_probes())
                except Exception as exc:
                    self._emit(f"⚠️ [{log_tag}] 普通11探针异常：{exc}", "DEBUG")
                    has_normal_11 = False
                if has_normal_11:
                    normal_seen = True
                    normal_gone_samples = 0
                    if now - last_normal_click >= click_interval_s:
                        if not self._click_region_safe(regions, "对话框.普通确认", use_foreground):
                            return False
                        last_normal_click = now
                elif normal_seen:
                    normal_gone_samples += 1
                    if normal_gone_samples >= confirm_gone_required:
                        self._emit(
                            f"✅ [{log_tag}] 普通11已完成，放生确认完成，继续下一次放生",
                            "SUCCESS",
                        )
                        return True

            if now - last_log >= 0.5:
                self._emit(
                    f"🔎 [{log_tag}] {slot_key}={slot_rgb}->{slot_state}，"
                    f"phase={phase}，left11={left_seen}:{left_gone_samples}/{confirm_gone_required}，"
                    f"middleFour={middle_four_color or '-'}，"
                    f"normal11={normal_seen}:{normal_gone_samples}/{confirm_gone_required}",
                    "DEBUG",
                )
                last_log = now
            time.sleep(slot_poll_s)

        self._emit(
            f"❌ [{log_tag}] 放生确认超时：phase={phase}，"
            f"left11={left_seen}:{left_gone_samples}/{confirm_gone_required}，"
            f"middleFour={middle_four_color or '-'}，"
            f"normal11={normal_seen}:{normal_gone_samples}/{confirm_gone_required}",
            "ERROR",
        )
        return False

    def _one_click_release_follow_purple_from_open_bag(
        self,
        use_foreground: bool,
        *,
        log_tag: str,
    ) -> bool:
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            return False
        stop_event = getattr(self.bot, "_stop_event", threading.Event())
        scan = drr.scan_pick_bag_party_color_slots_any(
            stop_event,
            log_tag,
            timeout_s=10.0,
            min_cyan=0,
            min_purple=1,
        )
        purple = scan.get("purple") if isinstance(scan, dict) else None
        if not scan.get("ok") or not purple:
            self._emit(f"❌ [{log_tag}] 未识别到紫色精灵，无法设置跟随", "ERROR")
            return False
        if not drr._click_pet_with_selection_check(str(purple), use_foreground, stop_event):
            self._emit(f"❌ [{log_tag}] 紫色精灵{purple}选中失败", "ERROR")
            return False
        drr._click_region("精灵背包.身边跟随", use_foreground)
        time.sleep(0.5)
        return True

    def _one_click_release_click_category_until_slot1_color(
        self,
        regions,
        category_key: str,
        wanted_colors: set[str],
        use_foreground: bool,
        *,
        log_tag: str,
        timeout_s: float = 8.0,
    ) -> bool:
        """Keep selecting a warehouse category until slot 1 reflects the expected page state."""
        if not regions.get(category_key):
            self._emit(f"❌ [{log_tag}] 缺少区域：{category_key}", "ERROR")
            return False
        if not regions.get("精灵仓库.1"):
            self._emit(f"❌ [{log_tag}] 缺少区域：精灵仓库.1", "ERROR")
            return False

        t0 = time.time()
        attempts = 1
        last_log = 0.0
        expected = "/".join(sorted(wanted_colors))
        self._emit(
            f"⏳ [{log_tag}] 首次点击 {category_key}，等待 0.8s 后追踪 精灵仓库.1",
            "DEBUG",
        )
        if not self._click_region_safe(regions, category_key, use_foreground):
            return False
        # 分类切换的画面需要先稳定，不能把 ALL 或上一系遗留的 1 号位颜色当成当前系首页。
        time.sleep(0.8)
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            rgb = mean_rgb_for_region_key(regions, "精灵仓库.1")
            color = self._psychic_exp_slot_color(rgb)
            if color in wanted_colors:
                self._emit(
                    f"✅ [{log_tag}] {category_key} 已就绪：精灵仓库.1 RGB={rgb} -> {color}",
                    "SUCCESS",
                )
                return True
            attempts += 1
            if time.time() - last_log >= 1.0:
                self._emit(
                    f"⏳ [{log_tag}] 点击 {category_key} 等待 精灵仓库.1 变为 {expected}："
                    f"RGB={rgb} -> {color}，尝试 {attempts}",
                    "DEBUG",
                )
                last_log = time.time()
            if not self._click_region_safe(regions, category_key, use_foreground):
                return False
            attempts += 1
            time.sleep(0.25)

        rgb = mean_rgb_for_region_key(regions, "精灵仓库.1")
        color = self._psychic_exp_slot_color(rgb)
        self._emit(
            f"❌ [{log_tag}] 点击 {category_key} 超时：精灵仓库.1 RGB={rgb} -> {color}，"
            f"期望 {expected}",
            "ERROR",
        )
        return False

    def run_one_click_release_mode(self, use_foreground: bool = False) -> bool:
        """Release cyan pets in configured warehouse categories, scanning every page to its end."""
        regions = getattr(self.bot, "regions", None)
        drr = getattr(self.bot, "dar_route_runner", None)
        if regions is None or drr is None:
            self._emit("❌ [一键放生] 缺少 regions 或 dar_route_runner", "ERROR")
            return False
        required = (
            "精灵背包.打开精灵背包",
            "精灵背包.精灵仓库",
            "精灵仓库.ALL",
            "精灵仓库.单属性",
            "精灵仓库.双属性",
            "精灵仓库.自然系",
            "精灵仓库.机械系",
            "精灵仓库.超能系",
            "精灵仓库.普通系",
            "精灵仓库.冰系",
            "精灵仓库.暗影系",
            "精灵仓库.水超能",
            "精灵仓库.右",
            "精灵仓库.放生",
            "精灵仓库.关闭",
            "精灵仓库.展示探针",
            "对话框.左边确认",
            "对话框.普通确认",
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ [一键放生] 缺少区域：{key}", "ERROR")
                return False
        for slot in range(1, 10):
            if not regions.get(f"精灵仓库.{slot}"):
                self._emit(f"❌ [一键放生] 缺少区域：精灵仓库.{slot}", "ERROR")
                return False
        for slot_cn in ("一", "二", "三", "四", "五", "六", "七", "八", "九"):
            selection_key = f"精灵仓库.选中{slot_cn}"
            if not regions.get(selection_key):
                self._emit(f"❌ [一键放生] 缺少区域：{selection_key}", "ERROR")
                return False

        from core.logger import kernel_cursor

        stop_event = getattr(self.bot, "_stop_event", threading.Event())
        tag = "一键放生"
        self._emit(f"🐾 [{tag}] 打开精灵背包", "SYSTEM")
        if not self._click_region_safe(regions, "精灵背包.打开精灵背包", use_foreground):
            return False
        if not wait_pet_bag_ui_ready_after_open(
            regions,
            emit_fn=self._emit,
            stop_check=self._should_abort,
            log_tag=tag,
            timeout_s=8.0,
        ):
            return False

        storage_cursor = kernel_cursor()
        self._emit(f"📂 [{tag}] 打开精灵仓库", "SYSTEM")
        if not self._click_region_safe(regions, "精灵背包.精灵仓库", use_foreground):
            return False
        if not self._wait_kernel_line_matches(
            RE_PETSTORAGE_SWF,
            log_tag=f"{tag}·PetStorage",
            timeout_s=10.0,
            success_msg=f"✅ [{tag}] 检测到 PetStorage.swf",
            start_cursor=storage_cursor,
        ):
            return False
        if not drr._warehouse_click_all_until_right_available(
            use_foreground,
            stop_event,
            log_tag=f"{tag}·仓库ALL",
        ):
            return False

        categories = ONE_CLICK_RELEASE_CATEGORIES
        released_total = 0
        released_by_category = {category: 0 for _mode, category in categories}
        for category_index, (mode_name, category) in enumerate(categories):
            if self._should_abort():
                return False
            mode_key = f"精灵仓库.{mode_name}"
            category_key = f"精灵仓库.{category}"
            if category_index:
                self._emit(f"📂 [{tag}] 切换 {category} 前以自然系复位到第一页", "SYSTEM")
                if not self._one_click_release_click_category_until_slot1_color(
                    regions,
                    "精灵仓库.自然系",
                    {"white"},
                    use_foreground,
                    log_tag=f"{tag}·{category}·自然系复位",
                ):
                    return False
            self._emit(f"📂 [{tag}] 切换 {mode_name}·{category}", "SYSTEM")
            if not self._click_region_safe(regions, mode_key, use_foreground):
                return False
            time.sleep(0.15)
            if not self._one_click_release_click_category_until_slot1_color(
                regions,
                category_key,
                {"orange", "cyan", "purple"},
                use_foreground,
                log_tag=f"{tag}·{category}·首页就绪",
            ):
                return False

            page = 1
            page_turns = 0
            while True:
                if self._should_abort():
                    return False
                self._emit(f"🔎 [{tag}] {category} 第{page}页扫描", "INFO")
                slot = 1
                releases_on_page = 0
                while slot <= 9:
                    if self._should_abort():
                        return False
                    key = f"精灵仓库.{slot}"
                    rgb = mean_rgb_for_region_key(regions, key)
                    color = self._psychic_exp_slot_color(rgb)
                    self._emit(f"🔎 [{tag}] {category} {key} RGB={rgb} -> {color}", "DEBUG")
                    if color != "cyan":
                        slot += 1
                        continue
                    self._emit(f"🐾 [{tag}] {category} 第{page}页 {key} 为青色，执行放生", "SYSTEM")
                    if not self._one_click_release_select_slot_until_yellow(
                        regions,
                        slot,
                        use_foreground,
                        log_tag=f"{tag}·{category}·{page}页·{slot}·选中",
                    ):
                        return False
                    if not self._one_click_release_wait_display_probe_light_blue(
                        regions,
                        log_tag=f"{tag}·{category}·{page}页·{slot}·展示就绪",
                    ):
                        return False
                    if not self._click_region_safe(regions, "精灵仓库.放生", use_foreground):
                        return False
                    if not self._one_click_release_dismiss_confirmations(
                        use_foreground,
                        log_tag=f"{tag}·{category}·{page}页·{slot}",
                    ):
                        return False
                    released_total += 1
                    released_by_category[category] += 1
                    releases_on_page += 1
                    self._emit(
                        f"📊 [{tag}] {category} 已放生={released_by_category[category]}，总计={released_total}",
                        "INFO",
                    )
                    time.sleep(0.25)
                    # 放生后后续精灵会补到当前格位，必须原地重扫。

                right_rgb = mean_rgb_for_region_key(regions, "精灵仓库.右")
                if drr._warehouse_right_rgb_is_end(right_rgb):
                    self._emit(
                        f"✅ [{tag}] {category} 已扫描至末页：{page}页，放生={released_by_category[category]}",
                        "SUCCESS",
                    )
                    break
                if not drr._warehouse_right_rgb_is_available(right_rgb):
                    self._emit(f"❌ [{tag}] {category} 右翻状态异常：RGB={right_rgb}", "ERROR")
                    return False
                if page_turns >= WAREHOUSE_PAGE_TURN_MAX_COUNT:
                    self._emit(
                        f"❌ [{tag}] {category} 右翻达到上限 "
                        f"{WAREHOUSE_PAGE_TURN_MAX_COUNT} 次仍未到末页，停止扫描",
                        "ERROR",
                    )
                    return False
                if not self._click_region_safe(regions, "精灵仓库.右", use_foreground):
                    return False
                page_turns += 1
                time.sleep(0.45)
                page += 1
        self._emit(f"📦 [{tag}] 关闭精灵仓库", "SYSTEM")
        drr._click_pet_warehouse_close(use_foreground, log_tag=tag)
        time.sleep(0.5)
        self._emit(f"💼 [{tag}] 放生完成，关闭精灵背包", "INFO")
        drr._close_pet_bag_with_verify(
            use_foreground,
            stop_event,
            "精灵背包.打开精灵背包",
            "精灵背包.打开精灵背包按钮",
            log_tag=f"{tag}·关闭背包",
        )
        category_summary = "，".join(
            f"{category}={released_by_category[category]}"
            for _mode, category in categories
        )
        self._emit(
            f"✅ [{tag}] 完成，累计放生青色精灵={released_total}；{category_summary}",
            "SUCCESS",
        )
        return True

    def _run_yilu_release_selected_light_pet_from_open_bag(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
    ) -> bool:
        """依卢战后：背包保持打开时进仓库，ALL就绪后双击光系第一只并放生，再关仓库回到背包。"""
        required = (
            "精灵背包.精灵仓库",
            "精灵仓库.ALL",
            "精灵仓库.光系",
            "精灵仓库.7",
            "精灵仓库.8",
            "精灵仓库.9",
            "精灵仓库.放生",
            "精灵仓库.关闭",
            "对话框.左边确认",
            "对话框.普通确认",
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ [{log_tag}] 缺少区域：{key}", "ERROR")
                return False

        dar_runner = getattr(self.bot, "dar_route_runner", None)
        dismiss_fn = getattr(dar_runner, "_dismiss_specific_111_until_disappear", None)
        if (
            dar_runner is None
            or not callable(dismiss_fn)
            or not hasattr(dar_runner, "_check_left_1and1and1_probes")
            or not hasattr(dar_runner, "_check_normal_1and1and1_probes")
        ):
            self._emit(f"❌ [{log_tag}] 缺少放生 111 清理能力", "ERROR")
            return False

        from core.logger import kernel_cursor

        stop_event = getattr(self.bot, "_stop_event", None)
        if stop_event is None:
            stop_event = threading.Event()

        storage_cursor = kernel_cursor()
        self._emit(f"🖱️ [{log_tag}] 点击 精灵背包.精灵仓库", "SYSTEM")
        if not self._click_region_safe(regions, "精灵背包.精灵仓库", use_foreground):
            return False

        if not self._wait_kernel_line_matches(
            RE_PETSTORAGE_SWF,
            log_tag=f"{log_tag}·PetStorage",
            timeout_s=10.0,
            success_msg=f"✅ [{log_tag}] 检测到 PetStorage.swf",
            start_cursor=storage_cursor,
        ):
            return False

        self._emit(f"📂 [{log_tag}] 点击 精灵仓库.ALL 直到 精灵仓库.右 变亮灰", "INFO")
        if not self._warehouse_click_all_until_tail_color_ready(
            regions,
            use_foreground,
            log_tag=f"{log_tag}·仓库ALL",
        ):
            return False
        time.sleep(0.2)

        self._emit(f"🖱️ [{log_tag}] 双击 精灵仓库.光系", "SYSTEM")
        if not self._click_region_safe_twice(
            regions,
            "精灵仓库.光系",
            use_foreground,
            gap_s=0.08,
        ):
            return False
        time.sleep(0.25)

        self._emit(f"🐾 [{log_tag}] 点击 精灵仓库.放生", "SYSTEM")
        if not self._click_region_safe(regions, "精灵仓库.放生", use_foreground):
            return False
        time.sleep(0.20)

        if not dismiss_fn(
            checker=dar_runner._check_left_1and1and1_probes,
            confirm_key="对话框.左边确认",
            use_foreground=use_foreground,
            stop_event=stop_event,
            timeout_s=20.0,
            log_tag=f"{log_tag}·放生左边111",
            poll_s=0.08,
            need_gone=2,
            click_while_waiting=False,
        ):
            return False
        time.sleep(0.20)
        if not dismiss_fn(
            checker=dar_runner._check_normal_1and1and1_probes,
            confirm_key="对话框.普通确认",
            use_foreground=use_foreground,
            stop_event=stop_event,
            timeout_s=20.0,
            log_tag=f"{log_tag}·放生普通111",
            poll_s=0.08,
            need_gone=2,
            click_while_waiting=False,
        ):
            return False

        self._emit(f"🖱️ [{log_tag}] 点击 精灵仓库.关闭，返回背包关闭流程", "SYSTEM")
        if not self._click_region_safe(regions, "精灵仓库.关闭", use_foreground):
            return False
        time.sleep(0.4)
        return True

    def _run_yilu_release_from_closed_bag(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
        append_record: bool,
    ) -> bool:
        bag_open_key = "精灵背包.打开精灵背包"
        self._emit(
            f"💼 [{log_tag}] 一次性模式已结束，打开背包直接放生，不恢复精灵",
            "SYSTEM",
        )
        if not self._click_region_safe(regions, bag_open_key, use_foreground):
            return False
        if not wait_pet_bag_ui_ready_after_open(
            regions,
            emit_fn=self._emit,
            stop_check=self._should_abort,
            log_tag=f"{log_tag}·开包",
        ):
            self._emit(f"❌ [{log_tag}] 背包UI未就绪，停止放生", "ERROR")
            self._request_outer_mode_restart(f"{log_tag}-背包UI未就绪")
            return False
        if not self._run_yilu_release_selected_light_pet_from_open_bag(
            regions,
            use_foreground,
            log_tag=f"{log_tag}·放生",
        ):
            return False
        self._emit(f"💼 [{log_tag}] 放生完成，关闭精灵背包", "INFO")
        if not self._click_region_safe(regions, bag_open_key, use_foreground):
            return False
        time.sleep(0.5)
        if append_record:
            self._append_yilu_daily_record(
                phase="released",
                note="依卢战后放生完成（一次性胜利后不恢复）",
            )
        return True

    def _run_yilu_rare_battle(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
        initial_cursor,
        window_deadline: Optional[datetime] = None,
    ) -> bool:
        if not self._ensure_unified_framework(regions):
            return False

        stop_event = getattr(self.bot, "_stop_event", threading.Event())
        dar_runner = getattr(self.bot, "dar_route_runner", None)
        if dar_runner is None or not hasattr(dar_runner, "_switch_pet_for_rare_mode"):
            self._emit(f"❌ [{log_tag}] 缺少稀有切换机塔逻辑", "ERROR")
            return False

        state = {
            "switch_failed_capsule_only": False,
            "skill2_after_jita": 0,
            "last_skill1_round": None,
            "skill1_count": 0,
            "next_skill1": False,
            # 10 分截止只限制“等待依卢出现/入战”。已经收到 PetItem 后应完成
            # 当前战斗和放生，不能把账号半途留在战斗界面。
            "battle_started": False,
        }

        def abort_check() -> bool:
            return (
                self._should_abort()
                or stop_event.is_set()
                or (
                    not state["battle_started"]
                    and self._yilu_window_expired(window_deadline)
                )
            )

        def action_callback(round_idx: int) -> str:
            if round_idx == 1:
                state["switch_failed_capsule_only"] = False
                state["skill2_after_jita"] = 0
                state["last_skill1_round"] = None
                state["skill1_count"] = 0
                state["next_skill1"] = False
                self._emit(f"🎯 [{log_tag}] 第1回合：先使用技能一", "INFO")
                return "skill"

            if state["switch_failed_capsule_only"]:
                self._emit(f"💊 [{log_tag}] 第{round_idx}回合：切机塔失败降级，持续胶囊", "WARN")
                return "capsule"

            if round_idx == 2:
                self._emit(f"🔄 [{log_tag}] 第2回合：切换机塔", "INFO")
                if not dar_runner._switch_pet_for_rare_mode("jita", use_foreground, stop_event):
                    state["switch_failed_capsule_only"] = True
                    self._emit(f"⚠️ [{log_tag}] 切换机塔失败，本场改为胶囊循环", "WARN")
                    return "capsule"
                return "switch"

            if state["skill2_after_jita"] < 3:
                state["skill2_after_jita"] += 1
                self._emit(
                    f"🎯 [{log_tag}] 第{round_idx}回合：机塔出战后二技能 "
                    f"{state['skill2_after_jita']}/3",
                    "INFO",
                )
                return "skill2"

            if state["last_skill1_round"] is None:
                state["last_skill1_round"] = round_idx
                state["skill1_count"] = 1
                state["next_skill1"] = False
                self._emit(
                    f"🎯 [{log_tag}] 第{round_idx}回合：三次二技能后开始技能一红探针捕捉循环",
                    "INFO",
                )
                return "skill"

            cap_fn = getattr(dar_runner, "_pickmode_skill1_cap", None)
            skill1_cap = int(cap_fn()) if callable(cap_fn) else 15
            if state["skill1_count"] >= skill1_cap:
                return "capsule"

            if state["next_skill1"]:
                state["last_skill1_round"] = round_idx
                state["skill1_count"] += 1
                state["next_skill1"] = False
                self._emit(
                    f"🎯 [{log_tag}] 第{round_idx}回合：补技能一（第{state['skill1_count']}次）",
                    "INFO",
                )
                return "skill"

            rounds_since_skill1 = round_idx - int(state["last_skill1_round"])
            if rounds_since_skill1 == 1:
                self._emit(f"💊 [{log_tag}] 第{round_idx}回合：技能一后第一回合，投胶囊", "INFO")
                return "capsule"

            if rounds_since_skill1 >= 2:
                probe_model = self._unified_framework._load_probe_templates()
                if probe_model:
                    probe_state, _, _ = self._unified_framework._detect_round_probe(probe_model)
                    if probe_state == "BLUE":
                        if self._yilu_fear_probe_red():
                            state["next_skill1"] = True
                            self._emit(
                                f"🔴 [{log_tag}] 第{round_idx}回合：红色害怕探针仍在，本回合胶囊，下回合技能一",
                                "INFO",
                            )
                            return "capsule"
                        state["last_skill1_round"] = round_idx
                        state["skill1_count"] += 1
                        state["next_skill1"] = False
                        self._emit(
                            f"⚪ [{log_tag}] 第{round_idx}回合：害怕探针非红，使用技能一（第{state['skill1_count']}次）",
                            "INFO",
                        )
                        return "skill"

                state["last_skill1_round"] = round_idx
                state["skill1_count"] += 1
                state["next_skill1"] = False
                self._emit(
                    f"⚠️ [{log_tag}] 第{round_idx}回合：红探针检测失败，默认技能一（第{state['skill1_count']}次）",
                    "WARN",
                )
                return "skill"

            return "capsule"

        cycle_fn = getattr(dar_runner, "_capsule_cycle_tiers_for_current_battle", None)
        cycle_ov = cycle_fn() if callable(cycle_fn) else None
        config = BattleConfig(
            mode=BattleMode.WILD,
            use_foreground=use_foreground,
            skill_key="对战.使用技能一",
            action_callback=action_callback,
            abort_check=abort_check,
            round_timeout_sec=60.0,
            capsule_cycle_tiers_override=cycle_ov,
            skip_map10_white_end=True,
        )

        ok, calib_result = self._unified_framework.stage2_calibration_and_petitem(
            trigger_callback=None,
            use_foreground=use_foreground,
            timeout_s=45.0,
            skip_stage1=True,
            config=config,
            initial_cursor=initial_cursor,
        )
        if calib_result == "reconnect_needed":
            self._emit(f"❌ [{log_tag}] 校准返回 reconnect_needed，退出依卢任务", "ERROR")
            return False
        if not ok:
            if self._yilu_window_expired(window_deadline):
                self._yilu_window_available(
                    window_deadline,
                    log_tag=f"{log_tag}·入战",
                )
                return False
            self._emit(f"❌ [{log_tag}] 未检测到 PetItem 入战", "ERROR")
            return False

        state["battle_started"] = True
        self._emit(f"⚔️ [{log_tag}] PetItem 已出现，进入三次二技能 + 红探针捕捉流程", "SYSTEM")
        if not self._unified_framework.stage3_battle_loop(config):
            self._emit(f"❌ [{log_tag}] 战斗循环失败", "ERROR")
            return False

        self._unified_framework.stage4_post_battle(config, is_training_room=False)
        if not self._run_yilu_release_from_closed_bag(
            regions,
            use_foreground,
            log_tag=f"{log_tag}·战后",
            append_record=True,
        ):
            return False
        self._emit(f"✅ [{log_tag}] 单场依卢稀有流程完成", "SUCCESS")
        return True

    def run_lanlan_mode(
        self,
        use_foreground: bool = False,
    ) -> bool:
        """岚岚按钮：地图→90太空站→map102+NPC→地图.102to108→map108→岚岚循环；黄色后停止。"""
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions", "ERROR")
            return False

        required = (
            NEW_DAILY_SEQ8_MAP_BTN_KEY,
            NEW_DAILY_SEQ9_SPOT_90_KEY,
            LANLAN_TO_108_KEY,
            MAP10_WHITE_PROBE_KEY_NIEO,
            LANLAN_NPC_KEY,
            LANLAN_WHITE_PROBE_KEY,
            LANLAN_START_KEY,
            "对战.使用技能一",
            "对战.使用技能二",
            "对战.使用技能三",
            "对战.使用技能四",
            "对战.胜利探针",
            "对话框.对战胜利确认",
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ 岚岚流程缺少区域：{key}", "ERROR")
                return False

        tag = "岚岚"
        self._emit(
            f"📋 [{tag}] 开始执行：地图→90太空站→map102+NPC→地图.102to108→map108→岚岚→二技能循环",
            "SYSTEM",
        )

        if not self._wait_after_follow_before_next_ui(f"{tag}·打开地图前"):
            return False
        if not self._new_daily_click_map_then_delay(
            regions, use_foreground, log_tag=tag
        ):
            return False
        self._emit(f"🖱️ [{tag}] 点击 {NEW_DAILY_SEQ9_SPOT_90_KEY}", "SYSTEM")
        if not self._click_region_safe(
            regions, NEW_DAILY_SEQ9_SPOT_90_KEY, use_foreground
        ):
            return False
        if not self._wait_map_npc_then_delay(
            NEW_DAILY_SEQ9_MAP_AFTER_90, log_tag=f"{tag}·map102"
        ):
            return False
        if not self._new_daily_gap_before_step(1, 2):
            return False

        self._emit(f"🖱️ [{tag}] 点击 {LANLAN_TO_108_KEY}", "SYSTEM")
        if not self._click_region_safe(
            regions, LANLAN_TO_108_KEY, use_foreground
        ):
            return False

        self._emit(f"⏳ [{tag}] 等待 map108 信号", "INFO")
        if not self._wait_for_map_kernel(108, timeout_s=NEW_DAILY_MAP_WAIT_TIMEOUT_SEC):
            self._emit(f"❌ [{tag}] 等待 map108 超时", "ERROR")
            return False

        if not self._wait_yilu_white_probe_disappear(
            regions,
            log_tag=f"{tag}·map108白探针",
            timeout_s=45.0,
        ):
            return False

        if not self._lanlan_click_npc_until_white_probe(
            regions,
            use_foreground,
            log_tag=f"{tag}·NPC",
        ):
            return False

        if not self._run_lanlan_skill2_until_yellow(
            regions,
            use_foreground,
            log_tag=f"{tag}·战斗",
        ):
            return False

        self._emit(
            f"✅ [{tag}] 黄色结束，按要求停止",
            "SUCCESS",
        )
        if not self._lanlan_has_daily_record_today():
            self._append_lanlan_daily_record(note="黄色结束")
        else:
            self._emit(f"📋 [{tag}] 黄色结束记录已在战斗阶段写入", "INFO")
        return True

    def run_light_mantis_mode(
        self,
        use_foreground: bool = False,
    ) -> bool:
        """光螳螂按钮：地图→90太空站→map102+NPC→0白123→左边1AND1→普通确认至4橙→4入战→黄色结束。"""
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions", "ERROR")
            return False

        required = (
            NEW_DAILY_SEQ8_MAP_BTN_KEY,
            NEW_DAILY_SEQ9_SPOT_90_KEY,
            LIGHT_MANTIS_CLICK0_KEY,
            LIGHT_MANTIS_WHITE_PROBE_KEY,
            *LIGHT_MANTIS_CLICK_KEYS,
            LIGHT_MANTIS_ENTRY_KEY,
            "对话框.通用探针",
            "对话框.普通确认探针",
            "对话框.左边确认",
            "对战.使用技能一",
            "对战.使用技能二",
            "对战.使用技能四",
            "对战信息.敌方害怕",
            "对战.胜利探针",
            "对话框.对战胜利确认",
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ 光螳螂流程缺少区域：{key}", "ERROR")
                return False

        tag = "光螳螂"
        self._emit(
            f"📋 [{tag}] 开始执行：地图→90太空站→map102+NPC→0白123→左边1AND1→普通确认至4橙→入战",
            "SYSTEM",
        )

        if not self._wait_after_follow_before_next_ui(f"{tag}·打开地图前"):
            return False
        if not self._new_daily_click_map_then_delay(
            regions, use_foreground, log_tag=tag
        ):
            return False
        self._emit(f"🖱️ [{tag}] 点击 {NEW_DAILY_SEQ9_SPOT_90_KEY}", "SYSTEM")
        if not self._click_region_safe(
            regions, NEW_DAILY_SEQ9_SPOT_90_KEY, use_foreground
        ):
            return False
        if not self._wait_map_npc_then_delay(
            NEW_DAILY_SEQ9_MAP_AFTER_90, log_tag=f"{tag}·map102"
        ):
            return False

        if not self._run_light_mantis_until_yellow(
            regions,
            use_foreground,
            log_tag=f"{tag}·战斗",
        ):
            return False
        dar_runner = getattr(self.bot, "dar_route_runner", None)
        restore_primary = getattr(
            dar_runner,
            "restore_pet166_primary_after_197_success_or_reseat",
            None,
        )
        if not callable(restore_primary):
            self._emit(f"❌ [{tag}] 缺少黄色后恢复166首发入口", "ERROR")
            return False
        stop_event = getattr(self.bot, "_stop_event", None)
        if not isinstance(stop_event, threading.Event):
            stop_event = threading.Event()
        if not restore_primary(
            use_foreground,
            stop_event,
            log_tag=f"{tag}·黄色后恢复166首发",
        ):
            self._emit(f"❌ [{tag}] 黄色后恢复166首发失败", "ERROR")
            return False
        self._append_light_mantis_weekly_record(note="黄色结束")
        self._emit(f"✅ [{tag}] 黄色结束，流程完成", "SUCCESS")
        return True

    def run_yilu_mode(
        self,
        use_foreground: bool = False,
        window_deadline: Optional[datetime] = None,
    ) -> bool:
        """依卢按钮：重连后进入108，白探针消失后扫依卢1-6橙点，入战后切机塔三次二技能，再走技能一红探针捕捉。"""
        if not window_manager.find_window():
            self._emit("❌ 未检测到游戏窗口", "ERROR")
            return False

        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ DailyRunner 缺少 bot.regions", "ERROR")
            return False

        tag = "依卢"
        if not self._yilu_window_available(window_deadline, log_tag=tag):
            return False
        stop_check = lambda: self._should_abort() or self._yilu_window_expired(
            window_deadline
        )
        if not self._wait_after_follow_before_next_ui(f"{tag}·后续操作前"):
            return False
        if not self._yilu_window_available(window_deadline, log_tag=tag):
            return False
        if self._yilu_has_daily_record_today():
            self._emit(
                f"🧾 [{tag}] 今日 06:00 业务日内已有记录，跳过太空站与捕捉，直接执行放生且不恢复",
                "SYSTEM",
            )
            return self._run_yilu_release_from_closed_bag(
                regions,
                use_foreground,
                log_tag=f"{tag}·今日已记录",
                append_record=False,
            )

        required = (
            NEW_DAILY_SEQ8_MAP_BTN_KEY,
            NEW_DAILY_SEQ9_SPOT_90_KEY,
            LANLAN_TO_108_KEY,
            MAP10_WHITE_PROBE_KEY_NIEO,
            "对战.使用技能一",
            "对战.使用技能二",
            "对战信息.敌方害怕",
        ) + YILU_POINT_KEYS
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ 依卢流程缺少区域：{key}", "ERROR")
                return False

        self._emit(
            f"📋 [{tag}] 开始执行：地图→90太空站→map102+NPC→地图.102to108→map108→白探针消失→依卢橙点→稀有捕捉",
            "SYSTEM",
        )

        if not self._new_daily_click_map_then_delay(
            regions,
            use_foreground,
            log_tag=tag,
            stop_check=stop_check,
        ):
            return False
        self._emit(f"🖱️ [{tag}] 点击 {NEW_DAILY_SEQ9_SPOT_90_KEY}", "SYSTEM")
        if not self._click_region_safe(
            regions, NEW_DAILY_SEQ9_SPOT_90_KEY, use_foreground
        ):
            return False
        if not self._wait_map_npc_then_delay(
            NEW_DAILY_SEQ9_MAP_AFTER_90,
            log_tag=f"{tag}·map102",
            stop_check=stop_check,
        ):
            return False
        if not self._new_daily_gap_before_step(1, 2):
            return False
        if not self._yilu_window_available(window_deadline, log_tag=tag):
            return False

        self._emit(f"🖱️ [{tag}] 点击 {LANLAN_TO_108_KEY}", "SYSTEM")
        if not self._click_region_safe(
            regions, LANLAN_TO_108_KEY, use_foreground
        ):
            return False

        self._emit(f"⏳ [{tag}] 等待 map108 信号", "INFO")
        if not self._wait_for_map_kernel(
            108,
            timeout_s=NEW_DAILY_MAP_WAIT_TIMEOUT_SEC,
            stop_check=stop_check,
        ):
            self._emit(f"❌ [{tag}] 等待 map108 超时", "ERROR")
            return False

        if not self._wait_yilu_white_probe_disappear(
            regions,
            log_tag=f"{tag}·白探针",
            timeout_s=45.0,
            window_deadline=window_deadline,
        ):
            self._yilu_window_available(window_deadline, log_tag=f"{tag}·白探针")
            return False

        orange_key = self._wait_yilu_orange_point(
            regions,
            log_tag=f"{tag}·六点扫描",
            window_deadline=window_deadline,
        )
        if not orange_key:
            return False

        from core.logger import kernel_cursor

        start_cursor = kernel_cursor()
        self._emit(f"🖱️ [{tag}] 点击橙色点 {orange_key} 并等待 PetItem", "SYSTEM")
        if not self._click_region_safe(regions, orange_key, use_foreground):
            return False

        return self._run_yilu_rare_battle(
            regions,
            use_foreground,
            log_tag=f"{tag}·战斗",
            initial_cursor=start_cursor,
            window_deadline=window_deadline,
        )

    def run_new_daily_chain_1_to_9(
        self,
        use_foreground: bool = False,
        *,
        skip_hero_tower: bool = False,
        from_daily_chain: bool = True,
        start_variant: str = "1",
        start_step: int = 1,
        track_progress: bool = False,
    ) -> bool:
        """一键新日常：从指定方案顺序执行到 9，首个方案可从指定步数开始。"""
        start_v = (start_variant or "1").strip()
        try:
            first_step = max(1, int(start_step or 1))
        except (TypeError, ValueError):
            first_step = 1
        if track_progress and start_v not in NEW_DAILY_CHAIN_VARIANTS:
            self.finish_one_click_daily_progress("failed", "unknown start variant")
        if start_v not in NEW_DAILY_CHAIN_VARIANTS:
            self._emit(f"❌ 新日常链：未知起始方案 {start_v!r}", "ERROR")
            return False
        start_idx = NEW_DAILY_CHAIN_VARIANTS.index(start_v)
        variants = NEW_DAILY_CHAIN_VARIANTS[start_idx:]
        ok_all = True
        for idx, v in enumerate(variants):
            if self._should_abort():
                if track_progress:
                    self.finish_one_click_daily_progress("stopped", "manual stop")
                return ok_all
            step = first_step if idx == 0 else 1
            if track_progress:
                self.mark_one_click_daily_progress(
                    f"方案{v} 第{step}步", variant=v, step=step
                )
            chain_idx = NEW_DAILY_CHAIN_VARIANTS.index(v) + 1
            self._emit(
                f"📋 新日常链：开始方案 {v}/9 第 {step} 步（{chain_idx}/{len(NEW_DAILY_CHAIN_VARIANTS)}）",
                "SYSTEM",
            )
            try:
                ok = self.run_new_daily_mode(
                    use_foreground,
                    variant=v,
                    start_step=step,
                    skip_hero_tower=skip_hero_tower if v == "9" else False,
                    from_daily_chain=from_daily_chain,
                    is_chain_entry_variant=idx == 0,
                )
            except Exception as e:
                self._emit(f"💥 新日常方案 {v} 异常: {e}", "ERROR")
                ok = False
            ok_all = ok_all and ok
            if self._should_abort():
                if track_progress:
                    self.finish_one_click_daily_progress("stopped", "manual stop")
                return ok_all
            if not ok:
                self._emit(
                    f"⚠️ 新日常链：方案 {v} 未成功，停止后续方案",
                    "WARN",
                )
                if track_progress:
                    self.finish_one_click_daily_progress(
                        "failed", f"variant {v} failed"
                    )
                return ok_all
            if track_progress:
                self.mark_one_click_daily_progress(
                    f"方案{v} 第{NEW_DAILY_VARIANT_MAX_STEPS[v]}步",
                    variant=v,
                    step=NEW_DAILY_VARIANT_MAX_STEPS[v],
                    completed=True,
                )
            if idx < len(variants) - 1:
                if not self._new_daily_step_gap():
                    if track_progress:
                        status = "stopped" if self._should_abort() else "failed"
                        self.finish_one_click_daily_progress(
                            status, "step gap did not complete"
                        )
                    return ok_all
        if track_progress:
            self.finish_one_click_daily_progress("complete", "all variants complete")
        return ok_all

    @staticmethod
    def _psychic_exp_slot_color(rgb: Optional[Tuple[int, int, int]]) -> str:
        if rgb is None:
            return "unknown"
        r, g, b = rgb
        if r >= 245 and g >= 245 and b >= 245:
            return "white"
        if r >= 200 and 70 <= g <= 200 and b <= 110:
            return "orange"
        # 兼容新显示器高亮青色的红通道偏移（实测最高约195）。
        if b >= 145 and g >= 120 and r <= 205:
            return "cyan"
        if r >= 95 and b >= 120 and g <= 145 and (b - g) >= 25:
            return "purple"
        return "unknown"

    def _psychic_exp_click_first(self, regions, keys: Tuple[str, ...], use_foreground: bool) -> bool:
        for key in keys:
            if regions.get(key):
                return self._click_region_safe(regions, key, use_foreground)
        self._emit(f"❌ 缺少区域：{keys[0]}", "ERROR")
        return False

    def _warehouse_tail_color_ready(
        self,
        regions,
    ) -> Tuple[bool, str]:
        wanted = {"orange", "purple", "cyan"}
        states = []
        for slot in (7, 8, 9):
            key = f"精灵仓库.{slot}"
            rgb = mean_rgb_for_region_key(regions, key)
            color = self._psychic_exp_slot_color(rgb)
            states.append(f"{key} RGB={rgb}->{color}")
            if color in wanted:
                return True, "; ".join(states)
        return False, "; ".join(states)

    @staticmethod
    def _warehouse_right_rgb_is_available(rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        r, g, b = rgb
        # 右翻按钮可点击时约为 (183,183,183) 或 (223,223,223)。
        # 只要 RGB 接近且整体偏大，都按亮灰处理；结束态会明显偏蓝。
        return min(r, g, b) >= 150 and max(r, g, b) - min(r, g, b) <= 35

    def _warehouse_right_available_state(self, regions) -> Tuple[bool, str]:
        key = "精灵仓库.右"
        rgb = mean_rgb_for_region_key(regions, key)
        ready = self._warehouse_right_rgb_is_available(rgb)
        return ready, f"{key} RGB={rgb}->{'亮灰' if ready else '未亮灰'}"

    def _warehouse_nav_state(self, regions, key: str) -> Tuple[str, str]:
        """复用 DarRouteRunner 最新翻页颜色：亮灰可翻，明显蓝色为端点。"""
        rgb = mean_rgb_for_region_key(regions, key)
        drr = getattr(self.bot, "dar_route_runner", None)
        end_checker = getattr(drr, "_warehouse_right_rgb_is_end", None)
        if callable(end_checker):
            is_end = bool(end_checker(rgb))
        elif rgb is not None:
            r, g, b = rgb
            is_end = (
                b >= 190
                and g >= r + 30
                and b >= g + 25
                and b >= r + 70
            )
        else:
            is_end = False
        if is_end:
            return "end", f"{key} RGB={rgb}->到头"
        if self._warehouse_right_rgb_is_available(rgb):
            return "available", f"{key} RGB={rgb}->亮灰可翻"
        return "unknown", f"{key} RGB={rgb}->未知"

    def _warehouse_click_right_until_end(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
    ) -> bool:
        """持续右翻，直到按钮从亮灰可翻态进入最新版末端态。"""
        key = "精灵仓库.右"
        clicks = 0
        last_log = 0.0
        while not self._should_abort():
            state, detail = self._warehouse_nav_state(regions, key)
            if state == "end":
                self._emit(
                    f"✅ [{log_tag}] 仓库已自动翻到最后一页（右翻 {clicks} 次）；{detail}",
                    "SUCCESS",
                )
                return True
            now = time.time()
            if now - last_log >= 1.0:
                self._emit(
                    f"➡️ [{log_tag}] 精灵仓库.右尚未到头，继续点击：{detail}，累计 {clicks} 次",
                    "DEBUG",
                )
                last_log = now
            if clicks >= WAREHOUSE_PAGE_TURN_MAX_COUNT:
                self._emit(
                    f"❌ [{log_tag}] 仓库右翻达到上限 "
                    f"{WAREHOUSE_PAGE_TURN_MAX_COUNT} 次仍未到末页；{detail}",
                    "ERROR",
                )
                return False
            if not self._click_region_safe(regions, key, use_foreground):
                return False
            clicks += 1
            time.sleep(0.15)
        return False

    def _warehouse_click_all_until_tail_color_ready(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
        timeout_s: float = 8.0,
    ) -> bool:
        for key in ("精灵仓库.ALL", "精灵仓库.右"):
            if not regions.get(key):
                self._emit(f"❌ [{log_tag}] 缺少区域：{key}", "ERROR")
                return False

        t0 = time.time()
        attempts = 0
        last_log = 0.0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            ready, state = self._warehouse_right_available_state(regions)
            if ready:
                self._emit(
                    f"✅ [{log_tag}] 精灵仓库.ALL 已生效：精灵仓库.右已亮灰；{state}",
                    "SUCCESS",
                )
                return True
            attempts += 1
            if time.time() - last_log >= 1.0:
                self._emit(
                    f"⏳ [{log_tag}] 点击 精灵仓库.ALL 等待 精灵仓库.右亮灰：{state}，尝试 {attempts}",
                    "DEBUG",
                )
                last_log = time.time()
            if not self._click_region_safe(regions, "精灵仓库.ALL", use_foreground):
                return False
            time.sleep(0.25)
        _ready, state = self._warehouse_right_available_state(regions)
        self._emit(
            f"❌ [{log_tag}] 点击 精灵仓库.ALL 超时：精灵仓库.右仍未亮灰；{state}",
            "ERROR",
        )
        return False

    def _warehouse_click_all_until_slot9_orange(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
        timeout_s: float = 8.0,
    ) -> bool:
        return self._warehouse_click_all_until_tail_color_ready(
            regions,
            use_foreground,
            log_tag=log_tag,
            timeout_s=timeout_s,
        )

    def _psychic_exp_type_text(self, text: str) -> bool:
        try:
            from pynput.keyboard import Controller, Key

            kb = Controller()
            kb.press(Key.ctrl)
            kb.press("a")
            kb.release("a")
            kb.release(Key.ctrl)
            time.sleep(0.05)
            kb.type(str(text))
            return True
        except Exception as e:
            self._emit(f"❌ [超能经验] 键盘输入失败：{e}", "ERROR")
            return False

    def _psychic_exp_clipboard_set_text(self, text: str) -> bool:
        try:
            import tkinter as tk

            root = tk.Tk()
            root.withdraw()
            root.clipboard_clear()
            root.clipboard_append(str(text))
            root.update()
            root.destroy()
            return True
        except Exception as e:
            self._emit(f"⚠️ [超能经验] 写入剪贴板失败：{e}", "WARN")
            return False

    def _psychic_exp_clipboard_get_text(self) -> str:
        try:
            import tkinter as tk

            root = tk.Tk()
            root.withdraw()
            try:
                text = root.clipboard_get()
            except Exception:
                text = ""
            root.destroy()
            return str(text or "")
        except Exception:
            return ""

    def _psychic_exp_hotkey(self, *keys) -> bool:
        try:
            from pynput.keyboard import Controller

            kb = Controller()
            for key in keys:
                kb.press(key)
            for key in reversed(keys):
                kb.release(key)
            return True
        except Exception as e:
            self._emit(f"❌ [超能经验] 键盘快捷键失败：{e}", "ERROR")
            return False

    def _psychic_exp_post_key_to_game(self, vk_code: int, *, char: Optional[str] = None, hold_s: float = 0.08) -> bool:
        try:
            import win32con
            import win32gui

            if not window_manager.find_window() or not getattr(window_manager, "hwnd", None):
                self._emit("❌ [超能经验] 后台键盘输入失败：未找到游戏窗口", "ERROR")
                return False
            hwnd = window_manager.hwnd
            win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, int(vk_code), 1)
            time.sleep(hold_s)
            if char is not None:
                win32gui.PostMessage(hwnd, win32con.WM_CHAR, ord(char), 1)
                time.sleep(hold_s)
            win32gui.PostMessage(hwnd, win32con.WM_KEYUP, int(vk_code), 1)
            time.sleep(hold_s)
            return True
        except Exception as e:
            self._emit(f"❌ [超能经验] 后台键盘输入失败：{e}", "ERROR")
            return False

    def _psychic_exp_post_hotkey_to_game(self, *vk_codes: int, hold_s: float = 0.05) -> bool:
        try:
            import win32con
            import win32gui

            if not window_manager.find_window() or not getattr(window_manager, "hwnd", None):
                self._emit("❌ [超能经验] 后台快捷键失败：未找到游戏窗口", "ERROR")
                return False
            hwnd = window_manager.hwnd
            modifiers = {
                win32con.VK_CONTROL,
                win32con.VK_LCONTROL,
                win32con.VK_RCONTROL,
                win32con.VK_SHIFT,
                win32con.VK_LSHIFT,
                win32con.VK_RSHIFT,
                win32con.VK_MENU,
                win32con.VK_LMENU,
                win32con.VK_RMENU,
            }
            mods = [int(vk) for vk in vk_codes if int(vk) in modifiers]
            keys = [int(vk) for vk in vk_codes if int(vk) not in modifiers]
            for vk in mods:
                win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, 0)
                time.sleep(hold_s)
            for vk in keys:
                win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, 0)
                time.sleep(hold_s)
                win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk, 0)
                time.sleep(hold_s)
            for vk in reversed(mods):
                win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk, 0)
                time.sleep(hold_s)
            return True
        except Exception as e:
            self._emit(f"❌ [超能经验] 后台快捷键失败：{e}", "ERROR")
            return False

    def _psychic_exp_post_char_to_game(self, char_code: int, *, hold_s: float = 0.08) -> bool:
        try:
            import win32con
            import win32gui

            if not window_manager.find_window() or not getattr(window_manager, "hwnd", None):
                self._emit("❌ [超能经验] 后台字符输入失败：未找到游戏窗口", "ERROR")
                return False
            win32gui.PostMessage(window_manager.hwnd, win32con.WM_CHAR, int(char_code), 1)
            time.sleep(hold_s)
            return True
        except Exception as e:
            self._emit(f"❌ [超能经验] 后台字符输入失败：{e}", "ERROR")
            return False

    def _psychic_exp_input_background(self, text: str) -> bool:
        try:
            target = str(text)
            for ch in target:
                if self._should_abort():
                    return False
                if not self._psychic_exp_post_char_to_game(ord(ch), hold_s=0.01):
                    return False
                time.sleep(0.015)
            time.sleep(0.12)
            self._emit(f"✅ [超能经验] 已简单输入 {target}", "SUCCESS")
            return True
        except Exception as e:
            self._emit(f"❌ [超能经验] 简单输入异常：{e}", "ERROR")
            return False

    def _psychic_exp_clear_focused_input(self, use_foreground: bool) -> bool:
        try:
            if use_foreground:
                from pynput.keyboard import Controller, Key

                kb = Controller()
                kb.press(Key.ctrl)
                kb.press("a")
                kb.release("a")
                kb.release(Key.ctrl)
                time.sleep(0.05)
                kb.press(Key.backspace)
                kb.release(Key.backspace)
                time.sleep(0.08)
                return True

            import win32con

            if not self._psychic_exp_post_hotkey_to_game(win32con.VK_CONTROL, ord("A"), hold_s=0.03):
                return False
            time.sleep(0.05)
            if not self._psychic_exp_post_key_to_game(win32con.VK_BACK, hold_s=0.03):
                return False
            time.sleep(0.08)
            return True
        except Exception as e:
            self._emit(f"❌ [超能经验] 清空经验输入框失败：{e}", "ERROR")
            return False

    def _psychic_exp_probe_is_white(self, regions, key: str = "对话框.通用探针") -> bool:
        rgb = mean_rgb_for_region_key(regions, key)
        if rgb is None:
            return False
        r, g, b = rgb
        return r >= 245 and g >= 245 and b >= 245

    def _psychic_exp_probe_is_pure_white(self, regions, key: str) -> bool:
        rgb = mean_rgb_for_region_key(regions, key)
        if rgb is None:
            return False
        r, g, b = rgb
        return r >= 250 and g >= 250 and b >= 250 and (max(rgb) - min(rgb)) <= 5

    def _psychic_exp_probe_is_blue(self, regions, key: str) -> bool:
        rgb = mean_rgb_for_region_key(regions, key)
        if rgb is None:
            return False
        return not self._psychic_exp_probe_is_pure_white(regions, key)

    def _psychic_exp_probe_state(self, regions, key: str) -> str:
        if self._psychic_exp_probe_is_pure_white(regions, key):
            return "white"
        if self._psychic_exp_probe_is_blue(regions, key):
            return f"blue_or_non_white({mean_rgb_for_region_key(regions, key)})"
        rgb = mean_rgb_for_region_key(regions, key)
        return f"other({rgb})"

    def _psychic_exp_fast_right_flips(
        self,
        regions,
        use_foreground: bool,
        count: int,
        *,
        log_tag: str,
        purpose: str,
    ) -> bool:
        requested_count = max(0, int(count or 0))
        count = min(requested_count, WAREHOUSE_PAGE_TURN_MAX_COUNT)
        if count <= 0:
            return True
        if requested_count > WAREHOUSE_PAGE_TURN_MAX_COUNT:
            self._emit(
                f"⚠️ [{log_tag}] {purpose} 请求右翻 {requested_count} 次，"
                f"按仓库翻页上限截断为 {WAREHOUSE_PAGE_TURN_MAX_COUNT} 次",
                "WARN",
            )
        self._emit(f"➡️ [{log_tag}] {purpose}：快速右翻 {count} 次", "INFO")
        for _idx in range(count):
            if self._should_abort():
                return False
            if not self._click_region_safe(regions, "精灵仓库.右", use_foreground):
                return False
            time.sleep(0.05)
        time.sleep(0.25)
        return True

    def _psychic_exp_wait_general_probe_white(
        self, regions, *, timeout_s: float = 8.0, log_tag: str = "超能经验"
    ) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            if self._psychic_exp_probe_is_white(regions):
                return True
            time.sleep(0.05)
        self._emit(f"❌ [{log_tag}] 等待通用探针白色超时", "ERROR")
        return False

    def _psychic_exp_click_skill_cancel_until_probe_gone(
        self, regions, use_foreground: bool, *, timeout_s: float = 8.0
    ) -> bool:
        t0 = time.time()
        clicked = 0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            if not self._psychic_exp_probe_is_white(regions):
                self._emit(f"✅ [超能经验] 通用探针已消失（技能取消点击 {clicked} 次）", "SUCCESS")
                return True
            if not self._click_region_safe(regions, "对话框.技能取消", use_foreground):
                return False
            clicked += 1
            time.sleep(0.10)
        self._emit(f"⚠️ [超能经验] 技能取消后通用探针仍未消失（点击 {clicked} 次）", "WARN")
        return False

    def _psychic_exp_click_exp_skill_cancel_until_probe_gone(
        self, regions, use_foreground: bool, *, timeout_s: float = 8.0
    ) -> bool:
        t0 = time.time()
        clicked = 0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            if not self._psychic_exp_probe_is_white(regions, "经验.白色探针"):
                self._emit(f"✅ [超能经验] 经验白色探针已消失（技能取消点击 {clicked} 次）", "SUCCESS")
                return True
            if not self._click_region_safe(regions, "经验.技能取消", use_foreground):
                return False
            clicked += 1
            time.sleep(0.10)
        self._emit(f"⚠️ [超能经验] 经验白色探针仍未消失（点击 {clicked} 次）", "WARN")
        return False

    def _psychic_exp_seed_exp_value_once(
        self, regions, use_foreground: bool, exp_value: str = "5820"
    ) -> bool:
        target_text = str(exp_value or "5820")
        self._emit(f"🧪 [超能经验] 初始化经验输入：{target_text}（本批只执行一次）", "INFO")
        for attempt in range(1, 4):
            if self._should_abort():
                return False
            self._emit(f"🧪 [超能经验] 后台聚焦经验输入并发送 {target_text}（尝试 {attempt}/3）", "INFO")
            if not self._click_region_safe(regions, "经验.输入", use_foreground):
                return False
            time.sleep(0.80)
            if not self._psychic_exp_clear_focused_input(use_foreground):
                time.sleep(0.5)
                continue
            if use_foreground:
                ok_input = self._psychic_exp_type_text(target_text)
            else:
                ok_input = self._psychic_exp_input_background(target_text)
            if ok_input:
                self._emit(f"✅ [超能经验] {target_text} 输入完成", "SUCCESS")
                return True
            time.sleep(0.5)
        self._psychic_exp_request_refresh_retry(f"经验输入阶段无法后台输入 {target_text}")
        return False

    def _psychic_exp_request_refresh_retry(self, reason: str) -> None:
        self._psychic_exp_refresh_retry_requested = True
        self._emit(f"🔄 [超能经验] {reason}，标记刷新重连后重新执行", "WARN")

    def _psychic_exp_fail_refresh_retry(self, reason: str) -> bool:
        if not self._should_abort():
            self._psychic_exp_request_refresh_retry(reason)
        return False

    def _psychic_exp_prepare_refresh_login(self, use_foreground: bool, *, log_tag: str) -> bool:
        try:
            from core.swf_resource_ops import (
                ensure_newnpc_multi_4_to_4_og,
                sync_fusion_pet_254_set,
            )

            use_union = bool(
                getattr(self.bot, "_task_swf_full_base_done", False)
            )
            ok, msg = sync_fusion_pet_254_set(
                runtime_subset=use_union
            )
            if ok:
                self.bot._task_swf_full_base_done = True
            self._emit(
                f"{'✅' if ok else '❌'} [{log_tag}] 融合SWF：{msg}",
                "SUCCESS" if ok else "ERROR",
            )
            if not ok:
                return False
            ok_npc, msg_npc = ensure_newnpc_multi_4_to_4_og()
            self._emit(
                f"{'✅' if ok_npc else '⚠️'} [{log_tag}] newNpc/multi: {msg_npc}",
                "INFO" if ok_npc else "WARN",
            )
        except Exception as e:
            self._emit(f"❌ [{log_tag}] 融合SWF前置异常：{e}", "ERROR")
            return False

        clear_tmp = getattr(self.bot, "_clear_game_tmp_cache", None)
        if callable(clear_tmp):
            clear_tmp(log_tag=log_tag)

        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner，无法刷新重连", "ERROR")
            return False
        stop_event = self._new_daily_stop_event()
        step1 = getattr(drr, "_step1_trinity_clicks_and_wait_login_swf", None)
        step2 = getattr(drr, "_run_full_refresh_login_pipeline", None)
        if not callable(step1) or not callable(step2):
            self._emit(f"❌ [{log_tag}] dar_route_runner 缺少刷新登录管线", "ERROR")
            return False
        res = step1(use_foreground, log_tag, stop_event)
        if res != "login":
            self._emit(f"❌ [{log_tag}] 刷新重连 Step1 未进入 Login.swf：{res}", "ERROR")
            return False
        return bool(
            step2(
                use_foreground,
                stop_event,
                log_tag,
                skip_molecule_converter=True,
            )
        )

    def _psychic_exp_open_machine_dog_panel(self, regions, use_foreground: bool) -> bool:
        from core.logger import kernel_cursor

        key = "经验.nono区域"
        self._emit("🖱️ [超能经验] 点击经验.nono区域，等待 MachineDogPanel.swf", "INFO")
        start_cursor = kernel_cursor()
        if not self._click_region_safe(regions, key, use_foreground):
            return self._psychic_exp_fail_refresh_retry("点击经验.nono区域失败")
        if self._wait_kernel_line_matches(
            PSYCHIC_EXP_MACHINE_DOG_PANEL_RE,
            log_tag="超能经验·nono",
            timeout_s=3.0,
            success_msg="✅ [超能经验] 已检测到 MachineDogPanel.swf",
            start_cursor=start_cursor,
        ):
            return True

        self._emit("⚠️ [超能经验] 3s 内未检测到 MachineDogPanel.swf，补点一次经验.nono区域", "WARN")
        start_cursor = kernel_cursor()
        if not self._click_region_safe(regions, key, use_foreground):
            return self._psychic_exp_fail_refresh_retry("补点经验.nono区域失败")
        if self._wait_kernel_line_matches(
            PSYCHIC_EXP_MACHINE_DOG_PANEL_RE,
            log_tag="超能经验·nono补点",
            timeout_s=3.0,
            success_msg="✅ [超能经验] 补点后已检测到 MachineDogPanel.swf",
            start_cursor=start_cursor,
        ):
            return True
        self._psychic_exp_request_refresh_retry("MachineDogPanel.swf 检测失败")
        return False

    def _psychic_exp_wait_exp_panel_after_script(self, start_cursor: Optional[int] = None) -> bool:
        if self._wait_kernel_line_matches(
            PSYCHIC_EXP_ADM_PANEL_RE,
            log_tag="超能经验·经验面板",
            timeout_s=3.0,
            success_msg="✅ [超能经验] 已检测到 ExpAdmPanel.swf",
            start_cursor=start_cursor,
        ):
            return True
        self._psychic_exp_request_refresh_retry("ExpAdmPanel.swf 检测失败")
        return False

    def _psychic_exp_wait_white_blue_white_ready(
        self, regions, use_foreground: bool, *, timeout_s: float = 12.0
    ) -> bool:
        t0 = time.time()
        did_5s_reclick = False
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            dialog_general_white = self._psychic_exp_probe_is_white(regions, "对话框.通用探针")
            dialog_confirm_blue = self._psychic_exp_probe_is_blue(regions, "对话框.经验确认")
            exp_general_white = self._psychic_exp_probe_is_white(regions, "经验.通用探针")
            if dialog_general_white and dialog_confirm_blue and exp_general_white:
                self._emit("✅ [超能经验] 已检测到白+蓝+白（三探针就绪）", "SUCCESS")
                return True
            if (not did_5s_reclick) and (time.time() - t0 >= 5.0):
                self._emit("⚠️ [超能经验] 5s 内未出现白+蓝+白，补点一次经验.确认", "WARN")
                if not self._click_region_safe(regions, "经验.确认", use_foreground):
                    return False
                did_5s_reclick = True
            time.sleep(0.05)
        self._emit(
            f"❌ [超能经验] 未在 {timeout_s:.0f}s 内检测到白+蓝+白"
            f"（对话框通用={self._psychic_exp_probe_state(regions, '对话框.通用探针')}，"
            f"对话框经验确认={self._psychic_exp_probe_state(regions, '对话框.经验确认')}，"
            f"经验通用={self._psychic_exp_probe_state(regions, '经验.通用探针')}）",
            "ERROR",
        )
        return False

    def _psychic_exp_click_exp_confirm_until_white_white(
        self, regions, use_foreground: bool, *, timeout_s: float = 12.0
    ) -> bool:
        t0 = time.time()
        last_click = 0.0
        did_5s_reclick = False
        clicked = 0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            general_white = self._psychic_exp_probe_is_white(regions, "对话框.通用探针")
            confirm_white = self._psychic_exp_probe_is_white(regions, "对话框.经验确认")
            if general_white and confirm_white:
                self._emit(
                    f"✅ [超能经验] 经验确认探针已从白+蓝+白推进到白+白（点击 {clicked} 次）",
                    "SUCCESS",
                )
                return True
            now = time.time()
            if general_white and now - last_click >= 0.12:
                if not self._click_region_safe(regions, "对话框.经验确认", use_foreground):
                    return False
                clicked += 1
                last_click = now
            elif (not did_5s_reclick) and (now - t0 >= 5.0):
                self._emit("⚠️ [超能经验] 5s 内未变成白+白，补点一次对话框.经验确认", "WARN")
                if not self._click_region_safe(regions, "对话框.经验确认", use_foreground):
                    return False
                clicked += 1
                last_click = now
                did_5s_reclick = True
            time.sleep(0.05)
        self._emit(
            f"❌ [超能经验] 经验确认未在 {timeout_s:.0f}s 内变成白+白"
            f"（通用={self._psychic_exp_probe_state(regions, '对话框.通用探针')}，"
            f"经验确认={self._psychic_exp_probe_state(regions, '对话框.经验确认')}）",
            "ERROR",
        )
        return False

    def _psychic_exp_wait_normal_confirm_probe_white(
        self, regions, use_foreground: bool, *, timeout_s: float = 20.0
    ) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            if self._psychic_exp_probe_is_pure_white(regions, "经验.普通确认按钮探针"):
                self._emit("✅ [超能经验] 经验.普通确认按钮探针已变纯白", "SUCCESS")
                return True
            self._wait_1and1_clear(
                regions,
                use_foreground,
                timeout_s=2.0,
                min_confirm_clicks=1,
                log_tag="超能经验·经验确认后1AND1",
            )
            time.sleep(0.08)
        self._emit(
            f"❌ [超能经验] 1AND1 后普通确认按钮探针未变纯白，当前={mean_rgb_for_region_key(regions, '经验.普通确认按钮探针')}",
            "ERROR",
        )
        return False

    def _psychic_exp_click_exp_confirm_sweep(
        self, regions, use_foreground: bool, click_index: int
    ) -> bool:
        start_x = 545
        y = 511
        end_x = 595
        try:
            reg = regions.get("经验.确认")
            if reg:
                gx, _gy = reg.sample_click_point()
                end_x = max(start_x, int(round(gx)))
            positions = list(range(start_x, end_x + 1, 5))
            if not positions:
                positions = [start_x]
            x = positions[int(click_index) % len(positions)]
            if use_foreground:
                window_manager.click(x, y)
            else:
                window_manager.click_background(x, y)
            return True
        except Exception as e:
            self._emit(f"❌ [超能经验] 横扫点击经验确认失败：{e}", "ERROR")
            return False

    def _psychic_exp_click_confirm_until_skill_cancel_blue(
        self, regions, use_foreground: bool, *, timeout_s: float = 30.0
    ) -> bool:
        t0 = time.time()
        click_stop = threading.Event()
        click_failed = threading.Event()
        click_count = {"value": 0}
        last_log = 0.0

        def click_worker() -> None:
            idx = 0
            while not click_stop.is_set() and not self._should_abort():
                if not self._psychic_exp_click_exp_confirm_sweep(regions, use_foreground, idx):
                    click_failed.set()
                    return
                idx += 1
                click_count["value"] = idx
                click_stop.wait(0.04)

        worker = threading.Thread(
            target=click_worker,
            name="psychic-exp-confirm-sweep",
            daemon=True,
        )
        worker.start()
        try:
            while time.time() - t0 < timeout_s:
                if self._should_abort():
                    return False
                if click_failed.is_set():
                    return False
                if self._psychic_exp_probe_is_blue(regions, "经验.技能取消"):
                    self._emit(
                        f"✅ [超能经验] 经验.技能取消已从白色变非纯白（经验确认横扫点击 {click_count['value']} 次）",
                        "SUCCESS",
                    )
                    return True
                now = time.time()
                if now - last_log >= 1.0:
                    self._emit(
                        f"⏳ [超能经验] 等待经验.技能取消变非纯白：当前={self._psychic_exp_probe_state(regions, '经验.技能取消')}，已横扫点经验确认 {click_count['value']} 次",
                        "DEBUG",
                    )
                    last_log = now
                time.sleep(0.05)
            self._emit(
                f"❌ [超能经验] 点击经验确认后技能取消未变非纯白，当前={self._psychic_exp_probe_state(regions, '经验.技能取消')}，累计横扫点击 {click_count['value']} 次",
                "ERROR",
            )
            return False
        finally:
            click_stop.set()
            worker.join(timeout=0.5)

    def _psychic_exp_click_skill_cancel_until_white_probe_blue(
        self, regions, use_foreground: bool, *, timeout_s: float = 30.0
    ) -> bool:
        t0 = time.time()
        clicked = 0
        last_log = 0.0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            if self._psychic_exp_probe_is_blue(regions, "经验.白色探针"):
                self._emit(f"✅ [超能经验] 经验.白色探针已变非纯白（技能取消点击 {clicked} 次）", "SUCCESS")
                return True
            now = time.time()
            if now - last_log >= 1.0:
                self._emit(
                    f"⏳ [超能经验] 等待经验.白色探针变非纯白：当前={self._psychic_exp_probe_state(regions, '经验.白色探针')}，已点技能取消 {clicked} 次",
                    "DEBUG",
                )
                last_log = now
            if not self._click_region_safe(regions, "经验.技能取消", use_foreground):
                return False
            clicked += 1
            time.sleep(0.12)
        self._emit(
            f"❌ [超能经验] 技能取消后经验.白色探针未变非纯白，当前={self._psychic_exp_probe_state(regions, '经验.白色探针')}，累计点击 {clicked} 次",
            "ERROR",
        )
        return False

    def _psychic_exp_apply_one_slot(self, regions, use_foreground: bool, slot: int) -> bool:
        if not self._click_region_safe(regions, f"经验.{slot}", use_foreground):
            return self._psychic_exp_fail_refresh_retry(f"点击经验.{slot}失败")
        time.sleep(0.15)
        self._emit(f"📜 [超能经验] 精灵{slot} 执行程序化经验一流程", "INFO")
        if not self._click_region_safe(regions, "经验.确认", use_foreground):
            return self._psychic_exp_fail_refresh_retry(f"精灵{slot}点击经验.确认失败")
        time.sleep(0.12)
        if not self._psychic_exp_wait_white_blue_white_ready(regions, use_foreground):
            return self._psychic_exp_fail_refresh_retry(f"精灵{slot}白+蓝+白就绪失败")
        if not self._psychic_exp_click_exp_confirm_until_white_white(regions, use_foreground):
            return self._psychic_exp_fail_refresh_retry(f"精灵{slot}白+白推进失败")
        if not self._psychic_exp_wait_normal_confirm_probe_white(regions, use_foreground):
            return self._psychic_exp_fail_refresh_retry(f"精灵{slot}普通确认按钮探针纯白失败")
        if not self._psychic_exp_click_confirm_until_skill_cancel_blue(regions, use_foreground):
            return self._psychic_exp_fail_refresh_retry(f"精灵{slot}技能取消变非纯白失败")
        if not self._psychic_exp_click_skill_cancel_until_white_probe_blue(regions, use_foreground):
            return self._psychic_exp_fail_refresh_retry(f"精灵{slot}白色探针变非纯白失败")
        return True

    def _psychic_exp_run_batch(
        self,
        regions,
        use_foreground: bool,
        batch_count: int = 6,
        exp_value: str = "5820",
    ) -> bool:
        batch_count = max(1, min(6, int(batch_count or 6)))
        exp_script_path = self._resolve_script_path("经验")
        if not exp_script_path:
            self._emit("❌ [超能经验] 找不到 fix_script/经验.json", "ERROR")
            return False
        if not self._psychic_exp_open_machine_dog_panel(regions, use_foreground):
            return False
        self._emit(f"📜 [超能经验] 执行经验入口脚本：{os.path.basename(exp_script_path)}", "SYSTEM")
        from core.logger import kernel_cursor

        exp_panel_cursor = kernel_cursor()
        if not self.run_script(exp_script_path, bg_override=None):
            return self._psychic_exp_fail_refresh_retry("经验入口脚本执行失败")
        if not self._psychic_exp_wait_exp_panel_after_script(exp_panel_cursor):
            return False
        time.sleep(0.5)
        if not self._click_region_safe(regions, "经验.1", use_foreground):
            return self._psychic_exp_fail_refresh_retry("进入经验面板后点击经验.1失败")
        time.sleep(0.15)
        if not self._psychic_exp_seed_exp_value_once(regions, use_foreground, exp_value=exp_value):
            return False
        time.sleep(0.5)
        for slot in range(1, batch_count + 1):
            if self._should_abort():
                return False
            if not self._psychic_exp_apply_one_slot(regions, use_foreground, slot):
                return False
            time.sleep(0.15)
        if not self._click_region_safe(regions, "经验.关闭", use_foreground):
            return self._psychic_exp_fail_refresh_retry("经验批次结束点击经验.关闭失败")
        time.sleep(0.4)
        return True

    def _psychic_exp_open_warehouse_psychic(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
        category_key: str = "精灵仓库.超能系",
        initialize_warehouse: bool = True,
    ) -> bool:
        if not self._click_region_safe(regions, "精灵仓库.打开", use_foreground):
            return self._psychic_exp_fail_refresh_retry("点击精灵仓库.打开失败")
        time.sleep(0.5)
        if not initialize_warehouse:
            self._emit(
                f"📂 [{log_tag}] 已重开精灵仓库，保留上次关闭时的分类、页码和槽位",
                "INFO",
            )
            return True
        if regions.get("精灵仓库.单属性"):
            self._click_region_safe(regions, "精灵仓库.单属性", use_foreground)
            time.sleep(0.2)
        if regions.get("精灵仓库.ALL"):
            if not self._warehouse_click_all_until_slot9_orange(regions, use_foreground, log_tag=log_tag):
                return self._psychic_exp_fail_refresh_retry("点击精灵仓库.ALL后精灵仓库.右未亮灰")
            time.sleep(0.2)
        if not self._click_region_safe(regions, category_key, use_foreground):
            return self._psychic_exp_fail_refresh_retry(f"点击{category_key}失败")
        time.sleep(0.5)
        self._emit(f"📂 [{log_tag}] 首次仓库初始化完成，已打开{category_key}第一页", "INFO")
        return True

    def _psychic_exp_take_purple_slot(self, regions, use_foreground: bool, slot: int) -> bool:
        if not self._click_region_safe(regions, f"精灵仓库.{slot}", use_foreground):
            return self._psychic_exp_fail_refresh_retry(f"点击精灵仓库.{slot}失败")
        time.sleep(0.15)
        if not self._click_region_safe(regions, "精灵仓库.放入背包", use_foreground):
            return self._psychic_exp_fail_refresh_retry("点击精灵仓库.放入背包失败")
        if not self._wait_1and1_clear(
            regions,
            use_foreground,
            timeout_s=20.0,
            min_confirm_clicks=1,
            log_tag="超能经验·放入背包",
        ):
            return self._psychic_exp_fail_refresh_retry("放入背包 1AND1 处理失败")
        return True

    def run_psychic_exp_purple_mode(
        self,
        total_pages: Optional[int] = None,
        use_foreground: bool = False,
        *,
        warehouse_category_key: str = "精灵仓库.超能系",
        exp_value: str = "5820",
        mode_label: str = "超能经验",
        _retry_depth: int = 0,
    ) -> bool:
        """首次初始化仓库后从前向后扫描紫色精灵，满 6 只执行一次经验批次。"""
        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ [超能经验] 缺少 regions", "ERROR")
            return False
        required = (
            "刷新.基地",
            "刷新.基地右侧",
            "精灵仓库.打开",
            "精灵仓库.关闭",
            warehouse_category_key,
            "精灵仓库.右",
            "精灵仓库.放入背包",
            "经验.nono区域",
            "经验.输入",
            "经验.关闭",
            "经验.通用探针",
            "经验.白色探针",
            "经验.技能取消",
            "经验.普通确认按钮探针",
            "对话框.通用探针",
            "对话框.经验确认",
            "对话框.普通确认",
            "对话框.普通确认探针",
            "对话框.普通确认",
            "对话框.技能取消",
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ [超能经验] 缺少区域：{key}", "ERROR")
                return False
        for slot in range(1, 10):
            if not regions.get(f"精灵仓库.{slot}"):
                self._emit(f"❌ [超能经验] 缺少区域：精灵仓库.{slot}", "ERROR")
                return False
        for slot in range(1, 7):
            if not regions.get(f"经验.{slot}"):
                self._emit(f"❌ [超能经验] 缺少区域：经验.{slot}", "ERROR")
                return False

        exp_value = str(exp_value or "5820")
        tag = str(mode_label or "超能经验")
        self._psychic_exp_refresh_retry_requested = False
        self._emit(
            f"🚀 [{tag}] 启动：首次仓库初始化后从第一页 1→9 正向扫描，后续重开保持原位置",
            "SYSTEM",
        )

        def retry_whole(reason: str) -> bool:
            if self._should_abort():
                return False
            self._psychic_exp_request_refresh_retry(reason)
            if _retry_depth >= 5:
                self._emit(f"❌ [{tag}] 刷新重连重试超过 5 次，停止重试：{reason}", "ERROR")
                return False
            self._emit(f"🔄 [{tag}] {reason}，刷新重连后重新执行整轮（第 {_retry_depth + 1}/5 次）", "WARN")
            return self.run_psychic_exp_purple_mode(
                total_pages,
                use_foreground,
                warehouse_category_key=warehouse_category_key,
                exp_value=exp_value,
                mode_label=mode_label,
                _retry_depth=_retry_depth + 1,
            )

        if self._psychic_exp_clipboard_set_text(exp_value):
            self._emit(f"📋 [{tag}] 已预置剪贴板：{exp_value}", "INFO")
        else:
            self._emit(f"⚠️ [{tag}] 预置剪贴板失败，后续仍会尝试后台输入", "WARN")
        if not window_manager.find_window():
            return retry_whole("未检测到游戏窗口")
        if not self._psychic_exp_prepare_refresh_login(use_foreground, log_tag=tag):
            return retry_whole("刷新登录前置失败")

        self._emit(f"🖱️ [{tag}] 基地门控后点击中间 → 右边", "INFO")
        if not self._click_region_safe(regions, "刷新.基地", use_foreground):
            return retry_whole("点击刷新.基地失败")
        time.sleep(0.3)
        if not self._click_region_safe(regions, "刷新.基地右侧", use_foreground):
            return retry_whole("点击刷新.基地右侧失败")
        time.sleep(0.3)

        if not self._new_daily_clear_backpack(use_foreground, log_tag=tag):
            return retry_whole("清空精灵背包失败")
        if not self._psychic_exp_open_warehouse_psychic(
            regions,
            use_foreground,
            log_tag=tag,
            category_key=warehouse_category_key,
        ):
            return retry_whole(f"打开{warehouse_category_key}失败")

        bag_count = 0
        purple_total = 0
        pages_scanned = 0
        page_no = 1
        page_turns = 0
        reached_end = False
        while not reached_end and not self._should_abort():
            self._emit(f"🔎 [{tag}] 正向扫描第 {page_no} 页（1→9）", "SYSTEM")
            slot = 1
            while slot <= 9:
                if self._should_abort():
                    return False
                key = f"精灵仓库.{slot}"
                rgb = mean_rgb_for_region_key(regions, key)
                color = self._psychic_exp_slot_color(rgb)
                self._emit(f"📋 [{tag}] {key} RGB={rgb} -> {color}", "DEBUG")
                if color != "purple":
                    slot += 1
                    continue
                self._emit(f"🟣 [{tag}] {key} 为紫色，放入背包 ({bag_count + 1}/6)", "INFO")
                if not self._psychic_exp_take_purple_slot(regions, use_foreground, slot):
                    return retry_whole(f"{key} 放入背包失败")
                bag_count += 1
                purple_total += 1
                # 取走后后续精灵会补到当前格，像放生一样原地重扫，不能递增 slot。
                if bag_count >= 6:
                    self._emit(f"📦 [{tag}] 已收集 6 个紫色，开始执行经验批次", "SYSTEM")
                    if not self._click_region_safe(regions, "精灵仓库.关闭", use_foreground):
                        return retry_whole("执行经验批次前关闭仓库失败")
                    time.sleep(0.5)
                    if not self._psychic_exp_run_batch(regions, use_foreground, exp_value=exp_value):
                        if getattr(self, "_psychic_exp_refresh_retry_requested", False):
                            return retry_whole("经验批次失败")
                        return False
                    if not self._new_daily_clear_backpack(use_foreground, log_tag=tag):
                        return retry_whole("经验批次后清空精灵背包失败")
                    if not self._psychic_exp_open_warehouse_psychic(
                        regions,
                        use_foreground,
                        log_tag=f"{tag}·批次后续扫",
                        category_key=warehouse_category_key,
                        initialize_warehouse=False,
                    ):
                        return retry_whole(f"经验批次后重开{warehouse_category_key}失败")
                    bag_count = 0
            pages_scanned += 1
            right_state, right_detail = self._warehouse_nav_state(regions, "精灵仓库.右")
            if right_state == "end":
                reached_end = True
                self._emit(f"✅ [{tag}] 已正向扫描到最后一页；{right_detail}", "SUCCESS")
                break
            if right_state != "available":
                return retry_whole(f"仓库右翻按钮状态异常：{right_detail}")
            if page_turns >= WAREHOUSE_PAGE_TURN_MAX_COUNT:
                return retry_whole(
                    f"仓库右翻达到上限 {WAREHOUSE_PAGE_TURN_MAX_COUNT} 次仍未到末页"
                )
            self._emit(f"➡️ [{tag}] 当前页扫完，右翻继续正向扫描；{right_detail}", "INFO")
            if not self._click_region_safe(regions, "精灵仓库.右", use_foreground):
                return retry_whole("仓库右翻失败")
            page_turns += 1
            time.sleep(0.45)
            page_no += 1

        if bag_count > 0 and not self._should_abort():
            self._emit(f"📦 [{tag}] 已到头，剩余 {bag_count} 只紫色未满 6，执行尾批经验", "SYSTEM")
            if not self._click_region_safe(regions, "精灵仓库.关闭", use_foreground):
                return retry_whole("尾批执行前关闭仓库失败")
            time.sleep(0.5)
            if not self._psychic_exp_run_batch(
                regions,
                use_foreground,
                batch_count=bag_count,
                exp_value=exp_value,
            ):
                if getattr(self, "_psychic_exp_refresh_retry_requested", False):
                    return retry_whole("尾批经验失败")
                return False
            bag_count = 0

        self._emit(
            f"✅ [{tag}] 扫描结束：完成页扫描 {pages_scanned} 次，紫色总数={purple_total}，剩余未满批={bag_count}",
            "SUCCESS",
        )
        return True

    def _hatch_exp_prepare_refresh_login(self, use_foreground: bool, *, log_tag: str) -> bool:
        try:
            from core.swf_resource_ops import ensure_newnpc_multi_4_to_4_og

            ok_npc, msg_npc = ensure_newnpc_multi_4_to_4_og()
            self._emit(
                f"{'✅' if ok_npc else '⚠️'} [{log_tag}] newNpc/multi: {msg_npc}",
                "INFO" if ok_npc else "WARN",
            )
        except Exception as e:
            self._emit(f"❌ [{log_tag}] newNpc/multi 前置异常：{e}", "ERROR")
            return False

        clear_tmp = getattr(self.bot, "_clear_game_tmp_cache", None)
        if callable(clear_tmp):
            clear_tmp(log_tag=log_tag)

        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner，无法刷新重连", "ERROR")
            return False
        stop_event = self._new_daily_stop_event()
        step1 = getattr(drr, "_step1_trinity_clicks_and_wait_login_swf", None)
        step2 = getattr(drr, "_run_full_refresh_login_pipeline", None)
        if not callable(step1) or not callable(step2):
            self._emit(f"❌ [{log_tag}] dar_route_runner 缺少刷新登录管线", "ERROR")
            return False
        res = step1(use_foreground, log_tag, stop_event)
        if res != "login":
            self._emit(f"❌ [{log_tag}] 刷新重连 Step1 未进入 Login.swf：{res}", "ERROR")
            return False
        return bool(
            step2(
                use_foreground,
                stop_event,
                log_tag,
                skip_molecule_converter=True,
            )
        )

    @staticmethod
    def _pick_pet_exp_slot_color(rgb: Optional[Tuple[int, int, int]]) -> str:
        if rgb is None:
            return "unknown"
        r, g, b = rgb
        if r >= 245 and g >= 245 and b >= 245:
            return "white"
        if r >= 200 and 70 <= g <= 200 and b <= 110:
            return "orange"
        # 兼容新显示器高亮青色的红通道偏移（实测最高约195）。
        if b >= 145 and g >= 120 and r <= 205:
            return "cyan"
        if r >= 95 and b >= 120 and g <= 145 and (b - g) >= 25:
            return "purple"
        return "unknown"

    def _pick_pet_exp_warehouse_tail_color_ready(self, regions) -> Tuple[bool, str]:
        wanted = {"orange", "purple", "cyan"}
        states = []
        for slot in (7, 8, 9):
            key = f"精灵仓库.{slot}"
            rgb = mean_rgb_for_region_key(regions, key)
            color = self._pick_pet_exp_slot_color(rgb)
            states.append(f"{key} RGB={rgb}->{color}")
            if color in wanted:
                return True, "; ".join(states)
        return False, "; ".join(states)

    def _pick_pet_exp_click_all_until_tail_color_ready(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
        timeout_s: float = 8.0,
    ) -> bool:
        for key in ("精灵仓库.ALL", "精灵仓库.右"):
            if not regions.get(key):
                self._emit(f"❌ [{log_tag}] 缺少区域：{key}", "ERROR")
                return False
        t0 = time.time()
        attempts = 0
        last_log = 0.0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            ready, state = self._warehouse_right_available_state(regions)
            if ready:
                self._emit(f"✅ [{log_tag}] 精灵仓库.ALL 已生效：精灵仓库.右已亮灰；{state}", "SUCCESS")
                return True
            attempts += 1
            if time.time() - last_log >= 1.0:
                self._emit(f"⏳ [{log_tag}] 点击 精灵仓库.ALL 等待 精灵仓库.右亮灰：{state}，尝试 {attempts}", "DEBUG")
                last_log = time.time()
            if not self._click_region_safe(regions, "精灵仓库.ALL", use_foreground):
                return False
            time.sleep(0.25)
        _ready, state = self._warehouse_right_available_state(regions)
        self._emit(f"❌ [{log_tag}] 点击 精灵仓库.ALL 超时：{state}", "ERROR")
        return False

    def _pick_pet_exp_take_warehouse_slot(
        self,
        regions,
        use_foreground: bool,
        slot: int,
        *,
        log_tag: str,
    ) -> bool:
        if not self._click_region_safe(regions, f"精灵仓库.{slot}", use_foreground):
            return False
        time.sleep(0.15)
        if not self._click_region_safe(regions, "精灵仓库.放入背包", use_foreground):
            return False
        return self._wait_1and1_clear(
            regions,
            use_foreground,
            timeout_s=20.0,
            min_confirm_clicks=1,
            log_tag=f"{log_tag}·放入背包",
        )

    def _hatch_exp_take_first_category_color_forward(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
        category_key: str = "精灵仓库.超能系",
        target_color: str = "cyan",
        max_pages: int = 999,
    ) -> bool:
        # 首页不计翻页；最多允许 300 次右翻，因此最多扫描 301 页。
        max_pages = max(
            1,
            min(
                WAREHOUSE_PAGE_TURN_MAX_COUNT + 1,
                int(max_pages or (WAREHOUSE_PAGE_TURN_MAX_COUNT + 1)),
            ),
        )
        target_color = str(target_color or "cyan").strip().lower()
        if regions.get("精灵仓库.单属性"):
            if not self._click_region_safe(regions, "精灵仓库.单属性", use_foreground):
                return False
            time.sleep(0.2)
        if regions.get("精灵仓库.ALL"):
            if not self._pick_pet_exp_click_all_until_tail_color_ready(regions, use_foreground, log_tag=log_tag):
                return False
            time.sleep(0.2)
        self._emit(f"📂 [{log_tag}] 切换仓库分类：{category_key}", "INFO")
        if not self._click_region_safe(regions, category_key, use_foreground):
            return False
        time.sleep(0.35)
        self._emit(f"⬅️ [{log_tag}] 回到{category_key}第一页", "INFO")
        for _ in range(12):
            if self._should_abort():
                return False
            if not self._click_region_safe(regions, "精灵仓库.左", use_foreground):
                return False
            time.sleep(0.04)
        time.sleep(0.35)
        for page in range(1, max_pages + 1):
            for slot in range(1, 10):
                if self._should_abort():
                    return False
                key = f"精灵仓库.{slot}"
                rgb = mean_rgb_for_region_key(regions, key)
                color = self._pick_pet_exp_slot_color(rgb)
                self._emit(f"📋 [{log_tag}] 第{page}页正扫 {key} RGB={rgb} -> {color}", "DEBUG")
                if color != target_color:
                    continue
                self._emit(f"🔵 [{log_tag}] 第{page}页第一个{target_color}={key}，放入背包", "INFO")
                return self._pick_pet_exp_take_warehouse_slot(regions, use_foreground, slot, log_tag=log_tag)
            if page >= max_pages:
                break
            self._emit(f"➡️ [{log_tag}] 第{page}页未找到{target_color}，右翻到下一页继续", "INFO")
            if not self._click_region_safe(regions, "精灵仓库.右", use_foreground):
                return False
            time.sleep(0.45)
        self._emit(f"❌ [{log_tag}] {category_key} 从第一页正向扫描 {max_pages} 页仍未找到{target_color}", "ERROR")
        return False

    def _pick_pet_exp_type_text(self, text: str, *, log_tag: str) -> bool:
        try:
            from pynput.keyboard import Controller, Key

            kb = Controller()
            kb.press(Key.ctrl)
            kb.press("a")
            kb.release("a")
            kb.release(Key.ctrl)
            time.sleep(0.05)
            kb.type(str(text))
            return True
        except Exception as e:
            self._emit(f"❌ [{log_tag}] 键盘输入失败：{e}", "ERROR")
            return False

    def _pick_pet_exp_clipboard_set_text(self, text: str, *, log_tag: str) -> bool:
        try:
            import tkinter as tk

            root = tk.Tk()
            root.withdraw()
            root.clipboard_clear()
            root.clipboard_append(str(text))
            root.update()
            root.destroy()
            return True
        except Exception as e:
            self._emit(f"⚠️ [{log_tag}] 写入剪贴板失败：{e}", "WARN")
            return False

    def _pick_pet_exp_post_key_to_game(self, vk_code: int, *, hold_s: float = 0.08, log_tag: str) -> bool:
        try:
            import win32con
            import win32gui

            if not window_manager.find_window() or not getattr(window_manager, "hwnd", None):
                self._emit(f"❌ [{log_tag}] 后台键盘输入失败：未找到游戏窗口", "ERROR")
                return False
            hwnd = window_manager.hwnd
            win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, int(vk_code), 1)
            time.sleep(hold_s)
            win32gui.PostMessage(hwnd, win32con.WM_KEYUP, int(vk_code), 1)
            time.sleep(hold_s)
            return True
        except Exception as e:
            self._emit(f"❌ [{log_tag}] 后台键盘输入失败：{e}", "ERROR")
            return False

    def _pick_pet_exp_post_hotkey_to_game(self, *vk_codes: int, hold_s: float = 0.05, log_tag: str) -> bool:
        try:
            import win32con
            import win32gui

            if not window_manager.find_window() or not getattr(window_manager, "hwnd", None):
                self._emit(f"❌ [{log_tag}] 后台快捷键失败：未找到游戏窗口", "ERROR")
                return False
            hwnd = window_manager.hwnd
            modifiers = {
                win32con.VK_CONTROL,
                win32con.VK_LCONTROL,
                win32con.VK_RCONTROL,
                win32con.VK_SHIFT,
                win32con.VK_LSHIFT,
                win32con.VK_RSHIFT,
                win32con.VK_MENU,
                win32con.VK_LMENU,
                win32con.VK_RMENU,
            }
            mods = [int(vk) for vk in vk_codes if int(vk) in modifiers]
            keys = [int(vk) for vk in vk_codes if int(vk) not in modifiers]
            for vk in mods:
                win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, 0)
                time.sleep(hold_s)
            for vk in keys:
                win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, 0)
                time.sleep(hold_s)
                win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk, 0)
                time.sleep(hold_s)
            for vk in reversed(mods):
                win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk, 0)
                time.sleep(hold_s)
            return True
        except Exception as e:
            self._emit(f"❌ [{log_tag}] 后台快捷键失败：{e}", "ERROR")
            return False

    def _pick_pet_exp_post_char_to_game(self, char_code: int, *, hold_s: float = 0.08, log_tag: str) -> bool:
        try:
            import win32gui

            if not window_manager.find_window() or not getattr(window_manager, "hwnd", None):
                self._emit(f"❌ [{log_tag}] 后台字符输入失败：未找到游戏窗口", "ERROR")
                return False
            win32gui.PostMessage(window_manager.hwnd, 0x0102, int(char_code), 1)
            time.sleep(hold_s)
            return True
        except Exception as e:
            self._emit(f"❌ [{log_tag}] 后台字符输入失败：{e}", "ERROR")
            return False

    def _pick_pet_exp_input_background(self, text: str, *, log_tag: str) -> bool:
        try:
            target = str(text)
            for ch in target:
                if self._should_abort():
                    return False
                if not self._pick_pet_exp_post_char_to_game(ord(ch), hold_s=0.01, log_tag=log_tag):
                    return False
                time.sleep(0.015)
            time.sleep(0.12)
            self._emit(f"✅ [{log_tag}] 已简单输入 {target}", "SUCCESS")
            return True
        except Exception as e:
            self._emit(f"❌ [{log_tag}] 简单输入异常：{e}", "ERROR")
            return False

    def _pick_pet_exp_clear_focused_input(self, use_foreground: bool, *, log_tag: str) -> bool:
        try:
            if use_foreground:
                from pynput.keyboard import Controller, Key

                kb = Controller()
                kb.press(Key.ctrl)
                kb.press("a")
                kb.release("a")
                kb.release(Key.ctrl)
                time.sleep(0.05)
                kb.press(Key.backspace)
                kb.release(Key.backspace)
                time.sleep(0.08)
                return True

            import win32con

            if not self._pick_pet_exp_post_hotkey_to_game(win32con.VK_CONTROL, ord("A"), hold_s=0.03, log_tag=log_tag):
                return False
            time.sleep(0.05)
            if not self._pick_pet_exp_post_key_to_game(win32con.VK_BACK, hold_s=0.03, log_tag=log_tag):
                return False
            time.sleep(0.08)
            return True
        except Exception as e:
            self._emit(f"❌ [{log_tag}] 清空经验输入框失败：{e}", "ERROR")
            return False

    def _pick_pet_exp_probe_is_white(self, regions, key: str) -> bool:
        rgb = mean_rgb_for_region_key(regions, key)
        if rgb is None:
            return False
        r, g, b = rgb
        return r >= 245 and g >= 245 and b >= 245

    def _pick_pet_exp_probe_is_pure_white(self, regions, key: str) -> bool:
        rgb = mean_rgb_for_region_key(regions, key)
        if rgb is None:
            return False
        r, g, b = rgb
        return r >= 250 and g >= 250 and b >= 250 and (max(rgb) - min(rgb)) <= 5

    def _pick_pet_exp_probe_is_blue(self, regions, key: str) -> bool:
        rgb = mean_rgb_for_region_key(regions, key)
        if rgb is None:
            return False
        return not self._pick_pet_exp_probe_is_pure_white(regions, key)

    def _pick_pet_exp_probe_state(self, regions, key: str) -> str:
        if self._pick_pet_exp_probe_is_pure_white(regions, key):
            return "white"
        if self._pick_pet_exp_probe_is_blue(regions, key):
            return f"blue_or_non_white({mean_rgb_for_region_key(regions, key)})"
        return f"other({mean_rgb_for_region_key(regions, key)})"

    def _pick_pet_exp_request_refresh_retry(self, reason: str, *, log_tag: str) -> None:
        self._pick_pet_exp_refresh_retry_requested = True
        self._emit(f"🔄 [{log_tag}] {reason}，标记刷新重连后重新执行", "WARN")

    def _pick_pet_exp_fail_refresh_retry(self, reason: str, *, log_tag: str) -> bool:
        if not self._should_abort():
            self._pick_pet_exp_request_refresh_retry(reason, log_tag=log_tag)
        return False

    def _pick_pet_exp_open_machine_dog_panel(self, regions, use_foreground: bool, *, log_tag: str) -> bool:
        from core.logger import kernel_cursor

        key = "经验.nono区域"
        self._emit(f"🖱️ [{log_tag}] 点击经验.nono区域，等待 MachineDogPanel.swf", "INFO")
        start_cursor = kernel_cursor()
        if not self._click_region_safe(regions, key, use_foreground):
            return self._pick_pet_exp_fail_refresh_retry("点击经验.nono区域失败", log_tag=log_tag)
        if self._wait_kernel_line_matches(
            PICK_PET_EXP_MACHINE_DOG_PANEL_RE,
            log_tag=f"{log_tag}·nono",
            timeout_s=3.0,
            success_msg=f"✅ [{log_tag}] 已检测到 MachineDogPanel.swf",
            start_cursor=start_cursor,
        ):
            return True
        self._emit(f"⚠️ [{log_tag}] 3s 内未检测到 MachineDogPanel.swf，补点一次经验.nono区域", "WARN")
        start_cursor = kernel_cursor()
        if not self._click_region_safe(regions, key, use_foreground):
            return self._pick_pet_exp_fail_refresh_retry("补点经验.nono区域失败", log_tag=log_tag)
        if self._wait_kernel_line_matches(
            PICK_PET_EXP_MACHINE_DOG_PANEL_RE,
            log_tag=f"{log_tag}·nono补点",
            timeout_s=3.0,
            success_msg=f"✅ [{log_tag}] 补点后已检测到 MachineDogPanel.swf",
            start_cursor=start_cursor,
        ):
            return True
        self._pick_pet_exp_request_refresh_retry("MachineDogPanel.swf 检测失败", log_tag=log_tag)
        return False

    def _pick_pet_exp_wait_exp_panel_after_script(self, *, start_cursor: Optional[int], log_tag: str) -> bool:
        if self._wait_kernel_line_matches(
            PICK_PET_EXP_ADM_PANEL_RE,
            log_tag=f"{log_tag}·经验面板",
            timeout_s=3.0,
            success_msg=f"✅ [{log_tag}] 已检测到 ExpAdmPanel.swf",
            start_cursor=start_cursor,
        ):
            return True
        self._pick_pet_exp_request_refresh_retry("ExpAdmPanel.swf 检测失败", log_tag=log_tag)
        return False

    def _pick_pet_exp_seed_value_once(
        self,
        regions,
        use_foreground: bool,
        *,
        exp_value: str,
        log_tag: str,
    ) -> bool:
        target_text = str(exp_value or "5820")
        self._emit(f"🧪 [{log_tag}] 初始化经验输入：{target_text}", "INFO")
        for attempt in range(1, 4):
            if self._should_abort():
                return False
            self._emit(f"🧪 [{log_tag}] 聚焦经验输入并发送 {target_text}（尝试 {attempt}/3）", "INFO")
            if not self._click_region_safe(regions, "经验.输入", use_foreground):
                return False
            time.sleep(0.80)
            if not self._pick_pet_exp_clear_focused_input(use_foreground, log_tag=log_tag):
                time.sleep(0.5)
                continue
            ok_input = (
                self._pick_pet_exp_type_text(target_text, log_tag=log_tag)
                if use_foreground
                else self._pick_pet_exp_input_background(target_text, log_tag=log_tag)
            )
            if ok_input:
                self._emit(f"✅ [{log_tag}] {target_text} 输入完成", "SUCCESS")
                return True
            time.sleep(0.5)
        self._pick_pet_exp_request_refresh_retry(f"经验输入阶段无法输入 {target_text}", log_tag=log_tag)
        return False

    def _pick_pet_exp_wait_white_blue_white_ready(
        self, regions, use_foreground: bool, *, log_tag: str, timeout_s: float = 12.0
    ) -> bool:
        t0 = time.time()
        did_5s_reclick = False
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            dialog_general_white = self._pick_pet_exp_probe_is_white(regions, "对话框.通用探针")
            dialog_confirm_blue = self._pick_pet_exp_probe_is_blue(regions, "对话框.经验确认")
            exp_general_white = self._pick_pet_exp_probe_is_white(regions, "经验.通用探针")
            if dialog_general_white and dialog_confirm_blue and exp_general_white:
                self._emit(f"✅ [{log_tag}] 已检测到白+蓝+白（三探针就绪）", "SUCCESS")
                return True
            if (not did_5s_reclick) and (time.time() - t0 >= 5.0):
                self._emit(f"⚠️ [{log_tag}] 5s 内未出现白+蓝+白，补点一次经验.确认", "WARN")
                if not self._click_region_safe(regions, "经验.确认", use_foreground):
                    return False
                did_5s_reclick = True
            time.sleep(0.05)
        self._emit(
            f"❌ [{log_tag}] 未在 {timeout_s:.0f}s 内检测到白+蓝+白"
            f"（对话框通用={self._pick_pet_exp_probe_state(regions, '对话框.通用探针')}，"
            f"对话框经验确认={self._pick_pet_exp_probe_state(regions, '对话框.经验确认')}，"
            f"经验通用={self._pick_pet_exp_probe_state(regions, '经验.通用探针')}）",
            "ERROR",
        )
        return False

    def _pick_pet_exp_click_exp_confirm_until_white_white(
        self, regions, use_foreground: bool, *, log_tag: str, timeout_s: float = 12.0
    ) -> bool:
        t0 = time.time()
        last_click = 0.0
        clicked = 0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            general_white = self._pick_pet_exp_probe_is_white(regions, "对话框.通用探针")
            confirm_white = self._pick_pet_exp_probe_is_white(regions, "对话框.经验确认")
            if general_white and confirm_white:
                self._emit(f"✅ [{log_tag}] 经验确认推进到白+白（点击 {clicked} 次）", "SUCCESS")
                return True
            now = time.time()
            if general_white and now - last_click >= 0.12:
                if not self._click_region_safe(regions, "对话框.经验确认", use_foreground):
                    return False
                clicked += 1
                last_click = now
            time.sleep(0.05)
        self._emit(f"❌ [{log_tag}] 经验确认未变成白+白", "ERROR")
        return False

    def _pick_pet_exp_wait_normal_confirm_probe_white(
        self, regions, use_foreground: bool, *, log_tag: str, timeout_s: float = 20.0
    ) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            if self._pick_pet_exp_probe_is_pure_white(regions, "经验.普通确认按钮探针"):
                self._emit(f"✅ [{log_tag}] 经验.普通确认按钮探针已变纯白", "SUCCESS")
                return True
            self._wait_1and1_clear(
                regions,
                use_foreground,
                timeout_s=2.0,
                min_confirm_clicks=1,
                log_tag=f"{log_tag}·经验确认后1AND1",
            )
            time.sleep(0.08)
        self._emit(f"❌ [{log_tag}] 1AND1 后普通确认按钮探针未变纯白", "ERROR")
        return False

    def _pick_pet_exp_click_exp_confirm_sweep(self, regions, use_foreground: bool, click_index: int, *, log_tag: str) -> bool:
        start_x = 545
        y = 511
        end_x = 595
        try:
            reg = regions.get("经验.确认")
            if reg:
                gx, _gy = reg.sample_click_point()
                end_x = max(start_x, int(round(gx)))
            positions = list(range(start_x, end_x + 1, 5)) or [start_x]
            x = positions[int(click_index) % len(positions)]
            if use_foreground:
                window_manager.click(x, y)
            else:
                window_manager.click_background(x, y)
            return True
        except Exception as e:
            self._emit(f"❌ [{log_tag}] 横扫点击经验确认失败：{e}", "ERROR")
            return False

    def _pick_pet_exp_click_confirm_until_skill_cancel_blue(
        self, regions, use_foreground: bool, *, log_tag: str, timeout_s: float = 30.0
    ) -> bool:
        t0 = time.time()
        click_stop = threading.Event()
        click_failed = threading.Event()
        click_count = {"value": 0}

        def click_worker() -> None:
            idx = 0
            while not click_stop.is_set() and not self._should_abort():
                if not self._pick_pet_exp_click_exp_confirm_sweep(regions, use_foreground, idx, log_tag=log_tag):
                    click_failed.set()
                    return
                idx += 1
                click_count["value"] = idx
                click_stop.wait(0.04)

        worker = threading.Thread(target=click_worker, name="pick-pet-exp-confirm-sweep", daemon=True)
        worker.start()
        try:
            while time.time() - t0 < timeout_s:
                if self._should_abort() or click_failed.is_set():
                    return False
                if self._pick_pet_exp_probe_is_blue(regions, "经验.技能取消"):
                    self._emit(f"✅ [{log_tag}] 经验.技能取消已变非纯白（横扫点击 {click_count['value']} 次）", "SUCCESS")
                    return True
                time.sleep(0.05)
            self._emit(f"❌ [{log_tag}] 点击经验确认后技能取消未变非纯白", "ERROR")
            return False
        finally:
            click_stop.set()
            worker.join(timeout=0.5)

    def _pick_pet_exp_click_skill_cancel_until_white_probe_blue(
        self, regions, use_foreground: bool, *, log_tag: str, timeout_s: float = 30.0
    ) -> bool:
        t0 = time.time()
        clicked = 0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            if self._pick_pet_exp_probe_is_blue(regions, "经验.白色探针"):
                self._emit(f"✅ [{log_tag}] 经验.白色探针已变非纯白（技能取消点击 {clicked} 次）", "SUCCESS")
                return True
            if not self._click_region_safe(regions, "经验.技能取消", use_foreground):
                return False
            clicked += 1
            time.sleep(0.12)
        self._emit(f"❌ [{log_tag}] 技能取消后经验.白色探针未变非纯白", "ERROR")
        return False

    def _pick_pet_exp_apply_one_slot(self, regions, use_foreground: bool, slot: int, *, log_tag: str) -> bool:
        if not self._click_region_safe(regions, f"经验.{slot}", use_foreground):
            return self._pick_pet_exp_fail_refresh_retry(f"点击经验.{slot}失败", log_tag=log_tag)
        time.sleep(0.15)
        self._emit(f"📜 [{log_tag}] 精灵{slot} 执行独立经验流程", "INFO")
        if not self._click_region_safe(regions, "经验.确认", use_foreground):
            return self._pick_pet_exp_fail_refresh_retry(f"精灵{slot}点击经验.确认失败", log_tag=log_tag)
        time.sleep(0.12)
        if not self._pick_pet_exp_wait_white_blue_white_ready(regions, use_foreground, log_tag=log_tag):
            return self._pick_pet_exp_fail_refresh_retry(f"精灵{slot}白+蓝+白就绪失败", log_tag=log_tag)
        if not self._pick_pet_exp_click_exp_confirm_until_white_white(regions, use_foreground, log_tag=log_tag):
            return self._pick_pet_exp_fail_refresh_retry(f"精灵{slot}白+白推进失败", log_tag=log_tag)
        if not self._pick_pet_exp_wait_normal_confirm_probe_white(regions, use_foreground, log_tag=log_tag):
            return self._pick_pet_exp_fail_refresh_retry(f"精灵{slot}普通确认按钮探针纯白失败", log_tag=log_tag)
        if not self._pick_pet_exp_click_confirm_until_skill_cancel_blue(regions, use_foreground, log_tag=log_tag):
            return self._pick_pet_exp_fail_refresh_retry(f"精灵{slot}技能取消变非纯白失败", log_tag=log_tag)
        if not self._pick_pet_exp_click_skill_cancel_until_white_probe_blue(regions, use_foreground, log_tag=log_tag):
            return self._pick_pet_exp_fail_refresh_retry(f"精灵{slot}白色探针变非纯白失败", log_tag=log_tag)
        return True

    def _pick_pet_exp_run_slots(
        self,
        regions,
        use_foreground: bool,
        slots: Optional[Sequence[int]] = None,
        *,
        exp_value: str = "5820",
        log_tag: str = "pick精灵经验",
        target_color: Optional[str] = None,
    ) -> bool:
        valid_slots = [max(1, min(6, int(slot))) for slot in (slots or ())]
        target_color = str(target_color or "").strip().lower() or None
        if not valid_slots and not target_color:
            self._emit(f"❌ [{log_tag}] 未指定经验槽位", "ERROR")
            return False
        exp_script_path = self._resolve_script_path("经验")
        if not exp_script_path:
            self._emit(f"❌ [{log_tag}] 找不到 fix_script/经验.json", "ERROR")
            return False
        if not self._pick_pet_exp_open_machine_dog_panel(regions, use_foreground, log_tag=log_tag):
            return False
        self._emit(f"📜 [{log_tag}] 执行经验入口脚本：{os.path.basename(exp_script_path)}", "SYSTEM")
        from core.logger import kernel_cursor

        exp_panel_cursor = kernel_cursor()
        if not self.run_script(exp_script_path, bg_override=None):
            return self._pick_pet_exp_fail_refresh_retry("经验入口脚本执行失败", log_tag=log_tag)
        if not self._pick_pet_exp_wait_exp_panel_after_script(start_cursor=exp_panel_cursor, log_tag=log_tag):
            return False
        time.sleep(0.5)
        if target_color:
            found_slot = self._pick_pet_exp_find_panel_slot_by_color(
                regions,
                target_color,
                log_tag=log_tag,
            )
            if not found_slot:
                return self._pick_pet_exp_fail_refresh_retry(f"经验面板未找到{target_color}槽位", log_tag=log_tag)
            valid_slots = [found_slot]
        first_slot = valid_slots[0]
        if not self._click_region_safe(regions, f"经验.{first_slot}", use_foreground):
            return self._pick_pet_exp_fail_refresh_retry(f"进入经验面板后点击经验.{first_slot}失败", log_tag=log_tag)
        time.sleep(0.15)
        if not self._pick_pet_exp_seed_value_once(regions, use_foreground, exp_value=exp_value, log_tag=log_tag):
            return False
        time.sleep(0.5)
        for slot in valid_slots:
            if self._should_abort():
                return False
            if not self._pick_pet_exp_apply_one_slot(regions, use_foreground, slot, log_tag=log_tag):
                return False
            time.sleep(0.15)
        if not self._click_region_safe(regions, "经验.关闭", use_foreground):
            return self._pick_pet_exp_fail_refresh_retry("经验槽位结束点击经验.关闭失败", log_tag=log_tag)
        time.sleep(0.4)
        return True

    def _pick_pet_exp_find_panel_slot_by_color(
        self,
        regions,
        target_color: str,
        *,
        log_tag: str,
        timeout_s: float = 8.0,
    ) -> Optional[int]:
        target_color = str(target_color or "").strip().lower()
        t0 = time.time()
        attempt = 0
        last_state = ""
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return None
            attempt += 1
            slots_by_color: Dict[str, List[int]] = {}
            snapshots = []
            for slot in range(1, 7):
                key = f"经验.{slot}"
                rgb = mean_rgb_for_region_key(regions, key)
                color = self._pick_pet_exp_slot_color(rgb)
                slots_by_color.setdefault(color, []).append(slot)
                snapshots.append(f"{key}=RGB{rgb}->{color}")
            last_state = "; ".join(snapshots)
            candidates = slots_by_color.get(target_color) or []
            if candidates:
                self._emit(
                    f"✅ [{log_tag}] 经验面板找到{target_color}槽位：经验.{candidates[0]}；{last_state}",
                    "SUCCESS",
                )
                return int(candidates[0])
            if attempt <= 3 or attempt % 5 == 0:
                self._emit(
                    f"⏳ [{log_tag}] 扫描经验面板1-6等待{target_color}；{last_state}",
                    "DEBUG",
                )
            time.sleep(0.2)
        self._emit(f"❌ [{log_tag}] 经验面板未找到{target_color}；最后={last_state}", "ERROR")
        return None

    def _collection_daily_open_bag_wait_slot1_orange(
        self,
        regions,
        use_foreground: bool,
        stop_event: threading.Event,
        *,
        log_tag: str,
        timeout_s: float = 7.0,
    ) -> bool:
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner，无法扫描背包", "ERROR")
            return False
        bag_open_key = "精灵背包.打开精灵背包"
        bag_open_btn_key = "精灵背包.打开精灵背包按钮"
        try:
            try:
                drr._click_region(bag_open_btn_key, use_foreground)
            except KeyError:
                drr._click_region(bag_open_key, use_foreground)
            if not drr._ensure_pet_bag_ui_ready_after_open(
                stop_event,
                use_foreground,
                bag_open_key,
                bag_open_btn_key,
                log_tag=log_tag,
            ):
                self._emit(f"ℹ️ [{log_tag}] {timeout_s:.0f}s 内未出现橙色，按背包已空处理", "INFO")
                return False
            deadline = time.time() + float(timeout_s)
            attempt = 0
            last_state = ""
            while time.time() <= deadline:
                if stop_event.is_set() or self._should_abort():
                    return False
                attempt += 1
                data = {}
                colored = []
                for pos in range(1, 7):
                    key = drr._bag_party_slot_probe_key(pos)
                    rgb = drr._mean_rgb_for_region_key(key)
                    color = drr._classify_pick_bag_slot_rgb(rgb)
                    data[pos] = (key, rgb, color)
                    if color in {"orange", "cyan", "purple"}:
                        colored.append(pos)
                last_state = "; ".join(
                    f"{pos}号({key})=RGB{rgb}->{drr._pick_bag_color_label(color)}"
                    for pos, (key, rgb, color) in data.items()
                )
                self._emit(f"🔍 [{log_tag}] 等待唯一精灵一橙色第{attempt}次：{last_state}", "DEBUG")
                slot1_color = data.get(1, ("", None, None))[2]
                if slot1_color == "orange" and colored == [1]:
                    self._emit(f"✅ [{log_tag}] 检测到唯一精灵一橙色：{last_state}", "SUCCESS")
                    return True
                time.sleep(0.25)
            self._emit(f"ℹ️ [{log_tag}] {timeout_s:.0f}s 内未等待到唯一精灵一橙色，按背包已空处理；最后={last_state}", "INFO")
            return False
        finally:
            try:
                drr._close_pet_bag_with_verify(
                    use_foreground,
                    stop_event,
                    bag_open_key,
                    bag_open_btn_key,
                    log_tag=log_tag,
                )
            except Exception:
                pass

    def _collection_daily_run_slot1_exp(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str,
        exp_value: str,
    ) -> bool:
        exp_script_path = self._resolve_script_path("经验")
        if not exp_script_path:
            self._emit(f"❌ [{log_tag}] 找不到 fix_script/经验.json", "ERROR")
            return False
        if not self._pick_pet_exp_open_machine_dog_panel(regions, use_foreground, log_tag=log_tag):
            return False
        self._emit(f"📜 [{log_tag}] 执行经验入口脚本：{os.path.basename(exp_script_path)}", "SYSTEM")
        from core.logger import kernel_cursor

        exp_panel_cursor = kernel_cursor()
        if not self.run_script(exp_script_path, bg_override=None):
            return self._pick_pet_exp_fail_refresh_retry("经验入口脚本执行失败", log_tag=log_tag)
        if not self._pick_pet_exp_wait_exp_panel_after_script(start_cursor=exp_panel_cursor, log_tag=log_tag):
            return False
        time.sleep(0.5)
        self._emit(f"🧪 [{log_tag}] 直接对经验.1输入 {exp_value}，不执行颜色判断", "SYSTEM")
        if not self._click_region_safe(regions, "经验.1", use_foreground):
            return self._pick_pet_exp_fail_refresh_retry("进入经验面板后点击经验.1失败", log_tag=log_tag)
        time.sleep(0.15)
        if not self._pick_pet_exp_seed_value_once(regions, use_foreground, exp_value=exp_value, log_tag=log_tag):
            return False
        time.sleep(0.5)
        if not self._pick_pet_exp_apply_one_slot(regions, use_foreground, 1, log_tag=log_tag):
            return False
        if not self._click_region_safe(regions, "经验.关闭", use_foreground):
            return self._pick_pet_exp_fail_refresh_retry("经验槽位结束点击经验.关闭失败", log_tag=log_tag)
        time.sleep(0.4)
        return True

    def _collection_daily_follow_first_orange_1_to_6(
        self,
        use_foreground: bool,
        stop_event: threading.Event,
        *,
        log_tag: str,
        min_slot: int = 1,
    ) -> bool:
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner，无法设置橙色跟随", "ERROR")
            return False
        bag_open_key = "精灵背包.打开精灵背包"
        bag_open_btn_key = "精灵背包.打开精灵背包按钮"
        cn = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六"}
        first_slot = min(6, max(1, int(min_slot)))
        try:
            try:
                drr._click_region(bag_open_btn_key, use_foreground)
            except KeyError:
                drr._click_region(bag_open_key, use_foreground)
            if not drr._ensure_pet_bag_ui_ready_after_open(
                stop_event,
                use_foreground,
                bag_open_key,
                bag_open_btn_key,
                log_tag=log_tag,
            ):
                return False
            last = []
            target_slot = None
            for pos in range(first_slot, 7):
                key = drr._bag_party_slot_probe_key(pos)
                rgb = drr._mean_rgb_for_region_key(key)
                color = drr._classify_pick_bag_slot_rgb(rgb)
                last.append(f"{pos}号({key})=RGB{rgb}->{drr._pick_bag_color_label(color)}")
                if color == "orange" and target_slot is None:
                    target_slot = cn[pos]
            self._emit(
                f"🔍 [{log_tag}] {first_slot}-6橙色扫描：{'; '.join(last)}",
                "INFO",
            )
            if not target_slot:
                self._emit(f"❌ [{log_tag}] {first_slot}-6未找到橙色精灵", "ERROR")
                return False
            if not drr._click_pet_with_selection_check(target_slot, use_foreground, stop_event):
                self._emit(f"❌ [{log_tag}] 精灵{target_slot}选中检测失败", "ERROR")
                return False
            drr._click_region("精灵背包.身边跟随", use_foreground)
            drr._sleep_abortable(stop_event, 0.5)
            self._emit(f"✅ [{log_tag}] 已跟随橙色精灵{target_slot}", "SUCCESS")
            return True
        except Exception as exc:
            self._emit(f"❌ [{log_tag}] 设置橙色跟随失败：{exc}", "ERROR")
            return False

    def _collection_daily_run_to_daily_1_wait_map1(
        self,
        use_foreground: bool,
        stop_event: threading.Event,
        *,
        log_tag: str,
    ) -> bool:
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner", "ERROR")
            return False
        script_path = self._resolve_script_path("to日常1")
        if not script_path:
            self._emit(f"❌ [{log_tag}] 找不到 fix_script/to日常1.json", "ERROR")
            return False
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            steps = list(data.get("steps") or [])
        except Exception as exc:
            self._emit(f"❌ [{log_tag}] 读取 to日常1 失败：{exc}", "ERROR")
            return False
        if len(steps) < 3:
            self._emit(f"❌ [{log_tag}] to日常1 步骤不足 3 步", "ERROR")
            return False
        if not window_manager.find_window():
            self._emit(f"❌ [{log_tag}] 未检测到游戏窗口", "ERROR")
            return False

        bg = not use_foreground
        window_manager.move_cancel()
        self._emit(f"📐 [{log_tag}] 执行 to日常1 前两步，第三步点击瞬间开始等待 map1+newNpc", "SYSTEM")
        try:
            for idx, step in enumerate(steps[:3], start=1):
                if self._should_abort() or stop_event.is_set():
                    window_manager.move_cancel()
                    return False
                self._wait_if_paused()
                action = str(step.get("action") or "click").strip().lower()
                if action != "click":
                    self._emit(f"❌ [{log_tag}] to日常1 第{idx}步不是 click，无法精确门控", "ERROR")
                    return False
                gx, gy = self._parse_step_xy(step)
                if gx is None or gy is None:
                    self._emit(f"❌ [{log_tag}] to日常1 第{idx}步坐标无效：{step}", "ERROR")
                    return False
                delay = max(0.0, float(step.get("delay", 0.2)))
                self._emit(f"🖱️ [{log_tag}] to日常1 第{idx}步 ({int(gx)}, {int(gy)}) delay={delay:.2f}s", "DEBUG")
                time.sleep(delay)
                if idx == 3:
                    from core.logger import kernel_cursor

                    gate_cursor = kernel_cursor()
                    if bg:
                        window_manager.click_background(gx, gy)
                    else:
                        window_manager.click(gx, gy)
                    found_map, has_newnpc = drr._wait_after_to_then_check_last_map_and_newnpc(
                        1,
                        stop_event,
                        timeout_s=45.0,
                        log_tag=f"{log_tag}·map1",
                        start_cursor=gate_cursor,
                        accepted_map_ids={1},
                        settle_s=0.0,
                        skip_reverse_check=True,
                    )
                    if int(found_map or -1) != 1 or not has_newnpc:
                        self._emit(
                            f"❌ [{log_tag}] to日常1 后 map1+newNpc 门控失败：map={found_map}, newNpc={has_newnpc}",
                            "ERROR",
                        )
                        return False
                    self._emit(f"✅ [{log_tag}] to日常1 后已检测到 map1+newNpc", "SUCCESS")
                    return True
                if bg:
                    window_manager.click_background(gx, gy)
                else:
                    window_manager.click(gx, gy)
            return False
        except Exception as exc:
            self._emit(f"❌ [{log_tag}] 执行 to日常1 异常：{exc}", "ERROR")
            return False
        finally:
            window_manager.move_cancel()

    def run_new_daily_1_1_follow_to_ocean_energy(self, use_foreground: bool = False) -> bool:
        tag = "一键新日常1/1前置"
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{tag}] 缺少 dar_route_runner", "ERROR")
            return False
        stop_event = getattr(self.bot, "_stop_event", None)
        if not isinstance(stop_event, threading.Event):
            stop_event = threading.Event()
        if not window_manager.find_window():
            self._emit(f"❌ [{tag}] 未检测到游戏窗口", "ERROR")
            return False
        if not drr.run_refresh_login_until_map(
            use_foreground,
            stop_event,
            include_base_and_map_gate=True,
        ):
            self._emit(f"❌ [{tag}] 重连/回基地门控失败", "ERROR")
            return False
        if not self._collection_daily_follow_first_orange_1_to_6(
            use_foreground,
            stop_event,
            log_tag=f"{tag}·精灵2-6橙色跟随",
            min_slot=2,
        ):
            return False
        from core.logger import kernel_cursor

        to_start_cursor = kernel_cursor()
        if not self.run_single_script("to海洋能量", bg_mode=not use_foreground):
            self._emit(f"❌ [{tag}] to海洋能量失败", "ERROR")
            return False
        last_map_id, has_newNPC = drr._wait_after_to_then_check_last_map_and_newnpc(
            20,
            stop_event,
            timeout_s=45.0,
            log_tag=f"{tag}·to海洋能量",
            start_cursor=to_start_cursor,
        )
        if last_map_id != 20 or not has_newNPC:
            self._emit(
                f"❌ [{tag}] to海洋能量后地图门控失败：map={last_map_id}，newNpc={has_newNPC}",
                "ERROR",
            )
            return False
        self._emit(f"✅ [{tag}] 已完成重连回基地门控、精灵2-6橙色跟随和to海洋能量", "SUCCESS")
        return True

    def _collection_daily_run_happy_valley_without_pre(
        self,
        use_foreground: bool,
        stop_event: threading.Event,
        *,
        log_tag: str,
        max_entry_retries: int = 20,
    ) -> bool:
        """Enter Happy Valley without repeating collection-daily pet preparation."""
        drr = getattr(self.bot, "dar_route_runner", None)
        regions = getattr(self.bot, "regions", None)
        if drr is None or regions is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner 或 regions", "ERROR")
            return False

        entry_retry_count = 0
        self._emit(f"🎡 [{log_tag}] 不跑欢乐谷前置，直接执行 to欢乐谷", "SYSTEM")
        while not self._should_abort():
            if entry_retry_count == 0:
                entered = self._happy_valley_enter_map_once(
                    use_foreground,
                    stop_event,
                    log_tag=f"{log_tag}·首次入口",
                )
            else:
                if entry_retry_count > max_entry_retries:
                    self._emit(
                        f"❌ [{log_tag}] 欢乐谷入口重连超过 {max_entry_retries} 次，停止",
                        "ERROR",
                    )
                    return False
                self._emit(
                    f"🔄 [{log_tag}] 第{entry_retry_count}/{max_entry_retries}次入口重连："
                    "刷新登录并屏蔽→跟随精灵一→to欢乐谷；不重跑日常开头、不重新取宠",
                    "WARN",
                )
                entered = self._happy_valley_refresh_reenter_phase(
                    "water",
                    use_foreground,
                    stop_event,
                    log_tag=f"{log_tag}·入口重连{entry_retry_count}",
                )
            if entered:
                break
            entry_retry_count += 1

        if self._should_abort():
            return False

        self._emit(f"✅ [{log_tag}] 已确认 map40510+NewNPC，开始小游戏流程", "SUCCESS")
        return self._run_happy_valley_phases_with_reconnect(
            use_foreground,
            stop_event,
            start_phase="water",
            log_tag=log_tag,
        )

    def _happy_valley_set_follow_for_phase(
        self,
        phase: str,
        use_foreground: bool,
        stop_event: threading.Event,
        *,
        log_tag: str,
    ) -> bool:
        """Set the companion required by one Happy Valley minigame phase."""
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            return False
        phase_key = str(phase or "").strip().lower()
        if phase_key not in {"water", "fire", "grass"}:
            self._emit(f"❌ [{log_tag}] 未知欢乐谷跟随阶段：{phase!r}", "ERROR")
            return False
        if phase_key == "water":
            return drr._pre_daily_follow_pet_one_after_daily_six_pets(
                use_foreground,
                stop_event,
                log_tag=f"{log_tag}·水阶段·橙色精灵一跟随",
            )
        follow_color = "purple" if phase_key == "fire" else "cyan"
        phase_cn = "火" if phase_key == "fire" else "草"
        return drr.set_follow_color_from_closed_bag(
            follow_color,
            use_foreground,
            stop_event,
            log_tag=f"{log_tag}·{phase_cn}阶段·{follow_color}跟随",
        )

    def _happy_valley_enter_map_once(
        self,
        use_foreground: bool,
        stop_event: threading.Event,
        *,
        log_tag: str,
    ) -> bool:
        """Run to欢乐谷 once and require map40510+NewNPC after map429."""
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            return False
        from core.logger import fetch_kernel_since, kernel_cursor

        script_start_cursor = kernel_cursor()
        if not self.run_single_script("to欢乐谷", bg_mode=not use_foreground):
            self._emit(f"⚠️ [{log_tag}] to欢乐谷脚本执行失败", "WARN")
            return False
        map429_cursor: Optional[int] = None
        try:
            rows = fetch_kernel_since(script_start_cursor, return_rows=True)
            if not isinstance(rows, list):
                rows = []
            for seq, _ts, line in reversed(rows):
                if first_map_id_in_line(str(line)) == 429:
                    map429_cursor = int(seq)
                    break
        except Exception as exc:
            self._emit(f"⚠️ [{log_tag}] 扫描 map429 异常：{exc}", "WARN")
        if map429_cursor is None:
            self._emit(f"⚠️ [{log_tag}] to欢乐谷结束后未向上扫描到 map429", "WARN")
            return False
        found_map, has_newnpc = drr._wait_after_to_then_check_last_map_and_newnpc(
            40510,
            stop_event,
            timeout_s=20.0,
            log_tag=f"{log_tag}·to欢乐谷后",
            start_cursor=map429_cursor,
            accepted_map_ids={40510},
            settle_s=0.0,
            skip_reverse_check=True,
        )
        if int(found_map or -1) != 40510 or not has_newnpc:
            self._emit(
                f"⚠️ [{log_tag}] 未检测到 map40510+NewNPC（实际map={found_map}，NewNPC={has_newnpc}）",
                "WARN",
            )
            return False
        self._emit(f"✅ [{log_tag}] 已重进 map40510+NewNPC", "SUCCESS")
        return True

    def _happy_valley_refresh_reenter_phase(
        self,
        phase: str,
        use_foreground: bool,
        stop_event: threading.Event,
        *,
        log_tag: str,
    ) -> bool:
        """Refresh, restore the phase-specific companion, then re-enter Happy Valley."""
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            return False
        if not drr.run_refresh_login_until_map(use_foreground, stop_event):
            return False
        if not self._happy_valley_set_follow_for_phase(
            phase,
            use_foreground,
            stop_event,
            log_tag=f"{log_tag}·重进前",
        ):
            return False
        return self._happy_valley_enter_map_once(
            use_foreground,
            stop_event,
            log_tag=log_tag,
        )

    def _run_happy_valley_phases_with_reconnect(
        self,
        use_foreground: bool,
        stop_event: threading.Event,
        *,
        start_phase: str,
        log_tag: str,
        max_phase_retries: int = 20,
    ) -> bool:
        """Run water/fire/grass, retrying only the failed phase after reconnect."""
        drr = getattr(self.bot, "dar_route_runner", None)
        regions = getattr(self.bot, "regions", None)
        if drr is None or regions is None:
            return False
        phase_defs = (
            ("water", "水", "小游戏水"),
            ("fire", "火", "小游戏火"),
            ("grass", "草", "小游戏草"),
        )
        phase_key = str(start_phase or "water").strip().lower()
        start_index = next(
            (idx for idx, item in enumerate(phase_defs) if item[0] == phase_key),
            None,
        )
        if start_index is None:
            self._emit(f"❌ [{log_tag}] 未知欢乐谷阶段：{start_phase!r}", "ERROR")
            return False

        needs_reentry = False
        for idx in range(start_index, len(phase_defs)):
            current_phase, phase_cn, script_name = phase_defs[idx]
            reconnect_count = 0
            while not self._should_abort():
                if needs_reentry:
                    reconnect_count += 1
                    if reconnect_count > max_phase_retries:
                        self._emit(
                            f"❌ [{log_tag}·{phase_cn}] 阶段重连超过 {max_phase_retries} 次，停止",
                            "ERROR",
                        )
                        return False
                    remaining_scripts = "→".join(
                        item[2] for item in phase_defs[idx:]
                    )
                    follow_label = {
                        "water": "橙色精灵一",
                        "fire": "紫色精灵",
                        "grass": "青色精灵",
                    }[current_phase]
                    self._emit(
                        f"🔄 [{log_tag}·{phase_cn}] 第{reconnect_count}/{max_phase_retries}次阶段重连："
                        f"刷新登录并屏蔽→跟随{follow_label}→to欢乐谷→执行{remaining_scripts}",
                        "WARN",
                    )
                    if not self._happy_valley_refresh_reenter_phase(
                        current_phase,
                        use_foreground,
                        stop_event,
                        log_tag=f"{log_tag}·{phase_cn}重连",
                    ):
                        continue

                self._emit(f"🎮 [{log_tag}] 开始{script_name}", "SYSTEM")
                if not self.run_single_script(script_name, bg_mode=not use_foreground):
                    self._emit(f"⚠️ [{log_tag}] {script_name}执行失败，重连后只重做当前阶段", "WARN")
                    needs_reentry = True
                    continue
                if not self._ensure_unified_framework(regions):
                    return False
                if not drr._dismiss_1and1_until_disappear(
                    use_foreground,
                    stop_event,
                    timeout_s=20.0,
                    log_tag=f"{log_tag}·{script_name}·清理",
                    require_seen=True,
                    require_click=True,
                ):
                    self._emit(
                        f"⚠️ [{log_tag}] {script_name}未完成至少一次1AND1清理，重连后只重做当前阶段",
                        "WARN",
                    )
                    needs_reentry = True
                    continue
                needs_reentry = False
                break
            if self._should_abort():
                return False

            next_index = idx + 1
            if next_index < len(phase_defs):
                next_phase, next_phase_cn, _next_script = phase_defs[next_index]
                if not self._happy_valley_set_follow_for_phase(
                    next_phase,
                    use_foreground,
                    stop_event,
                    log_tag=f"{log_tag}·{next_phase_cn}准备",
                ):
                    self._emit(
                        f"⚠️ [{log_tag}] {next_phase_cn}阶段跟随设置失败；重连后直接从{next_phase_cn}继续",
                        "WARN",
                    )
                    needs_reentry = True
        self._emit(f"✅ [{log_tag}] 欢乐谷水/火/草小游戏完成", "SUCCESS")
        return True

    def run_collection_daily_after_happy_valley(
        self,
        use_foreground: bool = False,
        *,
        log_tag: str = "集合日常·欢乐谷后",
    ) -> bool:
        """Continue the full daily chain after Happy Valley has completed."""
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner", "ERROR")
            self._collection_daily_tail_failure_reason = "欢乐谷后缺少 dar_route_runner"
            return False
        stop_event = getattr(self.bot, "_stop_event", None)
        if not isinstance(stop_event, threading.Event):
            stop_event = threading.Event()

        def fail(reason: str) -> bool:
            self._collection_daily_tail_failure_reason = str(reason)
            self._emit(f"❌ [{log_tag}] {reason}", "ERROR")
            return False

        self._collection_daily_tail_failure_reason = ""
        self._emit(
            f"📋 [{log_tag}] 继续完整日常："
            "2-6橙色跟随→to日常1→to日常2/海洋能量交接→方案1-9",
            "SYSTEM",
        )
        if not self._collection_daily_follow_first_orange_1_to_6(
            use_foreground,
            stop_event,
            log_tag=f"{log_tag}·to日常前2-6橙色跟随",
            min_slot=2,
        ):
            return fail("to日常前2-6橙色跟随失败")
        if not self._collection_daily_run_to_daily_1_wait_map1(
            use_foreground,
            stop_event,
            log_tag=f"{log_tag}·to日常1",
        ):
            return fail("to日常1或map1门控失败")
        if not drr.run_pre_daily_ocean_energy_handoff(use_foreground, stop_event):
            return fail("to日常2或海洋能量交接失败")
        if not self.run_new_daily_chain_1_to_9(
            use_foreground,
            skip_hero_tower=False,
            from_daily_chain=True,
            start_variant="1",
            start_step=1,
        ):
            return fail("一键日常方案1-9未完整完成")
        self._emit(f"✅ [{log_tag}] 欢乐谷后完整日常已执行到底", "SUCCESS")
        return True

    def run_collection_daily_mode(
        self,
        use_foreground: bool = False,
        *,
        skip_refresh_login: bool = False,
        skip_exp_input: bool = False,
        before_attempt: Optional[Callable[[int], bool]] = None,
    ) -> bool:
        """Run one complete collection-daily attempt and return to the caller."""
        if self._should_abort():
            return False
        if before_attempt is not None and not before_attempt(1):
            self._emit(
                "❌ [集合日常] 本轮外层前置失败，停止完整日常",
                "ERROR",
            )
            return False

        self._collection_daily_restart_reason = None
        if self._run_collection_daily_mode_once(
            use_foreground=use_foreground,
            skip_refresh_login=bool(skip_refresh_login),
            skip_exp_input=skip_exp_input,
        ):
            return True
        if self._should_abort():
            return False

        reason = str(
            getattr(self, "_collection_daily_restart_reason", "") or ""
        ).strip()
        if reason:
            self._emit(
                f"↪️ [集合日常] 本轮未完整完成，不在底层重连整套日常：{reason}",
                "WARN",
            )
        return False

    @staticmethod
    def _collection_daily_exp_plan(
        signin_count: int,
        *,
        skip_exp_input: bool,
    ) -> Tuple[bool, bool]:
        """返回（是否输入经验，取日常六宠前是否放回签到精灵）。"""
        day = int(signin_count)
        exp_day = (day == 28) or (day % 3 == 0 and day <= 27)
        return bool(exp_day and not skip_exp_input), bool(exp_day)

    def _run_collection_daily_mode_once(
        self,
        use_foreground: bool = False,
        *,
        skip_refresh_login: bool = False,
        skip_exp_input: bool = False,
    ) -> bool:
        """Run one collection-daily attempt and report restart reasons upward."""
        tag = "集合日常"
        regions = getattr(self.bot, "regions", None)
        drr = getattr(self.bot, "dar_route_runner", None)
        if regions is None or drr is None:
            self._emit(f"❌ [{tag}] 缺少 regions 或 dar_route_runner", "ERROR")
            return False
        stop_event = getattr(self.bot, "_stop_event", None)
        if not isinstance(stop_event, threading.Event):
            stop_event = threading.Event()

        def retry_whole(reason: str) -> bool:
            if self._should_abort():
                return False
            self._collection_daily_restart_reason = str(reason)
            self._emit(
                f"↩️ [{tag}] {reason}；退出本轮，交给完整日常外层重启",
                "WARN",
            )
            return False

        if not window_manager.find_window():
            return retry_whole("未检测到游戏窗口")
        self._pick_pet_exp_refresh_retry_requested = False
        if skip_refresh_login:
            self._emit(
                f"📋 [{tag}] 孵化后原地清包，不执行刷新重连；后续必接一键日常",
                "SYSTEM",
            )
        else:
            self._emit(
                f"📋 [{tag}] 开始：重连回基地中右→清包→签到→1AND1→小蜜蜂(仅第28-31次签到)→1AND1→接受任务→条件经验→日常六宠→欢乐谷→橙色跟随→to日常→一键日常",
                "SYSTEM",
            )
            if not self._hatch_exp_prepare_refresh_login(use_foreground, log_tag=tag):
                return retry_whole("刷新登录前置失败")
            if not self._click_region_safe(regions, "刷新.基地", use_foreground):
                return retry_whole("点击刷新.基地失败")
            time.sleep(0.3)
            if not self._click_region_safe(regions, "刷新.基地右侧", use_foreground):
                return retry_whole("点击刷新.基地右侧失败")
            time.sleep(0.3)
        if not drr._rotation_step2_clear_backpack(use_foreground, stop_event, log_tag=f"{tag}·首次清包"):
            return retry_whole("首次清空背包失败")

        drr._pre_daily_run_script_if_present(
            "每日签到",
            use_foreground,
            required=False,
            log_tag=f"{tag}·签到",
        )
        if not self._wait_1and1_clear(
            regions,
            use_foreground,
            log_tag=f"{tag}·签到1AND1",
        ):
            return retry_whole("签到后1AND1清理失败")
        signin_record = append_signin_record("collection_daily")
        if signin_record.get("ok"):
            counted_text = "计入次数" if signin_record.get("counted") else "同业务日重复，不重复计数"
            self._emit(
                f"🧾 [{tag}] 签到记录：业务日={signin_record.get('business_date')}，本月第{signin_record.get('signin_count')}次（{counted_text}）",
                "INFO",
            )
        else:
            self._emit(
                f"⚠️ [{tag}] 签到记录写入失败，按业务日号兜底：{signin_record.get('error')}",
                "WARN",
            )
        today_day = int(signin_record.get("signin_count") or self._business_day_6am())
        bee_awarded = has_monthly_bee_award()
        should_run_bee = (28 <= today_day <= 31) and not bee_awarded
        if should_run_bee and regions.get("日常.小蜜蜂"):
            try:
                self._emit(f"🐝 [{tag}] 本月第{today_day}次签到：点击小蜜蜂", "INFO")
                drr._click_region("日常.小蜜蜂", use_foreground)
                bee_record = append_monthly_bee_award("collection_daily")
                if bee_record.get("ok"):
                    record_text = "已写入领取记录" if bee_record.get("recorded") else "本月已有领取记录"
                    self._emit(f"🧾 [{tag}] 小蜜蜂本月奖励：{record_text}", "INFO")
                else:
                    self._emit(f"⚠️ [{tag}] 小蜜蜂领取记录写入失败：{bee_record.get('error')}", "WARN")
                drr._sleep_abortable(stop_event, 1.0)
                if not self._wait_1and1_clear(
                    regions,
                    use_foreground,
                    log_tag=f"{tag}·小蜜蜂1AND1",
                ):
                    return retry_whole("小蜜蜂后1AND1清理失败")
            except Exception as exc:
                self._emit(f"⚠️ [{tag}] 小蜜蜂点击异常：{exc}", "WARN")
        elif bee_awarded:
            self._emit(f"⏭️ [{tag}] 本月已领取过小蜜蜂奖励，跳过小蜜蜂", "INFO")
        elif should_run_bee:
            self._emit(f"⚠️ [{tag}] 本月第{today_day}次签到需要小蜜蜂，但缺少日常.小蜜蜂区域，跳过", "WARN")
        else:
            self._emit(f"⏭️ [{tag}] 本月第{today_day}次签到不在28-31次，跳过小蜜蜂", "INFO")
        if regions.get("日常.关闭签到"):
            try:
                drr._click_region("日常.关闭签到", use_foreground)
                drr._sleep_abortable(stop_event, 0.3)
            except Exception:
                pass
        if not drr._pre_daily_run_script_if_present(
            "接受任务",
            use_foreground,
            required=True,
            log_tag=f"{tag}·接受任务",
        ):
            return retry_whole("接受任务脚本失败")

        today_day = int(signin_record.get("signin_count") or self._business_day_6am())
        run_exp_input, clear_before_daily_pets = self._collection_daily_exp_plan(
            today_day,
            skip_exp_input=skip_exp_input,
        )
        if skip_exp_input:
            if clear_before_daily_pets:
                self._emit(
                    f"⏭️ [{tag}] 已勾选跳过经验输入；本月第{today_day}次仍为经验日，保留签到精灵放回仓库",
                    "INFO",
                )
            else:
                self._emit(f"⏭️ [{tag}] 已勾选跳过经验输入，签到后不执行经验流程", "INFO")
        elif run_exp_input:
            exp_value = "10000" if today_day == 28 else "6332"
            self._emit(f"🧪 [{tag}] 本月第{today_day}次签到需要签到精灵加经验，经验值={exp_value}", "SYSTEM")
            if not self._collection_daily_run_slot1_exp(
                regions,
                use_foreground,
                log_tag=f"{tag}·经验一",
                exp_value=exp_value,
            ):
                if getattr(self, "_pick_pet_exp_refresh_retry_requested", False):
                    return retry_whole("经验一加经验失败")
                return False
        else:
            self._emit(f"⏭️ [{tag}] 本月第{today_day}次签到不在加经验次数，跳过签到精灵加经验", "INFO")

        if not drr.prepare_happy_valley_daily_pets(
            use_foreground,
            stop_event,
            log_tag=f"{tag}·日常六宠",
            clear_backpack=clear_before_daily_pets,
        ):
            return retry_whole("Pick日常六宠失败")
        if not drr._pre_daily_follow_pet_one_after_daily_six_pets(
            use_foreground,
            stop_event,
            log_tag=f"{tag}·精灵一跟随",
        ):
            return retry_whole("日常六宠精灵一跟随失败")
        if not self._collection_daily_run_happy_valley_without_pre(
            use_foreground,
            stop_event,
            log_tag=f"{tag}·欢乐谷",
        ):
            return retry_whole("欢乐谷入口或阶段未完成")

        if not self.run_collection_daily_after_happy_valley(
            use_foreground,
            log_tag=f"{tag}·欢乐谷后",
        ):
            reason = str(
                getattr(self, "_collection_daily_tail_failure_reason", "")
                or "欢乐谷后半段未完成"
            )
            return retry_whole(reason)
        return True

    def run_hatch_exp_77_mode(self, use_foreground: bool = False, _retry_depth: int = 0) -> bool:
        """孵化配色+77青色：放回一个紫色，补超能第一页首个青色，只给青色输入5820经验。"""
        tag = "孵化77经验"
        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit(f"❌ [{tag}] 缺少 regions", "ERROR")
            return False
        required = (
            "刷新.基地",
            "刷新.基地右侧",
            "精灵背包.打开精灵背包",
            "精灵背包.精灵仓库",
            "精灵背包.放回仓库",
            "精灵仓库.关闭",
            "精灵仓库.ALL",
            "精灵仓库.超能系",
            "精灵仓库.左",
            "精灵仓库.右",
            "精灵仓库.放入背包",
            "经验.nono区域",
            "经验.输入",
            "经验.关闭",
            "经验.通用探针",
            "经验.白色探针",
            "经验.技能取消",
            "经验.普通确认按钮探针",
            "对话框.通用探针",
            "对话框.经验确认",
            "对话框.普通确认",
            "对话框.普通确认探针",
            "对话框.技能取消",
        )
        for key in required:
            if not regions.get(key):
                self._emit(f"❌ [{tag}] 缺少区域：{key}", "ERROR")
                return False
        for slot in range(1, 10):
            if not regions.get(f"精灵仓库.{slot}"):
                self._emit(f"❌ [{tag}] 缺少区域：精灵仓库.{slot}", "ERROR")
                return False
        for slot in range(1, 7):
            if not regions.get(f"经验.{slot}"):
                self._emit(f"❌ [{tag}] 缺少区域：经验.{slot}", "ERROR")
                return False

        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{tag}] 缺少 DarRouteRunner", "ERROR")
            return False
        stop_event = self._master_cup_stop_event()
        self._pick_pet_exp_refresh_retry_requested = False

        def retry_whole(reason: str) -> bool:
            if self._should_abort():
                return False
            self._pick_pet_exp_request_refresh_retry(reason, log_tag=tag)
            if _retry_depth >= 5:
                self._emit(f"❌ [{tag}] 刷新重连重试超过 5 次，停止重试：{reason}", "ERROR")
                return False
            self._emit(f"🔄 [{tag}] {reason}，刷新重连后重新执行整轮（第 {_retry_depth + 1}/5 次）", "WARN")
            return self.run_hatch_exp_77_mode(use_foreground=use_foreground, _retry_depth=_retry_depth + 1)

        if self._pick_pet_exp_clipboard_set_text("5820", log_tag=tag):
            self._emit(f"📋 [{tag}] 已预置剪贴板：5820", "INFO")
        if not window_manager.find_window():
            return retry_whole("未检测到游戏窗口")
        if not self._hatch_exp_prepare_refresh_login(use_foreground, log_tag=tag):
            return retry_whole("刷新登录前置失败")

        self._emit(f"🖱️ [{tag}] 基地门控后点击中间 → 右边", "INFO")
        if not self._click_region_safe(regions, "刷新.基地", use_foreground):
            return retry_whole("点击刷新.基地失败")
        time.sleep(0.3)
        if not self._click_region_safe(regions, "刷新.基地右侧", use_foreground):
            return retry_whole("点击刷新.基地右侧失败")
        time.sleep(0.3)

        if not self._master_cup_open_bag_ready(regions, use_foreground, stop_event, log_tag=tag):
            return retry_whole("打开精灵背包失败")
        scan = drr.scan_pick_bag_party_color_slots_any(
            stop_event,
            tag,
            timeout_s=10.0,
            min_cyan=0,
            min_purple=2,
        )
        if not scan.get("ok"):
            self._master_cup_close_bag(use_foreground, stop_event, log_tag=tag)
            return retry_whole("背包未识别到至少2紫")
        purple_slots = list(scan.get("purple_slots") or [])
        put_back_slot = purple_slots[0] if purple_slots else None
        if not put_back_slot:
            self._master_cup_close_bag(use_foreground, stop_event, log_tag=tag)
            return retry_whole("未找到可放回的紫色")
        self._emit(f"📦 [{tag}] 放回任意一个紫色：精灵{put_back_slot}", "INFO")
        if not drr.put_back_bag_slot_from_open_bag(
            str(put_back_slot),
            use_foreground,
            stop_event,
            tag,
            verify_hp=False,
        ):
            self._master_cup_close_bag(use_foreground, stop_event, log_tag=tag)
            return retry_whole(f"放回紫色精灵{put_back_slot}失败")

        if not drr.open_pickmode_bag_warehouse_from_ready_bag(
            use_foreground,
            stop_event,
            log_tag=f"{tag}·打开仓库",
        ):
            self._master_cup_close_bag(use_foreground, stop_event, log_tag=tag)
            return retry_whole("从背包打开精灵仓库失败")
        if not self._hatch_exp_take_first_category_color_forward(
            regions,
            use_foreground,
            log_tag=f"{tag}·取超能青",
            category_key="精灵仓库.超能系",
            target_color="cyan",
        ):
            self._master_cup_close_warehouse_keep_bag_open(use_foreground, log_tag=tag)
            self._master_cup_close_bag(use_foreground, stop_event, log_tag=tag)
            return retry_whole("取超能正向首个青色失败")
        if not self._master_cup_close_warehouse_keep_bag_open(use_foreground, log_tag=tag):
            self._master_cup_close_bag(use_foreground, stop_event, log_tag=tag)
            return retry_whole("关闭精灵仓库失败")

        self._emit(f"🔵 [{tag}] 将在经验面板重新扫描青色槽位并输入 5820", "INFO")
        self._master_cup_close_bag(use_foreground, stop_event, log_tag=tag)
        if not self._pick_pet_exp_run_slots(
            regions,
            use_foreground,
            exp_value="5820",
            log_tag=tag,
            target_color="cyan",
        ):
            if getattr(self, "_pick_pet_exp_refresh_retry_requested", False):
                return retry_whole("经验面板青色输入失败")
            return False
        self._emit(f"✅ [{tag}] 流程完成", "SUCCESS")
        return True

    @staticmethod
    def _fusion_rgb_distance(rgb: Optional[Tuple[int, int, int]], target: Tuple[int, int, int]) -> float:
        if rgb is None:
            return 999.0
        return sum((int(rgb[i]) - int(target[i])) ** 2 for i in range(3)) ** 0.5

    @classmethod
    def _fusion_is_deep_blue(cls, rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        r, g, b = rgb
        return cls._fusion_rgb_distance(rgb, (24, 73, 146)) <= 45 or (
            r <= 55 and 45 <= g <= 105 and 110 <= b <= 180 and (b - r) >= 70
        )

    @classmethod
    def _fusion_is_gray(cls, rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        r, g, b = rgb
        return cls._fusion_rgb_distance(rgb, (63, 63, 63)) <= 35 or (
            38 <= r <= 90 and 38 <= g <= 90 and 38 <= b <= 90 and max(rgb) - min(rgb) <= 28
        )

    @staticmethod
    def _fusion_is_yellow(rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        r, g, b = rgb
        return r >= 150 and g >= 120 and b <= 95 and (r - b) >= 70 and (g - b) >= 45

    @classmethod
    def _fusion_is_green_shadow(cls, rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        r, g, b = rgb
        return cls._fusion_rgb_distance(rgb, (43, 100, 56)) <= 45 or (
            25 <= r <= 75 and 80 <= g <= 135 and 35 <= b <= 90 and (g - r) >= 25
        )

    @classmethod
    def _fusion_is_dark_sub_slot(cls, rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        return cls._fusion_rgb_distance(rgb, (55, 79, 80)) <= 45

    @staticmethod
    def _fusion_is_white(rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None:
            return False
        r, g, b = rgb
        return r >= 245 and g >= 245 and b >= 245

    @classmethod
    def _fusion_is_pure_blue_probe(cls, rgb: Optional[Tuple[int, int, int]]) -> bool:
        return cls._fusion_is_blue_probe(rgb)

    @classmethod
    def _fusion_is_blue_probe(cls, rgb: Optional[Tuple[int, int, int]]) -> bool:
        if rgb is None or cls._fusion_is_white(rgb):
            return False
        r, g, b = rgb
        return cls._fusion_is_deep_blue(rgb) or (b >= 120 and (b - r) >= 20)

    def _fusion_wait_region_color(
        self,
        regions,
        key: str,
        predicate: Callable[[Optional[Tuple[int, int, int]]], bool],
        desc: str,
        *,
        timeout_s: float = 15.0,
        poll_s: float = 0.05,
        log_tag: str = "融合模式",
        use_foreground: bool = False,
        repoke_key: Optional[str] = None,
        repoke_interval_s: float = 3.0,
    ) -> bool:
        t0 = time.time()
        last_log = 0.0
        last_repoke = t0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            rgb = mean_rgb_for_region_key(regions, key)
            if predicate(rgb):
                self._emit(f"✅ [{log_tag}] {desc}：{key} RGB={rgb}", "SUCCESS")
                return True
            now = time.time()
            if repoke_key and now - last_repoke >= repoke_interval_s:
                self._emit(f"🔁 [{log_tag}] {desc} {repoke_interval_s:.0f}s 未就绪，补点 {repoke_key}", "DEBUG")
                if not self._click_region_safe(regions, repoke_key, use_foreground):
                    return False
                last_repoke = now
            if now - last_log >= 1.0:
                self._emit(f"⏳ [{log_tag}] 等待{desc}：{key} RGB={rgb}", "DEBUG")
                last_log = now
            time.sleep(poll_s)
        self._emit(
            f"❌ [{log_tag}] 等待{desc}超时：{key} RGB={mean_rgb_for_region_key(regions, key)}",
            "ERROR",
        )
        return False

    def _fusion_wait_white_after_fuse_click(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str = "融合模式",
        timeout_s: float = FUSION_FUSE_WHITE_TIMEOUT_SEC,
        repoke_interval_s: float = 3.0,
        poll_s: float = 0.05,
    ) -> bool:
        t0 = time.time()
        last_log = 0.0
        last_repoke = time.time()
        while time.time() - t0 < float(timeout_s):
            if self._should_abort():
                return False
            rgb = mean_rgb_for_region_key(regions, "融合.白色探针")
            if self._fusion_is_white(rgb):
                self._emit(
                    f"✅ [{log_tag}] 通用探针变白：融合.白色探针 RGB={rgb}",
                    "SUCCESS",
                )
                return True
            now = time.time()
            if now - last_repoke >= repoke_interval_s:
                self._emit(
                    f"🔁 [{log_tag}] 融合.白色探针 {repoke_interval_s:.0f}s 未变白，补点 融合.融合",
                    "DEBUG",
                )
                if not self._click_region_safe(regions, "融合.融合", use_foreground):
                    return False
                last_repoke = now
            if now - last_log >= 1.0:
                self._emit(
                    f"⏳ [{log_tag}] 等待通用探针变白：融合.白色探针 RGB={rgb}",
                    "DEBUG",
                )
                last_log = now
            time.sleep(poll_s)
        rgb = mean_rgb_for_region_key(regions, "融合.白色探针")
        self._emit(
            f"❌ [{log_tag}] 等待融合.白色探针变白超时({timeout_s:.0f}s)，最后RGB={rgb}",
            "ERROR",
        )
        return False

    def _fusion_ensure_pet_bag_open(self, regions, use_foreground: bool, *, log_tag: str = "融合模式") -> bool:
        if wait_pet_bag_ui_ready_after_open(
            regions,
            emit_fn=None,
            stop_check=self._should_abort,
            timeout_s=0.6,
            poll_s=0.08,
        ):
            return True
        self._emit(f"🖱️ [{log_tag}] 打开精灵背包", "INFO")
        if not self._click_region_safe(regions, "精灵背包.打开精灵背包", use_foreground):
            return False
        return wait_pet_bag_ui_ready_after_open(
            regions,
            emit_fn=self._emit,
            stop_check=self._should_abort,
            log_tag=log_tag,
            timeout_s=5.0,
        )

    def _fusion_open_warehouse_from_bag(self, regions, use_foreground: bool, *, log_tag: str = "融合模式") -> bool:
        self._emit(f"📂 [{log_tag}] 清空背包后直接点击 精灵背包.精灵仓库", "INFO")
        if not self._click_region_safe(regions, "精灵背包.精灵仓库", use_foreground):
            return False
        time.sleep(0.5)
        return True

    def _fusion_clear_backpack_keep_open(
        self, regions, use_foreground: bool, *, log_tag: str = "融合模式"
    ) -> bool:
        """清空背包但保持精灵背包打开，供后续直接点击 精灵背包.精灵仓库。"""
        self._emit(f"🔄 [{log_tag}] 清空背包（保持背包打开）", "SYSTEM")
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner，无法确认清空背包", "ERROR")
            return False
        stop_event = self._master_cup_stop_event()
        if not drr._rotation_step2_clear_backpack(
            use_foreground,
            stop_event,
            log_tag=log_tag,
            close_after=False,
        ):
            self._emit(f"❌ [{log_tag}] 清空背包未确认，停止打开仓库", "ERROR")
            return False
        self._emit(f"✅ [{log_tag}] 已逐只确认清空背包并保持打开", "SUCCESS")
        return True

    def _fusion_pick_warehouse_color(
        self,
        regions,
        use_foreground: bool,
        *,
        category_key: str,
        target_color: str,
        wanted_count: int,
        right_clicks: int,
        log_tag: str = "融合模式",
    ) -> bool:
        if regions.get("精灵仓库.单属性"):
            if not self._click_region_safe(regions, "精灵仓库.单属性", use_foreground):
                return False
            time.sleep(0.2)
        self._emit(f"📂 [{log_tag}] 点击 精灵仓库.ALL 直到 精灵仓库.右 变亮灰后再切分类", "INFO")
        if not self._warehouse_click_all_until_slot9_orange(regions, use_foreground, log_tag=log_tag):
            return False
        time.sleep(0.2)
        self._emit(f"📂 [{log_tag}] 切换仓库分类：{category_key}", "INFO")
        if not self._click_region_safe(regions, category_key, use_foreground):
            return False
        time.sleep(0.35)
        if not self._warehouse_click_right_until_end(
            regions,
            use_foreground,
            log_tag=f"{log_tag}·{category_key}定位末页",
        ):
            return False

        picked = 0
        pages_scanned = 0
        while picked < wanted_count:
            if self._should_abort():
                return False
            self._emit(
                f"🔎 [{log_tag}] {category_key} 倒扫第 {pages_scanned + 1} 页，目标={target_color}",
                "INFO",
            )
            for slot in range(9, 0, -1):
                if self._should_abort():
                    return False
                key = f"精灵仓库.{slot}"
                rgb = mean_rgb_for_region_key(regions, key)
                color = self._psychic_exp_slot_color(rgb)
                self._emit(f"📋 [{log_tag}] {key} RGB={rgb} -> {color}", "DEBUG")
                if color != target_color:
                    continue
                self._emit(
                    f"✅ [{log_tag}] {key} 命中 {target_color}，放入背包 ({picked + 1}/{wanted_count})",
                    "SUCCESS",
                )
                if not self._click_region_safe(regions, key, use_foreground):
                    return False
                time.sleep(0.12)
                if not self._click_region_safe(regions, "精灵仓库.放入背包", use_foreground):
                    return False
                if not self._wait_1and1_clear(
                    regions,
                    use_foreground,
                    timeout_s=20.0,
                    min_confirm_clicks=1,
                    log_tag=f"{log_tag}·放入背包",
                ):
                    return False
                picked += 1
                if picked >= wanted_count:
                    break
                time.sleep(0.2)
            if picked >= wanted_count:
                break
            pages_scanned += 1
            left_state, left_detail = self._warehouse_nav_state(regions, "精灵仓库.左")
            if left_state == "end":
                break
            if left_state != "available":
                self._emit(f"❌ [{log_tag}] 仓库左翻按钮状态异常：{left_detail}", "ERROR")
                return False
            if pages_scanned > WAREHOUSE_PAGE_TURN_MAX_COUNT:
                self._emit(
                    f"❌ [{log_tag}] 仓库左翻达到上限 "
                    f"{WAREHOUSE_PAGE_TURN_MAX_COUNT} 次仍未完成取宠",
                    "ERROR",
                )
                return False
            if not self._click_region_safe(regions, "精灵仓库.左", use_foreground):
                return False
            time.sleep(0.6)

        if picked < wanted_count:
            self._emit(
                f"❌ [{log_tag}] {category_key} 只找到 {picked}/{wanted_count} 个 {target_color}",
                "ERROR",
            )
            return False
        return True

    def _fusion_nav_warehouse_state(
        self,
        regions,
        use_foreground: bool,
        state: Dict[str, Any],
        *,
        current_category: Optional[str],
        current_page: int,
        initial_all: bool,
        log_tag: str = "融合模式",
    ) -> Tuple[bool, Optional[str], int]:
        category_key = str(state.get("category_key") or "")
        if initial_all:
            if regions.get("精灵仓库.单属性"):
                if not self._click_region_safe(regions, "精灵仓库.单属性", use_foreground):
                    return False, current_category, current_page
                time.sleep(0.2)
            self._emit(f"📂 [{log_tag}] 初次进入仓库：点击 精灵仓库.ALL 直到 精灵仓库.右 变亮灰后再切分类", "INFO")
            if not self._warehouse_click_all_until_slot9_orange(regions, use_foreground, log_tag=log_tag):
                return False, current_category, current_page
            time.sleep(0.2)
            current_category = None
        if current_category != category_key:
            self._emit(f"📂 [{log_tag}] 切换仓库分类：{category_key}", "INFO")
            if not self._click_region_safe(regions, category_key, use_foreground):
                return False, current_category, current_page
            time.sleep(0.35)
            current_category = category_key
        if not self._warehouse_click_right_until_end(
            regions,
            use_foreground,
            log_tag=f"{log_tag}·{category_key}定位末页",
        ):
            return False, current_category, current_page
        current_page = 1
        return True, current_category, current_page

    def _fusion_pick_warehouse_color_progress(
        self,
        regions,
        use_foreground: bool,
        state: Dict[str, Any],
        *,
        wanted_count: int = 3,
        current_category: Optional[str],
        current_page: int,
        initial_all: bool = False,
        log_tag: str = "融合模式",
    ) -> Tuple[bool, int, Optional[str], int, bool]:
        if state.get("exhausted"):
            self._emit(f"ℹ️ [{log_tag}] {state.get('category_key')} 已耗尽，跳过取宠", "INFO")
            return True, 0, current_category, current_page, True
        ok, current_category, current_page = self._fusion_nav_warehouse_state(
            regions,
            use_foreground,
            state,
            current_category=current_category,
            current_page=current_page,
            initial_all=initial_all,
            log_tag=log_tag,
        )
        if not ok:
            return False, 0, current_category, current_page, False

        target_color = str(state.get("color") or "")
        category_key = str(state.get("category_key") or "")
        picked = 0
        exhausted = False
        while picked < wanted_count:
            if self._should_abort():
                return False, picked, current_category, current_page, exhausted
            page_had_target = False
            self._emit(
                f"🔎 [{log_tag}] {category_key} 第 {current_page} 页倒扫，目标={target_color}，已取={picked}/{wanted_count}",
                "INFO",
            )
            for slot in range(9, 0, -1):
                if self._should_abort():
                    return False, picked, current_category, current_page, exhausted
                key = f"精灵仓库.{slot}"
                rgb = mean_rgb_for_region_key(regions, key)
                color = self._psychic_exp_slot_color(rgb)
                self._emit(f"📋 [{log_tag}] {key} RGB={rgb} -> {color}", "DEBUG")
                if color != target_color:
                    continue
                page_had_target = True
                self._emit(
                    f"✅ [{log_tag}] {key} 命中 {target_color}，放入背包 ({picked + 1}/{wanted_count})",
                    "SUCCESS",
                )
                if not self._click_region_safe(regions, key, use_foreground):
                    return False, picked, current_category, current_page, exhausted
                time.sleep(0.12)
                if not self._click_region_safe(regions, "精灵仓库.放入背包", use_foreground):
                    return False, picked, current_category, current_page, exhausted
                if not self._wait_1and1_clear(
                    regions,
                    use_foreground,
                    timeout_s=20.0,
                    min_confirm_clicks=1,
                    log_tag=f"{log_tag}·放入背包",
                ):
                    return False, picked, current_category, current_page, exhausted
                picked += 1
                if picked >= wanted_count:
                    break
                time.sleep(0.2)
            if picked >= wanted_count:
                break
            left_state, left_detail = self._warehouse_nav_state(regions, "精灵仓库.左")
            if left_state == "end":
                exhausted = True
                if not page_had_target:
                    self._emit(f"ℹ️ [{log_tag}] {category_key} 已扫到第一页且未找到目标，标记耗尽；{left_detail}", "INFO")
                else:
                    self._emit(f"ℹ️ [{log_tag}] {category_key} 第一页取后仍未满 {wanted_count}，标记耗尽；{left_detail}", "INFO")
                break
            if left_state != "available":
                self._emit(f"❌ [{log_tag}] 仓库左翻按钮状态异常：{left_detail}", "ERROR")
                return False, picked, current_category, current_page, exhausted
            if current_page > WAREHOUSE_PAGE_TURN_MAX_COUNT:
                self._emit(
                    f"❌ [{log_tag}] 仓库左翻达到上限 "
                    f"{WAREHOUSE_PAGE_TURN_MAX_COUNT} 次仍未完成取宠",
                    "ERROR",
                )
                return False, picked, current_category, current_page, exhausted
            current_page += 1
            self._emit(f"⬅️ [{log_tag}] 当前页扫完，左翻到倒数第 {current_page} 页；{left_detail}", "INFO")
            if not self._click_region_safe(regions, "精灵仓库.左", use_foreground):
                return False, picked, current_category, current_page, exhausted
            time.sleep(0.6)
        if picked < wanted_count:
            exhausted = True
            self._emit(f"⚠️ [{log_tag}] 本批只取到 {picked}/{wanted_count}，标记该色系耗尽", "WARN")
        state["exhausted"] = exhausted or bool(state.get("exhausted"))
        return True, picked, current_category, current_page, bool(state.get("exhausted"))

    def _fusion_scan_main_slots(
        self,
        regions,
        *,
        active_slots: int = 6,
        expected_pairs: int = 3,
        allow_fewer: bool = False,
        fallback_after_s: float = 3.0,
        timeout_s: float = 20.0,
        log_tag: str = "融合模式",
    ) -> Optional[Tuple[List[int], List[int], int]]:
        active_slots = max(2, min(6, int(active_slots)))
        expected_pairs = max(1, min(3, int(expected_pairs)))
        t0 = time.time()
        last_log = 0.0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return None
            blue_slots: List[int] = []
            gray_slots: List[int] = []
            snapshots = []
            for idx in range(1, active_slots + 1):
                rgb = mean_rgb_for_region_key(regions, f"融合.主{idx}")
                if self._fusion_is_deep_blue(rgb):
                    blue_slots.append(idx)
                    state = "blue"
                elif self._fusion_is_gray(rgb):
                    gray_slots.append(idx)
                    state = "gray"
                else:
                    state = "unknown"
                snapshots.append(f"{idx}:{state}:{rgb}")
            elapsed = time.time() - t0
            candidate_pairs = [expected_pairs]
            for candidate in candidate_pairs:
                candidate_active_slots = candidate * 2
                candidate_blue = [slot for slot in blue_slots if slot <= candidate_active_slots]
                candidate_gray = [slot for slot in gray_slots if slot <= candidate_active_slots]
                if len(candidate_blue) == candidate and len(candidate_gray) == candidate:
                    self._emit(
                        f"✅ [{log_tag}] 主槽扫描完成（1-{candidate_active_slots}，{candidate}蓝{candidate}灰）：蓝={candidate_blue}，灰={candidate_gray}",
                        "SUCCESS",
                    )
                    return candidate_blue, candidate_gray, candidate
            if allow_fewer and expected_pairs == 1 and elapsed >= max(6.0, fallback_after_s * 2):
                first_two_blue = [slot for slot in blue_slots if slot <= 2]
                first_two_gray = [slot for slot in gray_slots if slot <= 2]
                if len(first_two_blue) == 2 and not first_two_gray:
                    self._emit(
                        f"⚠️ [{log_tag}] 主槽 1-2 长时间无灰槽，判定背包内已无可融合对",
                        "WARN",
                    )
                    return [], [], 0
            now = time.time()
            if now - last_log >= 1.0:
                self._emit(f"⏳ [{log_tag}] 主槽等待 {expected_pairs}蓝{expected_pairs}灰：{' | '.join(snapshots)}", "DEBUG")
                last_log = now
            time.sleep(0.08)
        self._emit(f"❌ [{log_tag}] 主槽未达成 {expected_pairs}蓝{expected_pairs}灰", "ERROR")
        return None

    def _fusion_click_slot_until_selected(
        self,
        regions,
        use_foreground: bool,
        *,
        prefix: str,
        slot: int,
        timeout_s: float = 12.0,
        log_tag: str = "融合模式",
    ) -> bool:
        click_key = f"融合.{prefix}{slot}"
        selected_key = f"融合.{prefix}选中{slot}"
        t0 = time.time()
        clicked = 0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            if self._fusion_is_yellow(mean_rgb_for_region_key(regions, selected_key)):
                self._emit(
                    f"✅ [{log_tag}] {selected_key} 已变黄，{prefix}精灵选中",
                    "SUCCESS",
                )
                return True
            if not self._click_region_safe(regions, click_key, use_foreground):
                return False
            clicked += 1
            time.sleep(0.12)
        self._emit(
            f"❌ [{log_tag}] {prefix}{slot} 点击 {clicked} 次后仍未选中，{selected_key}={mean_rgb_for_region_key(regions, selected_key)}",
            "ERROR",
        )
        return False

    def _fusion_wait_sub_shadow_after_main_confirm(
        self,
        regions,
        initial_rgb: Optional[Tuple[int, int, int]],
        *,
        timeout_s: float = 10.0,
        log_tag: str = "融合模式",
    ) -> bool:
        if self._fusion_is_dark_sub_slot(initial_rgb):
            self._emit(f"📋 [{log_tag}] 副精灵初始为深黑：RGB={initial_rgb}", "INFO")
        else:
            self._emit(f"📋 [{log_tag}] 副精灵初始不是标准深黑，仍等待绿色阴影：RGB={initial_rgb}", "WARN")
        return self._fusion_wait_region_color(
            regions,
            "融合.副精灵",
            self._fusion_is_green_shadow,
            "副精灵蒙上绿色阴影",
            timeout_s=timeout_s,
            log_tag=log_tag,
        )

    def _fusion_wait_region_stable(
        self,
        regions,
        key: str,
        *,
        timeout_s: float = 12.0,
        samples_required: int = 5,
        tolerance: float = 3.0,
        log_tag: str = "融合模式",
    ) -> bool:
        last_rgb = None
        stable_count = 0
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            rgb = mean_rgb_for_region_key(regions, key)
            if rgb is not None and last_rgb is not None and self._fusion_rgb_distance(rgb, last_rgb) <= tolerance:
                stable_count += 1
            else:
                stable_count = 1
            last_rgb = rgb
            if rgb is not None and stable_count >= samples_required:
                self._emit(f"✅ [{log_tag}] {key} RGB 已稳定：{rgb}", "SUCCESS")
                return True
            time.sleep(0.2)
        self._emit(f"❌ [{log_tag}] {key} RGB 未稳定，最后={last_rgb}", "ERROR")
        return False

    def _fusion_click_y(
        self,
        regions,
        use_foreground: bool,
        y_value: int,
        *,
        current_page: int,
        log_tag: str = "融合模式",
    ) -> int:
        y = max(1, min(24, int(y_value or 1)))
        target_page = 1 if y <= 12 else 2
        slot = y if y <= 12 else y - 12
        if target_page != current_page:
            switch_key = "融合.右" if target_page == 2 else "融合.左"
            self._emit(f"➡️ [{log_tag}] 切换融合材料页：{current_page} -> {target_page}", "INFO")
            if not self._click_region_safe(regions, switch_key, use_foreground):
                return None
            time.sleep(0.25)
            current_page = target_page
        self._emit(f"🖱️ [{log_tag}] 点击融合.{slot}（Y={y}，页={current_page}）", "INFO")
        if not self._click_region_safe(regions, f"融合.{slot}", use_foreground):
            return None
        time.sleep(0.15)
        return current_page

    def _fusion_wait_final_1and1_probes(
        self,
        regions,
        *,
        timeout_s: float = 20.0,
        log_tag: str = "融合模式",
    ) -> bool:
        t0 = time.time()
        last_log = 0.0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            white_rgb = mean_rgb_for_region_key(regions, "融合.白色探针")
            blue_rgb = mean_rgb_for_region_key(regions, "融合.蓝色探针")
            if self._fusion_is_white(white_rgb) and self._fusion_is_blue_probe(blue_rgb):
                self._emit(
                    f"✅ [{log_tag}] 融合 1AND1 探针就绪：白={white_rgb} 蓝={blue_rgb}",
                    "SUCCESS",
                )
                return True
            now = time.time()
            if now - last_log >= 1.0:
                self._emit(f"⏳ [{log_tag}] 等待融合 1AND1 探针：白={white_rgb} 蓝={blue_rgb}", "DEBUG")
                last_log = now
            time.sleep(0.05)
        self._emit(f"❌ [{log_tag}] 融合 1AND1 探针等待超时", "ERROR")
        return False

    @classmethod
    def _fusion_classify_result_rgb(
        cls,
        rgb: Optional[Tuple[int, int, int]],
        *,
        normal_soulbead_id: str,
    ) -> str:
        if rgb is None:
            return "unknown"
        if cls._fusion_rgb_distance(rgb, (253, 171, 254)) <= 60:
            return "rare"
        if cls._fusion_is_white(rgb):
            return "failed"
        normal_id = str(normal_soulbead_id or "")
        if normal_id == "1000015":
            if cls._fusion_rgb_distance(rgb, (27, 220, 65)) <= 65:
                return "normal"
        else:
            if cls._fusion_rgb_distance(rgb, (176, 60, 255)) <= 65:
                return "normal"
        return "unknown"

    def _fusion_wait_result_rgb(
        self,
        regions,
        use_foreground: bool,
        *,
        normal_soulbead_id: str,
        timeout_s: float = 15.0,
        samples_required: int = 5,
        tolerance: float = 8.0,
        click_interval_s: float = 0.25,
        log_tag: str = "融合模式",
    ) -> str:
        last_rgb = None
        stable_count = 0
        t0 = time.time()
        last_log = 0.0
        last_click = 0.0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return "unknown"
            now = time.time()
            if now - last_click >= click_interval_s:
                if not self._click_region_safe(regions, "融合.确认", use_foreground):
                    return "unknown"
                last_click = now
            rgb = mean_rgb_for_region_key(regions, "融合.结果")
            if rgb is not None and last_rgb is not None and self._fusion_rgb_distance(rgb, last_rgb) <= tolerance:
                stable_count += 1
            else:
                stable_count = 1
            last_rgb = rgb
            result = self._fusion_classify_result_rgb(rgb, normal_soulbead_id=normal_soulbead_id)
            if stable_count >= samples_required and result != "unknown":
                self._emit(f"✅ [{log_tag}] 融合结果 RGB 稳定：RGB={rgb} -> {result}", "SUCCESS")
                return result
            if now - last_log >= 1.0:
                self._emit(f"⏳ [{log_tag}] 循环点击融合.确认并等待结果 RGB 稳定：RGB={rgb} -> {result}", "DEBUG")
                last_log = now
            time.sleep(0.15)
        final_rgb = mean_rgb_for_region_key(regions, "融合.结果")
        result = self._fusion_classify_result_rgb(final_rgb, normal_soulbead_id=normal_soulbead_id)
        self._emit(f"⚠️ [{log_tag}] 融合结果 RGB 等待超时：RGB={final_rgb} -> {result}", "WARN")
        return result

    def _fusion_log_result_from_kernel(
        self,
        *,
        scheme_label: str,
        normal_soulbead_id: str,
        log_tag: str = "融合模式",
    ) -> str:
        from core.logger import fetch_kernel_since

        rows = fetch_kernel_since(0, return_rows=True)
        panel_idx = None
        for idx in range(len(rows) - 1, -1, -1):
            _seq, _ts, line = rows[idx]
            if line_matches(FUSION_PANEL_RE, str(line)):
                panel_idx = idx
                break
        if panel_idx is None:
            self._emit(f"⚠️ [{log_tag}] 未在内核缓存中找到最近的 SpriteFusionPanel.swf，无法判定融合结果", "WARN")
            return "unknown"

        segment = [str(line) for _seq, _ts, line in rows[panel_idx:]]
        rare_re = re.compile(r"soulBead[\\/]icon[\\/]1000009\b|1000009", re.IGNORECASE)
        normal_id = str(normal_soulbead_id or "")
        normal_re = re.compile(
            rf"soulBead[\\/]icon[\\/]{re.escape(normal_id)}\b|{re.escape(normal_id)}",
            re.IGNORECASE,
        ) if normal_id else None

        rare_line = next((line for line in segment if rare_re.search(line)), None)
        normal_line = next((line for line in segment if normal_re and normal_re.search(line)), None)
        any_soulbead_line = next(
            (line for line in segment if re.search(r"soulBead[\\/]icon[\\/]\d+", line, re.IGNORECASE)),
            None,
        )
        if rare_line:
            self._emit(f"🌟 [{log_tag}] 融合结果：{scheme_label} 出极品（soulBead/icon/1000009 粉色）", "SUCCESS")
            self._emit(f"📋 [{log_tag}] 命中日志：{rare_line}", "INFO")
            return "rare"
        if normal_line:
            self._emit(f"✅ [{log_tag}] 融合结果：{scheme_label} 正常融合（soulBead/icon/{normal_id}）", "SUCCESS")
            self._emit(f"📋 [{log_tag}] 命中日志：{normal_line}", "INFO")
            return "normal"
        if any_soulbead_line:
            self._emit(f"⚠️ [{log_tag}] 融合结果：检测到非预期 soulBead，按未知处理", "WARN")
            self._emit(f"📋 [{log_tag}] 命中日志：{any_soulbead_line}", "INFO")
            return "unknown"
        self._emit(
            f"⚠️ [{log_tag}] 融合结果未判定：最近融合面板后未检测到新的 soulBead/icon（可能失败，也可能结果图标已缓存）",
            "WARN",
        )
        return "undetected"

    def _fusion_latest_map_id(self) -> Optional[int]:
        from core.logger import fetch_kernel_since

        try:
            lines = fetch_kernel_since(0)
            if not isinstance(lines, list):
                return None
            for line in reversed(lines):
                map_id = first_map_id_in_line(str(line))
                if map_id is not None:
                    return map_id
        except Exception:
            return None
        return None

    def _fusion_wait_latest_map(
        self,
        target_map_id: int,
        *,
        timeout_s: float = 20.0,
        log_tag: str = "融合模式",
    ) -> bool:
        self._emit(
            f"⏳ [{log_tag}] 扫描最后 map# 是否为 {target_map_id}（{timeout_s:.0f}s 超时）",
            "INFO",
        )
        t0 = time.time()
        last_id = None
        last_log = 0.0
        while time.time() - t0 < timeout_s:
            if self._should_abort():
                return False
            last_id = self._fusion_latest_map_id()
            if last_id == target_map_id:
                self._emit(f"✅ [{log_tag}] 最后 map#={target_map_id}，门控通过", "SUCCESS")
                return True
            now = time.time()
            if now - last_log >= 1.0:
                self._emit(
                    f"🔍 [{log_tag}] 当前最后 map={last_id}，等待 map={target_map_id}",
                    "DEBUG",
                )
                last_log = now
            time.sleep(0.1)
        self._emit(
            f"❌ [{log_tag}] 未在 {timeout_s:.0f}s 内检测到最后 map={target_map_id}，当前最后 map={last_id}",
            "ERROR",
        )
        return False

    def _fusion_prepare_refresh_to_lab(
        self,
        regions,
        use_foreground: bool,
        *,
        log_tag: str = "融合模式",
    ) -> bool:
        drr = getattr(self.bot, "dar_route_runner", None)
        if drr is None:
            self._emit(f"❌ [{log_tag}] 缺少 dar_route_runner，无法执行前置刷新重连", "ERROR")
            return False
        refresh = getattr(drr, "run_refresh_login_until_map", None)
        if not callable(refresh):
            self._emit(f"❌ [{log_tag}] dar_route_runner 缺少 run_refresh_login_until_map", "ERROR")
            return False

        self._emit(f"🔄 [{log_tag}] 前置刷新重连：刷新登录直到检测到 map", "SYSTEM")
        stop_event = self._new_daily_stop_event()
        if not refresh(use_foreground, stop_event):
            self._emit(f"❌ [{log_tag}] 前置刷新重连失败，停止融合流程", "ERROR")
            return False

        last_id = self._fusion_latest_map_id()
        self._emit(f"🗺️ [{log_tag}] 前置刷新后最后 map={last_id}（期望={FUSION_LAB_MAP_ID}）", "INFO")
        if last_id == FUSION_LAB_MAP_ID:
            self._emit(f"✅ [{log_tag}] 已在 map{FUSION_LAB_MAP_ID}，直接执行融合程序", "SUCCESS")
            return True

        map_key = None
        for candidate in ("融合.地图", "日常.地图", "巅峰对战.地图"):
            if regions.get(candidate):
                map_key = candidate
                break
        if not map_key:
            self._emit(f"❌ [{log_tag}] 缺少地图入口区域（融合.地图 / 日常.地图 / 巅峰对战.地图）", "ERROR")
            return False
        if not regions.get("融合.实验室"):
            self._emit(f"❌ [{log_tag}] 缺少 region：融合.实验室", "ERROR")
            return False
        if not regions.get(MAP10_WHITE_PROBE_KEY_NIEO):
            self._emit(f"❌ [{log_tag}] 缺少 region：{MAP10_WHITE_PROBE_KEY_NIEO}", "ERROR")
            return False

        self._emit(f"🖱️ [{log_tag}] 当前不在 map{FUSION_LAB_MAP_ID}，点击 {map_key} 打开地图", "INFO")
        if not self._click_region_safe(regions, map_key, use_foreground):
            return False
        time.sleep(0.6)

        self._emit(f"🖱️ [{log_tag}] 点击 融合.实验室，等待进入 map{FUSION_LAB_MAP_ID}", "INFO")
        if not self._click_region_safe(regions, "融合.实验室", use_foreground):
            return False
        if not self._fusion_wait_latest_map(FUSION_LAB_MAP_ID, timeout_s=20.0, log_tag=log_tag):
            return False
        return wait_map10_white_probe_ready(
            regions,
            emit_fn=self._emit,
            stop_check=self._should_abort,
            white_probe_key=MAP10_WHITE_PROBE_KEY_NIEO,
            log_tag=f"{log_tag}·实验室",
            timeout_s=20.0,
            two_phase=False,
        )

    def _fusion_run_single_pair(
        self,
        regions,
        use_foreground: bool,
        *,
        sequence: Tuple[int, int, int, int],
        scheme_label: str,
        normal_soulbead_id: str,
        yellow_probe_key: str,
        pairs_remaining: int,
        attempt_index: int,
        log_tag: str = "融合模式",
    ) -> Optional[Tuple[str, int]]:
        from core.logger import kernel_cursor

        active_slots = max(2, min(6, int(pairs_remaining) * 2))
        expected_pairs = max(1, min(3, int(pairs_remaining)))
        round_tag = f"{log_tag}·第{attempt_index}轮·剩{pairs_remaining}对"
        fusion_cursor = kernel_cursor()
        self._emit(f"🖱️ [{round_tag}] 点击融合.打开，等待 SpriteFusionPanel.swf", "INFO")
        if not self._click_region_safe(regions, "融合.打开", use_foreground):
            return None
        if not self._wait_kernel_line_matches(
            FUSION_PANEL_RE,
            log_tag=f"{round_tag}·面板",
            timeout_s=8.0,
            success_msg=f"✅ [{round_tag}] 已检测到 SpriteFusionPanel.swf，融合面板打开",
            start_cursor=fusion_cursor,
        ):
            return None
        if not self._click_region_safe(regions, "融合.主精灵", use_foreground):
            return None
        if not self._fusion_wait_region_color(
            regions,
            "融合.主探针",
            self._fusion_is_pure_blue_probe,
            "主探针变蓝",
            timeout_s=15.0,
            log_tag=round_tag,
            use_foreground=use_foreground,
            repoke_key="融合.主精灵",
            repoke_interval_s=3.0,
        ):
            return None
        scan_result = self._fusion_scan_main_slots(
            regions,
            active_slots=active_slots,
            expected_pairs=expected_pairs,
            allow_fewer=False,
            log_tag=round_tag,
        )
        if scan_result is None:
            return None
        blue_slots, gray_slots, detected_pairs = scan_result
        if detected_pairs <= 0:
            return "empty", 0
        main_slot = blue_slots[0]
        sub_slot = gray_slots[0]
        if not self._fusion_click_slot_until_selected(
            regions,
            use_foreground,
            prefix="主",
            slot=main_slot,
            log_tag=round_tag,
        ):
            return None
        initial_sub_rgb = mean_rgb_for_region_key(regions, "融合.副精灵")
        if not self._click_region_safe(regions, "融合.主确认", use_foreground):
            return None
        if not self._fusion_wait_sub_shadow_after_main_confirm(
            regions,
            initial_sub_rgb,
            timeout_s=10.0,
            log_tag=round_tag,
        ):
            return None
        if not self._click_region_safe(regions, "融合.副精灵", use_foreground):
            return None
        if not self._fusion_wait_region_color(
            regions,
            "融合.副探针",
            self._fusion_is_pure_blue_probe,
            "副探针变蓝",
            timeout_s=15.0,
            log_tag=round_tag,
            use_foreground=use_foreground,
            repoke_key="融合.副精灵",
            repoke_interval_s=3.0,
        ):
            return None
        if not self._fusion_click_slot_until_selected(
            regions,
            use_foreground,
            prefix="副",
            slot=sub_slot,
            log_tag=round_tag,
        ):
            return None
        if not self._click_region_safe(regions, "融合.副确认", use_foreground):
            return None
        if not self._fusion_wait_region_color(
            regions,
            yellow_probe_key,
            self._fusion_is_yellow,
            "融合黄色探针变黄",
            timeout_s=15.0,
            log_tag=round_tag,
        ):
            return None
        if not self._click_region_safe(regions, "融合.A", use_foreground):
            return None
        if not self._fusion_wait_region_stable(regions, "融合.1", log_tag=round_tag):
            return None

        current_page = 1
        step_buttons = ("融合.B", "融合.C", "融合.D")
        for idx, y_value in enumerate(sequence):
            next_page = self._fusion_click_y(
                regions,
                use_foreground,
                y_value,
                current_page=current_page,
                log_tag=round_tag,
            )
            if next_page is None:
                return None
            current_page = next_page
            if idx < len(step_buttons):
                if not self._click_region_safe(regions, step_buttons[idx], use_foreground):
                    return None
                time.sleep(0.15)

        if not self._click_region_safe(regions, "融合.融合", use_foreground):
            return None
        if not self._fusion_wait_white_after_fuse_click(
            regions,
            use_foreground,
            log_tag=round_tag,
            repoke_interval_s=3.0,
        ):
            return None
        result = self._fusion_wait_result_rgb(
            regions,
            use_foreground,
            normal_soulbead_id=normal_soulbead_id,
            timeout_s=15.0,
            log_tag=round_tag,
        )
        if not self._wait_1and1_clear(
            regions,
            use_foreground,
            timeout_s=20.0,
            min_confirm_clicks=1,
            log_tag=f"{round_tag}·最终1AND1",
        ):
            return None
        return result, detected_pairs

    def _fusion_run_progress_batches(
        self,
        regions,
        use_foreground: bool,
        *,
        sequence: Tuple[int, int, int, int],
        scheme_label: str,
        primary_category_key: str,
        primary_color: str,
        secondary_category_key: str,
        secondary_color: str,
        normal_soulbead_id: str,
        pink_target: int,
        fusion_limit: int,
        yellow_probe_key: str,
        log_tag: str = "融合模式",
    ) -> bool:
        fusion_count = 0
        rare_count = 0
        normal_count = 0
        failed_count = 0
        unknown_count = 0

        def _log_fusion_summary(reason: str, level: str = "SYSTEM") -> None:
            self._emit(
                f"📊 [{log_tag}] 融合统计（{reason}）：总融合={fusion_count}，普通={normal_count}，极品={rare_count}，失败={failed_count}，未知={unknown_count}",
                level,
            )

        if not self._fusion_clear_backpack_keep_open(regions, use_foreground, log_tag=log_tag):
            _log_fusion_summary("清空背包失败", "ERROR")
            return False
        if not self._fusion_open_warehouse_from_bag(regions, use_foreground, log_tag=log_tag):
            _log_fusion_summary("打开仓库失败", "ERROR")
            return False

        primary_state: Dict[str, Any] = {
            "name": "紫色系",
            "category_key": primary_category_key,
            "color": primary_color,
            "exhausted": False,
        }
        secondary_state: Dict[str, Any] = {
            "name": "青色系",
            "category_key": secondary_category_key,
            "color": secondary_color,
            "exhausted": False,
        }
        current_category: Optional[str] = None
        current_page = 1
        initial_all = True
        warehouse_open = True
        batch_index = 1

        while True:
            if self._should_abort():
                _log_fusion_summary("主动停止/中断", "WARN")
                return False
            if rare_count >= pink_target:
                self._emit(f"✅ [{log_tag}] 粉色极品数量 {rare_count}/{pink_target} 已达标，停止", "SUCCESS")
                _log_fusion_summary("粉色目标达成", "SUCCESS")
                return True
            if fusion_limit and fusion_count >= fusion_limit:
                self._emit(f"✅ [{log_tag}] 融合次数 {fusion_count}/{fusion_limit} 已达标，停止", "SUCCESS")
                _log_fusion_summary("融合次数达标", "SUCCESS")
                return True
            if primary_state.get("exhausted") and secondary_state.get("exhausted"):
                self._emit(f"✅ [{log_tag}] 紫色系与青色系均已耗尽，停止", "SUCCESS")
                _log_fusion_summary("仓库目标耗尽", "SUCCESS")
                return True

            if not warehouse_open:
                if not self._fusion_open_warehouse_from_bag(regions, use_foreground, log_tag=log_tag):
                    _log_fusion_summary("打开仓库失败", "ERROR")
                    return False
                warehouse_open = True

            order = [secondary_state, primary_state] if current_category == secondary_category_key else [primary_state, secondary_state]
            picked_by_category: Dict[str, int] = {primary_category_key: 0, secondary_category_key: 0}
            batch_incomplete = False
            for state in order:
                ok, picked, current_category, current_page, _exhausted = self._fusion_pick_warehouse_color_progress(
                    regions,
                    use_foreground,
                    state,
                    wanted_count=3,
                    current_category=current_category,
                    current_page=current_page,
                    initial_all=initial_all,
                    log_tag=f"{log_tag}·批{batch_index}·{state.get('name')}",
                )
                initial_all = False
                if not ok:
                    _log_fusion_summary("取宠失败", "ERROR")
                    return False
                picked_by_category[str(state.get("category_key"))] = picked
                if picked < 3:
                    batch_incomplete = True

            pairs_remaining = min(
                picked_by_category.get(primary_category_key, 0),
                picked_by_category.get(secondary_category_key, 0),
            )
            self._emit(
                f"📦 [{log_tag}] 批{batch_index} 取宠完成：紫={picked_by_category.get(primary_category_key, 0)}，青={picked_by_category.get(secondary_category_key, 0)}，可融合={pairs_remaining}对",
                "INFO",
            )
            self._emit(f"📦 [{log_tag}] 关闭精灵仓库", "INFO")
            if not self._click_region_safe(regions, "精灵仓库.关闭", use_foreground):
                _log_fusion_summary("关闭仓库失败", "ERROR")
                return False
            warehouse_open = False
            time.sleep(0.5)

            if batch_incomplete:
                self._emit(
                    f"ℹ️ [{log_tag}] 批{batch_index} 未取满完整三对（紫={picked_by_category.get(primary_category_key, 0)}，青={picked_by_category.get(secondary_category_key, 0)}），不进入融合，停止",
                    "INFO",
                )
                _log_fusion_summary("未取满完整三对", "SUCCESS")
                return True
            if pairs_remaining <= 0:
                self._emit(f"ℹ️ [{log_tag}] 批{batch_index} 没有可融合对，停止", "INFO")
                _log_fusion_summary("没有可融合对", "SUCCESS")
                return True

            while pairs_remaining > 0:
                if self._should_abort():
                    _log_fusion_summary("主动停止/中断", "WARN")
                    return False
                if rare_count >= pink_target:
                    self._emit(f"✅ [{log_tag}] 粉色极品数量 {rare_count}/{pink_target} 已达标，停止", "SUCCESS")
                    _log_fusion_summary("粉色目标达成", "SUCCESS")
                    return True
                if fusion_limit and fusion_count >= fusion_limit:
                    self._emit(f"✅ [{log_tag}] 融合次数 {fusion_count}/{fusion_limit} 已达标，停止", "SUCCESS")
                    _log_fusion_summary("融合次数达标", "SUCCESS")
                    return True
                single_result = self._fusion_run_single_pair(
                    regions,
                    use_foreground,
                    sequence=sequence,
                    scheme_label=scheme_label,
                    normal_soulbead_id=normal_soulbead_id,
                    yellow_probe_key=yellow_probe_key,
                    pairs_remaining=pairs_remaining,
                    attempt_index=fusion_count + 1,
                    log_tag=log_tag,
                )
                if single_result is None:
                    _log_fusion_summary("单次融合流程失败", "ERROR")
                    return False
                result, detected_pairs = single_result
                if result != "empty":
                    fusion_count += 1
                if result == "empty":
                    self._emit(f"✅ [{log_tag}] 检测到背包内已无可融合对，本批结束", "SUCCESS")
                    pairs_remaining = 0
                elif result == "rare":
                    rare_count += 1
                    pairs_remaining -= 1
                    self._emit(
                        f"🌟 [{log_tag}] 第{fusion_count}次融合结果=rare，粉色={rare_count}/{pink_target}，本批剩余={pairs_remaining}",
                        "SUCCESS",
                    )
                elif result in ("failed", "undetected", "unknown"):
                    if result == "failed":
                        failed_count += 1
                    else:
                        unknown_count += 1
                    self._emit(
                        f"⚠️ [{log_tag}] 第{fusion_count}次融合结果={result}，本批剩余对数暂保持 {pairs_remaining}",
                        "WARN",
                    )
                else:
                    normal_count += 1
                    pairs_remaining -= 1
                    self._emit(
                        f"✅ [{log_tag}] 第{fusion_count}次融合结果={result}，本批剩余对数={pairs_remaining}",
                        "SUCCESS",
                    )

            batch_index += 1
            self._emit(
                f"🔁 [{log_tag}] 批{batch_index - 1} 完成，继续下一批：当前停留 {current_category} 第 {current_page} 页",
                "INFO",
            )

    def run_fusion_mode(
        self,
        use_foreground: bool = False,
        *,
        sequence: Optional[Tuple[int, int, int, int]] = None,
        scheme_label: str = "卡鲁耶克",
        primary_category_key: str = "精灵仓库.飞行系",
        primary_color: str = "purple",
        secondary_category_key: str = "精灵仓库.超能系",
        secondary_color: str = "cyan",
        normal_soulbead_id: str = "1000008",
        primary_right_clicks: int = 30,
        secondary_right_clicks: int = 80,
        pink_target: int = 4,
        fusion_limit: int = 0,
    ) -> bool:
        """融合模式：按方案取三只主材 + 三只副材，并执行融合。"""
        tag = "融合模式"

        def _log_empty_fusion_summary(reason: str, level: str = "ERROR") -> None:
            self._emit(
                f"📊 [{tag}] 融合统计（{reason}）：总融合=0，普通=0，极品=0，失败=0，未知=0",
                level,
            )

        regions = getattr(self.bot, "regions", None)
        if regions is None:
            self._emit("❌ [融合模式] 缺少 regions", "ERROR")
            _log_empty_fusion_summary("缺少 regions")
            return False
        if sequence is None:
            sequence = (3, 3, 3, 3)
        try:
            primary_right_clicks = max(1, min(999, int(primary_right_clicks)))
        except (TypeError, ValueError):
            primary_right_clicks = 30
        try:
            secondary_right_clicks = max(1, min(999, int(secondary_right_clicks)))
        except (TypeError, ValueError):
            secondary_right_clicks = 80
        try:
            pink_target = max(1, min(999, int(pink_target)))
        except (TypeError, ValueError):
            pink_target = 4
        try:
            fusion_limit = max(0, min(999999, int(fusion_limit)))
        except (TypeError, ValueError):
            fusion_limit = 0
        required = [
            "精灵背包.打开精灵背包",
            "精灵背包.放回仓库",
            "精灵背包.清空精灵一",
            "精灵背包.精灵仓库",
            "精灵仓库.关闭",
            "精灵仓库.ALL",
            primary_category_key,
            secondary_category_key,
            "精灵仓库.右",
            "精灵仓库.左",
            "精灵仓库.放入背包",
            "融合.打开",
            "融合.主精灵",
            "融合.主探针",
            "融合.主确认",
            "融合.副精灵",
            "融合.副探针",
            "融合.副确认",
            "融合.A",
            "融合.B",
            "融合.C",
            "融合.D",
            "融合.左",
            "融合.右",
            "融合.融合",
            "融合.确认",
            "融合.白色探针",
            "融合.蓝色探针",
            "融合.结果",
        ]
        if regions.get("精灵仓库.单属性"):
            required.append("精灵仓库.单属性")
        yellow_probe_key = "融合.黄色探针" if regions.get("融合.黄色探针") else "融合.蓝色探针"
        if yellow_probe_key == "融合.蓝色探针":
            self._emit("⚠️ [融合模式] 未找到 融合.黄色探针，暂用 融合.蓝色探针 等待变黄", "WARN")
        required.append(yellow_probe_key)
        for idx in range(1, 10):
            required.append(f"精灵仓库.{idx}")
        for idx in range(1, 7):
            required.extend((f"融合.主{idx}", f"融合.主选中{idx}", f"融合.副{idx}", f"融合.副选中{idx}"))
        for idx in range(1, 13):
            required.append(f"融合.{idx}")
        missing = [key for key in required if not regions.get(key)]
        if missing:
            self._emit(f"❌ [融合模式] 缺少区域：{', '.join(missing)}", "ERROR")
            _log_empty_fusion_summary("缺少区域")
            return False

        fusion_limit_text = str(fusion_limit) if fusion_limit else "不限"
        self._emit(
            f"🧬 [融合模式] 启动：{scheme_label}，取宠={primary_category_key}/{primary_color}×3 + {secondary_category_key}/{secondary_color}×3，仓库自动灰色翻页到头，序列={sequence}，粉色目标={pink_target}，融合次数={fusion_limit_text}",
            "SYSTEM",
        )
        if not self._fusion_prepare_refresh_to_lab(regions, use_foreground, log_tag=tag):
            _log_empty_fusion_summary("前置重连/实验室门控失败")
            return False
        return self._fusion_run_progress_batches(
            regions,
            use_foreground,
            sequence=tuple(sequence),
            scheme_label=scheme_label,
            primary_category_key=primary_category_key,
            primary_color=primary_color,
            secondary_category_key=secondary_category_key,
            secondary_color=secondary_color,
            normal_soulbead_id=normal_soulbead_id,
            pink_target=pink_target,
            fusion_limit=fusion_limit,
            yellow_probe_key=yellow_probe_key,
            log_tag=tag,
        )
        if not self._fusion_clear_backpack_keep_open(regions, use_foreground, log_tag=tag):
            return False
        if not self._fusion_open_warehouse_from_bag(regions, use_foreground, log_tag=tag):
            return False
        if not self._fusion_pick_warehouse_color(
            regions,
            use_foreground,
            category_key=primary_category_key,
            target_color=primary_color,
            wanted_count=3,
            right_clicks=primary_right_clicks,
            log_tag=f"{tag}·主材",
        ):
            return False
        if not self._fusion_pick_warehouse_color(
            regions,
            use_foreground,
            category_key=secondary_category_key,
            target_color=secondary_color,
            wanted_count=3,
            right_clicks=secondary_right_clicks,
            log_tag=f"{tag}·副材",
        ):
            return False
        self._emit(f"📦 [{tag}] 关闭精灵仓库", "INFO")
        if not self._click_region_safe(regions, "精灵仓库.关闭", use_foreground):
            return False
        time.sleep(0.5)

        pairs_remaining = 3
        attempt_index = 1
        while pairs_remaining > 0:
            if self._should_abort():
                return False
            single_result = self._fusion_run_single_pair(
                regions,
                use_foreground,
                sequence=tuple(sequence),
                scheme_label=scheme_label,
                normal_soulbead_id=normal_soulbead_id,
                yellow_probe_key=yellow_probe_key,
                pairs_remaining=pairs_remaining,
                attempt_index=attempt_index,
                log_tag=tag,
            )
            if single_result is None:
                return False
            result, detected_pairs = single_result
            if result == "empty":
                self._emit(
                    f"✅ [{tag}] 第{attempt_index}轮检测到背包内已无可融合对，融合流程结束",
                    "SUCCESS",
                )
                pairs_remaining = 0
            elif result in ("failed", "undetected", "unknown"):
                self._emit(
                    f"⚠️ [{tag}] 第{attempt_index}轮融合结果={result}，背包剩余对数暂保持 {pairs_remaining}",
                    "WARN",
                )
            else:
                pairs_remaining -= 1
                self._emit(
                    f"✅ [{tag}] 第{attempt_index}轮融合结果={result}，背包剩余对数={pairs_remaining}",
                    "SUCCESS",
                )
            attempt_index += 1
        self._emit(f"✅ [{tag}] 背包内三组精灵已全部融合完成", "SUCCESS")
        return True

    def run_happy_valley_daily(
        self,
        use_foreground: bool = False,
        *,
        start_phase: str = "water",
        skip_pet_preparation: bool = False,
    ) -> bool:
        """Run Happy Valley from water, fire, or grass; optional direct-start skips pet setup."""
        tag = "欢乐谷日常"
        phase = str(start_phase or "water").strip().lower()
        phase_scripts = {
            "water": (("小游戏水", "purple"), ("小游戏火", "cyan"), ("小游戏草", None)),
            "fire": (("小游戏火", "cyan"), ("小游戏草", None)),
            "grass": (("小游戏草", None),),
        }
        if phase not in phase_scripts:
            self._emit(f"❌ [{tag}] 未知欢乐谷起始系别：{start_phase!r}", "ERROR")
            return False
        if not window_manager.find_window():
            self._emit(f"❌ [{tag}] 未检测到游戏窗口", "ERROR")
            return False
        regions = getattr(self.bot, "regions", None)
        drr = getattr(self.bot, "dar_route_runner", None)
        if regions is None or drr is None:
            self._emit(f"❌ [{tag}] 缺少 regions 或 dar_route_runner", "ERROR")
            return False

        stop_event = getattr(self.bot, "_stop_event", None)
        if not isinstance(stop_event, threading.Event):
            stop_event = threading.Event()
        first_connection = True
        attempt = 0

        while not self._should_abort():
            attempt += 1
            phase_follow_label = {
                "water": "橙色精灵一",
                "fire": "紫色精灵",
                "grass": "青色精灵",
            }[phase]
            self._emit(
                f"🎡 [{tag}] 第{attempt}次尝试："
                f"{'首次登录并取宠' if first_connection else f'重连后恢复{phase_follow_label}跟随'}",
                "SYSTEM",
            )
            if not drr.run_refresh_login_until_map(use_foreground, stop_event):
                if self._should_abort():
                    return False
                self._emit(f"⚠️ [{tag}] 刷新登录失败，继续重试", "WARN")
                continue

            if skip_pet_preparation:
                follow_ok = self._happy_valley_set_follow_for_phase(
                    phase,
                    use_foreground,
                    stop_event,
                    log_tag=f"{tag}·{phase}直达",
                )
                if not follow_ok:
                    self._emit(
                        f"⚠️ [{tag}] 直达{phase}前跟随{phase_follow_label}失败，继续重试",
                        "WARN",
                    )
                    continue
                first_connection = False
            elif first_connection:
                if not drr.prepare_happy_valley_daily_pets(
                    use_foreground,
                    stop_event,
                    log_tag=f"{tag}·首次",
                ):
                    return False
                if not self._happy_valley_set_follow_for_phase(
                    phase,
                    use_foreground,
                    stop_event,
                    log_tag=f"{tag}·首次",
                ):
                    return False
                first_connection = False
            else:
                if not self._happy_valley_set_follow_for_phase(
                    phase,
                    use_foreground,
                    stop_event,
                    log_tag=f"{tag}·重连",
                ):
                    self._emit(
                        f"⚠️ [{tag}] 重连后跟随{phase_follow_label}失败，继续重试",
                        "WARN",
                    )
                    continue

            from core.logger import fetch_kernel_since, kernel_cursor

            script_start_cursor = kernel_cursor()
            if not self.run_single_script("to欢乐谷", bg_mode=not use_foreground):
                if self._should_abort():
                    return False
                self._emit(f"⚠️ [{tag}] to欢乐谷执行失败，继续重试", "WARN")
                continue

            map429_cursor: Optional[int] = None
            try:
                rows = fetch_kernel_since(script_start_cursor, return_rows=True)
                if not isinstance(rows, list):
                    rows = []
                for seq, _ts, line in reversed(rows):
                    if first_map_id_in_line(str(line)) == 429:
                        map429_cursor = int(seq)
                        self._emit(
                            f"✅ [{tag}] to欢乐谷结束后向上扫描到 map429，seq={map429_cursor}",
                            "SUCCESS",
                        )
                        break
            except Exception as exc:
                self._emit(f"⚠️ [{tag}] 扫描 map429 异常：{exc}", "WARN")

            if map429_cursor is None:
                self._emit(f"⚠️ [{tag}] to欢乐谷结束后未向上扫描到 map429，重连重走路线", "WARN")
                continue

            found_map, has_newnpc = drr._wait_after_to_then_check_last_map_and_newnpc(
                40510,
                stop_event,
                timeout_s=20.0,
                log_tag=f"{tag}·to欢乐谷后",
                start_cursor=map429_cursor,
                accepted_map_ids={40510},
                settle_s=0.0,
                skip_reverse_check=True,
            )
            if int(found_map or -1) != 40510 or not has_newnpc:
                self._emit(
                    f"⚠️ [{tag}] 未检测到 map40510+NewNPC（实际map={found_map}，NewNPC={has_newnpc}），重连重走路线",
                    "WARN",
                )
                continue
            self._emit(f"✅ [{tag}] 已确认 map40510+NewNPC，开始小游戏流程", "SUCCESS")
            break

        if self._should_abort():
            return False

        return self._run_happy_valley_phases_with_reconnect(
            use_foreground,
            stop_event,
            start_phase=phase,
            log_tag=tag,
        )

    def run_new_daily_mode(
        self,
        use_foreground: bool = False,
        variant: str = "1",
        start_step: int = 1,
        *,
        skip_hero_tower: bool = False,
        from_daily_chain: bool = False,
        is_chain_entry_variant: bool = True,
    ) -> bool:
        """Dashboard「新日常」：按方案编号执行，可从指定步数起。"""
        v = (variant or "1").strip()
        step = max(1, int(start_step or 1))
        max_step = NEW_DAILY_VARIANT_MAX_STEPS.get(v)
        if max_step is None:
            self._emit(f"❌ 未知新日常方案：{v!r}", "ERROR")
            return False
        if step > max_step:
            self._emit(
                f"❌ 新日常方案 {v} 仅有 {max_step} 步，无法从第 {step} 步开始",
                "ERROR",
            )
            return False
        if v == "1":
            return self.run_new_daily_sequence_1(use_foreground, start_step=step)
        if v == "2":
            return self.run_new_daily_sequence_2(use_foreground, start_step=step)
        if v == "3":
            return self.run_new_daily_sequence_3(use_foreground, start_step=step)
        if v == "4":
            continue_from_previous = bool(
                from_daily_chain and not is_chain_entry_variant
            )
            return self.run_new_daily_sequence_4(
                use_foreground,
                start_step=step,
                reconnect_first_step=not continue_from_previous,
                inherit_jita_follow=continue_from_previous,
            )
        if v == "5":
            return self.run_new_daily_sequence_5(use_foreground, start_step=step)
        if v == "6":
            return self.run_new_daily_sequence_6(use_foreground, start_step=step)
        if v == "7":
            return self.run_new_daily_sequence_7(use_foreground, start_step=step)
        if v == "8":
            return self.run_new_daily_sequence_8(use_foreground, start_step=step)
        if v == "9":
            return self.run_new_daily_sequence_9(
                use_foreground,
                start_step=step,
                skip_hero_tower=skip_hero_tower,
                from_daily_chain=from_daily_chain,
            )
        self._emit(f"❌ 未知新日常方案：{v!r}", "ERROR")
        return False

    @staticmethod
    def _stop_signal_is_set(stop_signal: Optional[Any]) -> bool:
        if stop_signal is None:
            return False
        is_set = getattr(stop_signal, "is_set", None)
        return bool(callable(is_set) and is_set())

    def _abort_reason(self, stop_event: Optional[Any] = None) -> Optional[str]:
        if bool(getattr(self.bot, "user_stop_requested", False)):
            return "用户停止"
        if bool(getattr(self.bot, "stop_current", False)):
            return "stop_current"
        if self._stop_signal_is_set(stop_event):
            return "当前流程停止事件"
        task_stop_event = getattr(self.bot, "_stop_event", None)
        if task_stop_event is not stop_event and self._stop_signal_is_set(task_stop_event):
            return "任务停止事件"
        return None

    def _should_abort(self, stop_event: Optional[Any] = None) -> bool:
        return self._abort_reason(stop_event) is not None

    def _request_outer_mode_restart(self, reason: str) -> bool:
        """Ask BotWorker to reconnect and rerun the same top-level mode."""
        if not bool(getattr(self, "_outer_mode_restart_enabled", False)):
            return False
        bot = getattr(self, "bot", None)
        drr = getattr(bot, "dar_route_runner", None)
        stop_event = getattr(bot, "_stop_event", None)
        if drr is None or not isinstance(stop_event, threading.Event):
            self._emit(
                f"❌ [{reason}] 缺少模式重启控制器，无法请求重连",
                "ERROR",
            )
            return False
        requested = bool(drr._request_mode_restart(stop_event, reason))
        if requested:
            self._emit(
                f"🔄 [{reason}] 已向任务外层请求重连并重启同一模式",
                "SYSTEM",
            )
        return requested

    def _wait_if_paused(self):
        if hasattr(self.bot, "wait_if_paused") and callable(getattr(self.bot, "wait_if_paused")):
            self.bot.wait_if_paused()
            return

        while getattr(self.bot, "is_paused", False) and (not self._should_abort()):
            time.sleep(0.05)

    def _emit(self, text: str, level: str = "INFO"):
        self._track_one_click_daily_step_from_log(text)
        if hasattr(self.bot, "emit_and_log") and callable(getattr(self.bot, "emit_and_log")):
            self.bot.emit_and_log(text, level)
        else:
            try:
                self.bot.log_signal.emit(text, level)
            except Exception:
                pass
