# tools/map_recorder.py
"""
地图记录器（Map Recorder）

分批标注每张地图刷新点：Enter 截取 1200×700 client → GUI 点选 → 历史红点叠加。

默认输出 fix_script/map{kernel_map_id}.json；保存时同时生成 map{id}_summary.png 总结图。
--emit-regions 模式直接写 assets/regions，总结图在同目录。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import sys
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import messagebox
from typing import Any, Dict, List, Optional, Tuple, Union

from PIL import Image, ImageDraw, ImageFont, ImageTk
from pynput import keyboard


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

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import GAME_LOGIC_H, GAME_LOGIC_W
from core.utils import window_manager

SCHEMA = "nieopilot_map_points_v1"
MAP_ID_KIND = "kernel_map"
MAP_ID_NOTE = (
    "内核日志 /map/{id}.swf 的 map ID；日后可映射到 regions 前缀（如嘟咕噜）"
)
DEDUPE_RADIUS_PX = 20.0
FIX_SCRIPT_DIR = os.path.join(PROJECT_ROOT, "fix_script")
REGION_ROOT = os.path.join(PROJECT_ROOT, "assets", "regions")

SAVE_EXIT_KEYS = frozenset(
    {keyboard.Key.esc, keyboard.Key.f10, keyboard.Key.f12}
)

# order → region 文件名（11 点稀有模式）
RARE_ROUTE_POINT_LABELS: Dict[int, str] = {i: str(i) for i in range(1, 10)}
RARE_ROUTE_POINT_LABELS[10] = "A"
RARE_ROUTE_POINT_LABELS[11] = "B"

# 10 点尼奥模式：1-9 刷新点，10=Z
NIEO_ROUTE_POINT_LABELS: Dict[int, str] = {i: str(i) for i in range(1, 10)}
NIEO_ROUTE_POINT_LABELS[10] = "Z"

# 向后兼容
ROUTE_POINT_LABELS = RARE_ROUTE_POINT_LABELS


def route_point_labels_for_mode(label_mode: str) -> Dict[int, str]:
    if label_mode == "nieo":
        return NIEO_ROUTE_POINT_LABELS
    return RARE_ROUTE_POINT_LABELS


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


def expand_single_point(px: int, py: int) -> List[List[int]]:
    return [
        [px - 1, py - 1],
        [px + 1, py - 1],
        [px + 1, py + 1],
        [px - 1, py + 1],
    ]


@dataclass
class MarkPoint:
    order: int
    x: int
    y: int


@dataclass
class MapRecorderState:
    map_id: str
    capture_count: int = 0
    points: List[MarkPoint] = field(default_factory=list)
    has_capture: bool = False
    _next_order: int = 1

    def add_point(self, x: int, y: int) -> None:
        if not self.has_capture:
            return
        self.points.append(
            MarkPoint(order=self._next_order, x=int(x), y=int(y))
        )
        self._next_order += 1

    def undo_last_point(self) -> bool:
        if not self.points:
            return False
        self.points.pop()
        start = getattr(self, "_start_order", 1)
        self._next_order = (self.points[-1].order + 1) if self.points else start
        return True


@dataclass
class MapRecorderOptions:
    expect_points: Optional[int] = None
    emit_regions: bool = False
    route_prefix: str = ""
    label_mode: str = "rare"  # "rare" | "nieo"
    start_order: int = 1


def _dedupe_points(points: List[MarkPoint], radius: float = DEDUPE_RADIUS_PX) -> List[Dict[str, Any]]:
    clusters: List[Dict[str, Any]] = []
    for pt in points:
        placed = False
        for cluster in clusters:
            dx = pt.x - cluster["x"]
            dy = pt.y - cluster["y"]
            if math.hypot(dx, dy) <= radius:
                cluster["merged_from_orders"].append(pt.order)
                n = len(cluster["merged_from_orders"])
                cluster["x"] = int(round((cluster["x"] * (n - 1) + pt.x) / n))
                cluster["y"] = int(round((cluster["y"] * (n - 1) + pt.y) / n))
                placed = True
                break
        if not placed:
            clusters.append(
                {
                    "slot": len(clusters) + 1,
                    "x": pt.x,
                    "y": pt.y,
                    "merged_from_orders": [pt.order],
                }
            )
    for i, c in enumerate(clusters, start=1):
        c["slot"] = i
    return clusters


def _build_output_json(state: MapRecorderState) -> Dict[str, Any]:
    map_id_int: Any
    try:
        map_id_int = int(state.map_id)
    except ValueError:
        map_id_int = state.map_id

    deduped = _dedupe_points(state.points)
    return {
        "name": f"map{state.map_id}",
        "schema": SCHEMA,
        "map_id": map_id_int,
        "map_id_kind": MAP_ID_KIND,
        "map_id_note": MAP_ID_NOTE.format(id=state.map_id),
        "region_prefix_hint": "",
        "logic_size": [GAME_LOGIC_W, GAME_LOGIC_H],
        "create_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "points": [
            {"order": p.order, "x": p.x, "y": p.y}
            for p in state.points
        ],
        "points_deduped": deduped,
    }


def _load_summary_font(size: int, *, bold: bool = False) -> Union[ImageFont.FreeTypeFont, ImageFont.ImageFont]:
    names = ("segoeuib.ttf", "segoeui.ttf", "msyhbd.ttc", "msyh.ttc", "arialbd.ttf", "arial.ttf")
    if not bold:
        names = ("segoeui.ttf", "msyh.ttc", "arial.ttf", "segoeuib.ttf", "msyhbd.ttc", "arialbd.ttf")
    fonts_dir = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
    for name in names:
        path = os.path.join(fonts_dir, name)
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _summary_image_output_path(
    map_id: str,
    *,
    emit_regions: bool,
    route_prefix: str,
) -> str:
    if emit_regions and route_prefix.strip():
        save_dir = os.path.join(REGION_ROOT, route_prefix.strip())
    else:
        save_dir = FIX_SCRIPT_DIR
    os.makedirs(save_dir, exist_ok=True)
    return os.path.join(save_dir, f"map{map_id}_summary.png")


def save_map_summary_image(
    pil: Optional[Image.Image],
    state: MapRecorderState,
    options: MapRecorderOptions,
    *,
    out_path: Optional[str] = None,
) -> Optional[str]:
    """
    在 1200×700 截图上标注全部点位，下方附文字总结，保存 PNG。
    """
    if pil is None:
        print("⚠ 无游戏截图，跳过总结图")
        return None

    labels = route_point_labels_for_mode(options.label_mode)
    deduped = _dedupe_points(state.points)
    map_id = state.map_id
    prefix = (options.route_prefix or "").strip()

    base = pil.copy().convert("RGB")
    if base.size != (GAME_LOGIC_W, GAME_LOGIC_H):
        base = base.resize((GAME_LOGIC_W, GAME_LOGIC_H), Image.Resampling.LANCZOS)

    draw = ImageDraw.Draw(base)
    title_font = _load_summary_font(15, bold=True)
    label_font = _load_summary_font(13, bold=True)
    dot_r = 8

    banner_h = 28
    draw.rectangle([0, 0, GAME_LOGIC_W, banner_h], fill=(20, 20, 20))
    mode_lbl = "nieo" if options.label_mode == "nieo" else "rare"
    title = f"map{map_id}"
    if prefix:
        title += f"  ·  {prefix}"
    title += f"  ·  {len(state.points)} pts / {len(deduped)} deduped  ·  {mode_lbl}"
    draw.text((10, 6), title, fill=(255, 255, 255), font=title_font)

    for pt in state.points:
        x, y = int(pt.x), int(pt.y)
        lbl = labels.get(pt.order, str(pt.order))
        draw.ellipse(
            [x - dot_r, y - dot_r, x + dot_r, y + dot_r],
            fill=(255, 34, 34),
            outline=(255, 255, 255),
            width=2,
        )
        tw = draw.textlength(lbl, font=label_font) if hasattr(draw, "textlength") else 12
        draw.text((x - tw / 2, y - dot_r - 18), lbl, fill=(255, 255, 0), font=label_font)

    line_h = 18
    footer_pad = 12
    footer_lines: List[str] = [
        f"创建: {time.strftime('%Y-%m-%d %H:%M:%S')}  |  逻辑 {GAME_LOGIC_W}×{GAME_LOGIC_H}",
    ]
    for pt in state.points:
        lbl = labels.get(pt.order, str(pt.order))
        footer_lines.append(f"#{pt.order:>2} ({lbl:>2})  →  ({pt.x:4d}, {pt.y:4d})")
    if deduped:
        footer_lines.append("— 去重槽位 —")
        for slot in deduped:
            merged = slot.get("merged_from_orders") or []
            merge_note = f"  ← merge {merged}" if len(merged) > 1 else ""
            footer_lines.append(
                f"slot{slot['slot']:>2}  ({slot['x']:4d}, {slot['y']:4d}){merge_note}"
            )

    footer_h = footer_pad * 2 + line_h * len(footer_lines)
    out = Image.new("RGB", (GAME_LOGIC_W, GAME_LOGIC_H + footer_h), (248, 248, 248))
    out.paste(base, (0, 0))
    fdraw = ImageDraw.Draw(out)
    fdraw.rectangle([0, GAME_LOGIC_H, GAME_LOGIC_W, GAME_LOGIC_H + footer_h], fill=(248, 248, 248))
    fdraw.line(
        [(0, GAME_LOGIC_H), (GAME_LOGIC_W, GAME_LOGIC_H)],
        fill=(180, 180, 180),
        width=1,
    )
    body_font = _load_summary_font(12)
    y = GAME_LOGIC_H + footer_pad
    for i, line in enumerate(footer_lines):
        color = (40, 40, 40) if i > 0 else (80, 80, 80)
        font = title_font if i == 0 else body_font
        fdraw.text((12, y), line, fill=color, font=font)
        y += line_h

    if not out_path:
        out_path = _summary_image_output_path(
            map_id,
            emit_regions=options.emit_regions,
            route_prefix=prefix,
        )
    out.save(out_path, format="PNG")
    return out_path


def _write_route_regions(
    prefix: str,
    points: List[MarkPoint],
    *,
    label_mode: str = "rare",
) -> List[str]:
    """将标注点写入 assets/regions/{prefix}/{label}.json，返回已写路径。"""
    labels = route_point_labels_for_mode(label_mode)
    save_dir = os.path.join(REGION_ROOT, prefix)
    os.makedirs(save_dir, exist_ok=True)
    written: List[str] = []
    for pt in points:
        label = labels.get(pt.order)
        if not label:
            continue
        region = {
            "key": f"{prefix}.{label}",
            "category": prefix,
            "name": label,
            "shape": "polygon",
            "points": expand_single_point(pt.x, pt.y),
            "click": {"random": True},
            "meta": {
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "desc": f"map_recorder order={pt.order}",
            },
        }
        path = os.path.join(save_dir, f"{label}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(region, f, indent=4, ensure_ascii=False)
        written.append(path)
    return written


class MapRecorderApp:
    CANVAS_W = 960
    CANVAS_H = 560
    DOT_R = 6

    def __init__(self, map_id: str, options: Optional[MapRecorderOptions] = None) -> None:
        self.options = options or MapRecorderOptions()
        self.state = MapRecorderState(map_id=map_id.strip())
        start = max(1, int(self.options.start_order))
        self.state._next_order = start
        self.state._start_order = start
        os.makedirs(FIX_SCRIPT_DIR, exist_ok=True)

        self._key_queue: queue.Queue[str] = queue.Queue()
        self._keyboard_listener: Optional[keyboard.Listener] = None
        self._photo: Optional[ImageTk.PhotoImage] = None
        self._current_pil: Optional[Image.Image] = None
        self._display_offset = (0.0, 0.0)
        self._display_scale = 1.0
        self._display_rect = (0.0, 0.0, 0.0, 0.0)
        self._running = True

        title_suffix = ""
        if self.options.emit_regions and self.options.route_prefix:
            title_suffix = f" — {self.options.route_prefix}"
        elif self.options.expect_points:
            title_suffix = f" — {self.options.expect_points} 点模式"

        self.root = tk.Tk()
        self.root.title(f"地图记录器 — map{self.state.map_id}{title_suffix}")
        self.root.geometry(f"{self.CANVAS_W + 20}x{self.CANVAS_H + 100}")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._status_var = tk.StringVar(value=self._status_text())
        tk.Label(self.root, textvariable=self._status_var, anchor="w").pack(
            fill="x", padx=8, pady=4
        )

        frame = tk.Frame(self.root)
        frame.pack(fill="both", expand=True, padx=8, pady=4)
        self.canvas = tk.Canvas(
            frame,
            width=self.CANVAS_W,
            height=self.CANVAS_H,
            bg="#333333",
            highlightthickness=1,
            highlightbackground="#666666",
        )
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self._on_left_click)
        self.canvas.bind("<Button-3>", self._on_right_click)

        hint = (
            "Enter=截取游戏画面  |  在下方截图上左键标注  |  右键=撤销  |  "
            "F10/ESC=保存并退出"
        )
        if self.options.expect_points == 11:
            hint += "  |  1-9=刷新点，10=A，11=B"
        elif self.options.expect_points == 10 and self.options.label_mode == "nieo":
            hint += "  |  1-9=刷新点，10=Z"
        elif self.options.expect_points == 2 and self.options.start_order == 10:
            hint += "  |  10=A，11=B"
        tk.Label(self.root, text=hint, fg="#555555").pack(pady=4)

        self.canvas.create_text(
            self.CANVAS_W // 2,
            self.CANVAS_H // 2,
            text="请切到游戏画面后按 Enter 截取第一张图",
            fill="#cccccc",
            font=("Segoe UI", 12),
        )

    def _status_text(self) -> str:
        n_cap = self.state.capture_count
        n_pts = len(self.state.points)
        n_ded = len(_dedupe_points(self.state.points)) if self.state.points else 0
        cap_label = f"截图 #{n_cap}" if n_cap else "尚未截图"
        base = (
            f"地图 map{self.state.map_id} | {cap_label} | "
            f"已标 {n_pts} 点（去重 {n_ded}）"
        )
        exp = self.options.expect_points
        if exp:
            base += f"  |  目标 {exp} 点（{n_pts}/{exp}）"
        return base

    def _refresh_status(self) -> None:
        self._status_var.set(self._status_text())

    def _start_keyboard_listener(self) -> None:
        def on_press(key):
            if not self._running:
                return False
            if key in SAVE_EXIT_KEYS:
                self._key_queue.put("save_exit")
                return False
            if key == keyboard.Key.enter:
                self._key_queue.put("capture")
            return None

        self._keyboard_listener = keyboard.Listener(on_press=on_press)
        self._keyboard_listener.start()

    def _poll_keys(self) -> None:
        try:
            while True:
                cmd = self._key_queue.get_nowait()
                if cmd == "capture":
                    self._do_capture()
                elif cmd == "save_exit":
                    self._do_save_exit()
                    return
        except queue.Empty:
            pass
        if self._running:
            self.root.after(80, self._poll_keys)

    def _do_capture(self) -> None:
        if not window_manager.find_window():
            messagebox.showwarning("截图失败", "未找到游戏窗口，请先启动游戏。")
            return
        try:
            window_manager.scan_boundaries()
        except Exception:
            pass

        img = window_manager.grab_game_bbox(0, 0, GAME_LOGIC_W, GAME_LOGIC_H)
        if img is None:
            messagebox.showwarning("截图失败", "grab_game_bbox 返回空，请先校准屏幕。")
            return

        self.state.capture_count += 1
        self.state.has_capture = True
        self._current_pil = img.convert("RGB")
        self._redraw_canvas()
        self._refresh_status()
        print(f"📸 已截图 #{self.state.capture_count}（仅内存，未落盘）")

    def _compute_display_transform(self) -> None:
        assert self._current_pil is not None
        iw, ih = self._current_pil.size
        scale = min(self.CANVAS_W / iw, self.CANVAS_H / ih)
        disp_w = iw * scale
        disp_h = ih * scale
        off_x = (self.CANVAS_W - disp_w) / 2.0
        off_y = (self.CANVAS_H - disp_h) / 2.0
        self._display_scale = scale
        self._display_offset = (off_x, off_y)
        self._display_rect = (off_x, off_y, disp_w, disp_h)

    def _canvas_xy(self, event) -> Tuple[float, float]:
        return float(self.canvas.canvasx(event.x)), float(self.canvas.canvasy(event.y))

    def _canvas_point_in_screenshot(self, cx: float, cy: float) -> bool:
        ox, oy, dw, dh = self._display_rect
        return (ox <= cx <= ox + dw) and (oy <= cy <= oy + dh)

    def _canvas_to_logic(self, cx: float, cy: float) -> Optional[Tuple[int, int]]:
        if self._current_pil is None or self._display_scale <= 0:
            return None
        if not self._canvas_point_in_screenshot(cx, cy):
            return None
        ox, oy = self._display_offset
        iw, ih = self._current_pil.size
        ix = (cx - ox) / self._display_scale
        iy = (cy - oy) / self._display_scale
        gx = ix * GAME_LOGIC_W / max(1, iw)
        gy = iy * GAME_LOGIC_H / max(1, ih)
        gx = max(0.0, min(float(GAME_LOGIC_W), gx))
        gy = max(0.0, min(float(GAME_LOGIC_H), gy))
        return int(round(gx)), int(round(gy))

    def _logic_to_canvas(self, gx: float, gy: float) -> Tuple[float, float]:
        assert self._current_pil is not None
        ox, oy = self._display_offset
        iw, ih = self._current_pil.size
        ix = gx * iw / max(1, GAME_LOGIC_W)
        iy = gy * ih / max(1, GAME_LOGIC_H)
        return ox + ix * self._display_scale, oy + iy * self._display_scale

    def _redraw_canvas(self) -> None:
        self.canvas.delete("all")
        if self._current_pil is None:
            self.canvas.create_text(
                self.CANVAS_W // 2,
                self.CANVAS_H // 2,
                text="请切到游戏画面后按 Enter 截取第一张图",
                fill="#cccccc",
                font=("Segoe UI", 12),
            )
            return

        self._compute_display_transform()
        scaled = self._current_pil.resize(
            (
                int(self._current_pil.width * self._display_scale),
                int(self._current_pil.height * self._display_scale),
            ),
            Image.Resampling.LANCZOS,
        )
        self._photo = ImageTk.PhotoImage(scaled)
        ox, oy = self._display_offset
        self.canvas.create_image(ox, oy, anchor="nw", image=self._photo)

        labels = route_point_labels_for_mode(self.options.label_mode)
        for pt in self.state.points:
            cx, cy = self._logic_to_canvas(pt.x, pt.y)
            r = self.DOT_R
            label = labels.get(pt.order, str(pt.order))
            self.canvas.create_oval(
                cx - r, cy - r, cx + r, cy + r,
                fill="#ff2222", outline="#ffffff", width=2,
            )
            self.canvas.create_text(
                cx, cy - r - 8,
                text=label,
                fill="#ffff00",
                font=("Segoe UI", 9, "bold"),
            )

    def _on_left_click(self, event) -> None:
        if not self.state.has_capture or self._current_pil is None:
            messagebox.showinfo("提示", "请先按 Enter 截取第一张游戏画面。")
            return
        exp = self.options.expect_points
        if exp and len(self.state.points) >= exp:
            messagebox.showinfo("提示", f"已标满 {exp} 个点，请 F10/ESC 保存。")
            return
        self._compute_display_transform()
        cx, cy = self._canvas_xy(event)
        pos = self._canvas_to_logic(cx, cy)
        if pos is None:
            messagebox.showinfo(
                "提示",
                "请点击截图区域内（画布上显示的游戏画面；四周灰色空白无效）。",
            )
            return
        gx, gy = pos
        self.state.add_point(gx, gy)
        self._redraw_canvas()
        self._refresh_status()
        labels = route_point_labels_for_mode(self.options.label_mode)
        lbl = labels.get(self.state.points[-1].order, str(self.state.points[-1].order))
        print(f"✅ 标注点 #{self.state.points[-1].order} ({lbl}): ({gx}, {gy})")

    def _on_right_click(self, event) -> None:
        if self.state.undo_last_point():
            self._redraw_canvas()
            self._refresh_status()
            print("↩ 已撤销最后一点")

    def _do_save_exit(self) -> None:
        exp = self.options.expect_points
        n_pts = len(self.state.points)

        if not self.state.points:
            if not messagebox.askyesno(
                "保存确认",
                "尚未标注任何点，仍要保存吗？",
                default=messagebox.NO,
            ):
                return
        elif exp and n_pts != exp:
            if not messagebox.askyesno(
                "点数不符",
                f"期望 {exp} 点，当前 {n_pts} 点。仍要保存吗？",
                default=messagebox.NO,
            ):
                return

        if self.options.emit_regions:
            prefix = (self.options.route_prefix or "").strip()
            if not prefix:
                messagebox.showerror("保存失败", "emit-regions 模式需要 --route-prefix。")
                return
            paths = _write_route_regions(
                prefix, self.state.points, label_mode=self.options.label_mode
            )
            print(f"💾 已写入 {len(paths)} 个 region → assets/regions/{prefix}/")
            for p in paths:
                print(f"   {p}")
            summary_path = save_map_summary_image(
                self._current_pil, self.state, self.options
            )
            if summary_path:
                print(f"🖼️ 总结图: {summary_path}")
        else:
            data = _build_output_json(self.state)
            deduped = data.get("points_deduped") or []
            if len(deduped) > 9 and not exp:
                messagebox.showwarning(
                    "槽位过多",
                    f"去重后有 {len(deduped)} 个唯一槽位（超过 9），请检查是否重复标注。仍将保存。",
                )
            out_path = os.path.join(FIX_SCRIPT_DIR, f"map{self.state.map_id}.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            print(f"💾 已保存: {out_path}")
            print(f"   原始点 {n_pts} 个，去重 {len(deduped)} 槽位")
            summary_path = save_map_summary_image(
                self._current_pil, self.state, self.options
            )
            if summary_path:
                print(f"🖼️ 总结图: {summary_path}")

        self._running = False
        self.root.quit()

    def _on_close(self) -> None:
        if messagebox.askyesno("退出", "未保存，确定退出吗？", default=messagebox.NO):
            self._running = False
            self.root.quit()

    def run(self) -> None:
        self._start_keyboard_listener()
        self.root.after(80, self._poll_keys)
        self.root.mainloop()
        self._running = False
        if self._keyboard_listener is not None:
            try:
                self._keyboard_listener.stop()
            except Exception:
                pass


def _parse_map_id(raw: str) -> Optional[str]:
    s = (raw or "").strip()
    if not s or not s.isdigit():
        return None
    return s


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NieoPilot 地图记录器")
    p.add_argument("map_id", nargs="?", help="内核 map ID（如 323）")
    p.add_argument("--expect-points", type=int, default=None, help="期望标注点数（如 11）")
    p.add_argument("--emit-regions", action="store_true", help="保存为 assets/regions 而非 fix_script")
    p.add_argument("--route-prefix", default="", help="emit-regions 时的 category/文件夹名")
    p.add_argument(
        "--label-mode",
        default="rare",
        choices=("rare", "nieo"),
        help="点标签模式：rare=1-9+A+B，nieo=1-9+Z",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    _configure_console_utf8()
    args = _parse_args(argv)

    print("=== NieoPilot 地图记录器 (Map Recorder) ===\n")
    if args.emit_regions:
        print(f"模式：写入 assets/regions/{args.route_prefix or '?'}")
    else:
        print(f"输出：fix_script/map<内核mapID>.json  (schema: {SCHEMA})")
    print()

    if args.map_id:
        map_id = _parse_map_id(args.map_id)
    else:
        try:
            raw = input("请输入内核 map ID（如 323、11）: ").strip()
        except KeyboardInterrupt:
            print("\n已取消。")
            return
        map_id = _parse_map_id(raw)

    if not map_id:
        print("❌ map ID 无效（请输入数字，如 323）")
        return

    options = MapRecorderOptions(
        expect_points=args.expect_points,
        emit_regions=bool(args.emit_regions),
        route_prefix=args.route_prefix or "",
        label_mode=str(args.label_mode or "rare"),
    )

    print(f"📍 地图 map{map_id} | 逻辑分辨率 {GAME_LOGIC_W}×{GAME_LOGIC_H}")
    if not window_manager.find_window():
        print("⚠ 未检测到游戏窗口；可先开游戏，Enter 截图时会再次检测。")
    else:
        try:
            window_manager.scan_boundaries()
            print("✅ 已连接游戏窗口并完成扫边。")
        except Exception as e:
            print(f"⚠ 扫边失败（{e}），Enter 截图前建议在 Dashboard 校准屏幕。")

    app = MapRecorderApp(map_id, options)
    app.run()


if __name__ == "__main__":
    main()
