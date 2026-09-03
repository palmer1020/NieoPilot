# tools/region_recorder.py

import os
import sys
import json
import time
import threading
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
saved = False
save_lock = threading.RLock()
active_category = ""
active_name = ""
console_handler = None

SAVE_EXIT_KEYS = frozenset(
    {
        keyboard.Key.esc,
        keyboard.Key.f10,
        keyboard.Key.f12,
    }
)


# ======================================================
# 工具函数
# ======================================================
def _configure_console_utf8():
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


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


def save_region(category, name):
    global saved
    with save_lock:
        if saved:
            return True

        point_snapshot = list(points)
        if len(point_snapshot) == 0:
            print("❌ 未录制任何点，放弃保存")
            return False

        # =========================
        # 构造最终 polygon
        # =========================
        if len(point_snapshot) == 1:
            px, py = point_snapshot[0]
            polygon = expand_single_point(px, py)
            print("ℹ️ 使用单点模式，自动生成 3x3 区域")
        elif len(point_snapshot) >= 3:
            polygon = point_snapshot
        else:
            print("❌ 点数不足（至少 1 或 ≥3），放弃保存")
            return False

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

        saved = True
        print("\n" + "=" * 40)
        print(f"💾 区域已保存：{path}")
        print(f"📍 原始点数：{len(point_snapshot)}")
        print(f"📍 最终 polygon 点数：{len(polygon)}")
        print("=" * 40)
        return True


def finish_recording(save=True):
    global recording
    recording = False
    if save and active_category and active_name:
        save_region(active_category, active_name)


def install_console_close_handler():
    global console_handler
    if os.name != "nt":
        return
    try:
        import ctypes

        close_events = {2, 5, 6}  # CTRL_CLOSE_EVENT / LOGOFF / SHUTDOWN
        handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        def handler(ctrl_type):
            if ctrl_type not in close_events:
                return False
            if recording and not saved:
                print("\n⏹ 控制台正在关闭，尝试保存录制内容...")
                finish_recording(save=True)
                time.sleep(0.2)
            return True

        console_handler = handler_type(handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(console_handler, True)
    except Exception as e:
        print(f"⚠️ 控制台关闭保存保护启用失败: {e}")


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

        with save_lock:
            points.append([gx, gy])
            point_count = len(points)
        print(f"✅ 记录点 {point_count}: ({gx}, {gy})")

    elif button == mouse.Button.right:
        with save_lock:
            removed = points.pop() if points else None
        if removed:
            print(f"↩ 撤销点: {removed}")
        else:
            print("⚠ 当前没有可撤销的点")


# ======================================================
# 键盘事件
# ======================================================
def on_key_press(key):
    if key in SAVE_EXIT_KEYS:
        key_name = getattr(key, "name", str(key))
        print(f"\n⏹ 收到 {key_name.upper()}，准备保存并退出...")
        finish_recording(save=True)
        return False


# ======================================================
# 主逻辑
# ======================================================
def main():
    global active_category, active_name
    _configure_console_utf8()
    print("\n=== Region Recorder（单点 / 多点区域） ===\n")

    category = input("分类 (category): ").strip()
    name = input("名称 (name): ").strip()

    if not category or not name:
        print("❌ 分类或名称不能为空")
        return

    active_category = category
    active_name = name
    install_console_close_handler()

    print("\n🖱 操作说明：")
    print("  左键 = 记录点（支持 1 个或多个）")
    print("  右键 = 撤销")
    print("  F10 / F12 / ESC = 保存并退出")
    print("  ⚠ 自动忽略 1200x700 以外的点\n")

    print("🔍 正在连接游戏窗口...")
    if not window_manager.launch_game():
        print("❌ 未找到游戏窗口")
        return

    print("✅ 游戏窗口已锁定，开始录制\n")

    try:
        with mouse.Listener(on_click=on_click) as m_listener:
            with keyboard.Listener(on_press=on_key_press) as k_listener:
                k_listener.join()
    except KeyboardInterrupt:
        print("\n⏹ Ctrl+C 中断，尝试保存...")
        finish_recording(save=True)

    save_region(category, name)
    return

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


