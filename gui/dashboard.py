# gui/dashboard.py
import threading
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QComboBox, QLabel,
    QPushButton, QTextEdit, QGroupBox, QCheckBox, QLineEdit, QDateTimeEdit, QInputDialog
)
from PyQt6.QtCore import QDateTime
from PyQt6.QtCore import pyqtSignal, Qt, QMetaObject, Q_ARG, QDateTime

from core.utils import window_manager
from gui.kernel_log_window import KernelLogWindow
from core.logger import add_kernel_log_callback, remove_kernel_log_callback


class Dashboard(QWidget):
    start_signal = pyqtSignal(dict)
    stop_signal = pyqtSignal()

    kernel_log_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nieo Pilot - 智能控制台")
        self.resize(1000, 700)

        self.kernel_log_window = KernelLogWindow()
        self.kernel_log_signal.connect(self.kernel_log_window.append_log)

        self._kernel_cb = self.kernel_log_signal.emit
        add_kernel_log_callback(self._kernel_cb)

        self.init_ui()

    def closeEvent(self, event):
        try:
            remove_kernel_log_callback(self._kernel_cb)
        except Exception:
            pass
        super().closeEvent(event)

    def init_ui(self):
        main_layout = QHBoxLayout()
        control_panel = QVBoxLayout()

        # ---------- 基础控制 ----------
        base_group = QGroupBox("基础控制")
        base_layout = QHBoxLayout()

        self.btn_launch = QPushButton("🎮 启动游戏")
        self.btn_launch.clicked.connect(self.on_launch_game)

        self.btn_debug = QPushButton("🎯 校准屏幕")
        self.btn_debug.clicked.connect(self.on_debug_screen)
        
        # 🔧 校准测试：纯屏幕检测，不依赖日志
        self.btn_calibration_test = QPushButton("🔧 校准测试")
        self.btn_calibration_test.clicked.connect(self.on_run_calibration_test)
        
        # 📝 脚本录制器
        self.btn_script_recorder = QPushButton("📝 脚本录制器")
        self.btn_script_recorder.clicked.connect(self.on_open_script_recorder)
        
        # 📐 区域录制器
        self.btn_region_recorder = QPushButton("📐 区域录制器")
        self.btn_region_recorder.clicked.connect(self.on_open_region_recorder)
        
        # 🖼️ 模板录制器
        self.btn_template_recorder = QPushButton("🖼️ 模板录制器")
        self.btn_template_recorder.clicked.connect(self.on_open_template_recorder)

        base_layout.addWidget(self.btn_launch)
        base_layout.addWidget(self.btn_debug)
        base_layout.addWidget(self.btn_calibration_test)
        base_layout.addWidget(self.btn_script_recorder)
        base_layout.addWidget(self.btn_region_recorder)
        base_layout.addWidget(self.btn_template_recorder)
        base_group.setLayout(base_layout)
        control_panel.addWidget(base_group)

        # ---------- 日常 ----------
        daily_group = QGroupBox("📅 日常任务")
        daily_layout = QVBoxLayout()

        # 第一行：一键执行日常按钮和脚本执行
        row1 = QHBoxLayout()
        self.btn_run_daily = QPushButton("▶ 一键执行日常")
        self.btn_run_daily.clicked.connect(self.on_run_daily)

        # 脚本下拉框和执行按钮
        self.script_combo = QComboBox()
        self._load_fix_scripts()
        self.btn_run_script = QPushButton("▶ 执行脚本")
        self.btn_run_script.clicked.connect(self.on_run_script)

        self.chk_foreground = QCheckBox("前台运行（更稳定）")
        self.chk_foreground.setChecked(False)

        row1.addWidget(self.btn_run_daily, 2)
        row1.addWidget(self.script_combo, 2)
        row1.addWidget(self.btn_run_script, 1)
        row1.addWidget(self.chk_foreground, 1)
        daily_layout.addLayout(row1)

        # 第二行：扭蛋和勇者之塔独立按钮
        row2 = QHBoxLayout()
        
        # 扭蛋
        row2.addWidget(QLabel("扭蛋次数："))
        self.gacha_times_box = QLineEdit()
        self.gacha_times_box.setPlaceholderText("次数")
        self.gacha_times_box.setFixedWidth(80)
        self.btn_gacha = QPushButton("▶ 扭蛋")
        self.btn_gacha.clicked.connect(self.on_run_gacha)
        
        # 勇者之塔（固定10回合）
        self.btn_hero_tower = QPushButton("🗼 勇者之塔（10回合）")
        self.btn_hero_tower.clicked.connect(self.on_run_hero_tower)

        # 大乱斗x2
        self.btn_chaos_battle_x2 = QPushButton("⚔ 大乱斗x2")
        self.btn_chaos_battle_x2.clicked.connect(self.on_run_chaos_battle_x2)
        
        # 1v1x2
        self.btn_1v1_x2 = QPushButton("⚔ 1v1x2")
        self.btn_1v1_x2.clicked.connect(self.on_run_1v1_x2)

        row2.addWidget(self.gacha_times_box)
        row2.addWidget(self.btn_gacha)
        row2.addWidget(self.btn_hero_tower)
        row2.addWidget(self.btn_chaos_battle_x2)
        row2.addWidget(self.btn_1v1_x2)
        row2.addStretch()
        daily_layout.addLayout(row2)

        daily_group.setLayout(daily_layout)
        control_panel.addWidget(daily_group)
        
        # ---- Wild capture group ----
        wild_group = QGroupBox("野外捕捉")
        wild_layout = QVBoxLayout()

        row1 = QHBoxLayout()
        self.btn_mantis = QPushButton("螳螂模式（122）")
        self.btn_mantis.clicked.connect(self.start_mantis_capture)
        row1.addWidget(self.btn_mantis)
        wild_layout.addLayout(row1)

        row2 = QHBoxLayout()
        self.btn_rare = QPushButton("捕捉稀有精灵")
        self.btn_rare.clicked.connect(self.start_rare_capture)

        self.rare_combo = QComboBox()
        self.rare_combo.addItem("嘟咕噜（254 / map=323）", userData="dugulu")
        self.rare_combo.addItem("双塔（102/143 / map=320）", userData="shuangta")
        self.rare_combo.addItem("小豆芽（27 / map=11）", userData="xiaodouya")
        self.rare_combo.addItem("闪光皮皮（164 / map=10）", userData="flash_pipi")

        row2.addWidget(self.btn_rare)
        row2.addWidget(QLabel("目标："))
        row2.addWidget(self.rare_combo)
        wild_layout.addLayout(row2)

        # 智能追踪测试按钮（已隐藏，代码保留）
        # row3 = QHBoxLayout()
        # self.btn_smart_tracking_test = QPushButton("🧪 智能追踪测试（橙毛球）")
        # self.btn_smart_tracking_test.clicked.connect(self.start_smart_tracking_test)
        # row3.addWidget(self.btn_smart_tracking_test)
        # wild_layout.addLayout(row3)

        # 双塔刷新按钮（独立按钮）
        row3 = QHBoxLayout()
        self.btn_shuangta_refresh = QPushButton("🔄 双塔刷新（循环重连直到进入）")
        self.btn_shuangta_refresh.clicked.connect(self.on_shuangta_refresh)
        row3.addWidget(self.btn_shuangta_refresh)
        wild_layout.addLayout(row3)

        wild_group.setLayout(wild_layout)

        # ✅ 关键：把 group 加到外层 control_panel
        control_panel.addWidget(wild_group)

        # ---------- 尼尔家族测试 ---------- (已隐藏，代码保留)
        # nie_test_group = QGroupBox("🧪 尼尔家族测试（手动触发对战）")
        # nie_test_layout = QVBoxLayout()
        # 
        # row1 = QHBoxLayout()
        # self.btn_test_nie = QPushButton("🧪 测试尼尔（77/310，第二回合切精灵三）")
        # self.btn_test_nie.clicked.connect(self.start_test_nie)
        # self.btn_test_ni = QPushButton("🧪 测试尼奥（416，第二回合切精灵二）")
        # self.btn_test_ni.clicked.connect(self.start_test_ni)
        # row1.addWidget(self.btn_test_nie)
        # row1.addWidget(self.btn_test_ni)
        # nie_test_layout.addLayout(row1)
        # 
        # nie_test_group.setLayout(nie_test_layout)
        # control_panel.addWidget(nie_test_group)
        
        # ---------- 尼奥模式 ----------
        nieo_group = QGroupBox("🌊 尼奥模式（10/11地图循环）")
        nieo_layout = QVBoxLayout()
        
        row1 = QHBoxLayout()
        self.btn_nieo = QPushButton("🌊 启动尼奥模式")
        self.btn_nieo.clicked.connect(self.start_nieo_mode)
        row1.addWidget(self.btn_nieo)
        nieo_layout.addLayout(row1)
        
        # 不捕捉尼尔勾选框（仅用于尼奥模式）
        self.chk_skip_nie_77 = QCheckBox("不捕捉尼尔（77执行逃跑，310/416正常捕捉）")
        self.chk_skip_nie_77.setChecked(False)
        nieo_layout.addWidget(self.chk_skip_nie_77)
        
        # 测试勾选框
        row2 = QHBoxLayout()
        self.chk_test_nieo = QCheckBox("🧪 测试尼奥（普通精灵：第一回合切换，第二回合逃跑）")
        self.chk_test_nieo.setChecked(False)
        self.chk_test_nie = QCheckBox("🧪 测试尼尔（普通精灵：第一回合切换，第二回合逃跑）")
        self.chk_test_nie.setChecked(False)
        row2.addWidget(self.chk_test_nieo)
        row2.addWidget(self.chk_test_nie)
        nieo_layout.addLayout(row2)
        
        nieo_group.setLayout(nieo_layout)
        control_panel.addWidget(nieo_group)

        # ---------- 定时任务 ----------
        scheduled_group = QGroupBox("⏰ 定时任务（睡前自动捕捉）")
        scheduled_layout = QVBoxLayout()
        
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("执行时间："))
        self.datetime_scheduled = QDateTimeEdit()
        self.datetime_scheduled.setDateTime(QDateTime.currentDateTime())  # 默认当前时间
        self.datetime_scheduled.setCalendarPopup(True)
        self.datetime_scheduled.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        row1.addWidget(self.datetime_scheduled)
        scheduled_layout.addLayout(row1)
        
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("目标精灵："))
        self.scheduled_rare_combo = QComboBox()
        self.scheduled_rare_combo.addItem("嘟咕噜（254 / map=323）", userData="dugulu")
        self.scheduled_rare_combo.addItem("双塔（102/143 / map=320）", userData="shuangta")
        self.scheduled_rare_combo.addItem("小豆芽（27 / map=11）", userData="xiaodouya")
        self.scheduled_rare_combo.addItem("闪光皮皮（164 / map=10）", userData="flash_pipi")
        self.scheduled_rare_combo.addItem("螳螂（122 / map=11）", userData="mantis")
        self.scheduled_rare_combo.addItem("尼奥（21/22地图）", userData="nieo")
        row2.addWidget(self.scheduled_rare_combo)
        scheduled_layout.addLayout(row2)
        
        row3 = QHBoxLayout()
        self.btn_start_scheduled = QPushButton("▶ 启动定时任务")
        self.btn_start_scheduled.clicked.connect(self.start_scheduled_task)
        row3.addWidget(self.btn_start_scheduled)
        scheduled_layout.addLayout(row3)
        
        # 勾选框：睡前在挂机脚本
        self.chk_scheduled_from_hangup = QCheckBox("睡前在挂机脚本（先执行回到基地.json）")
        self.chk_scheduled_from_hangup.setChecked(False)
        scheduled_layout.addWidget(self.chk_scheduled_from_hangup)
        
        scheduled_group.setLayout(scheduled_layout)
        control_panel.addWidget(scheduled_group)

        # --- 训练室：练级 ---
        train_group = QGroupBox("🏫 训练室（练级）")
        train_layout = QHBoxLayout()

        self.btn_training_level = QPushButton("▶ 练级(单批次)")
        self.btn_training_level.clicked.connect(self.on_run_training_level)

        self.btn_training_to_100 = QPushButton("⬆ 升级直到100")
        self.btn_training_to_100.clicked.connect(self.on_run_training_to_100)

        self.training_battles_box = QLineEdit()
        self.training_battles_box.setPlaceholderText("战斗数(默认30,≤30)")
        self.training_battles_box.setFixedWidth(130)

        self.training_recover_every_box = QLineEdit()
        self.training_recover_every_box.setPlaceholderText("恢复间隔(默认5)")
        self.training_recover_every_box.setFixedWidth(120)

        self.training_stop_level_box = QLineEdit()
        self.training_stop_level_box.setPlaceholderText("调试：检测到>=等级停止(可选)")
        self.training_stop_level_box.setFixedWidth(190)

        train_layout.addWidget(self.btn_training_level)
        train_layout.addWidget(self.btn_training_to_100)
        train_layout.addWidget(self.training_battles_box)
        train_layout.addWidget(self.training_recover_every_box)
        train_layout.addWidget(self.training_stop_level_box)

        train_group.setLayout(train_layout)
        control_panel.addWidget(train_group)


        # ---------- 停止/内核日志/清空日志 ----------
        btn_row = QHBoxLayout()

        self.btn_stop = QPushButton("🛑 停止运行（Esc）")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.on_stop)

        self.btn_kernel_log = QPushButton("📜 内核日志")
        self.btn_kernel_log.clicked.connect(self.on_show_kernel_log)
        
        self.btn_clear_log = QPushButton("🗑️ 清空日志")
        self.btn_clear_log.clicked.connect(self.on_clear_log)

        btn_row.addWidget(self.btn_stop)
        btn_row.addWidget(self.btn_kernel_log)
        btn_row.addWidget(self.btn_clear_log)
        control_panel.addLayout(btn_row)

        control_panel.addStretch()

        # 右侧日志
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setStyleSheet(
            "background-color:#1e1e1e;"
            "color:#00ff00;"
            "font-family:Consolas;"
            "font-size:13px;"
        )

        main_layout.addLayout(control_panel, 2)
        main_layout.addWidget(self.log_box, 3)
        self.setLayout(main_layout)

    # ------------------ 基础：启动/校准 ------------------
    def on_launch_game(self):
        self.btn_launch.setEnabled(False)
        self.log_message("正在尝试启动 / 连接游戏窗口...", "SYSTEM")
        threading.Thread(target=self._launch_worker, daemon=True).start()

    def _launch_worker(self):
        success = window_manager.launch_game()
        self.log_message("✅ 游戏窗口已就绪" if success else "❌ 启动失败，请检查路径", "SUCCESS" if success else "ERROR")
        QMetaObject.invokeMethod(self.btn_launch, "setEnabled", Qt.ConnectionType.QueuedConnection, Q_ARG(bool, True))

    def on_debug_screen(self):
        self.btn_debug.setEnabled(False)
        self.log_message("🎯 开始校准屏幕...", "SYSTEM")
        threading.Thread(target=self._debug_worker, daemon=True).start()

    def _debug_worker(self):
        ok = window_manager.visual_debug()
        self.log_message("✅ 屏幕校准完成" if ok else "❌ 校准失败，未找到窗口", "SUCCESS" if ok else "ERROR")
        QMetaObject.invokeMethod(self.btn_debug, "setEnabled", Qt.ConnectionType.QueuedConnection, Q_ARG(bool, True))

    # ------------------ 任务触发 ------------------
    def _load_fix_scripts(self):
        """加载fix_script目录下的脚本到下拉框"""
        import os
        script_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fix_script")
        self.script_combo.clear()
        self.script_combo.addItem("-- 选择脚本 --", None)
        
        if os.path.exists(script_dir):
            try:
                files = [f for f in os.listdir(script_dir) if f.endswith('.json')]
                files.sort()
                for filename in files:
                    script_name = filename[:-5]  # 去掉.json后缀
                    self.script_combo.addItem(script_name, script_name)
            except Exception as e:
                self.log_message(f"❌ 加载脚本列表失败: {e}", "ERROR")

    def on_run_script(self):
        """执行选中的脚本"""
        script_name = self.script_combo.currentData()
        if not script_name:
            self.log_message("❌ 请先选择一个脚本", "WARN")
            return
        
        tasks = {
            "run_script": script_name,
            "use_foreground": self.chk_foreground.isChecked()
        }
        self.log_message(f"📜 启动脚本: {script_name}.json", "SYSTEM")
        self._lock_ui()
        self.start_signal.emit(tasks)

    def on_run_daily(self):
        tasks = {
            "daily_chain": True,
            "training_level": False,
            "use_foreground": self.chk_foreground.isChecked()
        }
        self.log_message("📅 启动日常任务", "SYSTEM")
        self._lock_ui()
        self.start_signal.emit(tasks)

    def on_run_gacha(self):
        """执行扭蛋"""
        try:
            times = int(self.gacha_times_box.text().strip())
            if times <= 0:
                raise ValueError()
        except:
            self.log_message("⚠ 扭蛋次数无效（请输入正整数）", "ERROR")
            return

        tasks = {
            "gacha": True,
            "gacha_times": times,
            "use_foreground": self.chk_foreground.isChecked()
        }
        self.log_message(f"🎲 启动扭蛋：{times} 次", "SYSTEM")
        self._lock_ui()
        self.start_signal.emit(tasks)

    def on_run_hero_tower(self):
        """执行勇者之塔10回合"""
        tasks = {
            "hero_tower": True,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self.log_message("🗼 启动勇者之塔：10回合", "SYSTEM")
        self._lock_ui()
        self.start_signal.emit(tasks)
    
    def on_run_chaos_battle_x2(self):
        """执行大乱斗x2"""
        tasks = {
            "chaos_battle_x2": True,
            "use_foreground": self.chk_foreground.isChecked()
        }
        self.log_message("⚔ 启动大乱斗x2", "SYSTEM")
        self._lock_ui()
        self.start_signal.emit(tasks)
    
    def on_run_1v1_x2(self):
        """执行1v1x2"""
        tasks = {
            "1v1_x2": True,
            "use_foreground": self.chk_foreground.isChecked()
        }
        self.log_message("⚔ 启动1v1x2", "SYSTEM")
        self._lock_ui()
        self.start_signal.emit(tasks)


    def on_open_script_recorder(self):
        """打开脚本录制器"""
        import subprocess
        import os
        import sys
        
        script_recorder_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "script_recorder.py")
        try:
            subprocess.Popen([sys.executable, script_recorder_path], creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0)
            self.log_message("📝 脚本录制器已启动", "SYSTEM")
        except Exception as e:
            self.log_message(f"❌ 启动脚本录制器失败: {e}", "ERROR")
    
    def on_open_region_recorder(self):
        """打开区域录制器"""
        import subprocess
        import os
        import sys
        
        region_recorder_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "region_recorder.py")
        try:
            subprocess.Popen([sys.executable, region_recorder_path], creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0)
            self.log_message("📐 区域录制器已启动", "SYSTEM")
        except Exception as e:
            self.log_message(f"❌ 启动区域录制器失败: {e}", "ERROR")
    
    def on_open_template_recorder(self):
        """打开模板录制器（区域模板捕获工具）"""
        import subprocess
        import os
        import sys
        
        # 使用对话框获取用户输入
        region_path, ok = QInputDialog.getText(
            self, 
            "模板录制器", 
            "输入 region 路径（支持 xx.xx.xx 格式，无需 .json）：",
            text=""
        )
        
        if not ok or not region_path.strip():
            return
        
        state_name, ok = QInputDialog.getText(
            self,
            "模板录制器",
            "输入状态名（例如：灰色 / 蓝色）：",
            text=""
        )
        
        if not ok or not state_name.strip():
            return
        
        template_recorder_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "region_templates.py")
        try:
            # 启动工具并传递参数
            subprocess.Popen(
                [sys.executable, template_recorder_path, region_path.strip(), state_name.strip()],
                creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
            )
            self.log_message(f"🖼️ 模板录制器已启动（region: {region_path.strip()}, state: {state_name.strip()}）", "SYSTEM")
        except Exception as e:
            self.log_message(f"❌ 启动模板录制器失败: {e}", "ERROR")

    def on_run_calibration_test(self):
        tasks = {
            "calibration_test": True,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self.log_message("🔧 启动校准测试（纯屏幕检测）：请确保已触发校准（大探针=2FA7EE AND 小探针=FFFFFF）", "SYSTEM")
        self._lock_ui()
        self.start_signal.emit(tasks)


    def on_run_dar_route_test(self):
        tasks = {
            "daily_chain": False,
            "gacha": False,
            "battle_defeat": False,
            "training_level": False,
            "training_until_level": False,

            "dar_route_test": True,
            "use_foreground": self.chk_foreground.isChecked()
        }
        self.log_message("启动螳螂捕捉(TEST)：请先切到克洛斯星二层", "SYSTEM")
        self._lock_ui()
        self.start_signal.emit(tasks)


    def _parse_training_params(self):
        # max_battles
        text = (self.training_battles_box.text() or "").strip()
        if text == "":
            max_battles = 30
        else:
            try:
                max_battles = int(text)
                if max_battles <= 0:
                    raise ValueError()
            except:
                self.log_message("⚠ 战斗数无效（请输入正整数或留空=30）", "ERROR")
                return None, None, None

        # recover_every
        rtext = (self.training_recover_every_box.text() or "").strip()
        if rtext == "":
            recover_every = 5
        else:
            try:
                recover_every = int(rtext)
                if recover_every <= 0:
                    raise ValueError()
                if recover_every > 30:
                    recover_every = 30
            except:
                self.log_message("⚠ 恢复间隔无效（请输入正整数，留空=5，最大30）", "ERROR")
                return None, None, None

        # ⭐ stop_level（调试）
        stext = (self.training_stop_level_box.text() or "").strip()
        if stext == "":
            stop_level = None
        else:
            try:
                stop_level = int(stext)
                if stop_level <= 0:
                    raise ValueError()
                if stop_level > 100:
                    stop_level = 100
            except:
                self.log_message("⚠ 停止等级无效（请输入 1~100，留空=关闭）", "ERROR")
                return None, None, None

        return max_battles, recover_every, stop_level

    def on_run_training_level(self):
        try:
            txt = (self.training_battles_box.text() or "").strip()
            max_battles = 30 if txt == "" else int(txt)
            if max_battles <= 0:
                raise ValueError()
            if max_battles > 30:
                max_battles = 30

            recover_txt = (self.training_recover_every_box.text() or "").strip()
            recover_every = 5 if recover_txt == "" else int(recover_txt)
            if recover_every <= 0:
                recover_every = 0
            if recover_every > 30:
                recover_every = 30

            stop_txt = (self.training_stop_level_box.text() or "").strip()
            debug_stop_level = int(stop_txt) if stop_txt != "" else None

        except:
            self.log_message("⚠ 练级输入无效（战斗数≤30，恢复间隔≤30，调试停级可空）", "ERROR")
            return

        tasks = {
            "daily_chain": False,
            "gacha": False,
            "battle_defeat": False,
            "training_level": True,
            "training_until_level": False,
            "max_battles": max_battles,
            "recover_every": recover_every,
            "debug_stop_level": debug_stop_level,
            "use_foreground": self.chk_foreground.isChecked()
        }
        self.log_message(f"🏫 启动训练室练级：{max_battles} 场", "SYSTEM")
        self._lock_ui()
        self.start_signal.emit(tasks)

    def _parse_int_box(self, box: QLineEdit, default: int, allow_empty=True) -> int:
        txt = (box.text() or "").strip()
        if txt == "":
            return default if allow_empty else None
        v = int(txt)
        return v

    def on_run_training_to_100(self):
        try:
            max_battles = self._parse_int_box(self.training_battles_box, 30, allow_empty=True)
            if max_battles <= 0:
                raise ValueError()
            if max_battles > 30:
                max_battles = 30

            recover_every = self._parse_int_box(self.training_recover_every_box, 5, allow_empty=True)
            if recover_every <= 0:
                recover_every = 0
            if recover_every > 30:
                recover_every = 30

            stop_txt = (self.training_stop_level_box.text() or "").strip()
            debug_stop_level = int(stop_txt) if stop_txt != "" else None

        except Exception:
            self.log_message("⚠ 输入无效：战斗数/恢复间隔/调试停级 需为整数", "ERROR")
            return

        tasks = {
            "daily_chain": False,
            "gacha": False,
            "battle_defeat": False,
            "training_level": False,
            "training_until_level": True,
            "battles_per_batch": max_battles,
            "recover_every": recover_every,
            "target_level": 100,
            "debug_stop_level": debug_stop_level,
            "use_foreground": self.chk_foreground.isChecked()
        }
        self.log_message("⬆ 启动：升级直到100（或调试停级）", "SYSTEM")
        self._lock_ui()
        self.start_signal.emit(tasks)


    def on_stop(self):
        self.stop_signal.emit()
        self.log_message("🛑 已请求停止当前任务（等待引擎收尾）", "SYSTEM")

    def on_show_kernel_log(self):
        self.kernel_log_window.show()
        self.kernel_log_window.raise_()

    # ------------------ UI状态 ------------------
    def _lock_ui_except_scheduled(self):
        """锁定UI但保持定时任务UI可用（允许定时任务与其他任务共存）"""
        self.btn_run_daily.setEnabled(False)
        self.btn_run_script.setEnabled(False)
        self.script_combo.setEnabled(False)
        self.btn_gacha.setEnabled(False)
        self.gacha_times_box.setEnabled(False)
        self.btn_hero_tower.setEnabled(False)
        self.btn_chaos_battle_x2.setEnabled(False)
        self.btn_1v1_x2.setEnabled(False)
        self.btn_training_level.setEnabled(False)
        self.btn_training_to_100.setEnabled(False)
        self.btn_stop.setEnabled(True)

        # ✅ 新增：锁住野外捕捉按钮
        if hasattr(self, "btn_mantis"):
            self.btn_mantis.setEnabled(False)
        if hasattr(self, "btn_rare"):
            self.btn_rare.setEnabled(False)
        if hasattr(self, "rare_combo"):
            self.rare_combo.setEnabled(False)
        if hasattr(self, "btn_smart_tracking_test"):
            self.btn_smart_tracking_test.setEnabled(False)

        # ✅ 旧按钮不存在就别动它
        if hasattr(self, "btn_dar_route_test"):
            self.btn_dar_route_test.setEnabled(False)

        if hasattr(self, "btn_calibration_test"):
            self.btn_calibration_test.setEnabled(False)
        
        # 录制器按钮保持可用（它们独立运行）
        # 定时任务UI保持可用（允许与其他任务共存）
        # 不锁定定时任务相关UI
    
    def _lock_ui(self):
        self.btn_run_daily.setEnabled(False)
        self.btn_run_script.setEnabled(False)
        self.script_combo.setEnabled(False)
        self.btn_gacha.setEnabled(False)
        self.gacha_times_box.setEnabled(False)
        self.btn_hero_tower.setEnabled(False)
        self.btn_chaos_battle_x2.setEnabled(False)
        self.btn_1v1_x2.setEnabled(False)
        self.btn_training_level.setEnabled(False)
        self.btn_training_to_100.setEnabled(False)
        self.btn_stop.setEnabled(True)

        # ✅ 新增：锁住野外捕捉按钮
        if hasattr(self, "btn_mantis"):
            self.btn_mantis.setEnabled(False)
        if hasattr(self, "btn_rare"):
            self.btn_rare.setEnabled(False)
        if hasattr(self, "rare_combo"):
            self.rare_combo.setEnabled(False)
        if hasattr(self, "btn_smart_tracking_test"):
            self.btn_smart_tracking_test.setEnabled(False)
        # 定时任务相关UI保持可用（允许与其他任务共存）
        # 不在这里锁定定时任务UI

        # ✅ 旧按钮不存在就别动它
        if hasattr(self, "btn_dar_route_test"):
            self.btn_dar_route_test.setEnabled(False)

        if hasattr(self, "btn_calibration_test"):
            self.btn_calibration_test.setEnabled(False)
        
        # 录制器按钮保持可用（它们独立运行）


    def _unlock_ui_stopped(self):
        self.btn_run_daily.setEnabled(True)
        self.btn_run_script.setEnabled(True)
        self.script_combo.setEnabled(True)
        self.btn_gacha.setEnabled(True)
        self.gacha_times_box.setEnabled(True)
        self.btn_hero_tower.setEnabled(True)
        self.btn_chaos_battle_x2.setEnabled(True)
        self.btn_1v1_x2.setEnabled(True)
        self.btn_training_level.setEnabled(True)
        self.btn_training_to_100.setEnabled(True)
        self.btn_stop.setEnabled(False)

        if hasattr(self, "btn_mantis"):
            self.btn_mantis.setEnabled(True)
        if hasattr(self, "btn_rare"):
            self.btn_rare.setEnabled(True)
        if hasattr(self, "rare_combo"):
            self.rare_combo.setEnabled(True)
        if hasattr(self, "btn_smart_tracking_test"):
            self.btn_smart_tracking_test.setEnabled(True)
        # 定时任务相关UI保持可用（允许与其他任务共存）
        # 不在这里解锁定时任务UI（因为从未锁定）

        if hasattr(self, "btn_dar_route_test"):
            self.btn_dar_route_test.setEnabled(True)

        if hasattr(self, "btn_calibration_test"):
            self.btn_calibration_test.setEnabled(True)
        
        # 录制器按钮保持可用（它们独立运行，不需要解锁）


    # ------------------ 日志 ------------------
    def log_message(self, text, level="INFO"):
        QMetaObject.invokeMethod(
            self.log_box, "append",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, f"[{level}] {text}")
        )
    
    def on_clear_log(self):
        """清空日志"""
        self.log_box.clear()
        self.log_message("日志已清空", "SYSTEM")

    
    def start_mantis_capture(self):
        tasks = {
            "wild_capture": True,
            "wild_capture_profile": "mantis",
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self.log_message("🪲 启动螳螂模式（122）", "SYSTEM")
        self._lock_ui()
        self.start_signal.emit(tasks)

    def start_rare_capture(self):
        profile = self.rare_combo.currentData() or "dugulu"
        tasks = {
            "wild_capture": True,
            "wild_capture_profile": profile,
            "use_foreground": self.chk_foreground.isChecked(),
            # skip_nie_77 已移除，仅在尼奥模式中使用
        }
        self.log_message(f"🧿 启动稀有精灵捕捉：{profile}", "SYSTEM")
        self._lock_ui()
        self.start_signal.emit(tasks)

    def start_smart_tracking_test(self):
        profile = self.rare_combo.currentData() or "dugulu"
        tasks = {
            "wild_capture": False,
            "smart_tracking_test": True,  # 新任务标识
            "wild_capture_profile": profile,
            "use_foreground": self.chk_foreground.isChecked(),
            "wild_battle_test_mode": False,  # 测试模式不使用声音触发
        }
        self.log_message(f"🧪 启动智能追踪测试：{profile}（请确保已进入目标地图）", "SYSTEM")
        self._lock_ui()
        self.start_signal.emit(tasks)
    
    def start_scheduled_task(self):
        """启动定时任务"""
        scheduled_datetime = self.datetime_scheduled.dateTime().toPyDateTime()
        profile = self.scheduled_rare_combo.currentData() or "dugulu"
        from_hangup = self.chk_scheduled_from_hangup.isChecked()
        
        tasks = {
            "scheduled_task": True,
            "scheduled_datetime": scheduled_datetime,
            "wild_capture_profile": profile,
            "use_foreground": self.chk_foreground.isChecked(),
            "scheduled_from_hangup": from_hangup,  # 是否从挂机脚本开始
        }
        
        formatted_time = scheduled_datetime.strftime("%Y-%m-%d %H:%M:%S")
        mode_text = "回到基地" if from_hangup else "登录"
        self.log_message(f"⏰ 定时任务已设置：{formatted_time}，目标：{profile}，模式：{mode_text}（如果已有定时任务将被覆盖）", "SYSTEM")
        # 定时任务不需要锁定UI，允许与其他任务共存
        # 只锁定其他UI，保持定时任务UI可用
        self._lock_ui_except_scheduled()
        self.start_signal.emit(tasks)
    
    def start_test_nie(self):
        """启动尼尔测试（77/310，第二回合切精灵三）"""
        tasks = {
            "nie_family_test": True,
            "nie_family_test_type": "nie",  # 尼尔模式（77/310）
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self.log_message("🧪 启动尼尔测试（77/310，第二回合切精灵三）", "SYSTEM")
        self.log_message("📝 请手动发起一次对战，程序将自动检测并执行对战流程", "INFO")
        self._lock_ui()
        self.start_signal.emit(tasks)
    
    def start_test_ni(self):
        """启动尼奥测试（416，第二回合切精灵二）"""
        tasks = {
            "nie_family_test": True,
            "nie_family_test_type": "ni",  # 尼奥模式（416）
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self.log_message("🧪 启动尼奥测试（416，第二回合切精灵二）", "SYSTEM")
        self.log_message("📝 请手动发起一次对战，程序将自动检测并执行对战流程", "INFO")
        self._lock_ui()
        self.start_signal.emit(tasks)
    
    def start_nieo_mode(self):
        """启动尼奥模式（10/11地图循环）"""
        tasks = {
            "nieo_mode": True,
            "use_foreground": self.chk_foreground.isChecked(),
            "test_nieo": self.chk_test_nieo.isChecked() if hasattr(self, "chk_test_nieo") else False,
            "test_nie": self.chk_test_nie.isChecked() if hasattr(self, "chk_test_nie") else False,
            "skip_nie_77": self.chk_skip_nie_77.isChecked() if hasattr(self, "chk_skip_nie_77") else False,
        }
        test_msg = ""
        if tasks.get("test_nieo"):
            test_msg += " [测试尼奥模式]"
        if tasks.get("test_nie"):
            test_msg += " [测试尼尔模式]"
        if tasks.get("skip_nie_77"):
            test_msg += " [不捕捉尼尔]"
        self.log_message(f"🌊 启动尼奥模式（10/11地图循环）{test_msg}", "SYSTEM")
        self._lock_ui()
        self.start_signal.emit(tasks)
    
    def on_shuangta_refresh(self):
        """执行双塔刷新流程"""
        self.btn_shuangta_refresh.setEnabled(False)
        self.log_message("🔄 开始执行双塔刷新流程...", "SYSTEM")
        threading.Thread(target=self._shuangta_refresh_worker, daemon=True).start()
    
    def _shuangta_refresh_worker(self):
        """双塔刷新流程的工作线程"""
        try:
            from core.dar_route_runner import DEFAULT_PROFILE_SHUANGTA
            
            # 检查是否有bot实例
            if not hasattr(self, "bot") or not self.bot:
                self.log_message("❌ bot实例不存在，无法执行刷新流程", "ERROR")
                QMetaObject.invokeMethod(self.btn_shuangta_refresh, "setEnabled", Qt.ConnectionType.QueuedConnection, Q_ARG(bool, True))
                return
            
            bot = self.bot
            
            # 检查是否有dar_route_runner
            if not hasattr(bot, "dar_route_runner") or not bot.dar_route_runner:
                self.log_message("❌ dar_route_runner不存在，无法执行刷新流程", "ERROR")
                QMetaObject.invokeMethod(self.btn_shuangta_refresh, "setEnabled", Qt.ConnectionType.QueuedConnection, Q_ARG(bool, True))
                return
            
            dar_route_runner = bot.dar_route_runner
            
            # 创建stop_event
            stop_event = threading.Event()
            
            # 执行刷新流程
            use_foreground = self.chk_foreground.isChecked()
            dar_route_runner._execute_refresh_flow_and_wait_login(DEFAULT_PROFILE_SHUANGTA, use_foreground, stop_event, retry_count=0, max_retries=10)
            
            self.log_message("✅ 双塔刷新流程执行完成", "SUCCESS")
        except Exception as e:
            self.log_message(f"❌ 双塔刷新流程执行异常: {e}", "ERROR")
            import traceback
            self.log_message(f"📋 异常详情: {traceback.format_exc()}", "ERROR")
        finally:
            QMetaObject.invokeMethod(self.btn_shuangta_refresh, "setEnabled", Qt.ConnectionType.QueuedConnection, Q_ARG(bool, True))

