# tools/region_recorder_settings_dialog.py
#
# 用途：
# - 录制「设置」子窗口(client)坐标系下的区域点
# - 适合刷新流程里的「刷新/保存」等按钮
# - 保存格式与现有 region_recorder 一致（polygon）

import os
import sys
import json
import time

import win32gui
from pynput import mouse, keyboard

# ======================================================
# 注入项目根路径
# ======================================================
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import SETTINGS_DIALOG_LOGIC_W, SETTINGS_DIALOG_LOGIC_H
from core.utils import window_manager


# ======================================================
# 常量
# ======================================================
REGION_ROOT = os.path.join(PROJECT_ROOT, "assets", "regions")
DEFAULT_CATEGORY = "设置窗口"

points = []
recording = True


def expand_single_point_logic(px: float, py: float, cw: int, ch: int):
    """单点模式：在逻辑坐标系中扩展约 1 个 client 像素对应的边长（与旧版 3×3 client 像素相当）。"""
    dx = max(1.0, float(SETTINGS_DIALOG_LOGIC_W) / max(1, cw))
    dy = max(1.0, float(SETTINGS_DIALOG_LOGIC_H) / max(1, ch))
    return [
        [px - dx, py - dy],
        [px + dx, py - dy],
        [px + dx, py + dy],
        [px - dx, py + dy],
    ]


def _get_settings_dialog_hwnd(timeout_s: float = 8.0) -> int:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        h = window_manager.find_settings_dialog_hwnd()
        if h:
            return h
        time.sleep(0.05)
    return 0


def _screen_to_settings_client(settings_hwnd: int, sx: int, sy: int):
    try:
        cx, cy = win32gui.ScreenToClient(settings_hwnd, (int(sx), int(sy)))
        return int(cx), int(cy)
    except Exception:
        return None


def _in_settings_client_bounds(settings_hwnd: int, cx: int, cy: int) -> bool:
    try:
        x1, y1, x2, y2 = win32gui.GetClientRect(settings_hwnd)
        return (x1 <= cx <= x2) and (y1 <= cy <= y2)
    except Exception:
        return False


def on_click_factory(settings_hwnd: int, cw: int, ch: int):
    lw, lh = float(SETTINGS_DIALOG_LOGIC_W), float(SETTINGS_DIALOG_LOGIC_H)

    def _on_click(x, y, button, pressed):
        global points
        if not pressed:
            return

        if button == mouse.Button.left:
            client_pt = _screen_to_settings_client(settings_hwnd, x, y)
            if not client_pt:
                print("❌ 无法转换到设置子窗口 client 坐标")
                return
            cx, cy = client_pt
            if not _in_settings_client_bounds(settings_hwnd, cx, cy):
                print(f"🚫 忽略窗口外点 ({cx}, {cy})")
                return
            lx = cx * lw / max(1, float(cw))
            ly = cy * lh / max(1, float(ch))
            points.append([lx, ly])
            print(
                f"✅ 记录点 {len(points)}: 逻辑({lx:.2f}, {ly:.2f}) "
                f"[client {cw}×{ch} 下对应像素 ({cx}, {cy})]"
            )
        elif button == mouse.Button.right:
            if points:
                removed = points.pop()
                print(f"↩ 撤销点: {removed}")
            else:
                print("⚠ 当前没有可撤销的点")

    return _on_click


def on_key_press(key):
    global recording
    if key == keyboard.Key.esc:
        recording = False
        return False


def main():
    print("\n=== Region Recorder（设置子窗口坐标系）===\n")
    print("请先在游戏中打开『刷新.设置』弹窗，并保持其可见。\n")

    category = input(f"分类 (category, 默认 {DEFAULT_CATEGORY}): ").strip() or DEFAULT_CATEGORY
    name = input("名称 (name): ").strip()
    if not name:
        print("❌ 名称不能为空")
        return

    print("\n🔍 正在连接游戏窗口...")
    if not window_manager.launch_game():
        print("❌ 未找到游戏窗口")
        return

    print("⏳ 正在等待『设置』子窗口（标题=设置）...")
    settings_hwnd = _get_settings_dialog_hwnd(timeout_s=8.0)
    if not settings_hwnd:
        print("❌ 未检测到『设置』子窗口，请先在游戏中打开设置弹窗后重试")
        return

    try:
        rx1, ry1, rx2, ry2 = win32gui.GetClientRect(settings_hwnd)
        w = max(1, int(rx2 - rx1))
        h = max(1, int(ry2 - ry1))
    except Exception:
        print("❌ 无法读取设置子窗口客户区大小，请重试")
        return

    dialog_meta = {}
    try:
        ratio = (float(w) / float(h)) if h > 0 else 0.0
        c_tl = (0, 0)
        c_tr = (w, 0)
        c_br = (w, h)
        c_bl = (0, h)
        s_tl = win32gui.ClientToScreen(settings_hwnd, c_tl)
        s_tr = win32gui.ClientToScreen(settings_hwnd, c_tr)
        s_br = win32gui.ClientToScreen(settings_hwnd, c_br)
        s_bl = win32gui.ClientToScreen(settings_hwnd, c_bl)
        dialog_meta = {
            "settings_hwnd": int(settings_hwnd),
            "settings_client_size": {"w": w, "h": h},
            "settings_client_aspect_ratio": round(ratio, 6),
            "settings_logic_ref": {
                "w": SETTINGS_DIALOG_LOGIC_W,
                "h": SETTINGS_DIALOG_LOGIC_H,
            },
            "settings_client_corners": {
                "tl": [c_tl[0], c_tl[1]],
                "tr": [c_tr[0], c_tr[1]],
                "br": [c_br[0], c_br[1]],
                "bl": [c_bl[0], c_bl[1]],
            },
            "settings_screen_corners": {
                "tl": [int(s_tl[0]), int(s_tl[1])],
                "tr": [int(s_tr[0]), int(s_tr[1])],
                "br": [int(s_br[0]), int(s_br[1])],
                "bl": [int(s_bl[0]), int(s_bl[1])],
            },
        }
        print(f"✅ 设置子窗口已锁定 HWND={settings_hwnd}，client={w}x{h}")
        print(
            "📐 四角(screen): "
            f"TL={dialog_meta['settings_screen_corners']['tl']} "
            f"TR={dialog_meta['settings_screen_corners']['tr']} "
            f"BR={dialog_meta['settings_screen_corners']['br']} "
            f"BL={dialog_meta['settings_screen_corners']['bl']}"
        )
        print(f"📏 宽高比(client): {w}:{h} ({ratio:.6f})")
    except Exception:
        print(f"⚠️ 锁定 HWND={settings_hwnd}，但未能写入完整几何元数据")

    print("\n🖱 操作说明：")
    print(
        f"  左键 = 记录点（标准化为逻辑 {SETTINGS_DIALOG_LOGIC_W}×{SETTINGS_DIALOG_LOGIC_H}，"
        "与主窗口 1200×700 同理）"
    )
    print("  右键 = 撤销")
    print("  ESC  = 保存并退出")
    print("  建议：单点录制按钮（自动扩展为与旧版相当的点击范围）\n")

    with mouse.Listener(on_click=on_click_factory(settings_hwnd, w, h)) as _m_listener:
        with keyboard.Listener(on_press=on_key_press) as k_listener:
            k_listener.join()

    if len(points) == 0:
        print("❌ 未录制任何点，放弃保存")
        return

    if len(points) == 1:
        px, py = points[0]
        polygon = expand_single_point_logic(float(px), float(py), w, h)
        print("ℹ️ 使用单点模式，在逻辑坐标中自动生成与旧版 3×3 像素相当的区域")
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
            "desc": "设置子窗口逻辑坐标 (1440×1000)，运行时按实际 client 缩放",
            "coord_space": "settings_dialog_logic",
            **dialog_meta,
        },
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
