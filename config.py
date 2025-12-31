# config.py

# 1. 游戏窗口标题
WINDOW_TITLE = "nieo" 

# 2. 游戏启动路径
GAME_PATH = r"C:\Users\dayuz\AppData\Local\Programs\nieo\nieo.exe"

# 3. 脚本逻辑分辨率 (写脚本时用的坐标系，保持 1200x700 不变，方便写逻辑)
GAME_LOGIC_W = 1200
GAME_LOGIC_H = 700

# 4. ★★★ 新增：真实测量比例 (系统锚点) ★★★
# 这是你刚才测量出来的“黄金尺寸”
REAL_WIDTH = 1200
REAL_HEIGHT = 700
# 计算出固定的宽高比 (约 1.7154)
FIXED_RATIO = REAL_WIDTH / REAL_HEIGHT 

# 5. 路径配置
import os
BASE_PATH = os.path.dirname(os.path.abspath(__file__))
ASSETS_PATH = os.path.join(BASE_PATH, "assets")

# ===============================================================
# 6. 日常任务执行顺序
# ===============================================================
# 这里填写你在 fix_script 文件夹里录好的文件名 (不需要 .json 后缀)

DAILY_SEQUENCE = [
     "A","B","C","D","E","F"
]

DAILY_SEQUENCE0 = [
    "登录"
]
