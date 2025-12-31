# tools/script_recorder.py
import os
import sys
import time
import json
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


class ScriptRecorder:
    def __init__(self, filename: str):
        self.filename = filename
        self.steps = []
        self.last_action_time = None
        self.is_recording = True

        print("🔍 正在锁定游戏窗口...")
        # 录制器里保持原逻辑：没有窗口就尝试启动/连接
        if not window_manager.launch_game():
            print("❌ 无法找到或启动游戏窗口！")
            self.is_recording = False
            return

        print("✅ 窗口已锁定，准备就绪。")
        print(f"🛡️ 有效区域限制: {GAME_LOGIC_W} x {GAME_LOGIC_H}")

    def on_click(self, x, y, button, pressed):
        if (not self.is_recording) or (not pressed):
            return

        # 左键录制
        if button == mouse.Button.left:
            res = window_manager.screen_to_game(x, y)
            if not res:
                print("❌ 点击无效：无法定位窗口坐标（viewport=None）")
                return

            gx, gy = res

            # ✅ 给一点容错：避免边缘浮点误差导致无故丢点
            # （DPI 修复后一般不会需要，但保留更稳）
            if gx < -5 or gx > GAME_LOGIC_W + 5 or gy < -5 or gy > GAME_LOGIC_H + 5:
                print(f"🚫 [无效点击] 落在屏幕之外: ({int(gx)}, {int(gy)}) -> 忽略")
                return

            gx = int(round(gx))
            gy = int(round(gy))

            # 最终硬边界
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
                "desc": f"Step {len(self.steps) + 1}"
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
        if key == keyboard.Key.esc:
            print("\n⏹ 停止录制...")
            self.save()
            self.is_recording = False
            return False

    def save(self):
        if not self.steps:
            print("❌ 未录制任何有效动作，放弃保存。")
            return

        safe_filename = self.filename
        if not safe_filename.endswith(".json"):
            safe_filename += ".json"

        full_path = os.path.join(SCRIPT_DIR, safe_filename)

        data = {
            "name": self.filename,
            "create_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "steps": self.steps
        }

        with open(full_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

        print("\n" + "=" * 40)
        print(f"💾 脚本已保存至: {full_path}")
        print(f"📊 有效步骤数: {len(self.steps)}")
        print("=" * 40)

    def start(self):
        if not self.is_recording:
            return

        print("\n" + "=" * 40)
        print(f"🎬 正在录制: 【{self.filename}】")
        print("-" * 40)
        print("🖱  左键 = 录制点击")
        print("↩  右键 = 撤销上一步")
        print("⏹  ESC  = 保存并退出")
        print("=" * 40 + "\n")

        with mouse.Listener(on_click=self.on_click) as m_listener:
            with keyboard.Listener(on_press=self.on_press) as k_listener:
                k_listener.join()


def main():
    os.system("cls" if os.name == "nt" else "clear")
    print("🎥 NieoPilot 脚本录制器 (带边界保护)")
    print("-" * 30)
    print("💡 提示：输入空名字并回车可直接取消。")

    try:
        name = input("请输入脚本名字 (例如 daily_task): ").strip()
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

