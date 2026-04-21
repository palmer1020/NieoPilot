# -*- coding: utf-8 -*-
# 使用方式：复制本文件为同目录下的 config.py，按本机修改路径。
# 仓库只提交本模板；config.py 已在 .gitignore 中，不会被提交。
# 运行 gui/main 时若缺少 config.py，会从本模板自动生成一份（见 config_bootstrap.py）。

import os

# 1. 游戏主窗口标题（须与 Win32 标题栏文字完全一致，含大小写；用于 FindWindow）
#    微端若改名，仍以任务栏/窗口左上角显示为准；与 GAME_PATH 里 exe 名称无关。
WINDOW_TITLE = "尼尔号"

# 2. 游戏可执行文件（微端，按本机安装路径修改）
GAME_PATH = r"C:\Users\dayuz\AppData\Local\Programs\nieo\NieoGame\Nieo.exe"

# 3. 脚本逻辑分辨率（写脚本坐标系，一般保持 1200×700）
GAME_LOGIC_W = 1200
GAME_LOGIC_H = 700

REAL_WIDTH = 1200
REAL_HEIGHT = 700
FIXED_RATIO = REAL_WIDTH / REAL_HEIGHT

# 4. 本工程路径（一般无需改）
BASE_PATH = os.path.dirname(os.path.abspath(__file__))
ASSETS_PATH = os.path.join(BASE_PATH, "assets")
REGIONS_PATH = os.path.join(ASSETS_PATH, "regions")
TEMPLATES_PATH = os.path.join(ASSETS_PATH, "templates")
FIX_SCRIPT_PATH = os.path.join(BASE_PATH, "fix_script")

# 5. Tesseract OCR（按本机安装路径修改）
TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
TESSDATA_PREFIX = r"C:\Program Files\Tesseract-OCR\tessdata"

# 6. 游戏内资源路径（仅前缀随本机 GAME_PATH 变化；NieoData 之后相对路径可保持默认）
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
# 对战 SWF 的 OG 备份目录（与 resource 下相对布局一致）
GAME_FIGHT_PET_SWF_OG_DIR = os.path.join(
    GAME_ASSET_BASE_PATH, "fightResource", "pet", "swf_og"
)
GAME_FIGHT_SKILL_SWF_OG_DIR = os.path.join(
    GAME_ASSET_BASE_PATH, "fightResource", "skill", "swf_og"
)

# 6b. 工程内模板 SWF → 覆盖到微端（见 core/swf_resource_ops.py、swf/sync_project_templates.py）
PROJECT_PETSTORAGE_SWF = os.path.join(ASSETS_PATH, "PetStorage.swf")
PROJECT_TEMPLATE_254_SWF = os.path.join(BASE_PATH, "swf", "254.swf")
# 宠物仓库（PetStorage）微端目录：NieoData\module\com\robot\module\app（与 resource 并列）
# 示例（仅说明形态；实际由 GAME_PATH 推导）：...NieoGame\NieoData\module\com\robot\module\app
GAME_ROBOT_MODULE_APP = os.path.join(
    _GAME_NIEODATA_ROOT, "module", "com", "robot", "module", "app"
)
GAME_PETSTORAGE_SWF = os.path.join(GAME_ROBOT_MODULE_APP, "PetStorage.swf")
# 与 PetStorage 同目录的 OG 备份文件名
GAME_PETSTORAGE_OG_SWF = os.path.join(GAME_ROBOT_MODULE_APP, "PetStorage.og.swf")

# 7. 项目数据目录（相对工程根）
HV_SAMPLES_PATH = os.path.join(BASE_PATH, "hv_samples")
BASELINE_DATA_PATH = os.path.join(BASE_PATH, "baseline_data")

# 8. 日常任务脚本顺序（fix_script 内文件名，无 .json 后缀）
DAILY_SEQUENCE = [
    "A", "B", "C", "D", "E", "F",
]

DAILY_SEQUENCE0 = [
    "登录",
]
