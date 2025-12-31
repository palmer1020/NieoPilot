def _set_dpi_awareness():
    try:
        import ctypes
        # Per-monitor DPI aware (Windows 8.1+)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes
            # System DPI aware fallback
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

_set_dpi_awareness()

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

    bot.start()  # 引擎常驻待命

    window.start_signal.connect(bot.set_tasks)
    window.stop_signal.connect(bot.stop)

    bot.log_signal.connect(window.log_message)
    bot.task_done_signal.connect(window._unlock_ui_stopped)

    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()


