# tools/rare_mode_builder.py
"""
稀有精灵模式建立器

向导：模式名 / 精灵 ID / 地图 A→B / 11 点路线 → 写入 regions + wild_modes manifest。
"""
from __future__ import annotations

import json
import os
import sys
import time
import tkinter as tk
from tkinter import messagebox, simpledialog
from typing import List, Optional, Tuple

from pynput import keyboard, mouse

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import GAME_LOGIC_H, GAME_LOGIC_W
from core.nieo_mode_registry import (
    copy_route_points_1_to_9,
    find_route_archives_for_map,
)
from core.utils import window_manager
from core.wild_mode_registry import (
    build_manifest,
    save_manifest,
    parse_int_list,
)
from tools.map_recorder import MapRecorderApp, MapRecorderOptions, expand_single_point

FIX_SCRIPT_DIR = os.path.join(PROJECT_ROOT, "fix_script")
REGION_ROOT = os.path.join(PROJECT_ROOT, "assets", "regions")
MAP_CATEGORY = "地图"


def _configure_console_utf8() -> None:
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


def _set_dpi_awareness() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_set_dpi_awareness()


def _prompt(title: str, prompt: str) -> Optional[str]:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        val = simpledialog.askstring(title, prompt, parent=root)
    finally:
        root.destroy()
    return val


def _info(title: str, msg: str) -> None:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        messagebox.showinfo(title, msg, parent=root)
    finally:
        root.destroy()


def _choose_from_list(title: str, options: List[str]) -> Optional[int]:
    if not options:
        return None
    if len(options) == 1:
        return 0

    result = {"idx": None}

    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    lb = tk.Listbox(root, width=60, height=min(10, len(options)))
    for opt in options:
        lb.insert(tk.END, opt)
    lb.pack(padx=8, pady=8)
    lb.selection_set(0)

    def on_ok():
        sel = lb.curselection()
        if sel:
            result["idx"] = int(sel[0])
        root.destroy()

    def on_cancel():
        root.destroy()

    tk.Button(root, text="确定", command=on_ok).pack(side="left", padx=8, pady=8)
    tk.Button(root, text="取消", command=on_cancel).pack(side="right", padx=8, pady=8)
    root.mainloop()
    return result["idx"]


def _ensure_to_script(route_hint: str) -> Tuple[str, bool]:
    """返回 (to_script_name, was_created_blank)。"""
    to_name = f"to{route_hint}"
    path = os.path.join(FIX_SCRIPT_DIR, f"{to_name}.json")
    os.makedirs(FIX_SCRIPT_DIR, exist_ok=True)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            steps = data.get("steps") or []
            if isinstance(steps, list) and len(steps) > 0:
                print(f"✅ 已存在 {to_name}.json（{len(steps)} 步）")
                return to_name, False
        except Exception:
            pass
        print(f"ℹ {to_name}.json 已存在但 steps 为空")
        return to_name, False

    blank = {
        "name": to_name,
        "create_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "steps": [],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(blank, f, indent=4, ensure_ascii=False)
    print(f"📝 已创建空白 {path}")
    _info(
        "需要录制 to 脚本",
        f"已创建空白 fix_script/{to_name}.json。\n\n"
        "请先用 Dashboard「脚本录制器」录完从基地到地图 A 的导航，\n"
        "完成后重新运行本建立器继续（或跳过地图录制步骤后手动补全）。",
    )
    return to_name, True


def _write_map_transition_region(map_a: int, map_b: int, gx: int, gy: int) -> str:
    stem = f"{map_a}to{map_b}"
    save_dir = os.path.join(REGION_ROOT, MAP_CATEGORY)
    os.makedirs(save_dir, exist_ok=True)
    region = {
        "key": f"{MAP_CATEGORY}.{stem}",
        "category": MAP_CATEGORY,
        "name": stem,
        "shape": "polygon",
        "points": expand_single_point(gx, gy),
        "click": {"random": True},
        "meta": {
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "desc": f"稀有模式建立器：地图 {map_a} → {map_b}",
        },
    }
    path = os.path.join(save_dir, f"{stem}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(region, f, indent=4, ensure_ascii=False)
    return path


def _record_map_transition_click(map_a: int, map_b: int) -> Optional[str]:
    print("\n=== 步骤 6：录制地图 A → B 传送点 ===")
    print(f"请先在游戏中进入 **地图 {map_a}**，然后左键点击进 **地图 {map_b}** 的传送点。")
    print("右键撤销 | ESC 取消\n")

    if not window_manager.find_window():
        print("❌ 未找到游戏窗口")
        return None

    clicked: List[Tuple[int, int]] = []
    done = {"cancel": False}

    def on_click(x, y, button, pressed):
        if not pressed:
            return
        if button == mouse.Button.right:
            if clicked:
                clicked.pop()
                print("↩ 已撤销")
            return
        if button != mouse.Button.left:
            return
        res = window_manager.screen_to_game(x, y)
        if not res:
            print("❌ 点击不在游戏窗口内")
            return
        gx, gy = map(int, res)
        if not (0 <= gx <= GAME_LOGIC_W and 0 <= gy <= GAME_LOGIC_H):
            print(f"🚫 忽略非法点 ({gx}, {gy})")
            return
        clicked.clear()
        clicked.append((gx, gy))
        print(f"✅ 记录点: ({gx}, {gy})")

    def on_key(key):
        if key == keyboard.Key.esc:
            done["cancel"] = True
            return False
        if key == keyboard.Key.enter and clicked:
            return False

    print("🔍 游戏窗口已锁定，等待左键点击…")
    with mouse.Listener(on_click=on_click) as ml:
        with keyboard.Listener(on_press=on_key) as kl:
            kl.join()

    if done["cancel"] or not clicked:
        print("❌ 未保存地图传送点")
        return None

    gx, gy = clicked[0]
    path = _write_map_transition_region(map_a, map_b, gx, gy)
    print(f"💾 已保存: {path}")
    return path


def _run_map_recorder_ab_only(route_hint: str, map_b: int) -> bool:
    print(f"\n=== 在地图 {map_b} 上标注 A、B 两点（1-9 已从档案复制）===")
    print("Enter 截图 → 左键标注 → F10/ESC 保存")
    print("第 1 点 = A，第 2 点 = B\n")

    options = MapRecorderOptions(
        expect_points=2,
        start_order=10,
        emit_regions=True,
        route_prefix=route_hint,
        label_mode="rare",
    )
    app = MapRecorderApp(str(map_b), options)
    app.run()
    return True


def _setup_map_b_points(route_hint: str, map_b: int) -> Optional[str]:
    """有稀有/尼奥档案则复制 1-9 并只录 A/B；否则全录 11 点。返回 import_from 显示名。"""
    archives = find_route_archives_for_map(PROJECT_ROOT, map_b)
    if archives:
        labels = [
            f"[{kind}] {name} → regions/{prefix}/"
            for kind, name, prefix in archives
        ]
        idx = _choose_from_list(
            "选择要复用的档案（1-9 刷新点；含稀有模式与尼奥模式）",
            labels,
        )
        if idx is not None:
            kind, name, src_prefix = archives[idx]
            print(f"📥 从{kind}档案「{name}」复制 1-9 → {route_hint}/")
            copy_route_points_1_to_9(
                PROJECT_ROOT,
                src_prefix,
                route_hint,
                builder_name="稀有模式建立器",
            )
            _run_map_recorder_ab_only(route_hint, map_b)
            return f"{kind}:{name}"

    _run_map_recorder_11(route_hint, map_b)
    return None


def _run_map_recorder_11(route_hint: str, map_b: int) -> bool:
    print(f"\n=== 步骤 7：在地图 {map_b} 上标注 11 个点 ===")
    print("Enter 截图 → 左键标注 → F10/ESC 保存")
    print("1-9 = 刷新点，10 = A，11 = B\n")

    options = MapRecorderOptions(
        expect_points=11,
        emit_regions=True,
        route_prefix=route_hint,
    )
    app = MapRecorderApp(str(map_b), options)
    app.run()
    return True


def main() -> None:
    _configure_console_utf8()
    print("=== NieoPilot 稀有精灵模式建立器 ===\n")

    route_hint = _prompt("模式名称", "模式名称（如 埃尔特；同时作为 to 脚本名 to{Name}）：")
    if not route_hint or not route_hint.strip():
        print("已取消。")
        return
    route_hint = route_hint.strip()

    target_raw = _prompt("目标精灵 ID", "目标精灵 ID（多个用逗号分隔，如 123 或 102,143）：")
    if not target_raw:
        print("已取消。")
        return
    target_pets = parse_int_list(target_raw)
    if not target_pets:
        print("❌ 目标精灵 ID 无效")
        return

    exclude_raw = _prompt(
        "删除 SWF ID",
        "要从 pet/swf 删掉的精灵序号（逗号分隔，如 491,492,493；可留空）：",
    ) or ""
    delete_swf = parse_int_list(exclude_raw)

    map_a_raw = _prompt("地图 A", "地图 A 数字 ID（to 脚本后的地图零）：")
    map_b_raw = _prompt("地图 B", "地图 B 数字 ID（挂机目标地图一）：")
    if not map_a_raw or not map_b_raw:
        print("已取消。")
        return
    try:
        map_a = int(map_a_raw.strip())
        map_b = int(map_b_raw.strip())
    except ValueError:
        print("❌ 地图 ID 须为数字")
        return

    print(f"\n📋 模式：{route_hint} | 精灵 {target_pets} | 删除 swf {delete_swf or '无'}")
    print(f"   地图零 A={map_a} → 挂机 B={map_b}\n")

    _ensure_to_script(route_hint)

    if _record_map_transition_click(map_a, map_b) is None:
        cont = _prompt("继续？", "地图传送点未保存。输入 y 仍继续 11 点录制，其它取消：")
        if (cont or "").strip().lower() != "y":
            print("已取消。")
            return

    import_from = _setup_map_b_points(route_hint, map_b)

    manifest = build_manifest(
        route_hint=route_hint,
        map_a_id=map_a,
        map_b_id=map_b,
        target_pet_ids=target_pets,
        target_mp3_ids=target_pets,
        delete_swf_ids=delete_swf,
        import_from=import_from,
    )
    path = save_manifest(PROJECT_ROOT, manifest)
    print(f"\n✅ 模式 manifest 已保存: {path}")
    print(f"   slug={manifest['slug']} | 重启 Dashboard 后在「捕捉稀有精灵」下拉中选择")
    _info(
        "建立完成",
        f"模式「{route_hint}」已写入。\n\n"
        f"manifest: assets/wild_modes/{manifest['slug']}.json\n"
        f"regions: assets/regions/{route_hint}/\n"
        f"地图: assets/regions/地图/{map_a}to{map_b}.json\n\n"
        "请重启 Dashboard，在下拉框中选择该模式并启动。",
    )


if __name__ == "__main__":
    main()
