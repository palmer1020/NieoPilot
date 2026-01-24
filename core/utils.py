# core/utils.py
import os
import time
import subprocess
import threading
import math

import win32gui
import win32api
import win32con
from PIL import ImageGrab

from config import WINDOW_TITLE, GAME_LOGIC_W, GAME_LOGIC_H, GAME_PATH, FIXED_RATIO
from core.logger import logger, emit_kernel_log


class WindowManager:
    def __init__(self):
        self.hwnd = 0
        self.content_padding = None

        # 只有通过本程序 launch_game() 启动时才会有
        self._proc = None
        self._log_thread_started = False

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
        if self.find_window():
            logger.info("窗口已存在，正在激活...")
            self.maximize_window()
            self.scan_boundaries()
            return True

        if not os.path.exists(GAME_PATH):
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

            # 等窗口出现
            for _ in range(40):
                if self.find_window():
                    logger.info("✅ 捕获窗口，正在最大化...")
                    self.maximize_window()
                    self.scan_boundaries()
                    return True
                time.sleep(0.5)

            logger.error("❌ 启动超时：未捕获到窗口")
            return False

        except Exception as e:
            logger.error(f"启动异常: {e}")
            return False

    def maximize_window(self):
        if not self.hwnd:
            return
        win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
        win32gui.ShowWindow(self.hwnd, win32con.SW_MAXIMIZE)
        try:
            win32gui.SetForegroundWindow(self.hwnd)
        except Exception:
            pass
        time.sleep(0.6)

    # ===============================
    # Calibration / Debug
    # ===============================
    def visual_debug(self) -> bool:
        """
        校准/可视化调试：
        1) 强制重扫边界
        2) 打印 viewport
        3) 鼠标依次指向几个关键点（左上/右上/右下/左下/中心）
        """
        if not self.find_window():
            logger.error("❌ 校准失败：未找到游戏窗口")
            return False

        self.content_padding = None
        self.scan_boundaries()

        vp = self.get_current_viewport()
        if not vp:
            logger.error("❌ 校准失败：无法获取 viewport")
            return False

        vx, vy, vw, vh = vp
        logger.info(f"👀 调试 viewport: x={vx:.1f}, y={vy:.1f}, w={vw:.1f}, h={vh:.1f}")

        points = [
            ("左上", 0, 0),
            ("右上", GAME_LOGIC_W, 0),
            ("右下", GAME_LOGIC_W, GAME_LOGIC_H),
            ("左下", 0, GAME_LOGIC_H),
            ("中心", GAME_LOGIC_W / 2, GAME_LOGIC_H / 2),
        ]

        logger.info("⏳ 1秒后开始演示鼠标定位...")
        time.sleep(1.0)

        for name, gx, gy in points:
            pos = self.game_to_screen(gx, gy)
            if not pos:
                continue
            sx, sy = pos
            win32api.SetCursorPos((sx, sy))
            logger.info(f"👉 {name} ({gx:.0f},{gy:.0f}) -> ({sx},{sy})")
            time.sleep(0.8)

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

    def scan_boundaries(self):
        """扫描游戏内容区 padding（会写入 self.content_padding）"""
        if not self.hwnd:
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
            px = img.load()

            bg = px[0, 0]

            # 扫四边 padding
            left_pad = 0
            for x in range(w):
                if not self._is_bg_color(px[x, h // 2], bg):
                    left_pad = x
                    break

            right_pad = 0
            for x in range(w - 1, -1, -1):
                if not self._is_bg_color(px[x, h // 2], bg):
                    right_pad = w - 1 - x
                    break

            top_pad = 0
            for y in range(h):
                if not self._is_bg_color(px[w // 2, y], bg):
                    top_pad = y
                    break

            bottom_pad = 0
            for y in range(h - 1, -1, -1):
                if not self._is_bg_color(px[w // 2, y], bg):
                    bottom_pad = h - 1 - y
                    break

            self.content_padding = (left_pad, top_pad, right_pad, bottom_pad)
            logger.info(f"📐 content_padding: L{left_pad}, T{top_pad}, R{right_pad}, B{bottom_pad}")

        except Exception as e:
            logger.error(f"scan_boundaries 异常: {e}")
            self.content_padding = None

    def get_current_viewport(self):
        """返回 (vx, vy, vw, vh) —— 游戏渲染区在屏幕坐标中的位置"""
        if not self.hwnd:
            if not self.find_window():
                return None

        l, t, r, b = win32gui.GetClientRect(self.hwnd)
        ox, oy = win32gui.ClientToScreen(self.hwnd, (0, 0))
        
        container_w = r - l
        container_h = b - t
        container_x = ox
        container_y = oy

        # 如果没扫过边界，先扫
        if self.content_padding is None:
            self.scan_boundaries()

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



window_manager = WindowManager()


