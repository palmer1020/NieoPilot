# tools/script_recorder.py
import os
import re
import sys
import time
import json
import threading
from pynput import mouse, keyboard

# =========================================================
# ✅ 关键修复：DPI Awareness（解决笔记本缩放坐标不一致）
# 必须尽早调用：在任何 win32 坐标/截图逻辑大量使用之前
# =========================================================
def _set_dpi_awareness():
    try:
        import ctypes
        # Per-monitor DPI aware (Windows 8.1+)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes
            # System DPI aware (fallback)
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_set_dpi_awareness()

# =========================================================
# 路径与环境配置
# =========================================================
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from core.utils import window_manager
from config import GAME_LOGIC_W, GAME_LOGIC_H

# 脚本保存目录
SCRIPT_FOLDER_NAME = "fix_script"
SCRIPT_DIR = os.path.join(project_root, SCRIPT_FOLDER_NAME)
os.makedirs(SCRIPT_DIR, exist_ok=True)

# 保存并退出的热键（ESC 常被游戏吞掉，优先用 F10 / F12）
SAVE_EXIT_KEYS = frozenset(
    {
        keyboard.Key.esc,
        keyboard.Key.f10,
        keyboard.Key.f12,
    }
)


def _configure_console_utf8() -> None:
    """Windows 控制台 UTF-8，避免中文脚本名乱码导致保存到错误文件名。"""
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


def _sanitize_script_filename(name: str) -> str:
    """去掉 .json 后缀与 Windows 非法字符。"""
    name = (name or "").strip()
    if name.lower().endswith(".json"):
        name = name[:-5]
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name.strip()


class ScriptRecorder:
    def __init__(self, filename: str):
        self.filename = _sanitize_script_filename(filename)
        self.steps = []
        self.last_action_time = None
        self.is_recording = True
        self._saved = False
        self._save_lock = threading.RLock()
        self._console_handler = None

        if not self.filename:
            print("❌ 脚本名字无效（为空或仅含非法字符）")
            self.is_recording = False
            return

        self._install_console_close_handler()

        print("🔍 正在锁定游戏窗口...")
        if not window_manager.launch_game():
            print("❌ 无法找到或启动游戏窗口！请先在 Dashboard 启动游戏。")
            self.is_recording = False
            return

        vp = window_manager.get_current_viewport()
        if vp:
            vx, vy, vw, vh = vp
            print(f"📐 当前视口: x={vx:.0f}, y={vy:.0f}, w={vw:.0f}, h={vh:.0f}")
        else:
            print("⚠ 未能获取视口；若左键一直无效，请先在 Dashboard 点【校准屏幕】。")

        print("✅ 窗口已锁定，准备就绪。")
        print(f"🛡️ 有效区域限制: {GAME_LOGIC_W} x {GAME_LOGIC_H}")
        print(f"📁 保存目录: {SCRIPT_DIR}")

    def on_click(self, x, y, button, pressed):
        if (not self.is_recording) or (not pressed):
            return

        # 左键录制
        if button == mouse.Button.left:
            res = window_manager.screen_to_game(x, y)
            if not res:
                print(
                    "❌ 点击无效：无法定位游戏坐标。"
                    "请确认点击在游戏画面内，且已在 Dashboard【校准屏幕】。"
                )
                return

            gx, gy = res

            if gx < -5 or gx > GAME_LOGIC_W + 5 or gy < -5 or gy > GAME_LOGIC_H + 5:
                print(f"🚫 [无效点击] 落在屏幕之外: ({int(gx)}, {int(gy)}) -> 忽略")
                return

            gx = int(round(gx))
            gy = int(round(gy))

            if gx < 0 or gx > GAME_LOGIC_W or gy < 0 or gy > GAME_LOGIC_H:
                print(f"🚫 [无效点击] 超出 {GAME_LOGIC_W}x{GAME_LOGIC_H}: ({gx}, {gy}) -> 忽略")
                return

            now = time.time()
            if self.last_action_time is None:
                delay = 1.0
            else:
                delay = max(now - self.last_action_time, 0.1)

            self.last_action_time = now

            step = {
                "action": "click",
                "x": gx,
                "y": gy,
                "delay": round(delay, 2),
                "desc": f"Step {len(self.steps) + 1}",
            }
            self.steps.append(step)
            print(f"✅ [步骤 {len(self.steps)}] 捕获: ({gx}, {gy}) | 延迟 {delay:.2f}s")

        # 右键撤销
        elif button == mouse.Button.right:
            if self.steps:
                removed = self.steps.pop()
                self.last_action_time = time.time()
                print(f"↩ 撤销步骤: {removed.get('desc', '')}")
            else:
                print("⚠ 列表为空，无法撤销")

    def on_press(self, key):
        if key not in SAVE_EXIT_KEYS:
            return
        key_name = getattr(key, "name", str(key))
        print(f"\n⏹ 收到 {key_name.upper()}，准备保存并退出...")
        self._finish(save=True)
        return False

    def _finish(self, save: bool) -> None:
        self.is_recording = False
        if save:
            self.save()

    def _install_console_close_handler(self) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes

            close_events = {2, 5, 6}  # CTRL_CLOSE_EVENT / LOGOFF / SHUTDOWN
            handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

            def handler(ctrl_type):
                if ctrl_type not in close_events:
                    return False
                if self.is_recording and not self._saved:
                    print("\n⏹ 控制台正在关闭，尝试保存录制内容...")
                    self._finish(save=True)
                    time.sleep(0.2)
                return True

            self._console_handler = handler_type(handler)
            ctypes.windll.kernel32.SetConsoleCtrlHandler(self._console_handler, True)
        except Exception as e:
            print(f"⚠️ 控制台关闭保存保护启用失败: {e}")

    def save(self) -> bool:
        with self._save_lock:
            if self._saved:
                return True

            if not self.steps:
                print("❌ 未录制任何有效步骤，无法保存。")
                print("   常见原因：")
                print("   1) 左键点在了游戏画面外（控制台/桌面）")
                print("   2) 未先在 Dashboard【校准屏幕】或游戏未启动")
                print("   3) 按了保存键但控制台里没有出现「✅ [步骤 N] 捕获」")
                print(f"   请重新录制；保存路径应为: {os.path.join(SCRIPT_DIR, self.filename + '.json')}")
                return False

            safe_filename = self.filename + ".json"
            full_path = os.path.join(SCRIPT_DIR, safe_filename)

            data = {
                "name": self.filename,
                "create_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "steps": self.steps,
            }

            try:
                if os.path.exists(full_path):
                    print(f"ℹ️ 将覆盖已有文件: {full_path}")
                with open(full_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
            except OSError as e:
                print(f"❌ 写入失败: {e}")
                print(f"   目标路径: {full_path}")
                return False

            self._saved = True
            print("\n" + "=" * 40)
            print(f"💾 脚本已保存至: {full_path}")
            print(f"📊 有效步骤数: {len(self.steps)}")
            print("=" * 40)
            return True

    def start(self):
        if not self.is_recording:
            return

        print("\n" + "=" * 40)
        print(f"🎬 正在录制: 【{self.filename}】")
        print("-" * 40)
        print("🖱  左键 = 录制点击（须点在【游戏画面】内）")
        print("↩  右键 = 撤销上一步")
        print("⏹  F10 / F12 / ESC = 保存并退出")
        print("💡 若 ESC 无反应（被游戏拦截），请改按 F10 或 F12")
        print("=" * 40 + "\n")

        try:
            with mouse.Listener(on_click=self.on_click) as m_listener:
                with keyboard.Listener(on_press=self.on_press) as k_listener:
                    k_listener.join()
        except KeyboardInterrupt:
            print("\n⏹ Ctrl+C 中断，尝试保存...")
            self._finish(save=True)


def main():
    _configure_console_utf8()
    os.system("cls" if os.name == "nt" else "clear")
    print("🎥 NieoPilot 脚本录制器 (带边界保护)")
    print("-" * 30)
    print(f"📁 脚本保存目录: {SCRIPT_DIR}")
    print("💡 提示：输入空名字并回车可直接取消。")
    print("💡 也可命令行传入名字: python tools/script_recorder.py 孵化")

    if len(sys.argv) > 1:
        name = " ".join(sys.argv[1:]).strip()
    else:
        try:
            name = input("请输入脚本名字 (例如 孵化): ").strip()
        except KeyboardInterrupt:
            print("\n🚫 检测到中断信号，已取消。")
            return

    if not name:
        print("🚫 已取消录制。")
        return

    recorder = ScriptRecorder(name)
    recorder.start()


if __name__ == "__main__":
    main()
