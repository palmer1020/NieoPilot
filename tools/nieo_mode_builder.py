# tools/nieo_mode_builder.py
"""
尼奥模式建立器

向导：模式名 / 三地图 A-B-C / 五条传送（A→B, B→C, C→B, B→A, A→B回写）/
B·C 操作 / 点录入 → regions + nieo_modes manifest。
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
    build_nieo_manifest,
    copy_map_transition_to_prefix,
    copy_route_points_1_to_9,
    duplicate_region_label,
    find_route_archives_for_map,
    save_nieo_manifest,
    write_region_point,
)
from core.utils import window_manager
from tools.map_recorder import MapRecorderApp, MapRecorderOptions, expand_single_point

FIX_SCRIPT_DIR = os.path.join(PROJECT_ROOT, "fix_script")
REGION_ROOT = os.path.join(PROJECT_ROOT, "assets", "regions")
MAP_CATEGORY = "地图"

ACTION_LABELS = {
    "capture": "捕捉",
    "defeat": "战胜",
    "skip": "跳过",
}


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


def _prompt_action(map_label: str) -> Optional[str]:
    raw = _prompt(
        f"地图 {map_label} 操作",
        "输入操作：捕捉 / 战胜 / 跳过（或 capture / defeat / skip）：",
    )
    if not raw:
        return None
    s = raw.strip().lower()
    mapping = {
        "捕捉": "capture",
        "战胜": "defeat",
        "跳过": "skip",
        "capture": "capture",
        "defeat": "defeat",
        "skip": "skip",
        "1": "capture",
        "2": "defeat",
        "3": "skip",
    }
    action = mapping.get(s) or mapping.get(raw.strip())
    if action not in ACTION_LABELS:
        print(f"❌ 无效操作：{raw}")
        return None
    print(f"✅ 地图 {map_label} 操作：{ACTION_LABELS[action]}")
    return action


def _parse_int_list(raw: str) -> Tuple[int, ...]:
    values = set()
    for part in str(raw or "").replace("，", ",").split(","):
        text = part.strip()
        if not text:
            continue
        try:
            value = int(text)
        except ValueError:
            continue
        if value > 0:
            values.add(value)
    return tuple(sorted(values))


def _prompt_action_pet_ids(map_label: str, action: str) -> Optional[Tuple[int, ...]]:
    if action == "skip":
        return ()
    action_label = "捕捉目标" if action == "capture" else "战胜目标"
    raw = _prompt(
        f"地图 {map_label} {action_label} pet ID",
        f"输入地图 {map_label} 的{action_label} pet ID（多个用逗号分隔）：",
    )
    if raw is None:
        return None
    values = _parse_int_list(raw)
    if not values:
        print(f"❌ 地图 {map_label} {action_label} pet ID 无效")
        return None
    print(f"✅ 地图 {map_label} {action_label} pet ID：{values}")
    return values


def _prompt_skip_route_points(map_label: str) -> Tuple[str, ...]:
    raw = _prompt(
        f"地图 {map_label} 屏蔽刷新点",
        f"地图 {map_label} 不参与扫描的区域编号（1-9，多个用逗号分隔；可留空）：",
    ) or ""
    points = []
    for value in _parse_int_list(raw):
        if 1 <= value <= 9:
            points.append(str(value))
    result = tuple(points)
    print(f"ℹ 地图 {map_label} 屏蔽刷新点：{result or '无'}")
    return result


def _ensure_to_script(route_hint: str) -> Tuple[str, bool]:
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
        "完成后重新运行本建立器继续。",
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
            "desc": f"尼奥模式建立器：地图 {map_a} → {map_b}",
        },
    }
    path = os.path.join(save_dir, f"{stem}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(region, f, indent=4, ensure_ascii=False)
    return path


def _record_map_transition_click(
    map_a: int, map_b: int, step_desc: str
) -> Optional[str]:
    print(f"\n=== {step_desc} ===")
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


def _record_z_point(prefix: str, map_id: int) -> bool:
    print(f"\n=== 录制 {prefix}.Z 点（地图 {map_id}）===")
    print("左键点击 Z 点（离开刷新点）位置 | ESC 取消\n")

    if not window_manager.find_window():
        print("❌ 未找到游戏窗口")
        return False

    clicked: List[Tuple[int, int]] = []
    done = {"cancel": False}

    def on_click(x, y, button, pressed):
        if not pressed:
            return
        if button == mouse.Button.right:
            if clicked:
                clicked.pop()
            return
        if button != mouse.Button.left:
            return
        res = window_manager.screen_to_game(x, y)
        if not res:
            return
        gx, gy = map(int, res)
        clicked.clear()
        clicked.append((gx, gy))
        print(f"✅ Z 点: ({gx}, {gy})")

    def on_key(key):
        if key == keyboard.Key.esc:
            done["cancel"] = True
            return False
        if key == keyboard.Key.enter and clicked:
            return False

    with mouse.Listener(on_click=on_click) as ml:
        with keyboard.Listener(on_press=on_key) as kl:
            kl.join()

    if done["cancel"] or not clicked:
        return False

    gx, gy = clicked[0]
    path = write_region_point(PROJECT_ROOT, prefix, "Z", gx, gy, desc="尼奥模式 Z 点")
    print(f"💾 已保存: {path}")
    return True


def _setup_map_points(
    map_id: int,
    prefix: str,
    action: str,
) -> Optional[str]:
    """若 action 为 skip 则跳过；否则复用稀有 1-9 或全录 10 点。返回 import_from route_hint 或 None。"""
    if action == "skip":
        print(f"⏭ 地图 {map_id}（{prefix}）操作为跳过，不录入刷新点")
        return None

    archives = find_route_archives_for_map(PROJECT_ROOT, map_id)
    import_from: Optional[str] = None

    if archives:
        labels = [
            f"[{kind}] {name} → regions/{prefix}/"
            for kind, name, prefix in archives
        ]
        idx = _choose_from_list(
            "选择要复用的档案（1-9 点；含稀有模式与尼奥模式）",
            labels,
        )
        if idx is not None:
            kind, name, src_prefix = archives[idx]
            import_from = f"{kind}:{name}"
            print(f"📥 从{kind}档案「{name}」复制 1-9 → {prefix}/")
            copy_route_points_1_to_9(
                PROJECT_ROOT,
                src_prefix,
                prefix,
                builder_name="尼奥模式建立器",
            )
            if not _record_z_point(prefix, map_id):
                cont = _prompt("继续？", "Z 点未保存。输入 y 仍继续，其它取消：")
                if (cont or "").strip().lower() != "y":
                    return None
            return import_from

    print(f"\n=== 在地图 {map_id} 上标注 10 个点（1-9 刷新 + Z）===")
    print("Enter 截图 → 左键标注 → F10/ESC 保存\n")
    options = MapRecorderOptions(
        expect_points=10,
        emit_regions=True,
        route_prefix=prefix,
        label_mode="nieo",
    )
    app = MapRecorderApp(str(map_id), options)
    app.run()
    return None


def main() -> None:
    _configure_console_utf8()
    print("=== NieoPilot 尼奥模式建立器 ===\n")

    route_hint = _prompt("模式名称", "模式名称（如 大地之核；同时作为 to 脚本名 to{Name}）：")
    if not route_hint or not route_hint.strip():
        print("已取消。")
        return
    route_hint = route_hint.strip()

    map_a_raw = _prompt("地图 A", "地图 A ID（to 脚本后的入口/恢复图）：")
    map_b_raw = _prompt("地图 B", "地图 B ID（第一张挂机图）：")
    map_c_raw = _prompt("地图 C", "地图 C ID（第二张挂机图）：")
    if not map_a_raw or not map_b_raw or not map_c_raw:
        print("已取消。")
        return
    try:
        map_a = int(map_a_raw.strip())
        map_b = int(map_b_raw.strip())
        map_c = int(map_c_raw.strip())
    except ValueError:
        print("❌ 地图 ID 须为数字")
        return

    same_bc_map = map_b == map_c
    prefix_b = f"{route_hint}一"
    prefix_c = prefix_b if same_bc_map else f"{route_hint}二"
    prefix_a = (
        prefix_b
        if map_a == map_b
        else f"{route_hint}二"
        if same_bc_map
        else prefix_c
    )

    print(f"\n📋 模式：{route_hint} | A={map_a} B={map_b} C={map_c}")
    print(f"   前缀 B={prefix_b} | 前缀 C/A={prefix_c}\n")

    delete_swf_raw = _prompt(
        "删除 SWF ID",
        "启动模式时要从 pet/swf 删除的精灵 ID（多个用逗号分隔；可留空）：",
    ) or ""
    delete_swf_ids = _parse_int_list(delete_swf_raw)
    print(f"ℹ 删除 SWF ID：{delete_swf_ids or '无'}；PetSWF=普通")

    no_spawn_raw = _prompt(
        "B 图无刷新超时",
        "B 图多少秒未检测到突变就放弃本轮并切回 A（0 或留空表示不启用）：",
    ) or "0"
    try:
        b_no_spawn_giveup_s = max(0.0, float(no_spawn_raw.strip()))
    except ValueError:
        print(f"❌ 无刷新超时无效：{no_spawn_raw}")
        return
    b_entry_white_probe_key = (
        _prompt(
            "B 图入图白探针",
            "B 图没有 newNPC 时使用的白探针区域 key（可留空，如 尼奥一.白色探针）：",
        )
        or ""
    ).strip()

    _ensure_to_script(route_hint)

    # 步骤 5：录入 A→B 传送点
    a_to_b_stem = f"{map_a}to{map_b}"
    if _record_map_transition_click(map_a, map_b, f"步骤 5：录入 A→B（{a_to_b_stem}）") is None:
        cont = _prompt("继续？", "A→B 未保存。输入 y 仍继续，其它取消：")
        if (cont or "").strip().lower() != "y":
            print("已取消。")
            return

    b_to_c_stem = f"{map_b}to{map_c}"
    c_to_b_stem = f"{map_c}to{map_b}"
    if same_bc_map:
        print(
            f"\nℹ B/C 同为 map{map_b}，按单挂机图处理，"
            "跳过 B→C 与 C→B 传送点录入"
        )
    else:
        # 步骤 6：录入 B→C 传送点
        if _record_map_transition_click(map_b, map_c, f"步骤 6：录入 B→C（{b_to_c_stem}）") is None:
            cont = _prompt("继续？", "B→C 未保存。输入 y 仍继续，其它取消：")
            if (cont or "").strip().lower() != "y":
                print("已取消。")
                return

        # 步骤 7：录入 C→B 传送点
        if _record_map_transition_click(map_c, map_b, f"步骤 7：录入 C→B（{c_to_b_stem}）") is None:
            cont = _prompt("继续？", "C→B 未保存。输入 y 仍继续，其它取消：")
            if (cont or "").strip().lower() != "y":
                print("已取消。")
                return

    # 步骤 8：录入 B→A 传送点（维护回图用）
    b_to_a_stem = f"{map_b}to{map_a}"
    if map_a == map_b:
        print(f"\nℹ A/B 同为 map{map_a}，跳过 B→A 传送点录入")
    elif map_a == map_c:
        # A 与 C 是同一张图，B→A 与 B→C 同一传送点，直接复用
        print(f"\nℹ A/C 同为 map{map_a}，B→A 传送点与 B→C 相同，跳过单独录入")
    else:
        if _record_map_transition_click(map_b, map_a, f"步骤 8：录入 B→A（{b_to_a_stem}）") is None:
            cont = _prompt("继续？", "B→A 未保存。输入 y 仍继续，其它取消：")
            if (cont or "").strip().lower() != "y":
                print("已取消。")
                return

    # 将传送点坐标写入各 prefix 的 region 文件
    if same_bc_map:
        if map_a != map_b:
            copy_map_transition_to_prefix(PROJECT_ROOT, b_to_a_stem, prefix_b, "BtoA")
            copy_map_transition_to_prefix(PROJECT_ROOT, a_to_b_stem, prefix_a, "AtoB")
            print(
                f"   ✅ B/C 单图：{prefix_b}.BtoA（{b_to_a_stem}）/ "
                f"{prefix_a}.AtoB（{a_to_b_stem}）已写入"
            )
    elif map_a == map_c:
        copy_map_transition_to_prefix(PROJECT_ROOT, b_to_c_stem, prefix_b, "BtoC")
        copy_map_transition_to_prefix(PROJECT_ROOT, c_to_b_stem, prefix_c, "CtoB")
        # A==C：BtoA 复制自 BtoC，AtoB 复制自 CtoB
        duplicate_region_label(
            PROJECT_ROOT, prefix_b, "BtoC", "BtoA",
            desc=f"A==C（map{map_a}），复制自 BtoC"
        )
        duplicate_region_label(
            PROJECT_ROOT, prefix_c, "CtoB", "AtoB",
            desc=f"A==C（map{map_a}），复制自 CtoB"
        )
        print(f"   ℹ A/C 同图：{prefix_b}.BtoA ← BtoC，{prefix_c}.AtoB ← CtoB")
    else:
        copy_map_transition_to_prefix(PROJECT_ROOT, b_to_c_stem, prefix_b, "BtoC")
        copy_map_transition_to_prefix(PROJECT_ROOT, c_to_b_stem, prefix_c, "CtoB")
        # A≠C：分别复制 BtoA 和 AtoB
        copy_map_transition_to_prefix(PROJECT_ROOT, b_to_a_stem, prefix_b, "BtoA")
        copy_map_transition_to_prefix(PROJECT_ROOT, a_to_b_stem, prefix_a, "AtoB")
        print(f"   ✅ {prefix_b}.BtoA（{b_to_a_stem}）/ {prefix_a}.AtoB（{a_to_b_stem}）已写入")

    # 操作 + 点录入
    action_b = _prompt_action("B")
    if not action_b:
        print("已取消。")
        return
    action_pet_ids_b = _prompt_action_pet_ids("B", action_b)
    if action_pet_ids_b is None:
        print("已取消。")
        return

    import_from_b = _setup_map_points(map_b, prefix_b, action_b)
    skip_route_points_b = (
        _prompt_skip_route_points("B") if action_b != "skip" else ()
    )

    if same_bc_map:
        action_c = action_b
        action_pet_ids_c = action_pet_ids_b
        import_from_c = import_from_b
        skip_route_points_c = skip_route_points_b
        print(f"ℹ B/C 同图：C 复用 B 的动作与刷新点（{ACTION_LABELS[action_b]}）")
    else:
        action_c = _prompt_action("C")
        if not action_c:
            print("已取消。")
            return
        action_pet_ids_c = _prompt_action_pet_ids("C", action_c)
        if action_pet_ids_c is None:
            print("已取消。")
            return

        import_from_c = _setup_map_points(map_c, prefix_c, action_c)
        skip_route_points_c = (
            _prompt_skip_route_points("C") if action_c != "skip" else ()
        )

    rare_capture_pets_b = action_pet_ids_b if action_b == "capture" else ()
    rare_capture_pets_c = action_pet_ids_c if action_c == "capture" else ()
    battle_pet_ids_b = action_pet_ids_b if action_b == "defeat" else ()
    battle_pet_ids_c = action_pet_ids_c if action_c == "defeat" else ()

    manifest = build_nieo_manifest(
        route_hint=route_hint,
        map_a_id=map_a,
        map_b_id=map_b,
        map_c_id=map_c,
        action_b=action_b,
        action_c=action_c,
        import_from_b=import_from_b,
        import_from_c=import_from_c,
        skip_route_points_b=skip_route_points_b,
        skip_route_points_c=skip_route_points_c,
        prefix_b=prefix_b,
        prefix_c=prefix_c,
        prefix_a=prefix_a,
        rare_capture_pets_b=rare_capture_pets_b,
        rare_capture_pets_c=rare_capture_pets_c,
        battle_pet_ids_b=battle_pet_ids_b,
        battle_pet_ids_c=battle_pet_ids_c,
        delete_swf_ids=delete_swf_ids,
        pet254_mode="normal",
        b_no_spawn_giveup_s=b_no_spawn_giveup_s,
        b_entry_white_probe_key=b_entry_white_probe_key,
    )
    path = save_nieo_manifest(PROJECT_ROOT, manifest)
    print(f"\n✅ 尼奥模式 manifest 已保存: {path}")
    print(f"   slug={manifest['slug']} | 重启 Dashboard 后在尼奥下拉中选择")

    if same_bc_map:
        btoa_note = (
            f"B==C 单挂机图（{map_b}）：只使用 {a_to_b_stem}/{b_to_a_stem}，"
            "不创建 B↔C 传送点\n"
        )
    elif map_a == map_c:
        btoa_note = f"A==C 同图（{map_a}）：BtoA/AtoB 复制自 BtoC/CtoB\n"
    else:
        btoa_note = (
            f"传送点（含 BtoA/AtoB）：{a_to_b_stem}, {b_to_c_stem}, "
            f"{c_to_b_stem}, {b_to_a_stem}\n"
        )
    _info(
        "建立完成",
        f"尼奥模式「{route_hint}」已写入。\n\n"
        f"manifest: assets/nieo_modes/{manifest['slug']}.json\n"
        f"B 图 regions: assets/regions/{prefix_b}/\n"
        f"C 图 regions: assets/regions/{prefix_c}/\n"
        f"{btoa_note}"
        f"每 25 场在地图 A（{map_a}）恢复精灵一，再 AtoB 回 B 继续\n"
        f"入战遇尼尔家族(77/310/416)：切艾斯菲格捕捉\n\n"
        "请重启 Dashboard，在尼奥下拉框中选择该模式并启动。",
    )


if __name__ == "__main__":
    main()
