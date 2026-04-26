# core/utils.py
import os
import time
import subprocess
import threading
import math

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
        # 未扫边时 get_current_viewport 只提示一次（不自动扫边，依赖界面手动校准）
        self._viewport_zero_pad_logged = False

        # 只有通过本程序 launch_game() 启动时才会有
        self._proc = None
        self._log_thread_started = False
        # launch_game 最近一次失败原因（供界面展示，避免误报「仅路径错误」）
        self.last_launch_error = ""

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
            logger.info("窗口已存在，正在激活...")
            self.maximize_window()
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
                    logger.info("✅ 捕获窗口，正在最大化...")
                    self.maximize_window()
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
        屏幕校准/可视化调试（不扫边）：
        1) 整段客户区 + FIXED_RATIO(12:7) 由 get_current_viewport 推算
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

        # 不调用 scan_boundaries；映射仅用整段 client 与 12:7
        self.content_padding = None

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
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
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
            logger.info(f"📷 已保存白框标出 12:7 内容区: {path_framed}")
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


window_manager = WindowManager()


