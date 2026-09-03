def _set_dpi_awareness():
    try:
        import ctypes
        # Per-monitor DPI aware (Windows 8.1+)
        # 尝试使用更新的 API
        try:
            # Windows 10 1703+ 推荐方式
            ctypes.windll.user32.SetProcessDpiAwarenessContext(-2)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        except (AttributeError, OSError):
            # 回退到旧 API
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (Exception, OSError):
        try:
            import ctypes
            # System DPI aware fallback
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass  # 忽略 DPI 设置失败，不影响功能


def _disable_console_quick_edit():
    """Prevent an accidental CMD text selection from pausing the process."""
    try:
        import ctypes

        std_input_handle = -10
        enable_quick_edit_mode = 0x0040
        enable_extended_flags = 0x0080
        handle = ctypes.windll.kernel32.GetStdHandle(std_input_handle)
        mode = ctypes.c_uint()
        if handle and ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            safe_mode = (mode.value | enable_extended_flags) & ~enable_quick_edit_mode
            ctypes.windll.kernel32.SetConsoleMode(handle, safe_mode)
    except Exception:
        # GUI-only launches may not have a console.
        pass


_set_dpi_awareness()
_disable_console_quick_edit()

import os

import pytesseract
from config import TESSERACT_CMD, TESSDATA_PREFIX

pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
os.environ["TESSDATA_PREFIX"] = TESSDATA_PREFIX

import sys
from PyQt6.QtWidgets import QApplication
from gui.dashboard import Dashboard
from core.bot_thread import BotWorker

def main():
    app = QApplication(sys.argv)

    project_root = os.path.dirname(os.path.abspath(__file__))

    window = Dashboard()
    bot = BotWorker(project_root)
    
    # ✅ 将bot实例赋值给window，以便dashboard中的按钮可以使用
    window.bot = bot

    bot.start()  # 引擎常驻待命

    window.start_signal.connect(bot.set_tasks)
    window.stop_signal.connect(bot.stop)

    bot.log_signal.connect(window.log_message)
    bot.task_done_signal.connect(window._unlock_ui_stopped)

    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()

