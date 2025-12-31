import sys
import os
import time
import ctypes
import win32gui
import win32con
from PIL import ImageGrab, ImageDraw

# =========================================================
# 路径配置
# =========================================================
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

from config import WINDOW_TITLE

# 开启高 DPI 感知
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except:
    ctypes.windll.user32.SetProcessDPIAware()

def is_same_color(c1, c2, threshold=10):
    """判断颜色是否接近"""
    return abs(c1[0]-c2[0])<threshold and \
           abs(c1[1]-c2[1])<threshold and \
           abs(c1[2]-c2[2])<threshold

def main():
    print("🚀 启动【黄金比例】测量工具...")
    
    hwnd = win32gui.FindWindow(None, WINDOW_TITLE)
    if not hwnd:
        print(f"❌ 未找到窗口 [{WINDOW_TITLE}]")
        return

    print("⏳ 正在最大化窗口并等待渲染 (3秒)...")
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    #win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(3.0)

    # 1. 截图整个 Client 区域
    l, t, r, b = win32gui.GetClientRect(hwnd)
    client_point = win32gui.ClientToScreen(hwnd, (0, 0))
    origin_x, origin_y = client_point
    
    w, h = r - l, b - t
    bbox = (origin_x, origin_y, origin_x + w, origin_y + h)
    
    print(f"📸 截图分析中... (区域: {w}x{h})")
    img = ImageGrab.grab(bbox, all_screens=True)
    pixels = img.load()
    
    cx, cy = w // 2, h // 2

    # 2. 采样背景色 (假设四个角落都是背景/黑边/白边)
    # 我们取左侧边缘中间的颜色作为基准
    bg_color = pixels[0, cy]
    top_bg_color = pixels[cx, 0] # 顶部可能是灰色菜单栏

    # 3. 极精细扫描
    # -------------------------------------------------
    
    # 找左边界 (Left)
    real_left = 0
    for x in range(cx):
        if not is_same_color(pixels[x, cy], bg_color):
            real_left = x
            break

    # 找右边界 (Right)
    real_right = w - 1
    for x in range(w - 1, cx, -1):
        if not is_same_color(pixels[x, cy], bg_color):
            real_right = x
            break

    # 找上边界 (Top) - 避开菜单栏
    real_top = 0
    for y in range(cy):
        # 既不像顶部背景，也不像侧边背景，说明进入游戏了
        if not is_same_color(pixels[cx, y], top_bg_color) and not is_same_color(pixels[cx, y], bg_color):
            real_top = y
            break

    # 找下边界 (Bottom)
    real_bottom = h - 1
    for y in range(h - 1, cy, -1):
        if not is_same_color(pixels[cx, y], bg_color):
            real_bottom = y
            break

    # 4. 计算结果
    game_w = real_right - real_left + 1
    game_h = real_bottom - real_top + 1
    ratio = game_w / game_h
    
    print("\n" + "="*50)
    print("📏 测量结果 (请把这些数字发给我)")
    print("="*50)
    print(f"   顶部偏移 (Menu Bar): {real_top} px")
    print(f"   左侧黑边 (Left Pad): {real_left} px")
    print("-" * 30)
    print(f"   📺 游戏真实宽度: {game_w}")
    print(f"   📺 游戏真实高度: {game_h}")
    print(f"   ➗ 真实宽高比: {ratio:.6f}")
    print("="*50)

    # 常见比例参考
    ratios = {
        (12, 7): 12/7,      # 1.714286
        (16, 9): 16/9,      # 1.777778
        (5, 3): 5/3,        # 1.666667 (例如 960x576)
        (960, 560): 960/560, # 1.714286 (同 12:7)
        (1000, 600): 1000/600 # 1.6666
    }
    
    print("\n🔍 正在匹配常见比例...")
    best_match = None
    min_diff = 999
    
    for (rw, rh), val in ratios.items():
        diff = abs(ratio - val)
        if diff < min_diff:
            min_diff = diff
            best_match = (rw, rh)
    
    if min_diff < 0.01:
        print(f"✅ 完美匹配标准比例: {best_match[0]}:{best_match[1]}")
    else:
        print(f"⚠ 这是一个非标准比例，建议直接使用测量出的 {game_w}x{game_h}")

    # 5. 生成验证图
    draw = ImageDraw.Draw(img)
    draw.rectangle([(real_left, real_top), (real_right, real_bottom)], outline="red", width=3)
    save_path = os.path.join(project_root, "debug_measure_ratio.png")
    img.save(save_path)
    print(f"\n💾 验证图已保存: {save_path} (请确认红框是否完美)")

if __name__ == "__main__":
    main()

