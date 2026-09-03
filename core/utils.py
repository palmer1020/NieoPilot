# core/utils.py
import os
import time
import subprocess
import threading
import math
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

import win32gui
import win32api
import win32con
import win32process
from datetime import datetime
from PIL import ImageDraw, ImageGrab

from config import (
    BASE_PATH,
    WINDOW_TITLE,
    GAME_LOGIC_W,
    GAME_LOGIC_H,
    GAME_PATH,
    FIXED_RATIO,
    SETTINGS_DIALOG_LOGIC_W,
    SETTINGS_DIALOG_LOGIC_H,
)
from core.logger import logger, emit_kernel_log


class WindowManager:
    def __init__(self):
        self.hwnd = 0
        self.content_padding = None
        self._fixed_viewport: Optional[Tuple[float, float, float, float]] = None
        self._fixed_viewport_hwnd = 0
        self.last_calibration_canvas_path = ""
        self.last_calibration_framed_path = ""
        self._calibration_revision = 0
        # 未扫边时 get_current_viewport 只提示一次（不自动扫边，依赖界面手动校准）
        self._viewport_zero_pad_logged = False

        # 只有通过本程序 launch_game() 启动时才会有
        self._proc = None
        self._log_thread_started = False
        # launch_game 最近一次失败原因（供界面展示，避免误报「仅路径错误」）
        self.last_launch_error = ""

        # move_start / move_end 拖拽状态
        self._drag_active = False
        self._drag_foreground = False
        self._drag_start_game: Optional[Tuple[float, float]] = None

    def move_cancel(self) -> None:
        """若左键仍按住则松开，并清除拖拽状态。"""
        if not self._drag_active:
            return
        try:
            if self._drag_foreground:
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            elif self.hwnd and self._drag_start_game:
                coords = self.game_to_screen(*self._drag_start_game)
                if coords:
                    lp = self._screen_to_client_lparam(coords[0], coords[1])
                    if lp is not None:
                        win32gui.PostMessage(self.hwnd, win32con.WM_LBUTTONUP, 0, lp)
        except Exception:
            pass
        self._drag_active = False
        self._drag_start_game = None

    def move_start(self, gx: float, gy: float, *, foreground: bool = False) -> bool:
        """move_start：移到起点并按下左键（保持按住）。"""
        if not self.find_window():
            return False
        coords = self.game_to_screen(gx, gy)
        if not coords:
            return False
        sx, sy = coords
        try:
            if foreground:
                win32api.SetCursorPos((sx, sy))
                time.sleep(0.02)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, sx, sy, 0, 0)
            else:
                if not self.hwnd:
                    return False
                lp = self._screen_to_client_lparam(sx, sy)
                if lp is None:
                    return False
                win32gui.PostMessage(
                    self.hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp
                )
                time.sleep(0.02)
        except Exception:
            return False
        self._drag_active = True
        self._drag_foreground = bool(foreground)
        self._drag_start_game = (float(gx), float(gy))
        return True

    def move_end(
        self,
        gx: float,
        gy: float,
        duration_s: float,
        *,
        foreground: Optional[bool] = None,
        abort_check: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """move_end：从 move_start 起点拖到终点，耗时 duration_s 后松开左键。"""
        if not self._drag_active or self._drag_start_game is None:
            return False
        if not self.find_window():
            self.move_cancel()
            return False

        use_fg = self._drag_foreground if foreground is None else bool(foreground)
        gx1, gy1 = self._drag_start_game
        start = self.game_to_screen(gx1, gy1)
        end = self.game_to_screen(gx, gy)
        if not start or not end:
            self.move_cancel()
            return False

        duration_s = max(0.0, float(duration_s))
        sx1, sy1 = start
        sx2, sy2 = end
        steps = max(2, int(math.ceil(duration_s / 0.01))) if duration_s > 0 else 1
        interval = duration_s / steps if duration_s > 0 else 0.0

        try:
            if use_fg:
                for i in range(1, steps + 1):
                    if abort_check and abort_check():
                        self.move_cancel()
                        return False
                    t = i / steps
                    cx = int(round(sx1 + (sx2 - sx1) * t))
                    cy = int(round(sy1 + (sy2 - sy1) * t))
                    win32api.SetCursorPos((cx, cy))
                    if interval > 0:
                        time.sleep(interval)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, sx2, sy2, 0, 0)
            else:
                if not self.hwnd:
                    self.move_cancel()
                    return False
                lp_end = self._screen_to_client_lparam(sx2, sy2)
                if lp_end is None:
                    self.move_cancel()
                    return False
                for i in range(1, steps + 1):
                    if abort_check and abort_check():
                        self.move_cancel()
                        return False
                    t = i / steps
                    cx = int(round(sx1 + (sx2 - sx1) * t))
                    cy = int(round(sy1 + (sy2 - sy1) * t))
                    lp = self._screen_to_client_lparam(cx, cy)
                    if lp is not None:
                        win32gui.PostMessage(
                            self.hwnd, win32con.WM_MOUSEMOVE, win32con.MK_LBUTTON, lp
                        )
                    if interval > 0:
                        time.sleep(interval)
                win32gui.PostMessage(self.hwnd, win32con.WM_LBUTTONUP, 0, lp_end)
        except Exception:
            self.move_cancel()
            return False

        self._drag_active = False
        self._drag_start_game = None
        return True

    # ===============================
    # Window / Process
    # ===============================
    def find_window(self) -> bool:
        new_hwnd = win32gui.FindWindow(None, WINDOW_TITLE)

        # 句柄变化：清缓存
        if new_hwnd != 0 and new_hwnd != self.hwnd:
            logger.info(f"🔄 检测到新窗口句柄 ({new_hwnd})，清除旧缓存...")
            self.hwnd = new_hwnd
            self.content_padding = None
            self._fixed_viewport = None
            self._fixed_viewport_hwnd = 0
            self._viewport_zero_pad_logged = False

        self.hwnd = new_hwnd
        return self.hwnd != 0

    def _monitor_logs(self, process: subprocess.Popen):
        """读取游戏 stdout，把 [内核] 行转发到主日志 + UI 回调"""
        try:
            while True:
                out = process.stdout.readline()
                if out == b"" and process.poll() is not None:
                    break
                if out:
                    text = out.decode("utf-8", errors="ignore").strip()
                    if text:
                        logger.info(f"[内核] {text}")
                        emit_kernel_log(text)
        except Exception as e:
            logger.error(f"⚠ 内核日志监控线程异常: {e}")

    def launch_game(self) -> bool:
        """
        启动或连接窗口。
        ⚠ 只有“通过这里启动”的进程，才能稳定读到 stdout 内核日志。
        如果游戏已手动打开，这里只会激活窗口，不会产生 stdout 内核日志。
        """
        self.last_launch_error = ""

        if self.find_window():
            logger.info("窗口已存在，已连接（不自动最大化）")
            return True

        if not os.path.exists(GAME_PATH):
            self.last_launch_error = f"找不到游戏 exe：{GAME_PATH}"
            logger.error(f"❌ 路径错误: {GAME_PATH}")
            return False

        try:
            logger.info(f"🚀 启动游戏: {GAME_PATH}")
            self._proc = subprocess.Popen(
                GAME_PATH,
                cwd=os.path.dirname(GAME_PATH),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            if not self._log_thread_started and self._proc.stdout is not None:
                threading.Thread(
                    target=self._monitor_logs, args=(self._proc,), daemon=True
                ).start()
                self._log_thread_started = True

            # 等窗口出现（FindWindow 按窗口标题精确匹配，须与 config.WINDOW_TITLE 一致）
            for _ in range(40):
                if self.find_window():
                    logger.info("✅ 捕获窗口（不自动最大化）")
                    return True
                time.sleep(0.5)

            self.last_launch_error = (
                "进程已启动，但在约 20 秒内未匹配到游戏窗口。"
                f"请把 config 里的 WINDOW_TITLE 改为与游戏主窗口标题完全一致（当前为 {WINDOW_TITLE!r}）。"
                "微端若改过窗口名，任务栏悬停即可看到标题。"
            )
            logger.error(
                f"❌ 启动超时：未捕获到标题为 {WINDOW_TITLE!r} 的窗口（FindWindow 精确匹配）"
            )
            return False

        except Exception as e:
            self.last_launch_error = str(e)
            logger.error(f"启动异常: {e}")
            return False

    def maximize_window(self):
        """
        前台并最大化游戏窗口。
        不在此处扫边：最大化动画/未完全铺满时自动扫边易得到错误 padding，改由界面手动校准写入 content_padding。
        """
        if not self.hwnd:
            return
        win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
        win32gui.ShowWindow(self.hwnd, win32con.SW_MAXIMIZE)
        try:
            win32gui.SetForegroundWindow(self.hwnd)
        except Exception:
            pass
        # 最大化动画与布局稳定后再点击，避免 region 偏移
        time.sleep(0.8)

    # ===============================
    # Calibration / Debug
    # ===============================
    def visual_debug(self) -> bool:
        """
        屏幕校准/可视化调试：
        1) 扫描纯黑 RGB=(0,0,0) 边界，遇到非纯黑内容得到初始 viewport
        2) 输出 viewport 与逻辑坐标到屏幕坐标对应关系
        3) 保存：screenshots/calibration/ 下全 client 截图 + 带细白框标出 12:7 内容区
        """
        if not self.find_window():
            logger.error("❌ 校准失败：未找到游戏窗口")
            return False

        try:
            win32gui.SetForegroundWindow(self.hwnd)
            time.sleep(0.2)
        except Exception:
            pass

        # 点击「校准屏幕」才允许重新扫边并定义固定画布；之后加载/截图不再扫边。
        self.content_padding = None
        self._fixed_viewport = None
        self._fixed_viewport_hwnd = 0
        self._viewport_zero_pad_logged = False
        self.last_calibration_canvas_path = ""
        self.last_calibration_framed_path = ""
        self._calibration_revision += 1
        logger.info(f"📐 第 {self._calibration_revision} 次校准：清除旧画布与旧 padding，强制重新扫边")
        self.scan_boundaries(force=True)

        vp = self.get_current_viewport()
        if not vp:
            logger.error("❌ 校准失败：无法获取 viewport")
            return False

        vx, vy, vw, vh = vp
        self._fixed_viewport = (float(vx), float(vy), float(vw), float(vh))
        self._fixed_viewport_hwnd = self.hwnd
        logger.info(f"👀 调试 viewport: x={vx:.1f}, y={vy:.1f}, w={vw:.1f}, h={vh:.1f}")
        logger.info(
            f"📌 按纯黑边界扫边后的固定 1200×700 画布已锁定：x={vx:.1f}, y={vy:.1f}, w={vw:.1f}, h={vh:.1f}；"
            "后续加载/截图将沿用该画布，直到再次点击校准屏幕"
        )

        points = [
            ("左上", 0, 0),
            ("右上", GAME_LOGIC_W, 0),
            ("右下", GAME_LOGIC_W, GAME_LOGIC_H),
            ("左下", 0, GAME_LOGIC_H),
            ("中心", GAME_LOGIC_W / 2, GAME_LOGIC_H / 2),
        ]

        # ✅ 暂时禁用指针移动逻辑：只计算坐标并输出日志
        logger.info("⏳ 开始计算校准坐标...")

        for name, gx, gy in points:
            pos = self.game_to_screen(gx, gy)
            if not pos:
                continue
            sx, sy = pos
            # ✅ 暂时禁用：不移动鼠标指针
            # win32api.SetCursorPos((sx, sy))
            logger.info(f"👉 {name} ({gx:.0f},{gy:.0f}) -> ({sx},{sy})")

        # 全客户区截图 + 带细白框标出 12:7 内容区（与 viewport 一致）
        try:
            l, t, r, b = win32gui.GetClientRect(self.hwnd)
            ox, oy = win32gui.ClientToScreen(self.hwnd, (0, 0))
            cw, ch = r - l, b - t
            full_img = ImageGrab.grab((ox, oy, ox + cw, oy + ch), all_screens=True)
            if full_img.mode != "RGB":
                full_img = full_img.convert("RGB")

            out_dir = os.path.join(BASE_PATH, "screenshots", "calibration")
            os.makedirs(out_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            path_full = os.path.join(out_dir, f"client_full_{ts}.png")
            full_img.save(path_full)
            logger.info(f"📷 已保存全 client: {path_full}")

            # 视口在客户区中的像素包络（与 get_current_viewport 一致，含 12:7 letterbox 内区）
            left = max(0, min(cw - 1, int(math.floor(vx - ox + 0.01))))
            top = max(0, min(ch - 1, int(math.floor(vy - oy + 0.01))))
            right = max(left, min(cw - 1, int(math.ceil(vx + vw - ox - 0.01)) - 1))
            bottom = max(top, min(ch - 1, int(math.ceil(vy + vh - oy - 0.01)) - 1))

            framed = full_img.copy()
            dr = ImageDraw.Draw(framed)
            wcol = (255, 255, 255)
            dr.line([left, top, right, top], fill=wcol, width=1)
            dr.line([left, bottom, right, bottom], fill=wcol, width=1)
            dr.line([left, top, left, bottom], fill=wcol, width=1)
            dr.line([right, top, right, bottom], fill=wcol, width=1)
            path_framed = os.path.join(out_dir, f"client_viewport_{ts}.png")
            framed.save(path_framed)
            self.last_calibration_framed_path = path_framed
            logger.info(f"📷 已保存白框标出 12:7 内容区: {path_framed}")

            canvas_img = ImageGrab.grab(
                (
                    int(math.floor(vx)),
                    int(math.floor(vy)),
                    int(math.ceil(vx + vw)),
                    int(math.ceil(vy + vh)),
                ),
                all_screens=True,
            )
            if canvas_img.mode != "RGB":
                canvas_img = canvas_img.convert("RGB")
            if canvas_img.size != (GAME_LOGIC_W, GAME_LOGIC_H):
                canvas_img = canvas_img.resize((GAME_LOGIC_W, GAME_LOGIC_H))
            path_canvas = os.path.join(out_dir, f"calibration_canvas_{ts}.png")
            canvas_img.save(path_canvas)
            self.last_calibration_canvas_path = path_canvas
            logger.info(f"📷 已保存固定 1200×700 校准画布: {path_canvas}")
        except Exception as e:
            logger.error(f"📷 校准截图保存失败: {e}")

        logger.info("✅ 已校准")
        return True

    # ===============================
    # Viewport / Mapping
    # ===============================
    def _is_bg_color(self, pixel, bg_color, threshold=20):
        return (
            abs(pixel[0] - bg_color[0]) < threshold
            and abs(pixel[1] - bg_color[1]) < threshold
            and abs(pixel[2] - bg_color[2]) < threshold
        )

    def scan_boundaries(self, *, force: bool = False):
        """扫描游戏内容区 padding（会写入 self.content_padding）"""
        if not self.hwnd:
            return
        if (
            not force
            and self._fixed_viewport is not None
            and self._fixed_viewport_hwnd == self.hwnd
        ):
            logger.info("📐 已有手动校准固定画布，跳过 scan_boundaries，避免重定义 1200×700 画布")
            return
        try:
            try:
                win32gui.SetForegroundWindow(self.hwnd)
                time.sleep(0.2)
            except Exception:
                pass

            l, t, r, b = win32gui.GetClientRect(self.hwnd)
            ox, oy = win32gui.ClientToScreen(self.hwnd, (0, 0))
            w, h = r - l, b - t
            
            img = ImageGrab.grab((ox, oy, ox + w, oy + h), all_screens=True)
            if img.mode != "RGB":
                img = img.convert("RGB")
            px = img.load()

            def is_pure_black(pixel) -> bool:
                return pixel[0] == 0 and pixel[1] == 0 and pixel[2] == 0

            def column_non_black_ratio(x: int) -> float:
                non_black = 0
                for y in range(h):
                    if not is_pure_black(px[x, y]):
                        non_black += 1
                return non_black / max(1, h)

            def row_non_black_ratio(y: int) -> float:
                non_black = 0
                for x in range(w):
                    if not is_pure_black(px[x, y]):
                        non_black += 1
                return non_black / max(1, w)

            # 扫四边 padding：找最靠外且非纯黑像素占比 >= 50% 的列/行。
            # 少量控件/文字浮在黑边上时不会把边界提前拉进黑边。
            min_non_black_ratio = 0.50
            left_pad = None
            for x in range(w):
                if column_non_black_ratio(x) >= min_non_black_ratio:
                    left_pad = x
                    break

            right_pad = None
            for x in range(w - 1, -1, -1):
                if column_non_black_ratio(x) >= min_non_black_ratio:
                    right_pad = w - 1 - x
                    break

            top_pad = None
            for y in range(h):
                if row_non_black_ratio(y) >= min_non_black_ratio:
                    top_pad = y
                    break

            bottom_pad = None
            for y in range(h - 1, -1, -1):
                if row_non_black_ratio(y) >= min_non_black_ratio:
                    bottom_pad = h - 1 - y
                    break

            if left_pad is None or right_pad is None or top_pad is None or bottom_pad is None:
                logger.warning("📐 scan_boundaries 未找到非纯黑占比 >= 50% 的边界，忽略本次扫边")
                self.content_padding = None
                return

            if (left_pad + right_pad) >= w * 0.25 or (top_pad + bottom_pad) >= h * 0.25:
                logger.warning(
                    f"📐 scan_boundaries 结果过大，疑似地图暗边误判，忽略: "
                    f"L{left_pad}, T{top_pad}, R{right_pad}, B{bottom_pad}"
                )
                self.content_padding = None
                return

            self.content_padding = (left_pad, top_pad, right_pad, bottom_pad)
            logger.info(
                f"📐 content_padding(非黑>=50%): L{left_pad}, T{top_pad}, R{right_pad}, B{bottom_pad}"
            )

        except Exception as e:
            logger.error(f"scan_boundaries 异常: {e}")
            self.content_padding = None

    def get_current_viewport(self):
        """返回 (vx, vy, vw, vh) —— 游戏渲染区在屏幕坐标中的位置"""
        if not self.hwnd:
            if not self.find_window():
                return None

        if self._fixed_viewport is not None:
            if self._fixed_viewport_hwnd == self.hwnd:
                return self._fixed_viewport
            self._fixed_viewport = None
            self._fixed_viewport_hwnd = 0

        l, t, r, b = win32gui.GetClientRect(self.hwnd)
        ox, oy = win32gui.ClientToScreen(self.hwnd, (0, 0))
        
        container_w = r - l
        container_h = b - t
        container_x = ox
        container_y = oy

        # 未手动校准时不自动扫边：按全客户区（等效 padding 全 0），避免未稳定最大化时扫出错误黑边
        if self.content_padding is None and not self._viewport_zero_pad_logged:
            logger.info(
                "📐 视口由整段客户区 + 12:7（FIXED_RATIO）推算，未使用扫边。可用界面「校准屏幕」检查截图。"
            )
            self._viewport_zero_pad_logged = True

        # 如果有 padding，就扣掉
        if self.content_padding is not None:
            lp, tp, rp, bp = self.content_padding
            container_x += lp
            container_y += tp
            container_w -= (lp + rp)
            container_h -= (tp + bp)

        # FIXED_RATIO：让游戏渲染区保持固定宽高比（黑边留在 container 内）
        if container_w / max(1, container_h) > FIXED_RATIO:
            # 太宽：以高度为准
            final_h = container_h
            final_w = final_h * FIXED_RATIO
            off_x = (container_w - final_w) / 2
            off_y = 0
        else:
            # 太高：以宽度为准
            final_w = container_w
            final_h = final_w / FIXED_RATIO
            off_x = 0
            off_y = (container_h - final_h) / 2

        return (container_x + off_x, container_y + off_y, final_w, final_h)

    def game_to_screen(self, gx, gy):
        """给点击用：返回 int 屏幕坐标"""
        vp = self.get_current_viewport()
        if not vp:
            return None
        vx, vy, vw, vh = vp
        sx = vx + (gx / GAME_LOGIC_W) * vw
        sy = vy + (gy / GAME_LOGIC_H) * vh
        return int(sx), int(sy)

    def game_to_screen_float(self, gx: float, gy: float):
        """给截图用：返回 float 屏幕坐标（避免 2px 探针被 int 截断偏移）"""
        vp = self.get_current_viewport()
        if not vp:
            return None
        vx, vy, vw, vh = vp
        sx = vx + (gx / GAME_LOGIC_W) * vw
        sy = vy + (gy / GAME_LOGIC_H) * vh
        return float(sx), float(sy)

    def screen_to_game(self, sx, sy):
        vp = self.get_current_viewport()
        if not vp:
            return None
        vx, vy, vw, vh = vp
        gx = (sx - vx) / vw * GAME_LOGIC_W
        gy = (sy - vy) / vh * GAME_LOGIC_H
        return gx, gy

    # ===============================
    # Click (不做随机；随机交给 Region)
    # ===============================
    def click(self, gx, gy, **_kwargs):
        coords = self.game_to_screen(gx, gy)
        if not coords:
            return
        sx, sy = coords
        win32api.SetCursorPos((sx, sy))
        time.sleep(0.03)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, sx, sy, 0, 0)
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, sx, sy, 0, 0)

    def hover_game_point_is_hand_cursor(
        self, gx: float, gy: float, *, settle_s: float = 0.12
    ) -> Optional[bool]:
        """Move the real cursor to a game point and report whether it becomes IDC_HAND."""
        coords = self.game_to_screen(gx, gy)
        if not coords:
            return None
        try:
            if self.hwnd and win32gui.GetForegroundWindow() != self.hwnd:
                win32gui.SetForegroundWindow(self.hwnd)
                time.sleep(0.08)
            if self.hwnd and win32gui.GetForegroundWindow() != self.hwnd:
                return None
            sx, sy = coords
            win32api.SetCursorPos((int(sx), int(sy)))
            time.sleep(max(0.0, float(settle_s)))
            cursor_info = win32gui.GetCursorInfo()
            if not cursor_info or len(cursor_info) < 2:
                return None
            current_cursor = cursor_info[1]
            hand_cursor = win32gui.LoadCursor(0, getattr(win32con, "IDC_HAND", 32649))
            return int(current_cursor) == int(hand_cursor)
        except Exception as e:
            logger.warning(f"前台悬停读取手型光标失败: {e}")
            return None

    def click_background(self, gx, gy, **_kwargs):
        coords = self.game_to_screen(gx, gy)
        if not coords or not self.hwnd:
            return
        sx, sy = coords
        try:
            ox, oy = win32gui.ClientToScreen(self.hwnd, (0, 0))
            rel_x = int(sx - ox)
            rel_y = int(sy - oy)
            l_param = (rel_y << 16) | (rel_x & 0xFFFF)

            win32gui.PostMessage(self.hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, l_param)
            time.sleep(0.03)
            win32gui.PostMessage(self.hwnd, win32con.WM_LBUTTONUP, 0, l_param)
        except Exception:
            pass

    def _screen_to_client_lparam(self, sx: int, sy: int) -> Optional[int]:
        if not self.hwnd:
            return None
        try:
            ox, oy = win32gui.ClientToScreen(self.hwnd, (0, 0))
            rel_x = int(sx - ox)
            rel_y = int(sy - oy)
            return (rel_y << 16) | (rel_x & 0xFFFF)
        except Exception:
            return None

    def move_drag(
        self,
        gx1: float,
        gy1: float,
        gx2: float,
        gy2: float,
        duration_s: float,
        *,
        foreground: bool = False,
        abort_check: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """兼容：等价于 move_start + move_end。"""
        if not self.move_start(gx1, gy1, foreground=foreground):
            return False
        return self.move_end(
            gx2, gy2, duration_s, foreground=foreground, abort_check=abort_check
        )

    def click_client(self, cx: int, cy: int, foreground: bool = False) -> bool:
        """
        主窗口 **client 像素** 坐标点击（不经过 1200×700 / 黑边视口缩放）。
        「刷新.设置」等贴在客户区左上角、与游戏画布逻辑坐标不一致时使用。
        """
        if not self.hwnd and not self.find_window():
            return False
        if not self.hwnd:
            return False
        try:
            ix = int(round(cx))
            iy = int(round(cy))
        except (TypeError, ValueError):
            return False
        try:
            if foreground:
                sx, sy = win32gui.ClientToScreen(self.hwnd, (ix, iy))
                win32api.SetCursorPos((int(sx), int(sy)))
                time.sleep(0.03)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, sx, sy, 0, 0)
                time.sleep(0.05)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, sx, sy, 0, 0)
            else:
                rel_x = ix & 0xFFFF
                rel_y = iy & 0xFFFF
                l_param = (rel_y << 16) | rel_x
                win32gui.PostMessage(
                    self.hwnd,
                    win32con.WM_LBUTTONDOWN,
                    win32con.MK_LBUTTON,
                    l_param,
                )
                time.sleep(0.03)
                win32gui.PostMessage(self.hwnd, win32con.WM_LBUTTONUP, 0, l_param)
            return True
        except Exception as e:
            logger.warning(f"click_client({ix},{iy}) 失败: {e}")
            return False

    def click_background_client_left_x(self, gx, gy, offset_x: int = 4) -> bool:
        """
        特殊后台点击：
        - X 不使用逻辑坐标换算，固定为主窗口 client 左边界 + offset_x
        - Y 使用逻辑坐标换算后的屏幕 y（仅参考转换出来的纵坐标）
        """
        if not self.hwnd:
            return False
        coords = self.game_to_screen(gx, gy)
        if not coords:
            return False
        _, sy = coords
        try:
            client_x, client_y = win32gui.ClientToScreen(self.hwnd, (0, 0))
            click_x = int(client_x + int(offset_x))
            click_y = int(sy)

            rel_x = int(click_x - client_x)
            rel_y = int(click_y - client_y)
            l_param = (rel_y << 16) | (rel_x & 0xFFFF)

            win32gui.PostMessage(
                self.hwnd,
                win32con.WM_LBUTTONDOWN,
                win32con.MK_LBUTTON,
                l_param,
            )
            time.sleep(0.03)
            win32gui.PostMessage(self.hwnd, win32con.WM_LBUTTONUP, 0, l_param)
            return True
        except Exception as e:
            logger.warning(f"click_background_client_left_x 失败: {e}")
            return False

    # 刷新 UI：点击「刷新.设置」后主窗口会弹出独立子窗口（标题通常为「设置」），
    # 「刷新」「保存」必须 PostMessage 到该 HWND，否则会点到主窗口客户区而无效。
    SETTINGS_DIALOG_TITLE = "设置"

    def find_settings_dialog_hwnd(self) -> int:
        """与游戏同进程、标题精确的「设置」顶层/弹窗句柄；找不到返回 0。"""
        if not self.hwnd and not self.find_window():
            return 0
        try:
            _, game_pid = win32process.GetWindowThreadProcessId(self.hwnd)
        except Exception:
            return 0
        found: list[int] = []

        def _enum(h, _):
            try:
                if not win32gui.IsWindowVisible(h):
                    return True
                title = win32gui.GetWindowText(h)
                if title != self.SETTINGS_DIALOG_TITLE:
                    return True
                _, pid = win32process.GetWindowThreadProcessId(h)
                if pid == game_pid:
                    found.append(h)
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(_enum, None)
        except Exception:
            return 0
        return found[0] if found else 0

    def wait_settings_dialog_hwnd(self, timeout_s: float = 1.5) -> int:
        t0 = time.time()
        while time.time() - t0 < float(timeout_s):
            h = self.find_settings_dialog_hwnd()
            if h:
                return h
            time.sleep(0.05)
        return 0

    def click_background_on_dialog_hwnd(self, target_hwnd: int, gx: float, gy: float) -> bool:
        """
        将逻辑坐标 (gx,gy) 转为屏幕坐标后，再映射到 target_hwnd 的 **client** 坐标并后台点击。
        用于设置子窗口内的按钮；屏幕位置与主视口映射一致（录制区域仍按 1200×700）。
        """
        if not target_hwnd:
            return False
        coords = self.game_to_screen(gx, gy)
        if not coords:
            return False
        sx, sy = coords
        try:
            cx, cy = win32gui.ScreenToClient(target_hwnd, (int(sx), int(sy)))
            rel_x = int(cx) & 0xFFFF
            rel_y = int(cy) & 0xFFFF
            l_param = (rel_y << 16) | rel_x
            win32gui.PostMessage(
                target_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, l_param
            )
            time.sleep(0.03)
            win32gui.PostMessage(target_hwnd, win32con.WM_LBUTTONUP, 0, l_param)
            return True
        except Exception as e:
            logger.warning(f"click_background_on_dialog_hwnd 失败: {e}")
            return False

    def click_background_on_dialog_client_xy(self, target_hwnd: int, cx: float, cy: float) -> bool:
        """
        直接使用设置子窗口 client 坐标后台点击（不做主窗口坐标转换）。
        适用于「设置」子窗口专用录制坐标系。
        """
        if not target_hwnd:
            return False
        try:
            rel_x = int(cx) & 0xFFFF
            rel_y = int(cy) & 0xFFFF
            l_param = (rel_y << 16) | rel_x
            win32gui.PostMessage(
                target_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, l_param
            )
            time.sleep(0.03)
            win32gui.PostMessage(target_hwnd, win32con.WM_LBUTTONUP, 0, l_param)
            return True
        except Exception as e:
            logger.warning(f"click_background_on_dialog_client_xy 失败: {e}")
            return False

    def click_background_settings_dialog(self, gx: float, gy: float) -> bool:
        """优先找「设置」子窗口并点击；找不到返回 False（调用方可回退主窗口）。"""
        h = self.find_settings_dialog_hwnd()
        if not h:
            return False
        return self.click_background_on_dialog_hwnd(h, gx, gy)

    def click_background_settings_dialog_client_xy(self, cx: float, cy: float) -> bool:
        """优先找「设置」子窗口并按其 client 像素坐标点击；找不到返回 False。"""
        h = self.find_settings_dialog_hwnd()
        if not h:
            return False
        return self.click_background_on_dialog_client_xy(h, cx, cy)

    def click_background_settings_dialog_logic_xy(self, lx: float, ly: float) -> bool:
        """
        「设置」子窗口逻辑坐标点击（参考尺寸 SETTINGS_DIALOG_LOGIC_W×SETTINGS_DIALOG_LOGIC_H，
        与主窗口 1200×700 用法一致：JSON 存逻辑坐标，运行时按当前子窗口 client 宽高缩放）。
        """
        h = self.find_settings_dialog_hwnd()
        if not h:
            return False
        try:
            x1, y1, x2, y2 = win32gui.GetClientRect(h)
            cw = max(1, int(x2 - x1))
            ch = max(1, int(y2 - y1))
            cx = lx * float(cw) / float(SETTINGS_DIALOG_LOGIC_W)
            cy = ly * float(ch) / float(SETTINGS_DIALOG_LOGIC_H)
            return self.click_background_on_dialog_client_xy(h, cx, cy)
        except Exception as e:
            logger.warning(f"click_background_settings_dialog_logic_xy 失败: {e}")
            return False

    def click_background_settings_dialog_logic_xy_on_hwnd(
        self, target_hwnd: int, lx: float, ly: float
    ) -> bool:
        """已知「设置」子窗口句柄时，按逻辑坐标后台点击（PostMessage）。"""
        if not target_hwnd:
            return False
        try:
            x1, y1, x2, y2 = win32gui.GetClientRect(target_hwnd)
            cw = max(1, int(x2 - x1))
            ch = max(1, int(y2 - y1))
            cx = lx * float(cw) / float(SETTINGS_DIALOG_LOGIC_W)
            cy = ly * float(ch) / float(SETTINGS_DIALOG_LOGIC_H)
            return self.click_background_on_dialog_client_xy(target_hwnd, cx, cy)
        except Exception as e:
            logger.warning(
                f"click_background_settings_dialog_logic_xy_on_hwnd 失败: {e}"
            )
            return False

    def send_key(self, vk_code: int):
        """
        发送键盘按键到游戏窗口（后台）
        
        Args:
            vk_code: 虚拟键码（win32con.VK_XXX）
        """
        if not self.hwnd:
            return
        try:
            win32gui.PostMessage(self.hwnd, win32con.WM_KEYDOWN, vk_code, 0)
            time.sleep(0.05)
            win32gui.PostMessage(self.hwnd, win32con.WM_KEYUP, vk_code, 0)
        except Exception:
            pass
    
    def send_key_arrow_down(self):
        """发送向下箭头键"""
        self.send_key(win32con.VK_DOWN)
    
    def send_key_enter(self):
        """发送Enter键"""
        self.send_key(win32con.VK_RETURN)
    
    def click_client_origin_offset(self, offset_x: int = 5, offset_y: int = 5):
        """
        点击client左上角坐标+偏移量的位置（屏幕坐标，用于刷新）
        
        Args:
            offset_x: X偏移量（默认5）
            offset_y: Y偏移量（默认5）
        """
        if not self.hwnd:
            return False
        try:
            # 获取client左上角的屏幕坐标
            client_rect = win32gui.GetClientRect(self.hwnd)
            client_x, client_y = win32gui.ClientToScreen(self.hwnd, (0, 0))
            
            # 计算点击位置（client左上角+偏移量）
            click_x = client_x + offset_x
            click_y = client_y + offset_y
            
            # 点击该位置（使用屏幕坐标）
            win32api.SetCursorPos((click_x, click_y))
            time.sleep(0.05)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, click_x, click_y, 0, 0)
            time.sleep(0.05)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, click_x, click_y, 0, 0)
            return True
        except Exception as e:
            logger.error(f"点击client左上角偏移位置失败: {e}")
            return False

    # ===============================
    # Grab (模板匹配/探针)
    # ===============================
    def grab_game_bbox(self, gx1, gy1, gx2, gy2, min_size_px: int = 2, pad_px: int = 0):
        """
        ✅ 关键修复：支持 2px×2px 极小探针
        - 使用 float 映射 + floor/ceil，避免 int 截断导致偏 1px
        - right/bottom 使用开区间（PIL ImageGrab 本身就是开区间）
        - 最终保证至少 min_size_px × min_size_px
        """
        p1 = self.game_to_screen_float(gx1, gy1)
        p2 = self.game_to_screen_float(gx2, gy2)
        if not p1 or not p2:
            return None

        left_f = min(p1[0], p2[0])
        top_f = min(p1[1], p2[1])
        right_f = max(p1[0], p2[0])
        bottom_f = max(p1[1], p2[1])

        left = int(math.floor(left_f)) - int(pad_px)
        top = int(math.floor(top_f)) - int(pad_px)
        right = int(math.ceil(right_f)) + int(pad_px)
        bottom = int(math.ceil(bottom_f)) + int(pad_px)

        # 保底最小尺寸（以前你这里 <2 直接 return None，是探针读色错位的根源）
        if right - left < min_size_px:
            cx = (left + right) // 2
            left = cx - min_size_px // 2
            right = left + min_size_px
        if bottom - top < min_size_px:
            cy = (top + bottom) // 2
            top = cy - min_size_px // 2
            bottom = top + min_size_px

        # 防负
        if left < 0:
            right -= left
            left = 0
        if top < 0:
            bottom -= top
            top = 0

        if right <= left or bottom <= top:
            return None

        return ImageGrab.grab((left, top, right, bottom), all_screens=True)
    
    # core/utils.py 里的 class WindowManager:

    def ensure_game_hwnd(self, maximize: bool = False, scan: bool = False) -> bool:
        """
        兼容旧代码/新runner里调用的 ensure_game_hwnd。
        - 返回 True: 找到游戏窗口句柄
        - 返回 False: 未找到
        可选：
        - maximize=True: 最大化窗口
        - scan=True: 扫描边界（game_bbox / padding）
        """
        ok = self.find_window()
        if not ok:
            return False

        if maximize:
            try:
                self.maximize_window()
            except Exception:
                pass

        if scan:
            try:
                self.scan_boundaries()
            except Exception:
                pass

        return True


def screenshots_subdir(project_root: str, category: str) -> str:
    """
    返回并确保存在：{project_root}/screenshots/{category}/
    category 示例：capture（捕捉放回仓库前）、client（全屏调试）、ocr_enemy（对战 OCR）、ocr_training（训练室等级 OCR）
    """
    d = os.path.join(project_root, "screenshots", category)
    os.makedirs(d, exist_ok=True)
    return d


BAG_UI_READY_PROBE_KEY = "精灵背包.清空精灵一"
BAG_UI_READY_ORANGE_RGB = (255, 153, 1)  # #FF9901
BAG_UI_READY_ORANGE_TOLERANCE = 45.0
BAG_OPEN_READY_TIMEOUT_SEC = 7.0
BAG_OPEN_READY_POLL_SEC = 0.12
BAG_EMPTY_DEEP_BLUE_CONFIRM_SEC = 10.0
BAG_SLOT_OCCUPIED_RGB_CENTERS = {
    "orange": (254, 104, 1),
    # 当前显示器常见的青色均值约为(186,238,253)；旧显示器的(148,223,252)
    # 到该中心仍在45距离阈值内，因此同时兼容两种显示效果。
    "cyan": (186, 238, 253),
    "purple": (71, 28, 83),
}
BAG_SLOT_OCCUPIED_MAX_DISTANCE = 45.0
BAG_SLOT_EMPTY_DEEP_BLUE_RGB = (24, 73, 146)  # #184992
BAG_SLOT_EMPTY_MAX_DISTANCE = 55.0
BAG_COUNT_SCAN_TIMEOUT_SEC: Optional[float] = 10.0
BAG_COUNT_SCAN_STABLE_SCANS = 2

# map10 白色探针（代替内核 newNpc/multi）：由纯白变为非纯白即就绪
MAP10_WHITE_PROBE_STILL_WHITE_MIN_CHANNEL = 240
MAP10_WHITE_PROBE_STILL_WHITE_MIN_MEAN = 248.0
MAP10_WHITE_PROBE_KEY_NIEO = "尼奥一.白色探针"
MAP10_WHITE_PROBE_KEY_FLASH = "闪光皮皮.白色探针"
MAP10_WHITE_PROBE_POLL_SEC = 0.02
MAP10_WHITE_PROBE_NEUTRAL_GRAY_MAX_SPREAD = 8
MAP10_WHITE_PROBE_NEUTRAL_GRAY_MIN_MEAN = 120.0


def is_map10_white_probe_still_white_rgb(rgb: Tuple[int, int, int]) -> bool:
    """白色探针仍为纯白（map10 NPC 尚未就绪）。"""
    r, g, b = rgb
    if min(r, g, b) < MAP10_WHITE_PROBE_STILL_WHITE_MIN_CHANNEL:
        return False
    return (r + g + b) / 3.0 >= MAP10_WHITE_PROBE_STILL_WHITE_MIN_MEAN


def is_map10_white_probe_ready_rgb(rgb: Tuple[int, int, int]) -> bool:
    """白色探针已变成有效非白色，视为 map10 NPC 已就绪。"""
    if is_map10_white_probe_still_white_rgb(rgb):
        return False
    r, g, b = rgb
    mean = (r + g + b) / 3.0
    spread = max(r, g, b) - min(r, g, b)
    if mean >= MAP10_WHITE_PROBE_NEUTRAL_GRAY_MIN_MEAN and spread <= MAP10_WHITE_PROBE_NEUTRAL_GRAY_MAX_SPREAD:
        return False
    return True


def resolve_map10_white_probe_key(
    regions,
    *,
    prefer_key: Optional[str] = None,
    mode: Optional[str] = None,
) -> Optional[str]:
    """解析 map10 白色探针 region key（显式 prefer > 尼奥模式尼奥一 > 闪光皮皮 > 尼奥一）。"""
    if prefer_key and regions.get(prefer_key):
        return prefer_key
    if mode == "nieo" and regions.get(MAP10_WHITE_PROBE_KEY_NIEO):
        return MAP10_WHITE_PROBE_KEY_NIEO
    if regions.get(MAP10_WHITE_PROBE_KEY_FLASH):
        return MAP10_WHITE_PROBE_KEY_FLASH
    if regions.get(MAP10_WHITE_PROBE_KEY_NIEO):
        return MAP10_WHITE_PROBE_KEY_NIEO
    return None


def wait_map10_white_probe_ready(
    regions,
    *,
    emit_fn: Optional[Callable[[str, str], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    white_probe_key: Optional[str] = None,
    mode: Optional[str] = None,
    log_tag: str = "map10",
    timeout_s: float = 30.0,
    poll_s: float = MAP10_WHITE_PROBE_POLL_SEC,
    two_phase: bool = False,
) -> bool:
    """
    轮询白色探针直至就绪（map10 / 露西之核 map55 等代替 newNpc 认证）。

    two_phase=False：探针当前为非纯白即就绪（map10 首开）。
    two_phase=True：须先观察到纯白（进图加载），再由纯白变为非纯白（NPC 就绪）；
      用于 A→B 再入时避免残留旧色被误判为就绪（如露西之核 54→55）。
    """
    probe_key = resolve_map10_white_probe_key(
        regions, prefer_key=white_probe_key, mode=mode
    )
    if not probe_key:
        if emit_fn:
            emit_fn(
                f"⚠️ [{log_tag}] 未找到白色探针区域"
                f"（{MAP10_WHITE_PROBE_KEY_FLASH} / {MAP10_WHITE_PROBE_KEY_NIEO}）",
                "WARN",
            )
        return False

    deadline = time.time() + timeout_s

    if two_phase:
        if emit_fn:
            emit_fn(
                f"🔍 [{log_tag}] 等待 {probe_key} 先变白再变非白（两阶段进图认证）…",
                "INFO",
            )
        saw_white = False
        last_sample_log_ts = 0.0
        while time.time() < deadline:
            if stop_check and stop_check():
                return False
            rgb = mean_rgb_for_region_key(regions, probe_key)
            if rgb and is_map10_white_probe_still_white_rgb(rgb):
                saw_white = True
                if emit_fn:
                    r, g, b = rgb
                    emit_fn(
                        f"✅ [{log_tag}] 白色探针已变白（RGB=({r},{g},{b})），等待变非白…",
                        "SUCCESS",
                    )
                break
            now = time.time()
            if emit_fn and now - last_sample_log_ts >= 0.5:
                emit_fn(
                    f"🔍 [{log_tag}] 等待白色探针先变白：{probe_key} RGB={rgb}",
                    "DEBUG",
                )
                last_sample_log_ts = now
            time.sleep(poll_s)
        if not saw_white:
            if emit_fn:
                emit_fn(
                    f"⏱️ [{log_tag}] 等待白色探针先变白超时（{timeout_s}s）",
                    "WARN",
                )
            return False
        last_sample_log_ts = 0.0
        while time.time() < deadline:
            if stop_check and stop_check():
                return False
            rgb = mean_rgb_for_region_key(regions, probe_key)
            if rgb and is_map10_white_probe_ready_rgb(rgb):
                if emit_fn:
                    r, g, b = rgb
                    emit_fn(
                        f"✅ [{log_tag}] 白色探针已非纯白（RGB=({r},{g},{b})）",
                        "SUCCESS",
                    )
                return True
            now = time.time()
            if emit_fn and now - last_sample_log_ts >= 0.5:
                emit_fn(
                    f"🔍 [{log_tag}] 等待白色探针变非白：{probe_key} RGB={rgb}",
                    "DEBUG",
                )
                last_sample_log_ts = now
            time.sleep(poll_s)
        if emit_fn:
            emit_fn(
                f"⏱️ [{log_tag}] 等待白色探针由白变非白超时（{timeout_s}s）",
                "WARN",
            )
        return False

    if emit_fn:
        emit_fn(
            f"🔍 [{log_tag}] 等待 {probe_key} 由白色变为非白色（map10 NPC 就绪）…",
            "INFO",
        )
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if stop_check and stop_check():
            return False
        rgb = mean_rgb_for_region_key(regions, probe_key)
        if rgb and is_map10_white_probe_ready_rgb(rgb):
            if emit_fn:
                r, g, b = rgb
                emit_fn(
                    f"✅ [{log_tag}] 白色探针已非纯白（RGB=({r},{g},{b})）",
                    "SUCCESS",
                )
            return True
        time.sleep(poll_s)
    if emit_fn:
        emit_fn(
            f"⏱️ [{log_tag}] 等待白色探针变非白超时（{timeout_s}s）",
            "WARN",
        )
    return False


def is_bag_ui_ready_orange_rgb(
    rgb: Tuple[int, int, int],
    tolerance: float = BAG_UI_READY_ORANGE_TOLERANCE,
) -> bool:
    """清空精灵一探针均值接近橙色 #FF9901 时视为背包 UI 已就绪。"""
    r, g, b = rgb
    ref_r, ref_g, ref_b = BAG_UI_READY_ORANGE_RGB
    dist = math.sqrt((r - ref_r) ** 2 + (g - ref_g) ** 2 + (b - ref_b) ** 2)
    return dist <= tolerance


def mean_rgb_for_region_key(regions, region_key: str) -> Optional[Tuple[int, int, int]]:
    """截取区域并返回 RGB 均值（失败返回 None）。"""
    try:
        reg = regions.get(region_key)
        if reg is None:
            return None
        x1, y1, x2, y2 = reg.outer_bbox()
        img = window_manager.grab_game_bbox(x1, y1, x2, y2, min_size_px=2)
        if img is None:
            return None
        rgb = img.convert("RGB").resize((1, 1)).getpixel((0, 0))
        return int(rgb[0]), int(rgb[1]), int(rgb[2])
    except Exception:
        return None


def classify_pet_bag_slot_rgb(
    rgb: Optional[Tuple[int, int, int]],
) -> Optional[str]:
    """背包槽位颜色：橙/青/紫为有宠，深蓝为无宠，其余无法确认。"""
    if rgb is None:
        return None
    r, g, b = rgb
    occupied_distance_sq, occupied_color = min(
        (
            (r - center[0]) ** 2 + (g - center[1]) ** 2 + (b - center[2]) ** 2,
            color,
        )
        for color, center in BAG_SLOT_OCCUPIED_RGB_CENTERS.items()
    )
    if occupied_distance_sq <= BAG_SLOT_OCCUPIED_MAX_DISTANCE ** 2:
        return occupied_color

    empty_r, empty_g, empty_b = BAG_SLOT_EMPTY_DEEP_BLUE_RGB
    empty_distance_sq = (
        (r - empty_r) ** 2 + (g - empty_g) ** 2 + (b - empty_b) ** 2
    )
    if empty_distance_sq <= BAG_SLOT_EMPTY_MAX_DISTANCE ** 2 or (
        r <= 70
        and 35 <= g <= 120
        and 100 <= b <= 195
        and (b - r) >= 55
        and (b - g) >= 25
    ):
        return "deep_blue"
    return None


def pet_bag_slot_color_label(color: Optional[str]) -> str:
    return {
        "orange": "橙色有宠",
        "cyan": "青色有宠",
        "purple": "紫色有宠",
        "deep_blue": "深蓝空槽",
    }.get(color or "", "未知")


def analyze_pet_bag_slot_colors(
    colors: Sequence[Optional[str]],
) -> Dict[str, Any]:
    """
    背包只接受连续前缀有宠、连续后缀空槽：1…N 有宠，N+1…6 深蓝。
    例如 1/2/3/4 有宠有效；1/3/4/5 有宠属于中间空洞，无效。
    """
    states = tuple(colors)
    occupied_colors = frozenset(BAG_SLOT_OCCUPIED_RGB_CENTERS)
    unknown_slots = [i + 1 for i, color in enumerate(states) if color is None]
    if unknown_slots:
        return {
            "ok": False,
            "count": None,
            "colors": states,
            "reason": f"槽位{unknown_slots}颜色未知",
        }

    first_empty = next(
        (i for i, color in enumerate(states) if color == "deep_blue"),
        len(states),
    )
    invalid_occupied_slots = [
        i + 1
        for i, color in enumerate(states[first_empty:], start=first_empty)
        if color in occupied_colors
    ]
    invalid_states = [
        i + 1
        for i, color in enumerate(states)
        if color not in occupied_colors and color != "deep_blue"
    ]
    if invalid_states:
        return {
            "ok": False,
            "count": None,
            "colors": states,
            "reason": f"槽位{invalid_states}状态无效",
        }
    if invalid_occupied_slots:
        return {
            "ok": False,
            "count": None,
            "colors": states,
            "reason": (
                f"槽位{invalid_occupied_slots}在空槽之后仍有宠，"
                "不符合后置空槽规则"
            ),
        }
    return {
        "ok": True,
        "count": first_empty,
        "colors": states,
        "reason": "",
    }


def scan_pet_bag_count(
    regions,
    *,
    emit_fn: Optional[Callable[[str, str], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    log_tag: str = "背包",
    timeout_s: Optional[float] = BAG_COUNT_SCAN_TIMEOUT_SEC,
    poll_s: float = BAG_OPEN_READY_POLL_SEC,
) -> Dict[str, Any]:
    """背包 UI 就绪后扫描 1–6 槽位，稳定确认连续宠物数量。"""
    required_keys = tuple(f"精灵背包.{pos}" for pos in range(1, 7))
    missing_keys = [key for key in required_keys if regions.get(key) is None]
    if missing_keys:
        result = {
            "ok": False,
            "count": None,
            "colors": (None,) * 6,
            "data": {},
            "reason": f"缺少槽位探针：{', '.join(missing_keys)}",
        }
        if emit_fn:
            emit_fn(f"⚠️ [{log_tag}] 背包数量扫描失败：{result['reason']}", "WARN")
        return result

    deadline = (
        None
        if timeout_s is None
        else time.time() + max(0.0, float(timeout_s))
    )
    stable_signature = None
    stable_scans = 0
    last_wait_log = 0.0
    last_result: Dict[str, Any] = {
        "ok": False,
        "count": None,
        "colors": (None,) * 6,
        "data": {},
        "reason": "尚未扫描",
    }
    while deadline is None or time.time() <= deadline:
        if stop_check and stop_check():
            return {
                **last_result,
                "ok": False,
                "count": None,
                "reason": "扫描被停止",
            }

        data: Dict[int, Dict[str, Any]] = {}
        colors = []
        for pos, key in enumerate(required_keys, start=1):
            rgb = mean_rgb_for_region_key(regions, key)
            color = classify_pet_bag_slot_rgb(rgb)
            colors.append(color)
            data[pos] = {"key": key, "rgb": rgb, "color": color}

        analyzed = analyze_pet_bag_slot_colors(colors)
        last_result = {**analyzed, "data": data}
        signature = tuple(colors)
        if analyzed["ok"] and signature == stable_signature:
            stable_scans += 1
        elif analyzed["ok"]:
            stable_signature = signature
            stable_scans = 1
        else:
            stable_signature = None
            stable_scans = 0

        if stable_scans >= BAG_COUNT_SCAN_STABLE_SCANS:
            desc = "；".join(
                f"{pos}={pet_bag_slot_color_label(item['color'])}"
                for pos, item in data.items()
            )
            if emit_fn:
                emit_fn(
                    f"🎒 [{log_tag}] 背包宠物数量={analyzed['count']}；{desc}",
                    "SUCCESS",
                )
            return last_result
        now = time.time()
        if emit_fn and now - last_wait_log >= 1.0:
            reason = analyzed.get("reason") or (
                f"等待相同结果稳定 {stable_scans}/{BAG_COUNT_SCAN_STABLE_SCANS}"
            )
            emit_fn(
                f"🔄 [{log_tag}] 背包数量继续等待稳定：{reason}",
                "DEBUG",
            )
            last_wait_log = now
        time.sleep(max(0.01, float(poll_s)))

    desc = "；".join(
        f"{pos}=RGB{item['rgb']}→{pet_bag_slot_color_label(item['color'])}"
        for pos, item in last_result.get("data", {}).items()
    )
    timed_out = {
        **last_result,
        "ok": False,
        "count": None,
        "reason": last_result.get("reason") or "有限扫描时间内结果未稳定",
    }
    if emit_fn:
        emit_fn(
            f"🔄 [{log_tag}] 有限扫描时间结束，未取得稳定结果："
            f"{timed_out['reason']}；{desc}",
            "DEBUG",
        )
    return timed_out


def wait_pet_bag_ui_ready_after_open(
    regions,
    *,
    emit_fn: Optional[Callable[[str, str], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    log_tag: str = "背包",
    timeout_s: float = BAG_OPEN_READY_TIMEOUT_SEC,
    poll_s: float = BAG_OPEN_READY_POLL_SEC,
    probe_key: str = BAG_UI_READY_PROBE_KEY,
    bag_scan_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    allow_empty_bag: bool = False,
    empty_confirm_s: float = BAG_EMPTY_DEEP_BLUE_CONFIRM_SEC,
) -> bool:
    """
    打开精灵背包后轮询「清空精灵一」探针，检测到橙色 #FF9901 即视为 UI 就绪。
    清包场景可启用 allow_empty_bag：精灵一探针连续深蓝满 10 秒，判定背包为 0 只。
    其他恢复/跟随场景不接受空背包，避免继续点击不存在的精灵。
    """
    empty_confirm_s = max(0.0, float(empty_confirm_s))
    effective_timeout_s = max(
        max(0.0, float(timeout_s)),
        empty_confirm_s if allow_empty_bag else 0.0,
    )
    if emit_fn:
        empty_text = (
            f"；若持续深蓝满 {empty_confirm_s:g}s，则确认背包为0只"
            if allow_empty_bag
            else ""
        )
        emit_fn(
            f"⏳ [{log_tag}] 扫描清空精灵一探针，等待橙色就绪(#FF9901)"
            f"{empty_text}，{effective_timeout_s:g}s超时",
            "INFO",
        )
    t0 = time.time()
    base_deadline = t0 + effective_timeout_s
    deep_blue_since = None
    deep_blue_deadline = None
    last_rgb = None
    last_debug_at = 0.0
    while True:
        loop_now = time.time()
        active_deadline = max(
            base_deadline,
            deep_blue_deadline
            if deep_blue_deadline is not None
            else base_deadline,
        )
        if loop_now > active_deadline:
            break
        if stop_check and stop_check():
            return False
        rgb = mean_rgb_for_region_key(regions, probe_key)
        last_rgb = rgb
        if rgb and is_bag_ui_ready_orange_rgb(rgb):
            if emit_fn:
                r, g, b = rgb
                emit_fn(
                    f"✅ [{log_tag}] 背包UI就绪（清空精灵一 RGB=({r},{g},{b})）",
                    "SUCCESS",
                )
            bag_scan = scan_pet_bag_count(
                regions,
                emit_fn=emit_fn,
                stop_check=stop_check,
                log_tag=log_tag,
            )
            if bag_scan_callback:
                try:
                    bag_scan_callback(bag_scan)
                except Exception:
                    pass
            if not bag_scan.get("ok"):
                if emit_fn:
                    emit_fn(
                        f"⚠️ [{log_tag}] 背包六槽数量在 "
                        f"{float(BAG_COUNT_SCAN_TIMEOUT_SEC or 0):g}s 内未稳定，"
                        "本次开包判定失败",
                        "WARN",
                    )
                return False
            return True

        now = time.time()
        probe_color = classify_pet_bag_slot_rgb(rgb)
        if allow_empty_bag and probe_color == "deep_blue":
            if deep_blue_since is None:
                deep_blue_since = now
                # 空包的 10 秒必须从首次确认深蓝开始完整计算，不能与开包总超时共用起点。
                deep_blue_deadline = (
                    deep_blue_since
                    + empty_confirm_s
                    + max(0.02, float(poll_s))
                )
            deep_blue_elapsed = now - deep_blue_since
            if deep_blue_elapsed >= empty_confirm_s:
                empty_scan = {
                    "ok": True,
                    "count": 0,
                    "colors": ("deep_blue",) * 6,
                    "data": {
                        1: {
                            "key": probe_key,
                            "rgb": rgb,
                            "color": "deep_blue",
                        }
                    },
                    "reason": "",
                }
                if bag_scan_callback:
                    try:
                        bag_scan_callback(empty_scan)
                    except Exception:
                        pass
                if emit_fn:
                    emit_fn(
                        f"✅ [{log_tag}] 精灵一探针连续深蓝 "
                        f"{empty_confirm_s:g}s，确认背包为0只精灵",
                        "SUCCESS",
                    )
                return True
        else:
            deep_blue_since = None
            deep_blue_deadline = None

        if emit_fn and now - last_debug_at >= 0.5:
            rgb_text = "None" if rgb is None else f"({rgb[0]},{rgb[1]},{rgb[2]})"
            if allow_empty_bag and deep_blue_since is not None:
                elapsed = max(0.0, now - deep_blue_since)
                emit_fn(
                    f"🔎 [{log_tag}] 精灵一为深蓝空槽候选，"
                    f"持续={elapsed:.1f}/{empty_confirm_s:g}s，RGB={rgb_text}",
                    "DEBUG",
                )
            else:
                emit_fn(
                    f"🔎 [{log_tag}] 清空精灵一探针未就绪，RGB={rgb_text}",
                    "DEBUG",
                )
            last_debug_at = now
        time.sleep(poll_s)
    if emit_fn:
        last_rgb_text = (
            "None"
            if last_rgb is None
            else f"({last_rgb[0]},{last_rgb[1]},{last_rgb[2]})"
        )
        emit_fn(
            f"⚠️ [{log_tag}] 等待背包UI就绪超时({effective_timeout_s:g}s)，"
            f"最后清空精灵一 RGB={last_rgb_text}",
            "WARN",
        )
    return False


window_manager = WindowManager()
