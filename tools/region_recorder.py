# tools/region_recorder.py

import os
import sys
import json
import time
from pynput import mouse, keyboard

# ======================================================
# 🔧 注入项目根路径
# ======================================================
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.utils import window_manager

# ======================================================
# 常量配置
# ======================================================
MAX_W = 1200
MAX_H = 700

REGION_ROOT = os.path.join(PROJECT_ROOT, "assets", "regions")

points = []
recording = True


# ======================================================
# 工具函数
# ======================================================
def in_bounds(x, y):
    return 0 <= x <= MAX_W and 0 <= y <= MAX_H


def expand_single_point(px, py):
    """以一个点为中心，生成 3x3 的矩形 polygon"""
    return [
        [px - 1, py - 1],
        [px + 1, py - 1],
        [px + 1, py + 1],
        [px - 1, py + 1],
    ]


# ======================================================
# 鼠标事件
# ======================================================
def on_click(x, y, button, pressed):
    global points
    if not pressed:
        return

    if button == mouse.Button.left:
        res = window_manager.screen_to_game(x, y)
        if not res:
            print("❌ 点击不在游戏窗口内")
            return

        gx, gy = map(int, res)

        # 边界检查
        if not in_bounds(gx, gy):
            print(f"🚫 忽略非法点 ({gx}, {gy}) 超出 1200x700")
            return

        points.append([gx, gy])
        print(f"✅ 记录点 {len(points)}: ({gx}, {gy})")

    elif button == mouse.Button.right:
        if points:
            removed = points.pop()
            print(f"↩ 撤销点: {removed}")
        else:
            print("⚠ 当前没有可撤销的点")


# ======================================================
# 键盘事件
# ======================================================
def on_key_press(key):
    global recording
    if key == keyboard.Key.esc:
        recording = False
        return False


# ======================================================
# 主逻辑
# ======================================================
def main():
    print("\n=== Region Recorder（单点 / 多点区域） ===\n")

    category = input("分类 (category): ").strip()
    name = input("名称 (name): ").strip()

    if not category or not name:
        print("❌ 分类或名称不能为空")
        return

    print("\n🖱 操作说明：")
    print("  左键 = 记录点（支持 1 个或多个）")
    print("  右键 = 撤销")
    print("  ESC  = 保存并退出")
    print("  ⚠ 自动忽略 1200x700 以外的点\n")

    print("🔍 正在连接游戏窗口...")
    if not window_manager.launch_game():
        print("❌ 未找到游戏窗口")
        return

    print("✅ 游戏窗口已锁定，开始录制\n")

    with mouse.Listener(on_click=on_click) as m_listener:
        with keyboard.Listener(on_press=on_key_press) as k_listener:
            k_listener.join()

    if len(points) == 0:
        print("❌ 未录制任何点，放弃保存")
        return

    # =========================
    # 构造最终 polygon
    # =========================
    if len(points) == 1:
        px, py = points[0]
        polygon = expand_single_point(px, py)
        print("ℹ️ 使用单点模式，自动生成 3x3 区域")
    elif len(points) >= 3:
        polygon = points
    else:
        print("❌ 点数不足（至少 1 或 ≥3），放弃保存")
        return

    region = {
        "key": f"{category}.{name}",
        "category": category,
        "name": name,
        "shape": "polygon",
        "points": polygon,
        "click": {"random": True},
        "meta": {
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "desc": ""
        }
    }

    save_dir = os.path.join(REGION_ROOT, category)
    os.makedirs(save_dir, exist_ok=True)

    path = os.path.join(save_dir, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(region, f, indent=4, ensure_ascii=False)

    print("\n" + "=" * 40)
    print(f"💾 区域已保存：{path}")
    print(f"📐 原始点数：{len(points)}")
    print(f"📐 最终 polygon 点数：{len(polygon)}")
    print("=" * 40)


if __name__ == "__main__":
    main()


