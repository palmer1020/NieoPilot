# -*- coding: utf-8 -*-
# 本机配置（随仓库提交）。微端默认：%LOCALAPPDATA%\Programs\nieo\NieoGame\Nieo.exe

import os

# 1. 游戏主窗口标题（须与 Win32 标题栏文字完全一致）
WINDOW_TITLE = "尼尔号"

# 2. 游戏可执行文件（微端）
#    另一台机示例：C:\Users\dayuz\AppData\Local\Programs\nieo\NieoGame\Nieo.exe
#    也可改为盘符根，例如 LOCAL_NIEO_ROOT = r"E:\\" → E:\nieo\NieoGame\Nieo.exe
LOCAL_NIEO_ROOT = os.path.join(
    os.environ.get("LOCALAPPDATA", r"C:\Users\dayuz\AppData\Local"),
    "Programs",
)
GAME_PATH = os.path.join(LOCAL_NIEO_ROOT, "nieo", "NieoGame", "Nieo.exe")

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
