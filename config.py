# -*- coding: utf-8 -*-
# 本机配置（随仓库提交）。GAME_PATH 见下方 _resolve_nieo_game_exe（双机自动探测）。

import os


def _resolve_nieo_game_exe() -> str:
    """
    两台电脑共用同一份 config：按顺序选用第一个真实存在的 Nieo.exe。

    优先级：
    1. 环境变量 NIEO_GAME_PATH（临时覆盖，可不提交）
    2. GAME_EXE_CANDIDATES 列表（两台已知安装路径，装在新位置时只改这里）
    """
    override = os.environ.get("NIEO_GAME_PATH", "").strip().strip('"')
    if override:
        path = os.path.abspath(override)
        if os.path.isfile(path):
            return path

    local_app = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        # 电脑 B：微端 %LOCALAPPDATA%\\Programs\\nieo\\...
        os.path.join(local_app, "Programs", "nieo", "NieoGame", "Nieo.exe")
        if local_app
        else "",
        # 电脑 A：E 盘 nieo
        os.path.join("E:\\", "nieo", "NieoGame", "Nieo.exe"),
    ]
    for raw in candidates:
        if not raw:
            continue
        path = os.path.abspath(raw)
        if os.path.isfile(path):
            return path

    for raw in candidates:
        if raw:
            return os.path.abspath(raw)
    return os.path.abspath(os.path.join("E:\\", "nieo", "NieoGame", "Nieo.exe"))


# 1. 游戏主窗口标题（须与 Win32 标题栏文字完全一致）
WINDOW_TITLE = "尼尔号"

# 2. 游戏可执行文件（启动时自动解析，见 _resolve_nieo_game_exe）
GAME_PATH = _resolve_nieo_game_exe()

# 3. 轮换重连-步骤3：精灵仓库「单取」倒数位置（1~10）
ROTATION_NIEO_REVERSE_GROUND = 1
ROTATION_NIEO_REVERSE_FLIGHT = 1
ROTATION_NIEO_REVERSE_PSYCHIC_FIRST = 7
ROTATION_NIEO_REVERSE_PSYCHIC_SECOND = 9
ROTATION_SHUANGTA_REVERSE_GROUND = 1
ROTATION_SHUANGTA_REVERSE_FLIGHT = 4
ROTATION_SHUANGTA_REVERSE_PSYCHIC_FIRST = 3
ROTATION_SHUANGTA_REVERSE_PSYCHIC_SECOND = 7

# 4. 各系内连点「精灵仓库.右」次数（Pick 主要读 NIEO 飞行/超能）
ROTATION_NIEO_RIGHT_CLICKS_GROUND = 15
ROTATION_NIEO_RIGHT_CLICKS_FLIGHT = 25
ROTATION_NIEO_RIGHT_CLICKS_PSYCHIC = 80
ROTATION_SHUANGTA_RIGHT_CLICKS_GROUND = 15
ROTATION_SHUANGTA_RIGHT_CLICKS_FLIGHT = 25
ROTATION_SHUANGTA_RIGHT_CLICKS_PSYCHIC = 80

# 5. 脚本逻辑分辨率
GAME_LOGIC_W = 1200
GAME_LOGIC_H = 700

REAL_WIDTH = 1200
REAL_HEIGHT = 700
FIXED_RATIO = REAL_WIDTH / REAL_HEIGHT

SETTINGS_DIALOG_LOGIC_W = 1440
SETTINGS_DIALOG_LOGIC_H = 1000

# 6. 本工程路径
BASE_PATH = os.path.dirname(os.path.abspath(__file__))
ASSETS_PATH = os.path.join(BASE_PATH, "assets")
REGIONS_PATH = os.path.join(ASSETS_PATH, "regions")
TEMPLATES_PATH = os.path.join(ASSETS_PATH, "templates")
FIX_SCRIPT_PATH = os.path.join(BASE_PATH, "fix_script")

# 7. Tesseract OCR（按本机安装路径修改）
TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
TESSDATA_PREFIX = r"C:\Program Files\Tesseract-OCR\tessdata"

# 8. 游戏内资源路径
_GAME_EXE_DIR = os.path.dirname(os.path.abspath(GAME_PATH))
_GAME_NIEODATA_ROOT = os.path.join(_GAME_EXE_DIR, "NieoData")
GAME_ASSET_BASE_PATH = os.path.join(_GAME_NIEODATA_ROOT, "resource")

GAME_SWF_FOLDER = os.path.join(GAME_ASSET_BASE_PATH, "pet", "swf")
GAME_SWF_OG_FOLDER = os.path.join(GAME_ASSET_BASE_PATH, "pet", "swf_og")
GAME_SWF_OG_TEMPLATE = os.path.join(GAME_SWF_OG_FOLDER, "254.swf")

GAME_FIGHT_PET_SWF_DIR = os.path.join(
    GAME_ASSET_BASE_PATH, "fightResource", "pet", "swf"
)
GAME_FIGHT_SKILL_SWF_DIR = os.path.join(
    GAME_ASSET_BASE_PATH, "fightResource", "skill", "swf"
)
GAME_FIGHT_PET_SWF_OG_DIR = os.path.join(
    GAME_ASSET_BASE_PATH, "fightResource", "pet", "swf_og"
)
GAME_FIGHT_SKILL_SWF_OG_DIR = os.path.join(
    GAME_ASSET_BASE_PATH, "fightResource", "skill", "swf_og"
)

# 8b. 工程内模板 SWF → 覆盖到微端
PROJECT_PETSTORAGE_SWF = os.path.join(ASSETS_PATH, "PetStorage.swf")
PROJECT_TEMPLATE_254_SWF = os.path.join(BASE_PATH, "swf", "254.swf")
GAME_ROBOT_MODULE_APP = os.path.join(
    _GAME_NIEODATA_ROOT, "module", "com", "robot", "module", "app"
)
GAME_PETSTORAGE_SWF = os.path.join(GAME_ROBOT_MODULE_APP, "PetStorage.swf")
GAME_PETSTORAGE_OG_SWF = os.path.join(GAME_ROBOT_MODULE_APP, "PetStorage.og.swf")

# 9. 项目数据目录
HV_SAMPLES_PATH = os.path.join(BASE_PATH, "hv_samples")
BASELINE_DATA_PATH = os.path.join(BASE_PATH, "baseline_data")

# 10. 日常任务脚本顺序
DAILY_SEQUENCE = [
    "1", "2", "3", "4", "5", "6",
]
