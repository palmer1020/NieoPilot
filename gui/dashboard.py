# gui/dashboard.py
import os
import shutil
import threading
import time
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QComboBox, QLabel,
    QPushButton, QTextEdit, QGroupBox, QCheckBox, QLineEdit, QDateTimeEdit, QInputDialog,
    QMessageBox,
)
from PyQt6.QtGui import QDoubleValidator, QIntValidator
from PyQt6.QtCore import QDateTime
from PyQt6.QtCore import pyqtSignal, Qt, QMetaObject, Q_ARG, QDateTime

from core.utils import window_manager
from gui.kernel_log_window import KernelLogWindow
from core.logger import add_kernel_log_callback, remove_kernel_log_callback
from core.daily_runner import DEFAULT_HERO_TOWER_BATTLES


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
        
        # ✅ 自动启动轮换模式的标志（日常任务完成后自动启动）
        self._auto_start_rotation_after_daily = False

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
        base_outer = QVBoxLayout()
        base_row1 = QHBoxLayout()
        base_row2 = QHBoxLayout()

        self.btn_launch = QPushButton("🎮 启动游戏")
        self.btn_launch.clicked.connect(self.on_launch_game)

        self.btn_clear_game_temp = QPushButton("🗑 清除缓存")
        self.btn_clear_game_temp.setToolTip(
            "删除游戏安装目录（Nieo.exe 所在文件夹，如 NieoGame）下的 tmp 文件夹；"
            "路径来自 config.GAME_PATH，无 tmp 则仅提示。"
        )
        self.btn_clear_game_temp.clicked.connect(self.on_clear_game_temp_cache)

        self.btn_delete_screenshots = QPushButton("🖼️ 删除截图")
        self.btn_delete_screenshots.setToolTip(
            "删除本项目 screenshots 目录下所有常见图片文件（png/jpg/gif/webp/bmp）；"
            "保留目录结构与子文件夹。"
        )
        self.btn_delete_screenshots.clicked.connect(self.on_delete_screenshots)

        self.btn_refresh_trinity = QPushButton("🔄 刷新登录")
        self.btn_refresh_trinity.setToolTip(
            "执行「刷新登录直到检测到 map」：预刷新→等待Login.swf→开始/登录→普通确认↔刷新登录→探针变白→OCR选服3→map。"
        )
        self.btn_refresh_trinity.clicked.connect(self.on_refresh_trinity)

        self.btn_debug = QPushButton("🎯 校准屏幕")
        self.btn_debug.clicked.connect(self.on_debug_screen)
        
        # 📝 脚本录制器
        self.btn_script_recorder = QPushButton("📝 脚本录制器")
        self.btn_script_recorder.clicked.connect(self.on_open_script_recorder)
        
        # 📐 区域录制器
        self.btn_region_recorder = QPushButton("📐 区域录制器")
        self.btn_region_recorder.clicked.connect(self.on_open_region_recorder)
        # 🧭 设置子窗口区域录制器
        self.btn_settings_region_recorder = QPushButton("🧭 设置窗口录制器")
        self.btn_settings_region_recorder.clicked.connect(
            self.on_open_settings_region_recorder
        )
        
        # 🖼️ 模板录制器
        self.btn_template_recorder = QPushButton("🖼️ 模板录制器")
        self.btn_template_recorder.clicked.connect(self.on_open_template_recorder)

        self.btn_map_recorder = QPushButton("🗺️ 地图记录器")
        self.btn_map_recorder.setToolTip(
            "分批标注地图刷新点：Enter 截取 1200×700 → GUI 点选 → 红点叠加；"
            "ESC/F10 保存 fix_script/map<内核mapID>.json（截图仅内存，不落盘）"
        )
        self.btn_map_recorder.clicked.connect(self.on_open_map_recorder)

        self.btn_rare_mode_builder = QPushButton("🧬 稀有模式建立器")
        self.btn_rare_mode_builder.setToolTip(
            "向导建立自定义稀有模式：to 脚本 / 地图 A→B / 11 点路线 → assets/wild_modes"
        )
        self.btn_rare_mode_builder.clicked.connect(self.on_open_rare_mode_builder)

        self.btn_nieo_mode_builder = QPushButton("🌊 尼奥模式建立器")
        self.btn_nieo_mode_builder.setToolTip(
            "向导建立自定义三图尼奥模式：A/B/C 地图 / 传送 / 捕捉·战胜·跳过 → assets/nieo_modes"
        )
        self.btn_nieo_mode_builder.clicked.connect(self.on_open_nieo_mode_builder)

        base_row1.addWidget(self.btn_launch)
        base_row1.addWidget(self.btn_clear_game_temp)
        base_row1.addWidget(self.btn_delete_screenshots)
        base_row1.addWidget(self.btn_refresh_trinity)
        base_row1.addWidget(self.btn_debug)
        base_row1.addStretch()

        base_row2.addWidget(self.btn_script_recorder)
        base_row2.addWidget(self.btn_region_recorder)
        base_row2.addWidget(self.btn_settings_region_recorder)
        base_row2.addWidget(self.btn_template_recorder)
        base_row2.addWidget(self.btn_map_recorder)
        base_row2.addWidget(self.btn_rare_mode_builder)
        base_row2.addWidget(self.btn_nieo_mode_builder)
        base_row2.addStretch()

        base_outer.addLayout(base_row1)
        base_outer.addLayout(base_row2)
        base_group.setLayout(base_outer)
        control_panel.addWidget(base_group)

        # ---------- 资源模板（SWF → 微端） ----------
        swf_res_group = QGroupBox("资源模板（写入微端；覆盖前自动生成 OG 备份）")
        swf_res_outer = QVBoxLayout()
        swf_res_layout = QHBoxLayout()
        self.btn_swf_petstorage = QPushButton("📦 PetStorage")
        self.btn_swf_petstorage.setToolTip(
            "宠物仓库：assets/PetStorage.swf → 微端 PetStorage.swf（备份 PetStorage.og.swf）。\n"
            "nono\\super：nono_1~4 备份为 .og.swf 后用 nono_5 覆盖；action → action_og。"
        )
        self.btn_swf_petstorage.clicked.connect(
            lambda: self._on_swf_sync("PetStorage", "sync_petstorage")
        )
        self.btn_swf_pet254 = QPushButton("🐾 Pet SWF (254)")
        self.btn_swf_pet254.setToolTip(
            "用 254.swf 同步两套目录（均先 og 补齐缺失、再备份 OG、再 254 覆盖）：\n"
            "• pet\\swf ← swf\\254.swf（备份 pet\\swf_og）\n"
            "• groupFightResource\\pet ← swf\\254.swf（备份 groupFightResource\\pet_og）"
        )
        self.btn_swf_pet254.clicked.connect(
            lambda: self._on_swf_sync("Pet 254", "sync_pet_254")
        )
        self.btn_swf_fight_pet = QPushButton("⚔ Fight pet")
        self.btn_swf_fight_pet.setToolTip(
            "swf/fightpet.swf 覆盖 fightResource/pet/swf 下全部 .swf（备份至 swf_og）"
        )
        self.btn_swf_fight_pet.clicked.connect(
            lambda: self._on_swf_sync("Fight pet", "sync_fight_pet")
        )
        self.btn_swf_fight_skill = QPushButton("✨ Fight skill")
        self.btn_swf_fight_skill.setToolTip(
            "swf/fightskill.swf 覆盖 fightResource/skill/swf 下全部 .swf（备份至 swf_og）"
        )
        self.btn_swf_fight_skill.clicked.connect(
            lambda: self._on_swf_sync("Fight skill", "sync_fight_skill")
        )
        swf_res_layout.addWidget(self.btn_swf_petstorage)
        swf_res_layout.addWidget(self.btn_swf_pet254)
        swf_res_layout.addWidget(self.btn_swf_fight_pet)
        swf_res_layout.addWidget(self.btn_swf_fight_skill)

        swf_restore_layout = QHBoxLayout()
        self.btn_restore_petstorage = QPushButton("↩ PetStorage OG")
        self.btn_restore_petstorage.setToolTip(
            "还原 PetStorage.og.swf；nono\\super 从 nono_1~4.og.swf 写回；action_og → action。"
        )
        self.btn_restore_petstorage.clicked.connect(
            lambda: self._on_swf_restore("PetStorage OG", "restore_petstorage_from_og")
        )
        self.btn_restore_pet254 = QPushButton("↩ Pet SWF OG")
        self.btn_restore_pet254.setToolTip(
            "从 OG 写回两套 pet 目录：pet\\swf ← swf_og；groupFightResource\\pet ← pet_og"
        )
        self.btn_restore_pet254.clicked.connect(
            lambda: self._on_swf_restore("Pet SWF OG", "restore_pet_254_from_og")
        )
        self.btn_restore_fight_pet = QPushButton("↩ Fight pet OG")
        self.btn_restore_fight_pet.setToolTip(
            "以 swf_og 为准写回 fight pet\\swf（含已删序号）；不删 live 中无 OG 的多余文件。"
        )
        self.btn_restore_fight_pet.clicked.connect(
            lambda: self._on_swf_restore("Fight pet OG", "restore_fight_pet_from_og")
        )
        self.btn_restore_fight_skill = QPushButton("↩ Fight skill OG")
        self.btn_restore_fight_skill.setToolTip(
            "以 swf_og 为准写回 fight skill\\swf（含已删序号）；不删 live 中无 OG 的多余文件。"
        )
        self.btn_restore_fight_skill.clicked.connect(
            lambda: self._on_swf_restore("Fight skill OG", "restore_fight_skill_from_og")
        )
        swf_restore_layout.addWidget(self.btn_restore_petstorage)
        swf_restore_layout.addWidget(self.btn_restore_pet254)
        swf_restore_layout.addWidget(self.btn_restore_fight_pet)
        swf_restore_layout.addWidget(self.btn_restore_fight_skill)

        swf_res_outer.addLayout(swf_res_layout)
        swf_res_outer.addLayout(swf_restore_layout)
        swf_res_group.setLayout(swf_res_outer)
        control_panel.addWidget(swf_res_group)

        # ---------- 日常 ----------
        daily_group = QGroupBox("📅 日常任务")
        daily_layout = QVBoxLayout()

        # 第一行：一键日常 + 脚本/扭蛋（下拉默认扭蛋）+ 次数 + 执行
        row1 = QHBoxLayout()
        self.btn_pre_daily = QPushButton("📋 预选日常")
        self.btn_pre_daily.setToolTip(
            "登录/基地门控后跳过分子转化仪（不改 30 min 节流），清背包→仓库 Pick→"
            "开背包身边跟随→接受任务→每日签到。\n"
            "与「一键执行日常」互不替代；本流程结束后不会自动衔接完整日常。"
        )
        self.btn_pre_daily.clicked.connect(self.on_run_pre_daily)

        self.btn_run_daily = QPushButton("▶ 一键执行日常")
        self.btn_run_daily.clicked.connect(self.on_run_daily)

        self.chk_daily_hero_tower = QCheckBox(f"勇者之塔×{DEFAULT_HERO_TOWER_BATTLES}")
        self.chk_daily_hero_tower.setChecked(True)
        self.chk_daily_hero_tower.setToolTip(
            "勾选（默认）：跑日常脚本 1→2→3→4→5→6，再勇者之塔两场，再 1v1×2、大乱斗×2。\n"
            "不勾选：只跑 1→2→3→4→5（跳过 6），跳过勇者之塔，再 1v1×2。"
        )

        self.script_combo = QComboBox()
        self._load_fix_scripts()

        self.task_repeat_box = QLineEdit()
        self.task_repeat_box.setText("1")
        self.task_repeat_box.setPlaceholderText("次数")
        self.task_repeat_box.setFixedWidth(52)
        self.task_repeat_box.setValidator(QIntValidator(1, 99999, self))

        self.btn_run_task = QPushButton("▶ 执行")
        self.btn_run_task.setToolTip("默认：扭蛋；可选 fix_script 下脚本，次数为执行遍数")
        self.btn_run_task.clicked.connect(self.on_run_selected_task)

        self.chk_foreground = QCheckBox("前台运行（更稳定）")
        self.chk_foreground.setChecked(False)

        row1.addWidget(self.btn_pre_daily, 1)
        row1.addWidget(self.btn_run_daily, 2)
        row1.addWidget(self.chk_daily_hero_tower, 0)
        row1.addWidget(self.script_combo, 3)
        row1.addWidget(self.task_repeat_box, 0)
        row1.addWidget(self.btn_run_task, 1)
        row1.addWidget(self.chk_foreground, 1)
        daily_layout.addLayout(row1)

        self.chk_enable_molecule_converter = QCheckBox("执行分子转化仪")
        self.chk_enable_molecule_converter.setChecked(True)
        self.chk_enable_molecule_converter.setToolTip(
            "勾选（默认）：刷新登录、轮换、野外重连等走到基地门控后，按原逻辑执行分子转化仪（含节流与强制路径）。\n"
            "不勾选：任何模式均不执行分子转化仪，且不写入 30 min 节流时间戳。"
        )

        # 第二行：勇者之塔等（扭蛋已合并到上行）
        row2 = QHBoxLayout()
        
        # 勇者之塔（默认场数：DEFAULT_HERO_TOWER_BATTLES）
        self.btn_hero_tower = QPushButton(f"🗼 勇者之塔（{DEFAULT_HERO_TOWER_BATTLES}场）")
        self.btn_hero_tower.clicked.connect(self.on_run_hero_tower)

        # 大乱斗x2
        self.btn_chaos_battle_x2 = QPushButton("⚔ 大乱斗x2")
        self.btn_chaos_battle_x2.clicked.connect(self.on_run_chaos_battle_x2)
        
        # 1v1x2
        self.btn_1v1_x2 = QPushButton("⚔ 1v1x2")
        self.btn_1v1_x2.clicked.connect(self.on_run_1v1_x2)

        self._capsule_tier_label = QLabel("捕捉胶囊：")
        self._capsule_tier_label.setToolTip(
            "除螳螂对战敌方野生 122（首回合仍投无敌胶囊）外，捕捉投掷均按本项策略。"
            "默认超→超→特循环；亦可改为全程仅超级/仅高级/仅特级。"
        )
        self.combo_cap_tier = QComboBox()
        self.combo_cap_tier.addItem("超超特（默认循环）", "cycle")
        self.combo_cap_tier.addItem("仅超级", "super")
        self.combo_cap_tier.addItem("仅高级", "high")
        self.combo_cap_tier.addItem("仅特级", "special")
        self.combo_cap_tier.setMinimumWidth(140)
        self.combo_cap_tier.setToolTip(
            "默认：超级→超级→特级 循环投掷。\n"
            "备选：全程只投超级 / 高级 / 特级单档。\n"
            "敌方为野生螳螂（122）时首回合仍为无敌胶囊，之后才跟本策略。"
        )

        row2.addWidget(self.btn_hero_tower)
        row2.addWidget(self.btn_chaos_battle_x2)
        row2.addWidget(self.btn_1v1_x2)
        row2.addWidget(self.chk_enable_molecule_converter)
        row2.addWidget(self._capsule_tier_label)
        row2.addWidget(self.combo_cap_tier)
        row2.addStretch()
        daily_layout.addLayout(row2)

        daily_group.setLayout(daily_layout)
        control_panel.addWidget(daily_group)

        # ---- 刷经验 + 雷伊特训 + 特训循环（单行紧凑）----
        exp_group = QGroupBox("刷经验 / 特训")
        exp_layout = QHBoxLayout()
        self.btn_exp_minor_battle = QPushButton("小号对战")
        self.btn_exp_minor_battle.clicked.connect(self.on_run_exp_minor_battle)
        self.combo_training_battle_mode = QComboBox()
        self.combo_training_battle_mode.addItem("雷伊特训", userData="leiyi")
        self.combo_training_battle_mode.addItem("嘟嘟卡拉", userData="dudukala")
        self.combo_training_battle_mode.setToolTip(
            "雷伊特训：特训.1/2，4→2→1→3，次数由右侧输入框；黄探针胜利结束，否则白探针打点3后恢复，直至次数用完。\n"
            "嘟嘟卡拉：嘟嘟卡拉1/2入战；无限直至黄探针胜利或停止；每回合技能一；每次出手后单独累计退场 map+newNpc；白探针不设上限，不打特训.3。"
        )
        self.btn_leiyi_training = QPushButton("⚔对战")
        self.btn_leiyi_training.setToolTip(
            "与左侧模式一并启动对战特训。"
            "雷伊：遵循循环次数。"
            "嘟嘟卡拉：无限循环，仅黄色胜利探针结束（或停止）。"
        )
        self.btn_leiyi_training.clicked.connect(self.on_run_leiyi_training)
        self.leiyi_loop_box = QLineEdit()
        self.leiyi_loop_box.setPlaceholderText("循环(默认10)")
        self.leiyi_loop_box.setFixedWidth(90)
        self.combo_training_battle_mode.currentIndexChanged.connect(
            lambda _idx: self._update_training_battle_loop_box_for_mode()
        )
        self._update_training_battle_loop_box_for_mode()
        self.btn_teixun_loop = QPushButton("🔄特训循环")
        self.btn_teixun_loop.setToolTip("特训循环（黄=1AND1，白=等待后直接恢复）")
        self.btn_teixun_loop.clicked.connect(self.on_run_teixun_loop)
        exp_layout.addWidget(self.btn_exp_minor_battle)
        exp_layout.addWidget(self.combo_training_battle_mode)
        exp_layout.addWidget(self.btn_leiyi_training)
        exp_layout.addWidget(self.leiyi_loop_box)
        exp_layout.addWidget(self.btn_teixun_loop)
        exp_layout.addStretch()
        exp_group.setLayout(exp_layout)
        control_panel.addWidget(exp_group)
        
        # ---- Wild capture group ----
        wild_group = QGroupBox("野外捕捉")
        wild_layout = QVBoxLayout()

        row_wild = QHBoxLayout()
        self.btn_rare = QPushButton("捕捉稀有精灵")
        self.btn_rare.clicked.connect(self.start_rare_capture)

        self.rare_combo = QComboBox()
        self._load_wild_modes()

        # 默认会跑整套野外前置；仅当勾选时才跳过（不跑 to 前整套）
        self.chk_wild_skip_rotation_pre = QCheckBox(
            "跳过前置重连（勾选则不跑闪光/螳螂专用或嘟咕噜等统一前置）"
        )
        self.chk_wild_skip_rotation_pre.setChecked(False)
        self.chk_wild_skip_rotation_pre.setEnabled(False)
        self.chk_wild_skip_rotation_pre.setToolTip(
            "未勾选：启动时先跑整管前置（与模式匹配：闪光/螳螂专用，或嘟咕噜/双塔/小豆芽/眼球统一前置）。"
            "勾选：直接进入捕捉主循环。"
        )
        self._update_rare_dependent_checkbox_state()

        def on_rare_combo_changed():
            self._update_rare_dependent_checkbox_state()

        self.rare_combo.currentIndexChanged.connect(on_rare_combo_changed)

        row_wild.addWidget(self.btn_rare)
        row_wild.addWidget(QLabel("目标："))
        row_wild.addWidget(self.rare_combo, 1)
        row_wild.addWidget(self.chk_wild_skip_rotation_pre)
        wild_layout.addLayout(row_wild)

        # 智能追踪测试按钮（已隐藏，代码保留）
        # row3 = QHBoxLayout()
        # self.btn_smart_tracking_test = QPushButton("🧪 智能追踪测试（橙毛球）")
        # self.btn_smart_tracking_test.clicked.connect(self.start_smart_tracking_test)
        # row3.addWidget(self.btn_smart_tracking_test)
        # wild_layout.addLayout(row3)

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
        self.combo_nieo_sub = QComboBox()
        self._load_nieo_modes()
        self.combo_nieo_sub.setToolTip(
            "与左侧按钮一起使用：内置尼奥 / 纯净能量 / 自定义三图尼奥（重启后刷新自定义项）"
        )
        row1.addWidget(self.combo_nieo_sub)
        nieo_layout.addLayout(row1)

        row_nieo_opts = QHBoxLayout()
        self.chk_nieo_skip_pre_rotation = QCheckBox(
            "跳过前置重连（勾选则直接进入尼奥/纯净，不跑三宠前置）"
        )
        self.chk_nieo_skip_pre_rotation.setChecked(False)
        self.chk_nieo_skip_pre_rotation.setToolTip(
            "未勾选：启动时先跑与野外稀有同源的三宠 Pick + to尼奥/to纯净。"
            "勾选：跳过前置，直接开始尼奥或纯净能量循环。"
        )
        row_nieo_opts.addWidget(self.chk_nieo_skip_pre_rotation)

        self.chk_nieo_test_force_switch = QCheckBox(
            "测试模式（10图全切闪光艾菲亚 / 11图全切艾斯菲格）"
        )
        self.chk_nieo_test_force_switch.setChecked(False)
        self.chk_nieo_test_force_switch.setToolTip(
            "勾选后：尼奥模式所有入战均按尼尔家族流程处理。"
            "10号地图的所有遭遇切换到闪光艾菲亚（416路径），"
            "11号地图的所有遭遇切换到艾斯菲格（77路径）。"
        )
        row_nieo_opts.addWidget(self.chk_nieo_test_force_switch)
        row_nieo_opts.addStretch()
        nieo_layout.addLayout(row_nieo_opts)
        
        nieo_group.setLayout(nieo_layout)
        control_panel.addWidget(nieo_group)

        # ---------- 挂机对战模式 ----------
        afk_group = QGroupBox("🎮 挂机对战模式")
        afk_layout = QHBoxLayout()
        self.btn_afk_normal = QPushButton("普通")
        self.btn_afk_normal.clicked.connect(lambda: self.start_afk_battle_mode("normal"))
        afk_layout.addWidget(self.btn_afk_normal)
        self.btn_afk_defeat = QPushButton("击败")
        self.btn_afk_defeat.clicked.connect(lambda: self.start_afk_battle_mode("defeat"))
        afk_layout.addWidget(self.btn_afk_defeat)
        self.btn_afk_rare = QPushButton("稀有")
        self.btn_afk_rare.clicked.connect(lambda: self.start_afk_battle_mode("rare"))
        afk_layout.addWidget(self.btn_afk_rare)
        self.btn_afk_nieo = QPushButton("尼奥")
        self.btn_afk_nieo.clicked.connect(lambda: self.start_afk_battle_mode("nieo"))
        afk_layout.addWidget(self.btn_afk_nieo)
        afk_group.setLayout(afk_layout)
        control_panel.addWidget(afk_group)

        # ---------- 🏆 巅峰对战模式 ----------
        pinnacle_group = QGroupBox("🏆 巅峰对战模式")
        pinnacle_layout = QVBoxLayout()
        pinnacle_btn_row = QHBoxLayout()
        self.btn_pinnacle_rank = QPushButton("进入排位")
        self.btn_pinnacle_rank.clicked.connect(lambda: self.start_pinnacle_mode("rank"))
        pinnacle_btn_row.addWidget(self.btn_pinnacle_rank)
        self.btn_pinnacle_fun = QPushButton("进入娱乐")
        self.btn_pinnacle_fun.clicked.connect(lambda: self.start_pinnacle_mode("fun"))
        pinnacle_btn_row.addWidget(self.btn_pinnacle_fun)
        pinnacle_layout.addLayout(pinnacle_btn_row)
        self.chk_pinnacle_small_account_mode = QCheckBox(
            "小号：PetItem 后不点技能一，直接刷新下一轮"
        )
        self.chk_pinnacle_small_account_mode.setChecked(False)
        pinnacle_layout.addWidget(self.chk_pinnacle_small_account_mode)
        pinnacle_group.setLayout(pinnacle_layout)
        control_panel.addWidget(pinnacle_group)

        # ---------- 双塔尼奥轮换模式（已替换原定时任务）----------
        rotation_group = QGroupBox("🔄 尼奥·稀有轮换（自动切换｜时间与原先一致）")
        rotation_layout = QVBoxLayout()

        rot_top = QHBoxLayout()
        self.btn_start_rotation = QPushButton("▶ 启动轮换模式")
        self.btn_start_rotation.clicked.connect(self.start_rotation_mode)
        rot_top.addWidget(self.btn_start_rotation)

        rot_top.addWidget(QLabel("非尼奥稀有："))
        self.rotation_rare_combo = QComboBox()
        self.rotation_rare_combo.setMinimumWidth(160)
        self.rotation_rare_combo.setToolTip(
            "北京时间非尼奥时段内跑所选稀有；尼奥时段仍为尼奥模式。"
            "含内置六种 + 稀有模式建立器创建的自定义模式（重启后刷新列表）。"
        )
        self._load_rotation_rare_modes()
        rot_top.addWidget(self.rotation_rare_combo)

        self.chk_rotation_test_mode = QCheckBox("测试模式（固定间隔切换）")
        self.chk_rotation_test_mode.setChecked(False)
        rot_top.addWidget(self.chk_rotation_test_mode)

        rot_top.addStretch()
        rotation_layout.addLayout(rot_top)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("尼奥→双塔(分钟)："))
        self.rotation_interval_minutes_nieo_input = QLineEdit()
        self.rotation_interval_minutes_nieo_input.setText("60")
        self.rotation_interval_minutes_nieo_input.setFixedWidth(70)
        self.rotation_interval_minutes_nieo_input.setValidator(QIntValidator(1, 1440))
        interval_row.addWidget(self.rotation_interval_minutes_nieo_input)

        interval_row.addWidget(QLabel("双塔→尼奥(分钟)："))
        self.rotation_interval_minutes_shuangta_input = QLineEdit()
        self.rotation_interval_minutes_shuangta_input.setText("60")
        self.rotation_interval_minutes_shuangta_input.setFixedWidth(70)
        self.rotation_interval_minutes_shuangta_input.setValidator(QIntValidator(1, 1440))
        interval_row.addWidget(self.rotation_interval_minutes_shuangta_input)

        interval_row.addWidget(QLabel("硬线(秒)："))
        self.petswf_hard_limit_sec_input = QLineEdit()
        self.petswf_hard_limit_sec_input.setText("8")
        self.petswf_hard_limit_sec_input.setFixedWidth(70)
        self.petswf_hard_limit_sec_input.setValidator(QDoubleValidator(0.1, 60.0, 2))
        interval_row.addWidget(self.petswf_hard_limit_sec_input)
        interval_row.addStretch()
        rotation_layout.addLayout(interval_row)

        pick_flags_row = QHBoxLayout()
        lbl_pick_default = QLabel("默认 Pick（波克尔/艾菲德斯/机塔）；轮换三步仓库与战后跟宠均已内置")
        lbl_pick_default.setStyleSheet("color: gray; font-size: 11px;")
        self.chk_resist_drain_logic = QCheckBox("抗减命逻辑")
        self.chk_resist_drain_logic.setChecked(False)
        self.chk_resist_drain_logic.setToolTip(
            "出战机塔后的技能节奏：勾选为「两回合二技能→一技能」；不勾选为一技能二技能交替。"
        )
        pick_flags_row.addWidget(lbl_pick_default)
        pick_flags_row.addWidget(self.chk_resist_drain_logic)
        pick_flags_row.addStretch()
        rotation_layout.addLayout(pick_flags_row)

        self.chk_rotation_test_mode.stateChanged.connect(self._update_rotation_test_inputs_enabled)
        self._update_rotation_test_inputs_enabled()

        info_label = QLabel(
            "北京时间：尼奥时段约 19:55–00:00；其余时段为非尼奥稀有（见左侧下拉，默认双塔）"
        )
        info_label.setStyleSheet("color: gray; font-size: 10px;")
        rotation_layout.addWidget(info_label)

        rotation_group.setLayout(rotation_layout)
        control_panel.addWidget(rotation_group)
        
        # ---------- 原定时任务（已禁用）----------
        # scheduled_group = QGroupBox("⏰ 定时任务（睡前自动捕捉）")
        # scheduled_layout = QVBoxLayout()
        # ... (原代码已注释，保留以备参考)

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
        if success:
            self.log_message("✅ 游戏窗口已就绪", "SUCCESS")
        else:
            detail = getattr(window_manager, "last_launch_error", "") or ""
            msg = detail if detail else "启动失败（未知原因）"
            self.log_message(f"❌ {msg}", "ERROR")
        QMetaObject.invokeMethod(self.btn_launch, "setEnabled", Qt.ConnectionType.QueuedConnection, Q_ARG(bool, True))

    def on_clear_game_temp_cache(self):
        self.log_message("🗑 正在检查游戏目录下的 tmp 文件夹…", "SYSTEM")
        threading.Thread(target=self._clear_game_temp_worker, daemon=True).start()

    def _clear_game_temp_worker(self):
        try:
            from config import GAME_PATH
        except Exception as e:
            self.log_message(f"❌ 无法读取 config.GAME_PATH: {e}", "ERROR")
            return
        game_root = os.path.dirname(os.path.abspath(GAME_PATH))
        tmp_dir = os.path.join(game_root, "tmp")
        if not os.path.isdir(tmp_dir):
            self.log_message(f"ℹ 未找到 tmp，跳过删除：{tmp_dir}", "INFO")
            return
        try:
            shutil.rmtree(tmp_dir)
            self.log_message(f"✅ 已删除 tmp：{tmp_dir}", "SUCCESS")
        except OSError as e:
            self.log_message(f"❌ 删除 tmp 失败（若游戏占用请先关闭）：{tmp_dir} | {e}", "ERROR")

    def _project_root(self) -> str:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def on_delete_screenshots(self):
        ans = QMessageBox.question(
            self,
            "确认删除截图",
            "将删除项目 screenshots 目录下所有常见图片文件（png / jpg / gif / webp / bmp），"
            "子目录内也会清理。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self.log_message("🗑 正在删除 screenshots 下的图片…", "SYSTEM")
        threading.Thread(target=self._delete_screenshots_worker, daemon=True).start()

    def _delete_screenshots_worker(self):
        root = os.path.join(self._project_root(), "screenshots")
        exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
        if not os.path.isdir(root):
            self.log_message(f"ℹ 无 screenshots 目录，跳过：{root}", "INFO")
            return
        removed = 0
        errors = 0
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    _base, ext = os.path.splitext(name)
                    if ext.lower() not in exts:
                        continue
                    p = os.path.join(dirpath, name)
                    try:
                        os.remove(p)
                        removed += 1
                    except OSError:
                        errors += 1
        except OSError as e:
            self.log_message(f"❌ 遍历 screenshots 失败：{root} | {e}", "ERROR")
            return
        if errors:
            self.log_message(
                f"⚠ 已删除 {removed} 个图片文件，{errors} 个删除失败（可能被占用）：{root}",
                "WARN",
            )
        else:
            self.log_message(
                f"✅ 已删除 {removed} 个图片文件：{root}",
                "SUCCESS",
            )

    def on_refresh_trinity(self):
        self.btn_refresh_trinity.setEnabled(False)
        self.log_message("🔄 开始刷新登录：直到检测到 map…", "SYSTEM")
        threading.Thread(target=self._refresh_trinity_worker, daemon=True).start()

    def _refresh_trinity_worker(self):
        try:
            if not hasattr(self, "bot") or not self.bot:
                self.log_message("❌ bot 未就绪，无法执行刷新登录", "ERROR")
                return
            drr = getattr(self.bot, "dar_route_runner", None)
            if not drr:
                self.log_message("❌ dar_route_runner 不存在，无法执行刷新登录", "ERROR")
                return
            use_fg = self.chk_foreground.isChecked()
            stop_event = threading.Event()
            ok = drr.run_refresh_login_until_map(use_fg, stop_event)
            if ok:
                self.log_message("✅ 刷新登录完成（已检测到 map）", "SUCCESS")
            else:
                self.log_message("⚠️ 刷新登录失败（见上方日志）", "WARN")
        except Exception as e:
            self.log_message(f"❌ 刷新登录异常: {e}", "ERROR")
            import traceback
            self.log_message(f"📋 异常详情: {traceback.format_exc()}", "ERROR")
        finally:
            QMetaObject.invokeMethod(
                self.btn_refresh_trinity,
                "setEnabled",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(bool, True),
            )

    def _on_swf_sync(self, label: str, op_name: str):
        self.log_message(f"⏳ [{label}] 正在同步…", "SYSTEM")
        threading.Thread(
            target=self._swf_sync_worker,
            args=(label, op_name),
            daemon=True,
        ).start()

    def _swf_sync_worker(self, label: str, op_name: str):
        try:
            from core import swf_resource_ops

            fn = getattr(swf_resource_ops, op_name)
            ok, msg = fn()
            self.log_message(
                f"{'✅' if ok else '❌'} [{label}] {msg}",
                "SUCCESS" if ok else "ERROR",
            )
        except Exception as e:
            self.log_message(f"❌ [{label}] {e}", "ERROR")

    def _on_swf_restore(self, label: str, op_name: str):
        self.log_message(f"⏳ [{label}] 正在从 OG 还原…", "SYSTEM")
        threading.Thread(
            target=self._swf_restore_worker,
            args=(label, op_name),
            daemon=True,
        ).start()

    def _swf_restore_worker(self, label: str, op_name: str):
        try:
            from core import swf_resource_ops

            fn = getattr(swf_resource_ops, op_name)
            ok, msg = fn()
            self.log_message(
                f"{'✅' if ok else '❌'} [{label}] {msg}",
                "SUCCESS" if ok else "ERROR",
            )
        except Exception as e:
            self.log_message(f"❌ [{label}] {e}", "ERROR")

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
        """加载 fix_script 脚本：扭蛋 → 放生 → 金豆 → 孵化（固定显示）→ 其余按名字排序。"""
        import os
        script_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fix_script")
        self.script_combo.clear()
        self.script_combo.addItem("🎲 扭蛋", "__gacha__")
        self.script_combo.addItem("🐾 放生", "放生")

        # 扭蛋、放生之后的固定顺序（始终显示；其余脚本仍按文件名排序）
        _after_release = ("金豆", "孵化")
        _after_release_icons = {"金豆": "💰", "孵化": "🥚"}

        if os.path.exists(script_dir):
            try:
                for name in _after_release:
                    icon = _after_release_icons.get(name, "📜")
                    self.script_combo.addItem(f"{icon} {name}", name)

                files = [f for f in os.listdir(script_dir) if f.endswith(".json")]
                files.sort()
                skip = frozenset(("放生",) + _after_release)
                for filename in files:
                    script_name = filename[:-5]
                    if script_name in skip:
                        continue
                    self.script_combo.addItem(script_name, script_name)
            except Exception as e:
                self.log_message(f"❌ 加载脚本列表失败: {e}", "ERROR")
        self.script_combo.setCurrentIndex(0)

    def on_run_selected_task(self):
        """下拉：扭蛋（默认）或 fix 脚本；次数框：执行遍数（默认 1）"""
        raw = (self.task_repeat_box.text() or "").strip()
        if raw == "":
            times = 1
        else:
            try:
                times = int(raw)
                if times < 1:
                    raise ValueError()
            except ValueError:
                self.log_message("⚠ 次数无效（请输入 ≥1 的整数）", "ERROR")
                return

        use_fg = self.chk_foreground.isChecked()
        data = self.script_combo.currentData()

        if data == "__gacha__":
            tasks = {
                "gacha": True,
                "gacha_times": times,
                "use_foreground": use_fg,
            }
            self.log_message(f"🎲 启动扭蛋：{times} 次", "SYSTEM")
        else:
            if not data:
                self.log_message("❌ 请在下拉框中选择扭蛋或脚本", "WARN")
                return
            tasks = {
                "run_script": data,
                "run_repeat": times,
                "use_foreground": use_fg,
            }
            self.log_message(f"📜 启动脚本 {data}.json × {times}", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_daily(self):
        tasks = {
            "daily_chain": True,
            "training_level": False,
            "use_foreground": self.chk_foreground.isChecked(),
            "daily_include_hero_tower": self.chk_daily_hero_tower.isChecked(),
        }
        if tasks["daily_include_hero_tower"]:
            tail = "脚本1–6，勇者之塔→1v1×2"
        else:
            tail = "脚本1–5，跳过6与塔→1v1×2"
        self.log_message(f"📅 启动日常任务（{tail}）", "SYSTEM")
        # ✅ 设置自动启动轮换模式的标志
        self._auto_start_rotation_after_daily = True
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_pre_daily(self):
        tasks = {
            "pre_daily_mode": True,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self.log_message(
            "📋 启动预选日常（跳过分子转化仪；Pick + 身边跟随 + 接受任务 + 每日签到）",
            "SYSTEM",
        )
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_hero_tower(self):
        """执行勇者之塔（默认场次由引擎常量决定，当前为 2 场）"""
        tasks = {
            "hero_tower": True,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self.log_message(f"🗼 启动勇者之塔：{DEFAULT_HERO_TOWER_BATTLES}场", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)
    
    def on_run_chaos_battle_x2(self):
        """执行大乱斗x2"""
        tasks = {
            "chaos_battle_x2": True,
            "use_foreground": self.chk_foreground.isChecked()
        }
        self.log_message("⚔ 启动大乱斗x2", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)
    
    def on_run_1v1_x2(self):
        """执行1v1x2"""
        tasks = {
            "1v1_x2": True,
            "use_foreground": self.chk_foreground.isChecked()
        }
        self.log_message("⚔ 启动1v1x2", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_exp_minor_battle(self):
        """执行小号对战（刷经验）"""
        tasks = {
            "exp_minor_battle": True,
            "use_foreground": self.chk_foreground.isChecked()
        }
        self.log_message("📚 启动小号对战（刷经验）", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def on_open_script_recorder(self):
        """打开脚本录制器"""
        import subprocess
        import os
        import sys
        
        script_recorder_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "script_recorder.py")
        try:
            env = os.environ.copy()
            env.setdefault("PYTHONIOENCODING", "utf-8")
            subprocess.Popen(
                [sys.executable, script_recorder_path],
                creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
                env=env,
            )
            self.log_message("📝 脚本录制器已启动", "SYSTEM")
        except Exception as e:
            self.log_message(f"❌ 启动脚本录制器失败: {e}", "ERROR")
    
    def _fill_rare_select_combo(self, combo, *, default_key=None) -> None:
        from core.wild_mode_registry import list_rare_select_options

        prev = combo.currentData() if combo.count() else None
        combo.clear()
        for label, key in list_rare_select_options(self._project_root()):
            combo.addItem(label, userData=key)
        pick = prev or default_key
        if pick:
            idx = combo.findData(pick)
            if idx >= 0:
                combo.setCurrentIndex(idx)

    def _load_rotation_rare_modes(self):
        """非尼奥轮换下拉：内置稀有 + assets/wild_modes。"""
        if not hasattr(self, "rotation_rare_combo"):
            return
        try:
            self._fill_rare_select_combo(self.rotation_rare_combo, default_key="shuangta")
        except Exception as e:
            self.log_message(f"⚠ 加载轮换稀有模式失败: {e}", "WARN")

    def _load_wild_modes(self):
        """野外捕捉下拉：内置稀有 + assets/wild_modes。"""
        if not hasattr(self, "rare_combo"):
            return
        try:
            self._fill_rare_select_combo(self.rare_combo, default_key="flash_pipi")
        except Exception as e:
            self.log_message(f"⚠ 加载自定义稀有模式失败: {e}", "WARN")

    def _fill_nieo_select_combo(self, combo, *, default_key=None) -> None:
        from core.nieo_mode_registry import list_nieo_select_options

        prev = combo.currentData() if combo.count() else None
        combo.clear()
        combo.addItem("尼奥（map=10/11）", "nieo")
        combo.addItem("纯净能量(资源)（map=26/27）", "pure_energy")
        for label, key in list_nieo_select_options(self._project_root()):
            combo.addItem(label, userData=key)
        pick = prev or default_key or "nieo"
        idx = combo.findData(pick)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _load_nieo_modes(self):
        """尼奥下拉：内置 + assets/nieo_modes。"""
        if not hasattr(self, "combo_nieo_sub"):
            return
        try:
            self._fill_nieo_select_combo(self.combo_nieo_sub, default_key="nieo")
        except Exception as e:
            self.log_message(f"⚠ 加载自定义尼奥模式失败: {e}", "WARN")

    def on_open_nieo_mode_builder(self):
        """打开尼奥模式建立器"""
        import subprocess
        import os
        import sys

        builder_path = os.path.join(self._project_root(), "tools", "nieo_mode_builder.py")
        try:
            env = os.environ.copy()
            env.setdefault("PYTHONIOENCODING", "utf-8")
            subprocess.Popen(
                [sys.executable, builder_path],
                creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
                env=env,
            )
            self.log_message("🌊 尼奥模式建立器已启动", "SYSTEM")
        except Exception as e:
            self.log_message(f"❌ 启动尼奥模式建立器失败: {e}", "ERROR")

    def on_open_rare_mode_builder(self):
        """打开稀有精灵模式建立器"""
        import subprocess
        import os
        import sys

        builder_path = os.path.join(self._project_root(), "tools", "rare_mode_builder.py")
        try:
            env = os.environ.copy()
            env.setdefault("PYTHONIOENCODING", "utf-8")
            subprocess.Popen(
                [sys.executable, builder_path],
                creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
                env=env,
            )
            self.log_message("🧬 稀有模式建立器已启动", "SYSTEM")
        except Exception as e:
            self.log_message(f"❌ 启动稀有模式建立器失败: {e}", "ERROR")

    def on_open_map_recorder(self):
        """打开地图记录器（分批标注刷新点）"""
        import subprocess
        import os
        import sys

        recorder_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tools",
            "map_recorder.py",
        )
        try:
            env = os.environ.copy()
            env.setdefault("PYTHONIOENCODING", "utf-8")
            subprocess.Popen(
                [sys.executable, recorder_path],
                creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
                env=env,
            )
            self.log_message("🗺️ 地图记录器已启动", "SYSTEM")
        except Exception as e:
            self.log_message(f"❌ 启动地图记录器失败: {e}", "ERROR")

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

    def on_open_settings_region_recorder(self):
        """打开设置子窗口区域录制器"""
        import subprocess
        import os
        import sys

        recorder_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tools",
            "region_recorder_settings_dialog.py",
        )
        try:
            subprocess.Popen(
                [sys.executable, recorder_path],
                creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
            )
            self.log_message("🧭 设置子窗口区域录制器已启动", "SYSTEM")
        except Exception as e:
            self.log_message(f"❌ 启动设置子窗口区域录制器失败: {e}", "ERROR")
    
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

    def on_run_dar_route_test(self):
        tasks = {
            "daily_chain": False,
            "gacha": False,
            "battle_defeat": False,
            "training_level": False,
            "training_until_level": False,

            "dar_route_test": True,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self.log_message("启动螳螂捕捉(TEST)：请先切到克洛斯星二层", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)


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
        self._emit_start(tasks)

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
        self._emit_start(tasks)

    def _update_training_battle_loop_box_for_mode(self) -> None:
        """嘟嘟卡拉无视循环次数输入，避免误解。"""
        if not hasattr(self, "combo_training_battle_mode") or not hasattr(self, "leiyi_loop_box"):
            return
        mode = self.combo_training_battle_mode.currentData() or "leiyi"
        if mode == "dudukala":
            self.leiyi_loop_box.setEnabled(False)
            self.leiyi_loop_box.setPlaceholderText("嘟嘟卡拉不适用")
            self.leiyi_loop_box.setToolTip("嘟嘟卡拉为无限循环，直至黄色胜利探针或手动停止。")
        else:
            self.leiyi_loop_box.setEnabled(True)
            self.leiyi_loop_box.setPlaceholderText("循环(默认10)")
            self.leiyi_loop_box.setToolTip("")

    def on_run_leiyi_training(self):
        mode = (
            self.combo_training_battle_mode.currentData()
            if hasattr(self, "combo_training_battle_mode")
            else "leiyi"
        ) or "leiyi"
        if mode not in ("leiyi", "dudukala"):
            mode = "leiyi"

        loop_count = 10
        if mode == "leiyi":
            try:
                loop_txt = (self.leiyi_loop_box.text() or "").strip()
                loop_count = 10 if loop_txt == "" else int(loop_txt)
                if loop_count <= 0:
                    raise ValueError()
                if loop_count > 999:
                    loop_count = 999
            except Exception:
                self.log_message("⚠ 雷伊特训：循环次数需为 1~999 的整数", "ERROR")
                return

        tasks = {
            "daily_chain": False,
            "gacha": False,
            "battle_defeat": False,
            "training_level": False,
            "training_until_level": False,
            "leiyi_training": True,
            "training_battle_mode": mode,
            "leiyi_loop_count": loop_count,
            "use_foreground": self.chk_foreground.isChecked()
        }
        mode_label = "嘟嘟卡拉" if mode == "dudukala" else "雷伊特训"
        if mode == "dudukala":
            self.log_message(f"⚔ 启动{mode_label}（无限循环，直至黄探针胜利或停止）", "SYSTEM")
        else:
            self.log_message(f"⚔ 启动{mode_label}：{loop_count} 次循环", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_teixun_loop(self):
        tasks = {
            "daily_chain": False,
            "gacha": False,
            "battle_defeat": False,
            "training_level": False,
            "training_until_level": False,
            "leiyi_training": False,
            "teixun_loop": True,
            "use_foreground": self.chk_foreground.isChecked()
        }
        self.log_message("🔄 启动特训循环（黄=1AND1，白=等待后直接恢复）", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def on_stop(self):
        self.stop_signal.emit()
        self.log_message("🛑 已请求停止当前任务（等待引擎收尾）", "SYSTEM")

    def on_show_kernel_log(self):
        self.kernel_log_window.show()
        self.kernel_log_window.raise_()

    # ------------------ UI状态 ------------------
    def _lock_ui_except_scheduled(self):
        """锁定UI但保持定时任务UI可用（允许定时任务与其他任务共存）"""
        if hasattr(self, "btn_pre_daily"):
            self.btn_pre_daily.setEnabled(False)
        self.btn_run_daily.setEnabled(False)
        self.btn_run_task.setEnabled(False)
        self.script_combo.setEnabled(False)
        self.task_repeat_box.setEnabled(False)
        self.btn_hero_tower.setEnabled(False)
        self.btn_chaos_battle_x2.setEnabled(False)
        self.btn_1v1_x2.setEnabled(False)
        self.btn_training_level.setEnabled(False)
        self.btn_training_to_100.setEnabled(False)
        if hasattr(self, "btn_leiyi_training"):
            self.btn_leiyi_training.setEnabled(False)
        if hasattr(self, "leiyi_loop_box"):
            self.leiyi_loop_box.setEnabled(False)
        if hasattr(self, "combo_training_battle_mode"):
            self.combo_training_battle_mode.setEnabled(False)
        if hasattr(self, "btn_teixun_loop"):
            self.btn_teixun_loop.setEnabled(False)
        self.btn_stop.setEnabled(True)

        # ✅ 新增：锁住野外捕捉按钮
        if hasattr(self, "btn_rare"):
            self.btn_rare.setEnabled(False)
        if hasattr(self, "rare_combo"):
            self.rare_combo.setEnabled(False)
        if hasattr(self, "chk_wild_skip_rotation_pre"):
            self.chk_wild_skip_rotation_pre.setEnabled(False)
        if hasattr(self, "btn_smart_tracking_test"):
            self.btn_smart_tracking_test.setEnabled(False)

        # ✅ 旧按钮不存在就别动它
        if hasattr(self, "btn_dar_route_test"):
            self.btn_dar_route_test.setEnabled(False)

        # 录制器按钮保持可用（它们独立运行）
        # 定时任务UI保持可用（允许与其他任务共存）
        # 不锁定定时任务相关UI
    
    def _lock_ui(self):
        if hasattr(self, "btn_pre_daily"):
            self.btn_pre_daily.setEnabled(False)
        self.btn_run_daily.setEnabled(False)
        self.btn_run_task.setEnabled(False)
        self.script_combo.setEnabled(False)
        self.task_repeat_box.setEnabled(False)
        self.btn_hero_tower.setEnabled(False)
        self.btn_chaos_battle_x2.setEnabled(False)
        self.btn_1v1_x2.setEnabled(False)
        self.btn_training_level.setEnabled(False)
        self.btn_training_to_100.setEnabled(False)
        if hasattr(self, "btn_leiyi_training"):
            self.btn_leiyi_training.setEnabled(False)
        if hasattr(self, "leiyi_loop_box"):
            self.leiyi_loop_box.setEnabled(False)
        if hasattr(self, "combo_training_battle_mode"):
            self.combo_training_battle_mode.setEnabled(False)
        if hasattr(self, "btn_teixun_loop"):
            self.btn_teixun_loop.setEnabled(False)
        self.btn_stop.setEnabled(True)

        # ✅ 新增：锁住野外捕捉按钮
        if hasattr(self, "btn_rare"):
            self.btn_rare.setEnabled(False)
        if hasattr(self, "rare_combo"):
            self.rare_combo.setEnabled(False)
        if hasattr(self, "chk_wild_skip_rotation_pre"):
            self.chk_wild_skip_rotation_pre.setEnabled(False)
        if hasattr(self, "btn_smart_tracking_test"):
            self.btn_smart_tracking_test.setEnabled(False)
        # 定时任务相关UI保持可用（允许与其他任务共存）
        # 不在这里锁定定时任务UI

        # ✅ 旧按钮不存在就别动它
        if hasattr(self, "btn_dar_route_test"):
            self.btn_dar_route_test.setEnabled(False)
        
        # ✅ 禁用轮换模式和尼奥模式的按钮和勾选框
        if hasattr(self, "btn_start_rotation"):
            self.btn_start_rotation.setEnabled(False)
        if hasattr(self, "rotation_rare_combo"):
            self.rotation_rare_combo.setEnabled(False)
        if hasattr(self, "chk_rotation_test_mode"):
            self.chk_rotation_test_mode.setEnabled(False)
        if hasattr(self, "combo_cap_tier"):
            self.combo_cap_tier.setEnabled(False)
        if hasattr(self, "_capsule_tier_label"):
            self._capsule_tier_label.setEnabled(False)
        if hasattr(self, "rotation_interval_minutes_nieo_input"):
            self.rotation_interval_minutes_nieo_input.setEnabled(False)
        if hasattr(self, "rotation_interval_minutes_shuangta_input"):
            self.rotation_interval_minutes_shuangta_input.setEnabled(False)
        if hasattr(self, "petswf_hard_limit_sec_input"):
            self.petswf_hard_limit_sec_input.setEnabled(False)
        
        if hasattr(self, "btn_nieo"):
            self.btn_nieo.setEnabled(False)
        if hasattr(self, "combo_nieo_sub"):
            self.combo_nieo_sub.setEnabled(False)
        if hasattr(self, "chk_nieo_skip_pre_rotation"):
            self.chk_nieo_skip_pre_rotation.setEnabled(False)
        if hasattr(self, "chk_nieo_test_force_switch"):
            self.chk_nieo_test_force_switch.setEnabled(False)
        for _b in ("btn_afk_normal", "btn_afk_defeat", "btn_afk_rare", "btn_afk_nieo"):
            if hasattr(self, _b):
                getattr(self, _b).setEnabled(False)
        for _b in ("btn_pinnacle_rank", "btn_pinnacle_fun"):
            if hasattr(self, _b):
                getattr(self, _b).setEnabled(False)
        if hasattr(self, "chk_pinnacle_small_account_mode"):
            self.chk_pinnacle_small_account_mode.setEnabled(False)
        
        # 录制器按钮保持可用（它们独立运行）


    def _unlock_ui_stopped(self):
        if hasattr(self, "btn_pre_daily"):
            self.btn_pre_daily.setEnabled(True)
        self.btn_run_daily.setEnabled(True)
        self.btn_run_task.setEnabled(True)
        self.script_combo.setEnabled(True)
        self.task_repeat_box.setEnabled(True)
        self.btn_hero_tower.setEnabled(True)
        self.btn_chaos_battle_x2.setEnabled(True)
        self.btn_1v1_x2.setEnabled(True)
        self.btn_training_level.setEnabled(True)
        self.btn_training_to_100.setEnabled(True)
        if hasattr(self, "btn_leiyi_training"):
            self.btn_leiyi_training.setEnabled(True)
        if hasattr(self, "leiyi_loop_box"):
            self.leiyi_loop_box.setEnabled(True)
        if hasattr(self, "combo_training_battle_mode"):
            self.combo_training_battle_mode.setEnabled(True)
        if hasattr(self, "btn_teixun_loop"):
            self.btn_teixun_loop.setEnabled(True)
        self.btn_stop.setEnabled(False)

        if hasattr(self, "_update_training_battle_loop_box_for_mode"):
            self._update_training_battle_loop_box_for_mode()

        if hasattr(self, "btn_rare"):
            self.btn_rare.setEnabled(True)
        if hasattr(self, "rare_combo"):
            self.rare_combo.setEnabled(True)
        if hasattr(self, "btn_smart_tracking_test"):
            self.btn_smart_tracking_test.setEnabled(True)
        self._update_rare_dependent_checkbox_state()  # 解锁后恢复螳螂/闪光皮皮相关勾选
        # 定时任务相关UI保持可用（允许与其他任务共存）
        # 不在这里解锁定时任务UI（因为从未锁定）

        if hasattr(self, "btn_dar_route_test"):
            self.btn_dar_route_test.setEnabled(True)

        # ✅ 检查是否需要自动启动轮换模式（日常任务完成后，或日常链大乱斗单场超时交接）
        handoff_rotation = False
        if self._auto_start_rotation_after_daily:
            self._auto_start_rotation_after_daily = False
            handoff_rotation = True
        bot = getattr(self, "bot", None)
        if bot is not None and getattr(bot, "rotation_handoff_after_chaos_timeout", False):
            try:
                bot.rotation_handoff_after_chaos_timeout = False
            except Exception:
                pass
            handoff_rotation = True

        if handoff_rotation:
            # 等待1秒后自动启动轮换模式
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(1000, self._auto_start_rotation_mode)
            # 不解锁轮换模式和尼奥模式的UI，保持锁定状态
        else:
            # ✅ 只有在不需要自动启动时，才重新启用轮换模式和尼奥模式的按钮和勾选框
            if hasattr(self, "btn_start_rotation"):
                self.btn_start_rotation.setEnabled(True)
            if hasattr(self, "rotation_rare_combo"):
                self.rotation_rare_combo.setEnabled(True)
            if hasattr(self, "chk_rotation_test_mode"):
                self.chk_rotation_test_mode.setEnabled(True)
                # 根据测试模式复选框状态更新输入框状态
                if hasattr(self, "_update_rotation_test_inputs_enabled"):
                    self._update_rotation_test_inputs_enabled()
            if hasattr(self, "combo_cap_tier"):
                self.combo_cap_tier.setEnabled(True)
            if hasattr(self, "_capsule_tier_label"):
                self._capsule_tier_label.setEnabled(True)
            
            if hasattr(self, "btn_nieo"):
                self.btn_nieo.setEnabled(True)
            if hasattr(self, "combo_nieo_sub"):
                self.combo_nieo_sub.setEnabled(True)
            if hasattr(self, "chk_nieo_skip_pre_rotation"):
                self.chk_nieo_skip_pre_rotation.setEnabled(True)
            if hasattr(self, "chk_nieo_test_force_switch"):
                self.chk_nieo_test_force_switch.setEnabled(True)
            for _b in ("btn_afk_normal", "btn_afk_defeat", "btn_afk_rare", "btn_afk_nieo"):
                if hasattr(self, _b):
                    getattr(self, _b).setEnabled(True)
            for _b in ("btn_pinnacle_rank", "btn_pinnacle_fun"):
                if hasattr(self, _b):
                    getattr(self, _b).setEnabled(True)
            if hasattr(self, "chk_pinnacle_small_account_mode"):
                self.chk_pinnacle_small_account_mode.setEnabled(True)
        
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

    def _capsule_task_kv(self) -> dict:
        """螳螂遇野生 122 首回合无敌；其余捕捉投掷由本下拉（默认超超特循环 / 单档备选）决定。"""
        tier = "cycle"
        combo = getattr(self, "combo_cap_tier", None)
        if combo is not None:
            d = combo.currentData()
            if d in ("cycle", "default", "super_super_special"):
                tier = "cycle"
            elif d in ("high", "special", "super"):
                tier = d
        return {"non_mantis_capsule_tier": tier}

    def _update_rare_dependent_checkbox_state(self):
        """根据稀有目标下拉：仅在支持前置的流程上启用「跳过前置重连」。"""
        if not hasattr(self, "rare_combo"):
            return
        sel = self.rare_combo.currentData() or ""
        _builtin_pre = (
            "flash_pipi",
            "mantis",
            "dugulu",
            "shuangta",
            "xiaodouya",
            "eyeball",
        )
        ok_pre = sel in _builtin_pre
        if not ok_pre and sel:
            from core.wild_mode_registry import get_profile

            pf = get_profile(self._project_root(), sel)
            if pf is not None:
                map_zero = getattr(pf, "map_zero_id", None)
                if map_zero is not None:
                    try:
                        ok_pre = int(map_zero) != int(pf.map_swf_id)
                    except (TypeError, ValueError):
                        ok_pre = bool(getattr(pf, "to_script", None))
                else:
                    ok_pre = bool(getattr(pf, "to_script", None))
        if hasattr(self, "chk_wild_skip_rotation_pre"):
            self.chk_wild_skip_rotation_pre.setEnabled(ok_pre)
            if not ok_pre:
                self.chk_wild_skip_rotation_pre.setChecked(False)

    def _wild_profile_label(self, profile_key: str) -> str:
        labels = {
            "mantis": "螳螂（122）",
            "dugulu": "嘟咕噜",
            "shuangta": "双塔",
            "xiaodouya": "小豆芽",
            "flash_pipi": "闪光皮皮",
            "eyeball": "眼球",
        }
        if profile_key in labels:
            return labels[profile_key]
        from core.wild_mode_registry import get_profile

        pf = get_profile(self._project_root(), profile_key)
        if pf is not None:
            return pf.name
        return profile_key

    def _wild_supports_pre(self, profile_key: str) -> bool:
        _builtin = (
            "flash_pipi",
            "mantis",
            "dugulu",
            "shuangta",
            "xiaodouya",
            "eyeball",
        )
        if profile_key in _builtin:
            return True
        from core.wild_mode_registry import get_profile

        pf = get_profile(self._project_root(), profile_key)
        if pf is None:
            return False
        map_zero = getattr(pf, "map_zero_id", None)
        if map_zero is not None:
            try:
                return int(map_zero) != int(pf.map_swf_id)
            except (TypeError, ValueError):
                pass
        return bool(getattr(pf, "to_script", None))

    def start_rare_capture(self):
        profile = self.rare_combo.currentData() or "flash_pipi"
        tasks = {
            "wild_capture": True,
            "wild_capture_profile": profile,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        wild_skip = False
        if self._wild_supports_pre(profile) and getattr(self, "chk_wild_skip_rotation_pre", None):
            wild_skip = bool(self.chk_wild_skip_rotation_pre.isChecked())
        if self._wild_supports_pre(profile):
            tasks["wild_skip_rotation_pre"] = wild_skip
        label = self._wild_profile_label(profile)
        sfx = ""
        if self._wild_supports_pre(profile):
            sfx = " [跳过前置]" if wild_skip else " [启动前置重连]"
        self.log_message(f"🧿 启动野外捕捉：{label}{sfx}", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def start_smart_tracking_test(self):
        profile = self.rare_combo.currentData() or "flash_pipi"
        tasks = {
            "wild_capture": False,
            "smart_tracking_test": True,  # 新任务标识
            "wild_capture_profile": profile,
            "use_foreground": self.chk_foreground.isChecked(),
            "wild_battle_test_mode": False,  # 测试模式不使用声音触发
        }
        self.log_message(f"🧪 启动智能追踪测试：{profile}（请确保已进入目标地图）", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)
    
    def _build_rotation_mode_tasks(self) -> dict:
        """与「启动轮换模式」按钮相同的一份任务字典；一键日常结束后自动轮换也走这里，保证勾选一致。"""
        is_test_mode = self.chk_rotation_test_mode.isChecked()
        interval_minutes_nieo = self._parse_float_with_default(
            self.rotation_interval_minutes_nieo_input.text(), 60.0
        )
        interval_minutes_shuangta = self._parse_float_with_default(
            self.rotation_interval_minutes_shuangta_input.text(), 60.0
        )
        hard_limit_sec = self._parse_float_with_default(
            self.petswf_hard_limit_sec_input.text(), 8.0
        )
        rare_slot = "shuangta"
        if hasattr(self, "rotation_rare_combo"):
            rare_slot = self.rotation_rare_combo.currentData() or "shuangta"
        return {
            "rotation_mode": True,
            "use_foreground": self.chk_foreground.isChecked(),
            "rotation_test_mode": is_test_mode,
            "rotation_interval_minutes_nieo": interval_minutes_nieo,
            "rotation_interval_minutes_shuangta": interval_minutes_shuangta,
            "petswf_hard_limit_sec": hard_limit_sec,
            "rotation_rare_slot": rare_slot,
        }

    def start_rotation_mode(self):
        """启动尼奥·稀有轮换模式（时间窗不变）"""
        tasks = self._build_rotation_mode_tasks()
        is_test_mode = bool(tasks.get("rotation_test_mode"))
        mode_text = "测试模式（固定时间间隔切换）" if is_test_mode else "正式模式（根据北京时间自动切换）"
        slot = tasks.get("rotation_rare_slot") or "shuangta"
        rare_text = self._wild_profile_label(slot)
        self.log_message(
            f"🔄 启动轮换模式（{mode_text}；非尼奥稀有={rare_text}）",
            "SYSTEM",
        )
        self._lock_ui()
        self._emit_start(tasks)
    
    def _auto_start_rotation_mode(self):
        """日常任务完成后自动启动轮换：与点击「启动轮换模式」使用同一套勾选与参数。"""
        tasks = self._build_rotation_mode_tasks()
        is_test_mode = bool(tasks.get("rotation_test_mode"))
        mode_text = "测试模式（固定时间间隔切换）" if is_test_mode else "正式模式（根据北京时间自动切换）"
        slot = tasks.get("rotation_rare_slot") or "shuangta"
        rare_text = self._wild_profile_label(slot)
        self.log_message(
            f"🔄 日常任务完成，自动启动轮换模式（{mode_text}；非尼奥稀有={rare_text}）",
            "SYSTEM",
        )
        # ✅ 日常任务完成后 _unlock_ui_stopped 已执行，UI已解锁，需重新锁定以匹配轮换模式运行状态
        self._lock_ui()
        self._emit_start(tasks)

    def _update_rotation_test_inputs_enabled(self):
        enabled = self.chk_rotation_test_mode.isChecked()
        self.rotation_interval_minutes_nieo_input.setEnabled(enabled)
        self.rotation_interval_minutes_shuangta_input.setEnabled(enabled)
        self.petswf_hard_limit_sec_input.setEnabled(enabled)

    def _parse_float_with_default(self, text: str, default_value: float) -> float:
        try:
            return float(text)
        except (TypeError, ValueError):
            return default_value

    def _pick_task_kv(self) -> dict:
        return {
            "pick_pet_mode": True,
            "resist_drain_logic": bool(self.chk_resist_drain_logic.isChecked()),
        }

    def _molecule_converter_task_kv(self) -> dict:
        chk = getattr(self, "chk_enable_molecule_converter", None)
        enabled = chk.isChecked() if chk is not None else True
        return {"enable_molecule_converter": enabled}

    def _emit_start(self, tasks: dict) -> None:
        self.start_signal.emit(
            {
                **tasks,
                **self._pick_task_kv(),
                **self._capsule_task_kv(),
                **self._molecule_converter_task_kv(),
            }
        )
    
    def start_scheduled_task(self):
        """定时任务入口已移除（引擎不再执行 scheduled_*）；请使用双塔尼奥轮换模式。"""
        self.log_message("⚠️ 定时任务已从引擎移除，请使用「启动轮换模式」", "WARN")
        return
    
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
        self._emit_start(tasks)
    
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
        self._emit_start(tasks)
    
    def start_afk_battle_mode(self, sub_mode: str = "normal"):
        """启动挂机对战模式"""
        labels = {"normal": "普通", "defeat": "击败", "rare": "稀有", "nieo": "尼奥"}
        tasks = {
            "afk_battle_mode": True,
            "afk_sub_mode": sub_mode,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self.log_message(f"🎮 启动挂机{labels.get(sub_mode, sub_mode)}模式", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def start_pinnacle_mode(self, mode: str = "rank"):
        """启动巅峰对战模式（排位/娱乐）"""
        if mode not in ("rank", "fun"):
            mode = "rank"
        label = "排位" if mode == "rank" else "娱乐"
        tasks = {
            "pinnacle_mode": True,
            "pinnacle_mode_type": mode,
            "pinnacle_small_account_mode": bool(
                getattr(self, "chk_pinnacle_small_account_mode", None)
                and self.chk_pinnacle_small_account_mode.isChecked()
            ),
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self.log_message(f"🏆 启动巅峰对战模式（{label}）", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def start_nieo_mode(self):
        """启动尼奥模式、纯净能量(资源)或自定义三图尼奥（下拉选择）"""
        sub = "nieo"
        if hasattr(self, "combo_nieo_sub"):
            sub = self.combo_nieo_sub.currentData() or "nieo"
        tasks = {
            "nieo_mode": True,
            "nieo_sub_mode": sub,
            "use_foreground": self.chk_foreground.isChecked(),
            "test_nieo": False,
            "test_nie": False,
            "skip_nie_77": False,
            "nieo_skip_pre_rotation": (
                self.chk_nieo_skip_pre_rotation.isChecked()
                if hasattr(self, "chk_nieo_skip_pre_rotation")
                else False
            ),
            "nieo_test_force_switch": (
                self.chk_nieo_test_force_switch.isChecked()
                if hasattr(self, "chk_nieo_test_force_switch")
                else False
            ),
        }
        test_msg = ""
        if tasks.get("nieo_skip_pre_rotation"):
            test_msg += " [跳过前置]"
        else:
            test_msg += " [启动前置重连]"
        if tasks.get("nieo_test_force_switch"):
            test_msg += " [测试·10图闪光艾菲亚/11图艾斯菲格]"
        if sub == "pure_energy":
            self.log_message(f"⚡ 启动纯净能量(资源)模式（26/27，技能四战胜）{test_msg}", "SYSTEM")
        elif sub not in ("nieo", "pure_energy"):
            tasks["nieo_custom_slug"] = sub
            self.log_message(f"🌊 启动自定义尼奥模式（slug={sub}）{test_msg}", "SYSTEM")
        else:
            self.log_message(f"🌊 启动尼奥模式（10/11地图循环）{test_msg}", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)
    
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

