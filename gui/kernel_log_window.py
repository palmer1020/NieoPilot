# gui/kernel_log_window.py
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QTextEdit, QPushButton, QHBoxLayout


class KernelLogWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nieo Pilot - 内核日志")
        self.resize(900, 600)

        layout = QVBoxLayout()

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setStyleSheet(
            "background-color:#111;"
            "color:#C8FFC8;"
            "font-family:Consolas;"
            "font-size:12px;"
        )

        btn_row = QHBoxLayout()
        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self.log_box.clear)
        btn_row.addStretch()
        btn_row.addWidget(btn_clear)

        layout.addWidget(self.log_box)
        layout.addLayout(btn_row)
        self.setLayout(layout)

    def append_log(self, text: str):
        self.log_box.append(text)


