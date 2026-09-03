# tools/region_viewer.py
"""
区域显示器：
  Enter 截取 → 左键在截图上标蓝点（显示坐标）→ 右键蓝点删除
  输入/浏览已有区域显示红点；浏览支持多选；RGB 同时显示中心点和运行同款区域扫描值
  输入 X/Y 后点击「定位坐标」，以绿色十字显示该逻辑坐标
  F10 保存标注 PNG；退出时对每个蓝点依次输入区域名并写入 assets/regions（空则跳过）

快捷键（输入框内 Enter = 添加已有区域）：
  Enter  截图（焦点不在输入框时）
  F10    保存图片
  ESC    退出（先处理蓝点保存对话框）
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
from dataclasses import dataclass
from tkinter import filedialog, messagebox, simpledialog
from typing import List, Optional, Tuple

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

from config import BASE_PATH, GAME_LOGIC_H, GAME_LOGIC_W
from core.region_store import Region, RegionStore
from core.utils import mean_rgb_for_region_key, window_manager

REGION_ROOT = os.path.join(PROJECT_ROOT, "assets", "regions")
OUTPUT_DIR = os.path.join(BASE_PATH, "screenshots", "region_viewer")


@dataclass
class OverlayMark:
    key: str
    gx: float
    gy: float
    region: Optional[Region] = None
    scan_rgb: Optional[Tuple[int, int, int]] = None


@dataclass
class BluePoint:
    """截图上左键点击的标点（退出时可保存为 region）。"""

    gx: int
    gy: int
    uid: int = 0
    scan_rgb: Optional[Tuple[int, int, int]] = None


def point_probe_region(gx: int, gy: int, *, key: str = "区域显示器.临时点") -> Region:
    """按区域记录器的单点规则，构造最终会保存的 2×2 逻辑探针。"""
    return Region(
        key=key,
        points=[tuple(point) for point in expand_single_point(int(gx), int(gy))],
        click={},
        meta={},
    )


def runtime_scan_rgb_for_region(region: Optional[Region]) -> Optional[Tuple[int, int, int]]:
    """复用任务运行时的区域取色函数，不从区域显示器整图反推颜色。"""
    if region is None:
        return None
    return mean_rgb_for_region_key({region.key: region}, region.key)


def apply_inherited_viewport(
    viewport: Optional[Tuple[float, float, float, float]],
    expected_hwnd: int = 0,
) -> bool:
    """让独立区域显示器沿用主程序启动它时的固定视口。"""
    if viewport is None or len(viewport) != 4:
        return False
    if not window_manager.find_window():
        return False
    current_hwnd = int(getattr(window_manager, "hwnd", 0) or 0)
    if expected_hwnd and current_hwnd != int(expected_hwnd):
        return False
    vx, vy, vw, vh = (float(value) for value in viewport)
    if vw <= 0 or vh <= 0:
        return False
    window_manager._fixed_viewport = (vx, vy, vw, vh)
    window_manager._fixed_viewport_hwnd = current_hwnd
    window_manager.content_padding = None
    return True


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


def parse_logic_coordinate_inputs(
    x_text: str,
    y_text: str,
) -> Tuple[Optional[Tuple[int, int]], str]:
    """解析区域显示器的逻辑坐标输入并返回用户可见错误。"""
    raw_x = (x_text or "").strip()
    raw_y = (y_text or "").strip()
    if not raw_x or not raw_y:
        return None, "请输入完整的 X 和 Y 坐标。"
    try:
        gx = int(raw_x)
        gy = int(raw_y)
    except ValueError:
        return None, "X、Y 必须是整数。"
    if not 0 <= gx <= GAME_LOGIC_W:
        return None, f"X 须在 0–{GAME_LOGIC_W} 之间。"
    if not 0 <= gy <= GAME_LOGIC_H:
        return None, f"Y 须在 0–{GAME_LOGIC_H} 之间。"
    return (gx, gy), ""


def _region_center(region: Region) -> Tuple[float, float]:
    return region.sample_click_point()


def _split_region_key(key: str) -> Tuple[str, str]:
    key = (key or "").strip().replace("\\", ".").replace("/", ".")
    while ".." in key:
        key = key.replace("..", ".")
    key = key.strip(".")
    if not key:
        return "", ""
    if "." not in key:
        return "misc", key
    i = key.index(".")
    return key[:i], key[i + 1 :]


def _load_region_from_json(path: str) -> Optional[Region]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return None
    pts_raw = data.get("points") or []
    pts: List[Tuple[float, float]] = []
    for p in pts_raw:
        try:
            pts.append((float(p[0]), float(p[1])))
        except Exception:
            pass
    if not pts:
        return None
    key = str(data.get("key") or "").strip()
    if not key:
        rel = os.path.relpath(path, REGION_ROOT)
        stem = os.path.splitext(rel)[0].replace("\\", ".").replace("/", ".")
        key = stem
    click = data.get("click") if isinstance(data.get("click"), dict) else {}
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    return Region(key=key, points=pts, click=click, meta=meta)


def _save_region_json(key: str, gx: int, gy: int) -> Tuple[bool, str]:
    category, name = _split_region_key(key)
    if not category or not name:
        return False, "key 须为「分类.名称」格式"
    polygon = expand_single_point(gx, gy)
    region = {
        "key": f"{category}.{name}",
        "category": category,
        "name": name,
        "shape": "polygon",
        "points": polygon,
        "click": {"random": True},
        "meta": {
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "desc": "region_viewer",
        },
    }
    save_dir = os.path.join(REGION_ROOT, category)
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"{name}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(region, f, indent=4, ensure_ascii=False)
        return True, path
    except OSError as e:
        return False, str(e)


class RegionPickDialog(tk.Toplevel):
    """从已加载区域列表中浏览、筛选并选择，支持一次多选。"""

    def __init__(self, parent: tk.Tk, keys: List[str], on_pick) -> None:
        super().__init__(parent)
        self.title("浏览区域")
        self.geometry("520x480")
        self.transient(parent)
        self.grab_set()
        self._on_pick = on_pick
        self._all_keys = list(keys)

        tk.Label(
            self,
            text="筛选（支持子串；Ctrl/Shift 可多选，确定后一次显示）:",
        ).pack(anchor="w", padx=8, pady=(8, 2))
        self._filter_var = tk.StringVar()
        ent = tk.Entry(self, textvariable=self._filter_var)
        ent.pack(fill="x", padx=8)
        ent.bind("<KeyRelease>", lambda _e: self._refill_list())
        ent.focus_set()

        frame = tk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        scroll = tk.Scrollbar(frame)
        scroll.pack(side="right", fill="y")
        self._listbox = tk.Listbox(
            frame,
            yscrollcommand=scroll.set,
            font=("Consolas", 10),
            selectmode=tk.EXTENDED,
            exportselection=False,
        )
        self._listbox.pack(side="left", fill="both", expand=True)
        scroll.config(command=self._listbox.yview)
        self._listbox.bind("<Double-Button-1>", self._pick_selection)
        self._listbox.bind("<Return>", self._pick_selection)

        btn_row = tk.Frame(self)
        btn_row.pack(fill="x", padx=8, pady=(0, 8))
        tk.Button(btn_row, text="确定显示", command=self._pick_selection).pack(side="left")
        tk.Button(btn_row, text="全选当前列表", command=self._select_all_visible).pack(side="left", padx=6)
        tk.Button(btn_row, text="取消", command=self.destroy).pack(side="left", padx=6)
        tk.Button(btn_row, text="从文件…", command=self._pick_file).pack(side="right")

        self._refill_list()

    def _refill_list(self) -> None:
        q = (self._filter_var.get() or "").strip().lower()
        self._listbox.delete(0, tk.END)
        for k in self._all_keys:
            if not q or q in k.lower():
                self._listbox.insert(tk.END, k)

    def _pick_selection(self, _event=None) -> None:
        sel = self._listbox.curselection()
        if not sel:
            return
        keys = [self._listbox.get(i) for i in sel]
        self._on_pick(keys)
        self.destroy()

    def _select_all_visible(self) -> None:
        self._listbox.selection_set(0, tk.END)
        self._listbox.focus_set()

    def _pick_file(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self,
            title="选择区域 JSON",
            initialdir=REGION_ROOT,
            filetypes=[("JSON", "*.json"), ("All", "*.*")],
        )
        if not paths:
            return
        regions: List[Region] = []
        bad: List[str] = []
        for path in paths:
            region = _load_region_from_json(path)
            if region is None:
                bad.append(os.path.basename(path))
            else:
                regions.append(region)
        if bad:
            messagebox.showerror(
                "无效文件",
                "以下区域 JSON 无法解析：\n" + "\n".join(bad[:12]),
                parent=self,
            )
        if not regions:
            return
        self._on_pick([r.key for r in regions], regions=regions)
        self.destroy()


class RegionViewerApp:
    CANVAS_W = 960
    CANVAS_H = 560
    RED_DOT_R = 7
    BLUE_DOT_R = 7
    BLUE_HIT_R = 16

    def __init__(self) -> None:
        self._store = RegionStore(project_root=PROJECT_ROOT)
        self._marks: List[OverlayMark] = []
        self._blue_points: List[BluePoint] = []
        self._blue_uid_next = 1
        self._key_queue: queue.Queue[str] = queue.Queue()
        self._keyboard_listener: Optional[keyboard.Listener] = None
        self._photo: Optional[ImageTk.PhotoImage] = None
        self._current_pil: Optional[Image.Image] = None
        self._located_point: Optional[Tuple[int, int]] = None
        self._located_scan_rgb: Optional[Tuple[int, int, int]] = None
        self._display_offset = (0.0, 0.0)
        self._display_scale = 1.0
        self._display_rect = (0.0, 0.0, 0.0, 0.0)
        self._running = True
        self._has_capture = False

        os.makedirs(OUTPUT_DIR, exist_ok=True)

        self.root = tk.Tk()
        self.root.title("区域显示器")
        self.root.geometry(f"{self.CANVAS_W + 40}x{self.CANVAS_H + 280}")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._status_var = tk.StringVar(value="尚未截图")
        tk.Label(self.root, textvariable=self._status_var, anchor="w").pack(
            fill="x", padx=8, pady=4
        )

        tk.Label(
            self.root,
            text=(
                "Enter=截图 | 截图上左键=蓝点+坐标 | 蓝点右键=删除 | "
                "X/Y定位=绿色十字 | F10=保存图片 | ESC=退出并保存蓝点"
            ),
            fg="#555555",
        ).pack(pady=2)

        pick_row = tk.Frame(self.root)
        pick_row.pack(fill="x", padx=8, pady=4)
        tk.Label(pick_row, text="已有区域 key:").pack(side="left")
        self._key_var = tk.StringVar()
        self._key_entry = tk.Entry(pick_row, textvariable=self._key_var, width=36)
        self._key_entry.pack(side="left", padx=4)
        self._key_entry.bind("<Return>", lambda _e: self._add_region_from_entry())
        tk.Button(pick_row, text="添加红点", command=self._add_region_from_entry).pack(
            side="left"
        )
        tk.Button(pick_row, text="浏览…", command=self._open_browse).pack(side="left", padx=4)
        tk.Button(pick_row, text="撤销红点", command=self._undo_last_red).pack(
            side="left", padx=4
        )

        coordinate_row = tk.Frame(self.root)
        coordinate_row.pack(fill="x", padx=8, pady=2)
        tk.Label(coordinate_row, text="坐标定位:").pack(side="left")
        tk.Label(coordinate_row, text="X").pack(side="left", padx=(6, 2))
        self._coordinate_x_var = tk.StringVar()
        self._coordinate_x_entry = tk.Entry(
            coordinate_row,
            textvariable=self._coordinate_x_var,
            width=8,
        )
        self._coordinate_x_entry.pack(side="left")
        tk.Label(coordinate_row, text="Y").pack(side="left", padx=(8, 2))
        self._coordinate_y_var = tk.StringVar()
        self._coordinate_y_entry = tk.Entry(
            coordinate_row,
            textvariable=self._coordinate_y_var,
            width=8,
        )
        self._coordinate_y_entry.pack(side="left")
        self._coordinate_x_entry.bind(
            "<Return>",
            lambda _event: self._show_input_coordinate(),
        )
        self._coordinate_y_entry.bind(
            "<Return>",
            lambda _event: self._show_input_coordinate(),
        )
        tk.Button(
            coordinate_row,
            text="定位坐标",
            command=self._show_input_coordinate,
        ).pack(side="left", padx=(8, 4))
        tk.Button(
            coordinate_row,
            text="清除定位",
            command=self._clear_input_coordinate,
        ).pack(side="left")

        lists_row = tk.Frame(self.root)
        lists_row.pack(fill="x", padx=8, pady=2)

        red_col = tk.Frame(lists_row)
        red_col.pack(side="left", fill="both", expand=True)
        red_head = tk.Frame(red_col)
        red_head.pack(fill="x")
        tk.Label(red_head, text="已有区域（红）:").pack(side="left")
        self._show_red_rgb_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            red_head,
            text="红RGB（点+实扫）",
            variable=self._show_red_rgb_var,
            command=self._on_toggle_rgb_visibility,
        ).pack(side="right")
        self._marks_list = tk.Listbox(red_col, height=3, font=("Consolas", 9))
        self._marks_list.pack(fill="x", expand=True)

        blue_col = tk.Frame(lists_row)
        blue_col.pack(side="left", fill="both", expand=True, padx=(12, 0))
        blue_head = tk.Frame(blue_col)
        blue_head.pack(fill="x")
        tk.Label(blue_head, text="点击标点（蓝）:").pack(side="left")
        self._show_blue_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            blue_head,
            text="显示蓝点",
            variable=self._show_blue_var,
            command=self._on_toggle_blue_visibility,
        ).pack(side="right")
        self._show_blue_rgb_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            blue_head,
            text="蓝RGB（点+实扫）",
            variable=self._show_blue_rgb_var,
            command=self._on_toggle_rgb_visibility,
        ).pack(side="right", padx=(0, 8))

        self._blue_list = tk.Listbox(blue_col, height=3, font=("Consolas", 9))
        self._blue_list.pack(fill="x", expand=True)
        self._blue_list.bind("<Button-3>", self._on_blue_list_right_click)

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
        self.canvas.bind("<Button-1>", self._on_canvas_left)
        self.canvas.bind("<Button-3>", self._on_canvas_right)

    def _refresh_status(self) -> None:
        cap = "已截图" if self._has_capture else "尚未截图"
        blue_vis = "显示" if self._show_blue_var.get() else "隐藏"
        red_rgb = "红RGB开" if self._show_red_rgb_var.get() else "红RGB关"
        blue_rgb = "蓝RGB开" if self._show_blue_rgb_var.get() else "蓝RGB关"
        located = (
            f" | 定位 {self._located_point}"
            if self._located_point is not None
            else ""
        )
        self._status_var.set(
            f"{cap} | 红 {len(self._marks)}（{red_rgb}） | "
            f"蓝 {len(self._blue_points)}（{blue_vis}，{blue_rgb}）{located}"
        )

    def _refill_marks_list(self) -> None:
        self._marks_list.delete(0, tk.END)
        for m in self._marks:
            label = f"{m.key}  ({int(m.gx)}, {int(m.gy)})"
            if self._show_red_rgb_var.get():
                label += self._format_point_and_scan_rgb(
                    self._rgb_at_logic(m.gx, m.gy),
                    m.scan_rgb,
                )
            self._marks_list.insert(tk.END, label)

    def _refill_blue_list(self) -> None:
        self._blue_list.delete(0, tk.END)
        for bp in self._blue_points:
            self._blue_list.insert(tk.END, self._blue_point_label(bp))

    def _on_toggle_blue_visibility(self) -> None:
        self._redraw_canvas()
        self._refresh_status()

    def _on_toggle_rgb_visibility(self) -> None:
        self._refill_marks_list()
        self._refill_blue_list()
        self._redraw_canvas()
        self._refresh_status()

    def _focus_is_key_entry(self) -> bool:
        w = self.root.focus_get()
        return w is not None and any(
            str(w) == str(entry)
            for entry in (
                self._key_entry,
                self._coordinate_x_entry,
                self._coordinate_y_entry,
            )
        )

    def _start_keyboard_listener(self) -> None:
        def on_press(key):
            if not self._running:
                return False
            if key == keyboard.Key.esc:
                self._key_queue.put("exit")
                return False
            if key == keyboard.Key.f10:
                self._key_queue.put("save")
                return None
            if key == keyboard.Key.enter:
                if self._focus_is_key_entry():
                    return None
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
                elif cmd == "save":
                    self._do_save_image()
                elif cmd == "exit":
                    self._finish_exit()
                    return
        except queue.Empty:
            pass
        if self._running:
            self.root.after(80, self._poll_keys)

    def _do_capture(self) -> None:
        if not window_manager.find_window():
            messagebox.showwarning("截图失败", "未找到游戏窗口，请先启动游戏。")
            return
        img = window_manager.grab_game_bbox(0, 0, GAME_LOGIC_W, GAME_LOGIC_H)
        if img is None:
            messagebox.showwarning("截图失败", "grab_game_bbox 返回空，请先校准屏幕。")
            return
        self._current_pil = img.convert("RGB")
        self._has_capture = True
        self._refresh_runtime_scan_rgbs()
        self._redraw_canvas()
        self._refill_marks_list()
        self._refill_blue_list()
        self._refresh_status()
        print("📸 已截取游戏画面")

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

    def _logic_to_image_px(self, gx: float, gy: float) -> Tuple[int, int]:
        assert self._current_pil is not None
        iw, ih = self._current_pil.size
        ix = int(round(gx * iw / max(1, GAME_LOGIC_W)))
        iy = int(round(gy * ih / max(1, GAME_LOGIC_H)))
        ix = max(0, min(iw - 1, ix))
        iy = max(0, min(ih - 1, iy))
        return ix, iy

    def _rgb_at_logic(self, gx: float, gy: float) -> Optional[Tuple[int, int, int]]:
        if self._current_pil is None:
            return None
        ix, iy = self._logic_to_image_px(gx, gy)
        rgb = self._current_pil.getpixel((ix, iy))
        try:
            return int(rgb[0]), int(rgb[1]), int(rgb[2])
        except Exception:
            return None

    @staticmethod
    def _format_rgb(rgb: Optional[Tuple[int, int, int]]) -> str:
        if rgb is None:
            return "RGB=(?, ?, ?)"
        return f"RGB=({rgb[0]}, {rgb[1]}, {rgb[2]})"

    @classmethod
    def _format_point_and_scan_rgb(
        cls,
        point_rgb: Optional[Tuple[int, int, int]],
        scan_rgb: Optional[Tuple[int, int, int]],
    ) -> str:
        return (
            f"  点{cls._format_rgb(point_rgb)}"
            f"  实扫{cls._format_rgb(scan_rgb)}"
        )

    def _refresh_runtime_scan_rgbs(self) -> None:
        for mark in self._marks:
            region = mark.region or self._store.get(mark.key)
            mark.scan_rgb = runtime_scan_rgb_for_region(region)
        for bp in self._blue_points:
            bp.scan_rgb = runtime_scan_rgb_for_region(
                point_probe_region(bp.gx, bp.gy, key=f"区域显示器.蓝点{bp.uid}")
            )
        if self._located_point is not None:
            gx, gy = self._located_point
            self._located_scan_rgb = runtime_scan_rgb_for_region(
                point_probe_region(gx, gy, key="区域显示器.坐标定位")
            )

    def _blue_point_label(self, bp: BluePoint) -> str:
        label = f"#{bp.uid}  ({bp.gx}, {bp.gy})"
        if self._show_blue_rgb_var.get():
            label += self._format_point_and_scan_rgb(
                self._rgb_at_logic(bp.gx, bp.gy),
                bp.scan_rgb,
            )
        return label

    def _find_blue_index_at_canvas(self, cx: float, cy: float) -> Optional[int]:
        if not self._show_blue_var.get():
            return None
        best_i: Optional[int] = None
        best_d = self.BLUE_HIT_R
        for i, bp in enumerate(self._blue_points):
            bx, by = self._logic_to_canvas(bp.gx, bp.gy)
            d = math.hypot(cx - bx, cy - by)
            if d <= best_d:
                best_i = i
                best_d = d
        return best_i

    def _redraw_canvas(self) -> None:
        self.canvas.delete("all")
        if self._current_pil is None:
            self.canvas.create_text(
                self.CANVAS_W // 2,
                self.CANVAS_H // 2,
                text="请切到游戏画面后按 Enter 截取",
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

        for m in self._marks:
            region = m.region or self._store.get(m.key)
            if region and len(region.points) >= 2:
                pts_canvas = [self._logic_to_canvas(p[0], p[1]) for p in region.points]
                flat = [c for pair in pts_canvas for c in pair]
                if len(flat) >= 4:
                    self.canvas.create_polygon(*flat, outline="#44ccff", fill="", width=1)
            cx, cy = self._logic_to_canvas(m.gx, m.gy)
            r = self.RED_DOT_R
            self.canvas.create_oval(
                cx - r, cy - r, cx + r, cy + r,
                fill="#ff2222", outline="#ffffff", width=2,
            )
            short = m.key if len(m.key) <= 28 else m.key[:25] + "…"
            red_text = short
            if self._show_red_rgb_var.get():
                red_text += "\n" + self._format_point_and_scan_rgb(
                    self._rgb_at_logic(m.gx, m.gy),
                    m.scan_rgb,
                ).strip()
            self.canvas.create_text(
                cx,
                cy - r - 12,
                text=red_text,
                fill="#ffff00",
                font=("Segoe UI", 8, "bold"),
            )

        if self._show_blue_var.get():
            for bp in self._blue_points:
                cx, cy = self._logic_to_canvas(bp.gx, bp.gy)
                r = self.BLUE_DOT_R
                self.canvas.create_oval(
                    cx - r, cy - r, cx + r, cy + r,
                    fill="#2288ff", outline="#ffffff", width=2,
                )
                coord = f"({bp.gx},{bp.gy})"
                if self._show_blue_rgb_var.get():
                    coord += "\n" + self._format_point_and_scan_rgb(
                        self._rgb_at_logic(bp.gx, bp.gy),
                        bp.scan_rgb,
                    ).strip()
                self.canvas.create_text(
                    cx, cy + r + 16, text=coord, fill="#aaddff", font=("Consolas", 9, "bold"),
                )

        if self._located_point is not None:
            gx, gy = self._located_point
            cx, cy = self._logic_to_canvas(gx, gy)
            r = 11
            self.canvas.create_oval(
                cx - r,
                cy - r,
                cx + r,
                cy + r,
                outline="#00ff66",
                width=3,
            )
            self.canvas.create_line(
                cx - 18,
                cy,
                cx + 18,
                cy,
                fill="#00ff66",
                width=2,
            )
            self.canvas.create_line(
                cx,
                cy - 18,
                cx,
                cy + 18,
                fill="#00ff66",
                width=2,
            )
            self.canvas.create_text(
                cx,
                cy - 30,
                text=f"定位 ({gx},{gy})",
                fill="#00ff66",
                font=("Consolas", 10, "bold"),
            )

    def _show_input_coordinate(self) -> None:
        if not self._has_capture:
            messagebox.showinfo("提示", "请先按 Enter 截取游戏画面。")
            return
        point, error = parse_logic_coordinate_inputs(
            self._coordinate_x_var.get(),
            self._coordinate_y_var.get(),
        )
        if point is None:
            messagebox.showerror("坐标无效", error, parent=self.root)
            return
        self._located_point = point
        gx, gy = point
        self._located_scan_rgb = runtime_scan_rgb_for_region(
            point_probe_region(gx, gy, key="区域显示器.坐标定位")
        )
        self._redraw_canvas()
        self._refresh_status()
        print(
            f"📍 定位坐标: ({gx}, {gy}) "
            f"{self._format_point_and_scan_rgb(self._rgb_at_logic(gx, gy), self._located_scan_rgb).strip()}"
        )

    def _clear_input_coordinate(self) -> None:
        if self._located_point is None:
            return
        self._located_point = None
        self._located_scan_rgb = None
        self._redraw_canvas()
        self._refresh_status()
        print("↩ 已清除坐标定位")

    def _add_blue_point(self, gx: int, gy: int) -> None:
        uid = self._blue_uid_next
        self._blue_uid_next += 1
        scan_rgb = runtime_scan_rgb_for_region(
            point_probe_region(gx, gy, key=f"区域显示器.蓝点{uid}")
        )
        self._blue_points.append(
            BluePoint(gx=gx, gy=gy, uid=uid, scan_rgb=scan_rgb)
        )
        self._refill_blue_list()
        self._redraw_canvas()
        self._refresh_status()
        print(f"🔵 蓝点 #{uid}: ({gx}, {gy}) {self._format_rgb(self._rgb_at_logic(gx, gy))}")

    def _remove_blue_at_index(self, index: int) -> None:
        if index < 0 or index >= len(self._blue_points):
            return
        removed = self._blue_points.pop(index)
        self._refill_blue_list()
        self._redraw_canvas()
        self._refresh_status()
        print(f"↩ 已删除蓝点 #{removed.uid}: ({removed.gx}, {removed.gy})")

    def _on_canvas_left(self, event) -> None:
        if not self._has_capture:
            messagebox.showinfo("提示", "请先按 Enter 截取游戏画面。")
            return
        self._compute_display_transform()
        cx, cy = self._canvas_xy(event)
        pos = self._canvas_to_logic(cx, cy)
        if pos is None:
            messagebox.showinfo("提示", "请点击截图区域内（灰色边距无效）。")
            return
        self._add_blue_point(pos[0], pos[1])

    def _on_canvas_right(self, event) -> None:
        if not self._has_capture or not self._blue_points:
            return
        self._compute_display_transform()
        cx, cy = self._canvas_xy(event)
        idx = self._find_blue_index_at_canvas(cx, cy)
        if idx is not None:
            self._remove_blue_at_index(idx)

    def _on_blue_list_right_click(self, event) -> None:
        sel = self._blue_list.curselection()
        if sel:
            self._remove_blue_at_index(sel[0])

    def _add_mark(
        self,
        key: str,
        region: Optional[Region] = None,
        *,
        redraw: bool = True,
        quiet_duplicate: bool = False,
    ) -> bool:
        if not self._has_capture:
            messagebox.showinfo("提示", "请先按 Enter 截取游戏画面。")
            return False
        key = (key or "").strip().replace("\\", ".").replace("/", ".")
        while ".." in key:
            key = key.replace("..", ".")
        key = key.strip(".")
        if not key:
            return False
        if region is None:
            region = self._store.get(key)
        if region is None:
            sug = self._store.suggest(key, limit=5)
            hint = "\n".join(sug) if sug else "（无相近 key）"
            messagebox.showerror("未找到区域", f"找不到: {key}\n\n建议:\n{hint}")
            return False
        if any(m.key == region.key for m in self._marks):
            if not quiet_duplicate:
                messagebox.showinfo("提示", f"已在图上显示: {region.key}")
            return False
        gx, gy = _region_center(region)
        self._marks.append(
            OverlayMark(
                key=region.key,
                gx=gx,
                gy=gy,
                region=region,
                scan_rgb=runtime_scan_rgb_for_region(region),
            )
        )
        if redraw:
            self._redraw_canvas()
            self._refill_marks_list()
            self._refresh_status()
        print(f"✅ 红点: {region.key} @ ({gx:.0f}, {gy:.0f})")
        return True

    def _add_marks(self, keys: List[str], regions: Optional[List[Region]] = None) -> None:
        region_by_key = {r.key: r for r in (regions or [])}
        added = 0
        skipped = 0
        for key in keys:
            region = region_by_key.get(key)
            if self._add_mark(
                key,
                region=region,
                redraw=False,
                quiet_duplicate=True,
            ):
                added += 1
            else:
                skipped += 1
        self._redraw_canvas()
        self._refill_marks_list()
        self._refresh_status()
        if added:
            print(f"✅ 批量显示红点：新增 {added} 个，跳过 {skipped} 个")
        elif skipped:
            print(f"ℹ️ 批量显示红点：没有新增，跳过 {skipped} 个")

    def _add_region_from_entry(self) -> None:
        self._add_mark(self._key_var.get())

    def _open_browse(self) -> None:
        RegionPickDialog(self.root, self._store.keys(), self._on_browse_pick)

    def _on_browse_pick(
        self,
        keys,
        region: Optional[Region] = None,
        regions: Optional[List[Region]] = None,
    ) -> None:
        if isinstance(keys, str):
            key_list = [keys]
            region_list = [region] if region is not None else None
        else:
            key_list = [str(k) for k in keys]
            region_list = regions
        if not key_list:
            return
        self._key_var.set(key_list[-1])
        self._add_marks(key_list, regions=region_list)

    def _undo_last_red(self) -> None:
        if not self._marks:
            return
        removed = self._marks.pop()
        self._redraw_canvas()
        self._refill_marks_list()
        self._refresh_status()
        print(f"↩ 已移除红点: {removed.key}")

    def _do_save_image(self) -> None:
        if self._current_pil is None:
            messagebox.showwarning("保存失败", "请先按 Enter 截取画面。")
            return
        out = self._current_pil.copy()
        draw = ImageDraw.Draw(out)
        try:
            font_sm = ImageFont.truetype("arial.ttf", 11)
        except Exception:
            font_sm = ImageFont.load_default()

        for m in self._marks:
            region = m.region or self._store.get(m.key)
            if region and len(region.points) >= 2:
                poly = [self._logic_to_image_px(p[0], p[1]) for p in region.points]
                if len(poly) >= 2:
                    draw.polygon(poly, outline=(68, 204, 255))
            ix, iy = self._logic_to_image_px(m.gx, m.gy)
            r = 8
            draw.ellipse(
                [ix - r, iy - r, ix + r, iy + r],
                fill=(255, 34, 34), outline=(255, 255, 255), width=2,
            )
            red_text = m.key
            if self._show_red_rgb_var.get():
                red_text += self._format_point_and_scan_rgb(
                    self._rgb_at_logic(m.gx, m.gy),
                    m.scan_rgb,
                )
            draw.text((ix + 10, iy - 14), red_text, fill=(255, 255, 0), font=font_sm)

        if self._show_blue_var.get():
            for bp in self._blue_points:
                ix, iy = self._logic_to_image_px(bp.gx, bp.gy)
                r = 8
                draw.ellipse(
                    [ix - r, iy - r, ix + r, iy + r],
                    fill=(34, 136, 255), outline=(255, 255, 255), width=2,
                )
                blue_text = f"({bp.gx},{bp.gy})"
                if self._show_blue_rgb_var.get():
                    blue_text += self._format_point_and_scan_rgb(
                        self._rgb_at_logic(bp.gx, bp.gy),
                        bp.scan_rgb,
                    )
                draw.text((ix + 8, iy + 10), blue_text, fill=(170, 220, 255), font=font_sm)

        if self._located_point is not None:
            gx, gy = self._located_point
            ix, iy = self._logic_to_image_px(gx, gy)
            r = 11
            draw.ellipse(
                [ix - r, iy - r, ix + r, iy + r],
                outline=(0, 255, 102),
                width=3,
            )
            draw.line([ix - 18, iy, ix + 18, iy], fill=(0, 255, 102), width=2)
            draw.line([ix, iy - 18, ix, iy + 18], fill=(0, 255, 102), width=2)
            draw.text(
                (ix + 12, iy - 24),
                f"LOCATE ({gx},{gy})",
                fill=(0, 255, 102),
                font=font_sm,
            )

        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(OUTPUT_DIR, f"region_view_{ts}.png")
        out.save(path, format="PNG")
        print(f"💾 已保存: {path}")
        messagebox.showinfo("已保存", path)

    def _prompt_save_blue_points(self) -> None:
        """退出前：每个蓝点依次弹窗输入区域名，空则跳过。"""
        if not self._blue_points:
            return
        n = len(self._blue_points)
        saved = 0
        skipped = 0
        for i, bp in enumerate(list(self._blue_points)):
            title = f"保存蓝点 {i + 1} / {n}"
            prompt = (
                f"坐标 ({bp.gx}, {bp.gy})\n\n"
                "输入区域 key（格式：分类.名称，如 对话框.测试点）\n"
                "留空或取消则跳过此点："
            )
            key = simpledialog.askstring(title, prompt, parent=self.root)
            if key is None:
                skipped += 1
                continue
            key = key.strip()
            if not key:
                skipped += 1
                print(f"⏭ 跳过蓝点 #{bp.uid} ({bp.gx}, {bp.gy})")
                continue
            ok, msg = _save_region_json(key, bp.gx, bp.gy)
            if ok:
                saved += 1
                print(f"💾 已保存 {key} -> {msg}")
            else:
                messagebox.showerror("保存失败", f"{key}: {msg}", parent=self.root)

        if saved or skipped:
            summary = f"蓝点保存完成：写入 {saved} 个"
            if skipped:
                summary += f"，跳过 {skipped} 个"
            print(summary)
            if saved:
                try:
                    self._store.reload()
                except Exception:
                    pass

    def _finish_exit(self) -> None:
        self._prompt_save_blue_points()
        self._running = False
        self.root.quit()

    def _on_close(self) -> None:
        if messagebox.askyesno("退出", "确定退出？（将依次询问蓝点区域名）", default=messagebox.NO):
            self._finish_exit()

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


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--viewport", nargs=4, type=float)
    parser.add_argument("--viewport-hwnd", type=int, default=0)
    args, _unknown = parser.parse_known_args(argv)

    _configure_console_utf8()
    print("=== NieoPilot 区域显示器 ===\n")
    print(f"区域库: {REGION_ROOT}")
    print(f"截图保存: {OUTPUT_DIR}\n")
    inherited = apply_inherited_viewport(
        tuple(args.viewport) if args.viewport else None,
        args.viewport_hwnd,
    )
    if inherited:
        print(
            "✅ 已继承主程序扫描视口："
            f"x={args.viewport[0]:.2f}, y={args.viewport[1]:.2f}, "
            f"w={args.viewport[2]:.2f}, h={args.viewport[3]:.2f}"
        )
    elif not window_manager.find_window():
        print("⚠ 未检测到游戏窗口；可先开游戏，Enter 截图时会再次检测。")
    else:
        print("✅ 已连接游戏窗口；未收到主程序视口，使用本进程推算视口。")
    app = RegionViewerApp()
    app.run()


if __name__ == "__main__":
    main()
