# gui/dashboard.py
import os
import shutil
import threading
import time
import win32gui
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QComboBox, QLabel,
    QPushButton, QTextEdit, QGroupBox, QCheckBox, QLineEdit, QDateTimeEdit, QInputDialog,
    QMessageBox, QDialog, QGridLayout,
)
from PyQt6.QtGui import QDoubleValidator, QIntValidator, QPixmap
from PyQt6.QtCore import QDateTime
from PyQt6.QtCore import pyqtSignal, pyqtSlot, Qt, QMetaObject, Q_ARG, QDateTime, QByteArray

from core.utils import window_manager
from gui.kernel_log_window import KernelLogWindow
from core.logger import add_kernel_log_callback, remove_kernel_log_callback, fetch_kernel_since
from core.daily_runner import DEFAULT_HERO_TOWER_BATTLES, NEW_DAILY_VARIANT_MAX_STEPS


class Dashboard(QWidget):
    start_signal = pyqtSignal(dict)
    stop_signal = pyqtSignal()

    kernel_log_signal = pyqtSignal(str)
    calibration_preview_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nieo Pilot - 智能控制台")
        self.resize(1000, 700)

        self.kernel_log_window = KernelLogWindow()
        self.kernel_log_signal.connect(self.kernel_log_window.append_log)
        self.calibration_preview_signal.connect(self._show_calibration_preview)

        self._kernel_cb = self._emit_kernel_log_with_timestamp
        add_kernel_log_callback(self._kernel_cb)
        
        # ✅ 自动启动轮换模式的标志（日常任务完成后自动启动）
        self._auto_start_rotation_after_daily = False
        self._auto_rotation_handoff_daily_completed = None
        # 融合模式弹窗：程序未关闭前，记住上一次执行的方案与参数。
        self._last_fusion_mode_config = {}

        self.init_ui()

    def closeEvent(self, event):
        try:
            remove_kernel_log_callback(self._kernel_cb)
        except Exception:
            pass
        super().closeEvent(event)

    @staticmethod
    def _format_log_timestamp(ts: float | None = None) -> str:
        now = time.time() if ts is None else ts
        base = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        return f"{base}.{int((now % 1) * 1000):03d}"

    def _append_log_timestamp(self, text: str, ts: float | None = None) -> str:
        return f"{text} [ts={self._format_log_timestamp(ts)}]"

    def _emit_kernel_log_with_timestamp(self, text: str):
        self.kernel_log_signal.emit(self._append_log_timestamp(text))

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

        self.btn_pure_refresh = QPushButton("🔄 仅刷新")
        self.btn_pure_refresh.setToolTip(
            "仅执行设置窗 + 后台「刷新」（不点保存）。\n"
            "不等待 Login.swf，不点开始/登录/选服，与「刷新登录」无关。"
        )
        self.btn_pure_refresh.clicked.connect(self.on_pure_refresh)

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

        self.btn_region_viewer = QPushButton("🔴 区域显示器")
        self.btn_region_viewer.setToolTip(
            "Enter 截图；左键标蓝点+坐标，右键删蓝点；已有区域显示红点；"
            "可输入X/Y并用绿色十字定位；"
            "浏览可多选区域；红/蓝RGB会同时显示中心点值和运行同款区域扫描值；"
            "可勾选隐藏蓝点；"
            "F10 存图；退出时逐个输入区域名保存蓝点（空跳过）。"
        )
        self.btn_region_viewer.clicked.connect(self.on_open_region_viewer)

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

        self.master_cup_label = QLabel("大师杯：")
        self.combo_master_cup = QComboBox()
        for _cup_name in self._load_master_cup_options():
            self.combo_master_cup.addItem(_cup_name, _cup_name)
        self.combo_master_cup.setFixedWidth(78)
        self.combo_master_cup.setToolTip(
            "来自 assets/regions/大师杯；飞行系保持原流程；诺姆保持只打568十次；"
            "其余系统一568青色、普通系第一页起首只青色，技能4→2→4→2→2后逃跑。"
        )
        self.btn_master_cup = QPushButton("执行")
        self.btn_master_cup.setToolTip(
            "大师杯：循环至右侧输入的黄色胜利次数；白色失利不计数；"
            "非飞行普通大师杯敌方仅568时逃跑，568加其他ID时战斗；"
            "下拉框选择诺姆时固定击败568十次；结束后自动接轮换。"
        )
        self.btn_master_cup.clicked.connect(self.on_run_master_cup)
        self.master_cup_yellow_target_box = QLineEdit()
        self.master_cup_yellow_target_box.setText("36")
        self.master_cup_yellow_target_box.setPlaceholderText("黄胜")
        self.master_cup_yellow_target_box.setFixedWidth(54)
        self.master_cup_yellow_target_box.setValidator(QIntValidator(0, 999, self))
        self.master_cup_yellow_target_box.setToolTip(
            "大师杯还需要的黄色胜利次数；默认 36；一键周常中填 0 会无打断跳过大师杯"
        )
        self.chk_master_cup_pre_setup = QCheckBox("前置")
        self.chk_master_cup_pre_setup.setChecked(True)
        self.chk_master_cup_pre_setup.setToolTip(
            "勾选后：重连、紫色跟随；飞行系按原指定宠，其余普通系从第一页起正扫首只青色，并走102→108→111后开始大师杯。"
        )
        self._master_cup_regular_yellow_target = "36"
        self.combo_master_cup.currentIndexChanged.connect(
            self._update_master_cup_target_for_type
        )
        self._update_master_cup_target_for_type()

        base_row1.addWidget(self.btn_launch)
        base_row1.addWidget(self.btn_clear_game_temp)
        base_row1.addWidget(self.btn_delete_screenshots)
        base_row1.addWidget(self.btn_refresh_trinity)
        base_row1.addWidget(self.btn_pure_refresh)
        base_row1.addWidget(self.btn_debug)
        base_row1.addWidget(self.btn_script_recorder)
        base_row1.addWidget(self.btn_region_recorder)
        base_row1.addStretch()

        base_row2.addWidget(self.btn_settings_region_recorder)
        base_row2.addWidget(self.btn_template_recorder)
        base_row2.addWidget(self.btn_map_recorder)
        base_row2.addWidget(self.btn_region_viewer)
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
            "nono\\super：nono_1~4 备份为 .og.swf 后用 nono_5 覆盖；action → action_og；exp → exp_og。\n"
            "newNpc\\multi：4.swf → 4_og.swf（与游戏内打开仓库一致）。"
        )
        self.btn_swf_petstorage.clicked.connect(
            lambda: self._on_swf_sync("PetStorage", "sync_petstorage")
        )
        self.btn_swf_pet254 = QPushButton("🐾 普通PetSWF")
        self.btn_swf_pet254.setToolTip(
            "同步两套 pet 目录（均先 og 补齐缺失、再备份 OG、再覆盖）：\n"
            "• 先全量用 assets\\254.swf（橙色）\n"
            "• 1337.swf 用 assets\\252.swf（紫色）\n"
            "• 197.swf 用 assets\\519.swf（青蓝色）\n"
            "• 其余全部保持 assets\\254.swf（橙色）\n"
            "• 还原仍从各自 OG 备份写回"
        )
        self.btn_swf_pet254.clicked.connect(
            lambda: self._on_swf_sync("普通PetSWF", "sync_pet_254")
        )
        self.btn_swf_pet254_special = QPushButton("融合SWF")
        self.btn_swf_pet254_special.setToolTip(
            "先全量用 assets\\254.swf（橙色）；"
            "77/164/471/480 -> assets\\252.swf（紫色）；"
            "79/473 -> assets\\519.swf（青蓝色）。"
            "同步 pet/swf 与 groupFightResource/pet。"
        )
        self.btn_swf_pet254_special.clicked.connect(
            lambda: self._on_swf_sync("融合SWF", "sync_fusion_pet_254_set")
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
        swf_res_layout.addWidget(self.btn_swf_pet254_special)
        swf_res_layout.addWidget(self.btn_swf_fight_pet)
        swf_res_layout.addWidget(self.btn_swf_fight_skill)

        swf_restore_layout = QHBoxLayout()
        self.btn_restore_petstorage = QPushButton("↩ PetStorage OG")
        self.btn_restore_petstorage.setToolTip(
            "还原 PetStorage.og.swf；nono\\super 从 nono_1~4.og.swf 写回；action_og → action；exp_og → exp；\n"
            "newNpc\\multi：4_og.swf 保持隐藏（OG 还原后仍检查 4→4_og）。"
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
        self.btn_new_daily = QPushButton("📋 新日常")
        self.btn_new_daily.setToolTip(
            "按右侧方案编号执行；步数框填从第几步开始（默认 1）。\n"
            "方案1第1步会自动执行预选前置；其他方案/步数直接执行日常链。\n"
            "各方案每步详情见 new_daily/步骤说明.md"
        )
        self.btn_new_daily.clicked.connect(self.on_run_new_daily)

        self.btn_pre_daily = QPushButton("🔄 预选重连")
        self.btn_pre_daily.setToolTip(
            "执行新预选链路：清背包、签到、日常、经验、Pick精灵，随后衔接欢乐谷与一键日常；等同下拉框的「预选」。"
        )
        self.btn_pre_daily.clicked.connect(self.on_run_pre_daily)

        self.btn_lanlan = QPushButton("岚岚")
        self.btn_lanlan.setToolTip(
            "仅允许：周二/周四00:00-23:45(347青)、周六00:00-05:45(683青)、周日00:00-23:45(1459青)。"
            "执行前固定1337紫色；不在时间窗直接返回。随后重连→屏蔽→紫色机塔跟随→岚岚循环。"
        )
        self.btn_lanlan.clicked.connect(self.on_run_lanlan)

        self.btn_yilu = QPushButton("依卢")
        self.btn_yilu.setToolTip(
            "刷新重连后进入108；白色探针消失后扫描依卢1-6橙色点，点击入战；R1技能一、R2切机塔、R3-5二技能、R6起技能一害怕探针捕捉。"
        )
        self.btn_yilu.clicked.connect(self.on_run_yilu)

        self.btn_light_mantis = QPushButton("光螳螂")
        self.btn_light_mantis.setToolTip(
            "刷新重连后进入102；执行光螳螂0白探针→1/2/3→左边1AND1→4入战；按害怕探针和阵亡切换逻辑循环到黄色结束。"
        )
        self.btn_light_mantis.clicked.connect(self.on_run_light_mantis)

        self.btn_one_click_release = QPushButton("一键放生")
        self.btn_one_click_release.setToolTip(
            "刷新重连并屏蔽后不回基地；扫描机械系、超能系、普通系、"
            "冰系、暗影系、水超能的每一页，"
            "放生所有青色精灵，最后身边跟随紫色精灵。"
        )
        self.btn_one_click_release.clicked.connect(self.on_run_one_click_release)

        self.btn_chip_gold_honor = QPushButton("一键周常")
        self.btn_chip_gold_honor.setToolTip(
            "先检查本周光螳螂记录，缺失则补跑→放生SWF刷新重连→"
            "map103瞭望露台紫色跟随→金豆×扭蛋次数框→"
            "回基地执行晶化气泡苏克×10→刷新→map5实验室跟随紫色→"
            "购买专用/通用芯片→回瞭望露台→荣誉兑换→"
            "首次重连后扭蛋99999次。"
        )
        self.btn_chip_gold_honor.clicked.connect(self.on_run_chip_gold_honor)

        self.combo_new_daily = QComboBox()
        self.combo_new_daily.addItem("孵化", "hatch")
        self.combo_new_daily.addItem("预选", "preselect")
        self.combo_new_daily.addItem("欢乐", "happy_valley")
        self.combo_new_daily.addItem("1", "1")
        self.combo_new_daily.addItem("2", "2")
        self.combo_new_daily.addItem("3", "3")
        self.combo_new_daily.addItem("4", "4")
        self.combo_new_daily.addItem("5", "5")
        self.combo_new_daily.addItem("6", "6")
        self.combo_new_daily.addItem("7", "7")
        self.combo_new_daily.addItem("8", "8")
        self.combo_new_daily.addItem("9", "9")
        self.combo_new_daily.setToolTip("起点：孵化、预选、欢乐谷或新日常方案编号")
        self.combo_new_daily.setFixedWidth(56)

        self.new_daily_start_step_box = QComboBox()
        self.new_daily_start_step_box.setToolTip(
            "从第几步开始执行当前所选方案（1=从头）；可选数字会随方案自动变化。"
        )
        self.new_daily_start_step_box.setFixedWidth(52)
        self.combo_new_daily.currentIndexChanged.connect(self._populate_new_daily_start_steps)
        self._populate_new_daily_start_steps()

        self.btn_run_daily = QPushButton("▶ 一键执行日常")
        self.btn_run_daily.clicked.connect(self.on_run_daily)

        self.chk_skip_daily_exp_input = QCheckBox("跳过经验输入")
        self.chk_skip_daily_exp_input.setChecked(True)
        self.chk_skip_daily_exp_input.setToolTip(
            "勾选（默认）：一键日常跳过签到精灵经验输入；经验日仍会先将签到精灵放回仓库，再继续后续取宠、欢乐谷和日常链。"
        )

        self.chk_daily_hero_tower = QCheckBox(f"勇者之塔×{DEFAULT_HERO_TOWER_BATTLES}")
        self.chk_daily_hero_tower.setChecked(True)
        self.chk_daily_hero_tower.setToolTip(
            f"勾选（默认）：一键日常方案9 在 map102 后执行 91勇者→勇者之塔×{DEFAULT_HERO_TOWER_BATTLES}→离开→92切换，再接大乱斗×2。\n"
            "不勾选：方案9 在 map102 后跳过勇者之塔，直接大乱斗×2。"
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
        row1.addWidget(self.btn_lanlan, 1)
        row1.addWidget(self.btn_yilu, 1)
        row1.addWidget(self.btn_light_mantis, 1)
        row1.addWidget(self.btn_one_click_release, 1)
        row1.addWidget(self.btn_chip_gold_honor, 1)
        row1.addWidget(self.combo_new_daily, 0)
        row1.addWidget(self.new_daily_start_step_box, 0)
        row1.addWidget(self.btn_new_daily, 1)
        row1.addWidget(self.btn_run_daily, 2)
        row1.addWidget(self.chk_skip_daily_exp_input, 0)
        row1.addWidget(self.chk_daily_hero_tower, 0)
        row1.addWidget(self.script_combo, 3)
        row1.addWidget(self.task_repeat_box, 0)
        row1.addWidget(self.btn_run_task, 1)
        row1.addWidget(self.chk_foreground, 1)
        daily_layout.addLayout(row1)

        self.chk_enable_molecule_converter = QCheckBox("执行分子转化仪")
        self.chk_enable_molecule_converter.setChecked(False)
        self.chk_enable_molecule_converter.setToolTip(
            "勾选：刷新登录、轮换、野外重连等走到基地门控后，按原逻辑执行分子转化仪（含节流与强制路径）。\n"
            "不勾选（默认）：任何模式均不执行分子转化仪，且不写入 30 min 节流时间戳。"
        )

        # 第二行：勇者之塔等（扭蛋已合并到上行）
        row2 = QHBoxLayout()
        
        # 勇者之塔（默认场数：DEFAULT_HERO_TOWER_BATTLES）
        self.btn_hero_tower = QPushButton(f"🗼 勇者之塔（{DEFAULT_HERO_TOWER_BATTLES}场）")
        self.btn_hero_tower.setToolTip(
            f"执行勇者之塔×{DEFAULT_HERO_TOWER_BATTLES}；自等待 map102 起 5 分钟保护，防进图失败无限卡住。"
        )
        self.btn_hero_tower.clicked.connect(self.on_run_hero_tower)

        # 大乱斗 + 轮换/自定义
        self.btn_1v1_chaos_rotation = QPushButton("⚔ 大乱斗+轮换")
        self.btn_1v1_chaos_rotation.setToolTip(
            "依次执行大乱斗×2（每场 30 分钟保护），完成后按右侧轮换设置启动尼奥/稀有轮换。"
        )
        self.btn_1v1_chaos_rotation.clicked.connect(self.on_run_1v1_chaos_rotation)

        self.btn_shanni_energy = QPushButton("闪尼吸能")
        self.btn_shanni_energy.setToolTip(
            "启动时删除 pet SWF 89、90，不刷新重连、不跑前置脚本；"
            "开局直接走105→106→105，再点击闪尼.吸能并观察 (390,200) 区域 1 秒。"
            "有变化则清理1AND1。"
            "首次无变化会先切图重试；成功吸能后下一次无变化则结束。"
        )
        self.btn_shanni_energy.clicked.connect(self.on_run_shanni_energy)

        self.combo_bag_test = QComboBox()
        self.combo_bag_test.addItem(
            "橙色槽位技能三探针",
            "orange_skill3_primary",
        )
        self.combo_bag_test.setMinimumWidth(210)
        self.combo_bag_test.setToolTip(
            "打开精灵背包，扫描精灵二至六的橙色槽位；按槽位升序逐只选中，"
            "等待“精灵背包.技能三探针”RGB稳定。稳定色接近 (192,165,165) 时设该精灵为首发，"
            "然后继续探索其余已记录的橙色槽位。"
        )
        self.btn_bag_test = QPushButton("测试模式")
        self.btn_bag_test.setToolTip("执行左侧下拉框选择的背包探针测试功能")
        self.btn_bag_test.clicked.connect(self.on_run_bag_test)

        self._capsule_tier_label = QLabel("捕捉胶囊：")
        self._capsule_tier_label.setToolTip(
            "除螳螂对战敌方野生 122（首回合仍投无敌胶囊）外，捕捉投掷均按本项策略。"
            "默认仅高级；亦可改为循环或全程仅超级/仅特级。"
        )
        self.combo_cap_tier = QComboBox()
        self.combo_cap_tier.addItem("超特循环", "cycle")
        self.combo_cap_tier.addItem("仅超级", "super")
        self.combo_cap_tier.addItem("仅高级", "high")
        self.combo_cap_tier.addItem("仅特级", "special")
        self.combo_cap_tier.setCurrentIndex(2)
        self.combo_cap_tier.setMinimumWidth(140)
        self.combo_cap_tier.setToolTip(
            "默认：全程仅高级。\n"
            "备选：超特循环 / 仅超级 / 仅特级。\n"
            "敌方为野生螳螂（122）时首回合仍为无敌胶囊，之后才跟本策略。"
        )

        row2.addWidget(self.btn_hero_tower)
        row2.addWidget(self.btn_1v1_chaos_rotation)
        row2.addWidget(self.btn_shanni_energy)
        row2.addWidget(self.combo_bag_test)
        row2.addWidget(self.btn_bag_test)
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
        self.combo_training_battle_mode.addItem("劳克蒙德", userData="laokemengde")
        self.combo_training_battle_mode.setToolTip(
            "雷伊特训：特训.1/2，4→2→1→3，次数由右侧输入框；黄探针胜利结束，否则白探针打点3后恢复，直至次数用完。\n"
            "嘟嘟卡拉：嘟嘟卡拉1/2入战；无限直至黄探针胜利或停止；每回合技能一；每次出手后单独累计退场 map+newNpc；白探针不设上限，不打特训.3。\n"
            "劳克蒙德：劳克蒙德.1→2→3循环入战；第一回合技能二、第二回合技能四；达到循环次数或黄色探针时结束。"
        )
        self.btn_leiyi_training = QPushButton("⚔对战")
        self.btn_leiyi_training.setToolTip(
            "与左侧模式一并启动对战特训。"
            "雷伊：遵循循环次数。"
            "嘟嘟卡拉：无限循环，仅黄色胜利探针结束（或停止）。"
            "劳克蒙德：遵循循环次数，也会在黄色胜利探针出现时提前结束。"
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
        self.combo_psychic_exp_type = QComboBox()
        self.combo_psychic_exp_type.addItem("超能系", userData={"category": "精灵仓库.超能系", "value": "5820", "label": "超能经验"})
        self.combo_psychic_exp_type.addItem("草系", userData={"category": "精灵仓库.草系", "value": "53199", "label": "草系经验"})
        self.combo_psychic_exp_type.setToolTip("经验紫：所选系别仓库从第一页向后扫描紫色精灵；超能=5820，草系=53199")
        self.btn_psychic_exp_purple = QPushButton("经验紫")
        self.btn_psychic_exp_purple.setToolTip("首次初始化仓库后从前到后扫描；满 6 只执行经验，重开后从原位置继续")
        self.btn_psychic_exp_purple.clicked.connect(self.on_run_psychic_exp_purple)
        self.btn_fusion_mode = QPushButton("融合模式")
        self.btn_fusion_mode.setToolTip("打开融合配置：选择方案与四个 1-24 资源编号后执行")
        self.btn_fusion_mode.clicked.connect(self.on_run_fusion_mode)
        self.btn_nono_soul_fusion = QPushButton('nono')
        self.btn_nono_soul_fusion.setToolTip("按CSV计时判断是否可孵化：到期则打开nono并执行孵化确认")
        self.btn_nono_soul_fusion.clicked.connect(self.on_run_nono_soul_fusion)
        exp_layout.addWidget(self.btn_exp_minor_battle)
        exp_layout.addWidget(self.master_cup_label)
        exp_layout.addWidget(self.combo_master_cup)
        exp_layout.addWidget(self.btn_master_cup)
        exp_layout.addWidget(self.master_cup_yellow_target_box)
        exp_layout.addWidget(self.chk_master_cup_pre_setup)
        exp_layout.addWidget(self.combo_training_battle_mode)
        exp_layout.addWidget(self.btn_leiyi_training)
        exp_layout.addWidget(self.leiyi_loop_box)
        exp_layout.addWidget(self.btn_teixun_loop)
        exp_layout.addWidget(self.combo_psychic_exp_type)
        exp_layout.addWidget(self.btn_psychic_exp_purple)
        exp_layout.addWidget(self.btn_fusion_mode)
        exp_layout.addWidget(self.btn_nono_soul_fusion)
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
        self.btn_nieo_resource_chain = QPushButton("🔗 资源五连×60")
        self.btn_nieo_resource_chain.setToolTip(
            "依次执行：晶化气泡、露西之核、水生海草、贝壳精华、水之精华；"
            "每个模式累计 60 次黄色胜利后切换，全部完成后进入普通轮换模式。"
        )
        self.btn_nieo_resource_chain.clicked.connect(
            self.start_nieo_resource_chain
        )
        row1.addWidget(self.btn_nieo_resource_chain)
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
            "测试模式（10图全切机塔 / 11图全切艾菲德斯）"
        )
        self.chk_nieo_test_force_switch.setChecked(False)
        self.chk_nieo_test_force_switch.setToolTip(
            "勾选后：尼奥模式所有入战均按尼尔家族流程处理。"
            "10号地图的所有遭遇切换到机塔，"
            "11号地图的所有遭遇切换到艾菲德斯。"
        )
        row_nieo_opts.addWidget(self.chk_nieo_test_force_switch)

        self.chk_nieo_single_map_escape = QCheckBox("单图模式")
        self.chk_nieo_single_map_escape.setChecked(False)
        self.chk_nieo_single_map_escape.setToolTip(
            "内置尼奥固定普通逃跑：08:00-10:00 强制双图，10:00-12:00 强制单图。"
            "其他时段及纯净能量/自定义资源按此开关执行；单图会避开上一轮选点。"
        )
        row_nieo_opts.addWidget(self.chk_nieo_single_map_escape)

        self.chk_nieo_follow_cyan = QCheckBox("跟随青色")
        self.chk_nieo_follow_cyan.setChecked(False)
        self.chk_nieo_follow_cyan.setToolTip(
            "适用于尼奥、纯净能量、自定义资源和资源五连："
            "尼奥与水生海草开局仍跟随紫色，首次到A图后再切青色进入B图；"
            "其他适用模式按原逻辑跟随青色。未勾选均跟随紫色。"
        )
        row_nieo_opts.addWidget(self.chk_nieo_follow_cyan)

        self.chk_nieo_yellow60_to_rotation = QCheckBox("黄60→普通轮换")
        self.chk_nieo_yellow60_to_rotation.setChecked(False)
        self.chk_nieo_yellow60_to_rotation.setToolTip(
            "仅资源模式可用：累计 60 次黄色战胜后，重连并进入普通轮换重连模式。"
        )
        row_nieo_opts.addWidget(self.chk_nieo_yellow60_to_rotation)
        self.combo_nieo_sub.currentIndexChanged.connect(
            self._update_nieo_yellow60_to_rotation_enabled
        )
        self.combo_nieo_sub.currentIndexChanged.connect(
            self._update_nieo_single_map_option_state
        )
        self._update_nieo_yellow60_to_rotation_enabled()
        self._update_nieo_single_map_option_state()
        row_nieo_opts.addStretch()
        nieo_layout.addLayout(row_nieo_opts)
        
        nieo_group.setLayout(nieo_layout)
        control_panel.addWidget(nieo_group)

        # ---------- 活动精灵模式（单图 A；遇敌/战斗白盒）----------
        event_pet_group = QGroupBox("🌿 活动精灵模式（单图挂机）")
        event_pet_layout = QVBoxLayout()
        row_ep = QHBoxLayout()
        self.btn_event_pet = QPushButton("🌿 启动活动精灵")
        self.btn_event_pet.clicked.connect(self.start_event_pet_mode)
        row_ep.addWidget(self.btn_event_pet)
        self.combo_event_pet = QComboBox()
        self._load_event_pet_modes()
        self.combo_event_pet.setToolTip(
            "内置伊特(471) + assets/event_pet_modes 自定义项（重启 Dashboard 刷新）"
        )
        row_ep.addWidget(self.combo_event_pet)
        event_pet_layout.addLayout(row_ep)
        self.chk_event_pet_skip_pre = QCheckBox(
            "跳过前置重连（不跑三宠 Pick + to 脚本，直接进活动图）"
        )
        self.chk_event_pet_skip_pre.setChecked(False)
        event_pet_layout.addWidget(self.chk_event_pet_skip_pre)
        event_pet_group.setLayout(event_pet_layout)
        control_panel.addWidget(event_pet_group)

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
        rotation_group = QGroupBox("🔄 尼奥·稀有轮换（自动切换｜北京时间）")
        rotation_layout = QVBoxLayout()

        rot_top = QHBoxLayout()
        self.btn_start_rotation = QPushButton("🧪 测试完整轮换")
        self.btn_start_rotation.setToolTip(
            "保留完整日常检测、整点补跑及 00/06 点完整链路，用于验证完整轮换流程。"
        )
        self.btn_start_rotation.clicked.connect(self.start_full_rotation_test)
        rot_top.addWidget(self.btn_start_rotation)

        self.btn_rotation_reconnect = QPushButton("▶ 轮换重连模式")
        self.btn_rotation_reconnect.setToolTip(
            "直接启动轮换，不在首次点击时检查或补跑一键日常。"
        )
        self.btn_rotation_reconnect.clicked.connect(self.start_rotation_mode)
        rot_top.addWidget(self.btn_rotation_reconnect)

        self.chk_rotation_nieo_follow_cyan = QCheckBox("跟随青色")
        self.chk_rotation_nieo_follow_cyan.setChecked(False)
        self.chk_rotation_nieo_follow_cyan.setToolTip(
            "轮换中的尼奥与水生海草开局先跟随紫色，首次到A图后切青色再进B图；"
            "纯净能量及其他配置型资源保持原有青色跟随逻辑。"
        )
        rot_top.addWidget(self.chk_rotation_nieo_follow_cyan)

        self.btn_rotation_chain_test = QPushButton("🧪 链测试")
        self.btn_rotation_chain_test.setToolTip(
            "依次验证：白天模式→伊特→螳螂→尼奥→稀有，每段成功进图后立即切下一段"
        )
        self.btn_rotation_chain_test.clicked.connect(self.on_run_rotation_chain_test)
        rot_top.addWidget(self.btn_rotation_chain_test)

        rot_top.addWidget(QLabel("非尼奥稀有："))
        self.rotation_rare_combo = QComboBox()
        self.rotation_rare_combo.setMinimumWidth(160)
        self.rotation_rare_combo.setToolTip(
            "0:00–6:00、18:00–20:00 跑所选稀有；若未勾白天模式，6:00–18:00 也跑所选稀有；"
            "20:00 起按伊特/螳螂/尼奥规则。"
        )
        self._load_rotation_rare_modes()
        rot_top.addWidget(self.rotation_rare_combo)

        self.chk_rotation_test_mode = QCheckBox("测试模式（固定间隔切换）")
        self.chk_rotation_test_mode.setChecked(False)
        rot_top.addWidget(self.chk_rotation_test_mode)

        rot_top.addStretch()
        rotation_layout.addLayout(rot_top)

        rot_opts = QHBoxLayout()
        self.chk_rotation_resource = QCheckBox("白天模式")
        self.chk_rotation_resource.setChecked(True)
        self.chk_rotation_resource.setToolTip(
            "勾选后 6:00–18:00 跑下方所选白天模式（资源或稀有）；18:00–20:00 跑非尼奥稀有；20:00 起按 evening 规则"
        )
        self.rotation_resource_combo = QComboBox()
        self.rotation_resource_combo.setMinimumWidth(170)
        self._load_rotation_resource_modes()
        self.chk_rotation_eit = QCheckBox("伊特 20–21")
        self.chk_rotation_eit.setChecked(False)
        self.chk_rotation_mantis = QCheckBox("螳螂")
        self.chk_rotation_mantis.setToolTip(
            "仅勾选：20:00–22:00 螳螂、22:00–24:00 尼奥；"
            "与伊特同勾选：21:00–22:00 螳螂"
        )
        self.chk_rotation_eit.setToolTip("单独勾选：20–21 伊特、21–24 尼奥；与螳螂同勾选则 20–21 伊特、21–22 螳螂、22–24 尼奥")
        rot_opts.addWidget(self.chk_rotation_resource)
        rot_opts.addWidget(self.rotation_resource_combo)
        rot_opts.addWidget(self.chk_rotation_eit)
        rot_opts.addWidget(self.chk_rotation_mantis)
        rot_opts.addStretch()
        rotation_layout.addLayout(rot_opts)

        self.chk_rotation_resource.stateChanged.connect(self._update_rotation_resource_combo_enabled)
        self.chk_rotation_resource.stateChanged.connect(self._update_rotation_schedule_hint)
        self.chk_rotation_eit.stateChanged.connect(self._update_rotation_schedule_hint)
        self.chk_rotation_mantis.stateChanged.connect(self._update_rotation_schedule_hint)
        self.rotation_resource_combo.currentIndexChanged.connect(self._update_rotation_schedule_hint)
        self._update_rotation_resource_combo_enabled()

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
        self.chk_rotation_nieo_single_map_escape = QCheckBox("资源单图")
        self.chk_rotation_nieo_single_map_escape.setChecked(False)
        self.chk_rotation_nieo_single_map_escape.setToolTip(
            "纯净能量与配置型资源按此开关执行；单图会避开上次选点。"
            "内置尼奥仍由时段固定：08:00-10:00 双图逃跑，10:00-12:00 单图逃跑。"
        )
        pick_flags_row.addWidget(lbl_pick_default)
        pick_flags_row.addWidget(self.chk_resist_drain_logic)
        pick_flags_row.addWidget(self.chk_rotation_nieo_single_map_escape)
        pick_flags_row.addStretch()
        rotation_layout.addLayout(pick_flags_row)

        # The Nieo panel and rotation panel expose the same single-map policy.
        # Keep both controls in sync so either entry point has identical behavior.
        if hasattr(self, "chk_nieo_single_map_escape"):
            self.chk_rotation_nieo_single_map_escape.setChecked(
                self.chk_nieo_single_map_escape.isChecked()
            )
            self.chk_nieo_single_map_escape.toggled.connect(
                self.chk_rotation_nieo_single_map_escape.setChecked
            )
            self.chk_rotation_nieo_single_map_escape.toggled.connect(
                self.chk_nieo_single_map_escape.setChecked
            )

        self.rotation_resource_combo.currentIndexChanged.connect(
            self._update_rotation_single_map_option_state
        )
        self._update_rotation_single_map_option_state()

        self.chk_rotation_test_mode.stateChanged.connect(self._update_rotation_test_inputs_enabled)
        self._update_rotation_test_inputs_enabled()

        self.rotation_schedule_info_label = QLabel()
        self.rotation_schedule_info_label.setStyleSheet("color: gray; font-size: 10px;")
        self.rotation_schedule_info_label.setWordWrap(True)
        rotation_layout.addWidget(self.rotation_schedule_info_label)
        self._update_rotation_schedule_hint()

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

        self.btn_export_logs = QPushButton("📤 导出日志")
        self.btn_export_logs.setToolTip("导出当前明面日志、内核日志窗口、内核日志缓存，以及 log 目录文件副本")
        self.btn_export_logs.clicked.connect(self.on_export_logs)
        
        self.btn_clear_log = QPushButton("🗑️ 清空日志")
        self.btn_clear_log.clicked.connect(self.on_clear_log)

        btn_row.addWidget(self.btn_stop)
        btn_row.addWidget(self.btn_kernel_log)
        btn_row.addWidget(self.btn_export_logs)
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

    def _ensure_newnpc_multi_4(
        self,
        log_tag: str = "前置",
        *,
        restore_yilu_90000: bool = True,
    ) -> None:
        try:
            from core.swf_resource_ops import ensure_newnpc_multi_4_to_4_og

            ok, msg = ensure_newnpc_multi_4_to_4_og()
            self.log_message(
                f"{'✅' if ok else '⚠️'} [{log_tag}] newNpc/multi: {msg}",
                "INFO" if ok else "WARN",
            )
        except Exception as e:
            self.log_message(f"❌ [{log_tag}] newNpc/multi 检查失败: {e}", "ERROR")
        if restore_yilu_90000:
            self._ensure_newnpc_multi_90000_for_task({}, log_tag)

    def _ensure_newnpc_multi_90000_for_task(self, tasks: dict, log_tag: str = "前置") -> None:
        try:
            from core.swf_resource_ops import (
                ensure_newnpc_multi_90000_hidden_for_yilu,
                ensure_newnpc_multi_90000_restored_for_non_yilu,
            )

            if tasks.get("yilu_mode"):
                ok, msg = ensure_newnpc_multi_90000_hidden_for_yilu()
                action = "90000→90000_og"
            else:
                ok, msg = ensure_newnpc_multi_90000_restored_for_non_yilu()
                action = "90000_og→90000"
            self.log_message(
                f"{'✅' if ok else '⚠️'} [{log_tag}] newNpc/multi {action}: {msg}",
                "INFO" if ok else "WARN",
            )
        except Exception as e:
            self.log_message(f"❌ [{log_tag}] newNpc/multi 90000 检查失败: {e}", "ERROR")

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
            self._ensure_newnpc_multi_4("清除缓存")
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

    @pyqtSlot()
    def _restore_refresh_buttons_if_idle(self):
        """任务运行中（停止钮可用）时保持禁用，仅空闲时恢复两个刷新按钮。"""
        if hasattr(self, "btn_stop") and self.btn_stop.isEnabled():
            return
        for btn_name in ("btn_refresh_trinity", "btn_pure_refresh"):
            btn = getattr(self, btn_name, None)
            if btn is not None:
                btn.setEnabled(True)

    def on_refresh_trinity(self):
        self._lock_ui()
        self.log_message("🔄 开始刷新登录：直到检测到 map…", "SYSTEM")
        threading.Thread(target=self._refresh_trinity_worker, daemon=True).start()

    def on_pure_refresh(self):
        self._lock_ui()
        self.log_message("🔄 开始仅刷新（不登录）…", "SYSTEM")
        threading.Thread(target=self._pure_refresh_worker, daemon=True).start()

    def _reset_direct_refresh_stop_flags(self, label: str) -> None:
        """刷新登录/仅刷新是 Dashboard 直连线程；启动前要清掉上次停止残留。"""
        bot = getattr(self, "bot", None)
        if not bot:
            return
        try:
            bot.stop_current = False
            bot.user_stop_requested = False
            stop_event = getattr(bot, "_stop_event", None)
            if stop_event is not None:
                stop_event.clear()
            self.log_message(f"🔄 [{label}] 已清除上次停止标志", "DEBUG")
        except Exception as e:
            self.log_message(f"⚠️ [{label}] 清除停止标志失败：{e}", "WARN")

    def _prepare_refresh_login_canvas(self, use_foreground: bool) -> bool:
        """刷新登录专用：先双击整个 client 中心，再校准并锁定 1200×700 画布。"""
        if not window_manager.find_window() or not getattr(window_manager, "hwnd", None):
            self.log_message("❌ [刷新登录] 未找到游戏窗口，无法执行中心双击/校准", "ERROR")
            return False
        try:
            x1, y1, x2, y2 = win32gui.GetClientRect(window_manager.hwnd)
            cx = int((x2 - x1) / 2)
            cy = int((y2 - y1) / 2)
        except Exception as e:
            self.log_message(f"❌ [刷新登录] 获取 client 中心失败：{e}", "ERROR")
            return False

        self.log_message(f"🖱️ [刷新登录] 开局双击主窗口 client 中心 ({cx},{cy})", "INFO")
        if not window_manager.click_client(cx, cy, foreground=use_foreground):
            self.log_message("❌ [刷新登录] 第一次点击 client 中心失败", "ERROR")
            return False
        time.sleep(0.08)
        if not window_manager.click_client(cx, cy, foreground=use_foreground):
            self.log_message("❌ [刷新登录] 第二次点击 client 中心失败", "ERROR")
            return False

        self.log_message("🎯 [刷新登录] 开局执行一次屏幕校准", "SYSTEM")
        ok = window_manager.visual_debug()
        if not ok:
            self.log_message("❌ [刷新登录] 开局屏幕校准失败，停止刷新登录", "ERROR")
            return False
        if getattr(window_manager, "last_calibration_canvas_path", ""):
            self.calibration_preview_signal.emit(window_manager.last_calibration_canvas_path)
        self.log_message("✅ [刷新登录] 开局屏幕校准完成，继续刷新登录流程", "SUCCESS")
        return True

    def _pure_refresh_worker(self):
        try:
            self._ensure_newnpc_multi_4("仅刷新")
            if not hasattr(self, "bot") or not self.bot:
                self.log_message("❌ bot 未就绪，无法执行仅刷新", "ERROR")
                return
            drr = getattr(self.bot, "dar_route_runner", None)
            if not drr:
                self.log_message("❌ dar_route_runner 不存在，无法执行仅刷新", "ERROR")
                return
            self._reset_direct_refresh_stop_flags("仅刷新")
            use_fg = self.chk_foreground.isChecked()
            ok = drr.run_pure_refresh(use_fg)
            if not ok:
                self.log_message("⚠️ 仅刷新失败（见上方日志）", "WARN")
        except Exception as e:
            self.log_message(f"❌ 仅刷新异常: {e}", "ERROR")
            import traceback
            self.log_message(f"📋 异常详情: {traceback.format_exc()}", "ERROR")
        finally:
            QMetaObject.invokeMethod(
                self,
                "_unlock_ui_stopped",
                Qt.ConnectionType.QueuedConnection,
            )

    def _refresh_trinity_worker(self):
        try:
            self._ensure_newnpc_multi_4("刷新登录")
            if not hasattr(self, "bot") or not self.bot:
                self.log_message("❌ bot 未就绪，无法执行刷新登录", "ERROR")
                return
            drr = getattr(self.bot, "dar_route_runner", None)
            if not drr:
                self.log_message("❌ dar_route_runner 不存在，无法执行刷新登录", "ERROR")
                return
            self._reset_direct_refresh_stop_flags("刷新登录")
            use_fg = self.chk_foreground.isChecked()
            if not self._prepare_refresh_login_canvas(use_fg):
                return
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
                self,
                "_unlock_ui_stopped",
                Qt.ConnectionType.QueuedConnection,
            )

    def _on_swf_sync(self, label: str, op_name: str):
        self.log_message(f"⏳ [{label}] 正在同步…", "SYSTEM")
        threading.Thread(
            target=self._swf_sync_worker,
            args=(label, op_name),
            daemon=True,
        ).start()

    def _prepare_fly_pet_1337_for_swf_button(self, label: str) -> bool:
        from core.swf_resource_ops import ensure_fly_pet_483_and_1337_from_50

        ok, msg = ensure_fly_pet_483_and_1337_from_50()
        self.log_message(
            f"{'✅' if ok else '❌'} [{label}·flyPet-483/1337] {msg}",
            "SUCCESS" if ok else "ERROR",
        )
        return bool(ok)

    def _swf_sync_worker(self, label: str, op_name: str):
        try:
            from core import swf_resource_ops

            if not self._prepare_fly_pet_1337_for_swf_button(label):
                return
            fn = getattr(swf_resource_ops, op_name)
            ok, msg = fn()
            self.log_message(
                f"{'✅' if ok else '❌'} [{label}] {msg}",
                "SUCCESS" if ok else "ERROR",
            )
            self._ensure_newnpc_multi_4(label)
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

            if not self._prepare_fly_pet_1337_for_swf_button(label):
                return
            fn = getattr(swf_resource_ops, op_name)
            ok, msg = fn()
            self.log_message(
                f"{'✅' if ok else '❌'} [{label}] {msg}",
                "SUCCESS" if ok else "ERROR",
            )
            self._ensure_newnpc_multi_4(label)
        except Exception as e:
            self.log_message(f"❌ [{label}] {e}", "ERROR")

    def on_debug_screen(self):
        self.btn_debug.setEnabled(False)
        self.log_message("🎯 开始校准屏幕...", "SYSTEM")
        threading.Thread(target=self._debug_worker, daemon=True).start()

    def _debug_worker(self):
        ok = window_manager.visual_debug()
        self.log_message("✅ 屏幕校准完成" if ok else "❌ 校准失败，未找到窗口", "SUCCESS" if ok else "ERROR")
        if ok and getattr(window_manager, "last_calibration_canvas_path", ""):
            self.calibration_preview_signal.emit(window_manager.last_calibration_canvas_path)
        QMetaObject.invokeMethod(self.btn_debug, "setEnabled", Qt.ConnectionType.QueuedConnection, Q_ARG(bool, True))

    @pyqtSlot(str)
    def _show_calibration_preview(self, path: str):
        if not path or not os.path.exists(path):
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("校准屏幕 - 固定 1200×700 画布")
        layout = QVBoxLayout(dlg)
        info = QLabel("这就是后续加载区域使用的固定 1200×700 画布；再次点击“校准屏幕”前不会重新定义。")
        info.setWordWrap(True)
        layout.addWidget(info)
        img = QLabel()
        pix = QPixmap()
        try:
            with open(path, "rb") as f:
                pix.loadFromData(QByteArray(f.read()))
        except Exception:
            pix = QPixmap(path)
        if not pix.isNull():
            img.setPixmap(
                pix.scaled(
                    960,
                    560,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        layout.addWidget(img)
        btn = QPushButton("关闭")
        btn.clicked.connect(dlg.accept)
        layout.addWidget(btn)
        dlg.resize(1000, 640)
        dlg.exec()

    # ------------------ 任务触发 ------------------
    def _load_fix_scripts(self):
        """加载 fix_script 脚本：扭蛋 → 放生 → 金豆 → 孵化（固定显示）→ 其余按名字排序。"""
        import os
        script_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fix_script")
        self.script_combo.clear()
        self.script_combo.addItem("🎲 扭蛋", "__gacha_test__")
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

    def _load_master_cup_options(self):
        preferred = ["水系", "火系", "草系", "电系", "冰系", "地面系", "飞行系", "机械系", "战斗系", "诺姆"]
        folder = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "assets",
            "regions",
            "大师杯",
        )
        found = set()
        try:
            for filename in os.listdir(folder):
                if not filename.lower().endswith(".json"):
                    continue
                name = filename[:-5]
                if name == "1":
                    continue
                found.add(name)
        except Exception:
            found = set(preferred)
        found.add("诺姆")

        ordered = [name for name in preferred if name in found]
        ordered.extend(sorted(name for name in found if name not in ordered))
        return ordered or preferred

    def _update_master_cup_target_for_type(self, *_args) -> None:
        if not hasattr(self, "combo_master_cup") or not hasattr(
            self, "master_cup_yellow_target_box"
        ):
            return
        cup_type = str(
            self.combo_master_cup.currentData()
            or self.combo_master_cup.currentText()
            or "水系"
        ).strip()
        target_box = self.master_cup_yellow_target_box
        is_norm = cup_type == "诺姆"
        if is_norm:
            current = (target_box.text() or "").strip()
            if current and current != "10":
                self._master_cup_regular_yellow_target = current
            target_box.setText("10")
            target_box.setEnabled(False)
            target_box.setToolTip("诺姆固定只击败568十次")
            return
        if not target_box.isEnabled():
            target_box.setText(
                str(getattr(self, "_master_cup_regular_yellow_target", "36") or "36")
            )
        target_box.setEnabled(True)
        target_box.setToolTip(
            "大师杯还需要的黄色胜利次数；默认 36；一键周常中填 0 会无打断跳过大师杯"
        )

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

        if data == "__gacha_test__":
            gacha_test_then_rotation = times > 10
            rotation_tasks = dict(self._build_rotation_mode_tasks())
            rotation_tasks.pop("rotation_mode", None)
            tasks = {
                "gacha_test": True,
                "gacha_test_times": times,
                "use_foreground": use_fg,
                "gacha_test_then_rotation": gacha_test_then_rotation,
                **rotation_tasks,
            }
            suffix = "；全部完成后轮换" if gacha_test_then_rotation else ""
            initial_reconnect_text = "；第1次前先重连" if times > 10 else ""
            self.log_message(
                f"🎲 启动扭蛋：{times} 次{initial_reconnect_text}；失败自动重连，"
                f"重连后3次内再失败则轮换{suffix}",
                "SYSTEM",
            )
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

    def _start_daily_special_mode(self, variant: str) -> None:
        phase = self._current_happy_valley_start_phase()
        labels = {
            "hatch": "孵化→预选→欢乐谷→新日常",
            "preselect": "预选（清包、签到、日常、经验、Pick）",
            "happy_valley": f"欢乐谷·{phase}",
        }
        tasks = {
            "daily_start_mode": variant,
            "happy_valley_start_phase": phase,
            "use_foreground": self.chk_foreground.isChecked(),
            "skip_daily_exp_input": self.chk_skip_daily_exp_input.isChecked(),
        }
        # 孵化、预选和欢乐谷直达的完整日常链结束或内部失败后都交接轮换。
        # 欢乐谷从火系/草系开始时，仍需继续执行方案1-9，完成后再进入轮换模式。
        self._auto_start_rotation_after_daily = variant in {"hatch", "preselect", "happy_valley"}
        if self._auto_start_rotation_after_daily:
            self.log_message("🔄 完整一键日常结束后将自动交接轮换；内部重连不取消交接", "INFO")
        self.log_message(f"📅 启动日常起点：{labels.get(variant, variant)}", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_daily(self):
        include_tower = self.chk_daily_hero_tower.isChecked()
        variant = self._current_new_daily_variant()
        if self._is_daily_special_start(variant):
            self._start_daily_special_mode(variant)
            return
        start_step = self._current_new_daily_start_step()
        tasks = {
            "new_daily_chain_mode": True,
            "new_daily_variant": variant,
            "new_daily_start_step": start_step,
            "training_level": False,
            "use_foreground": self.chk_foreground.isChecked(),
            "daily_include_hero_tower": include_tower,
            "skip_daily_exp_input": self.chk_skip_daily_exp_input.isChecked(),
        }
        if include_tower:
            tail = "方案1–9（方案9含勇者之塔）"
        else:
            tail = "方案1–9（方案9跳过勇者之塔）"
        if variant != "1" or start_step > 1:
            tower_text = "含勇者之塔" if include_tower else "跳过勇者之塔"
            tail = f"方案{variant}第{start_step}步→9（方案9{tower_text}）"
        if variant == "1" and start_step == 1:
            tail = f"{tail}；含预选前置"
        self.log_message(f"📅 启动一键新日常（{tail}）", "SYSTEM")
        self._auto_start_rotation_after_daily = True
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_pre_daily(self):
        self._start_daily_special_mode("preselect")

    def on_run_lanlan(self):
        tasks = {
            "lanlan_mode": True,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self._auto_start_rotation_after_daily = False
        self.log_message("🔄 启动岚岚（重连→屏蔽→紫色机塔跟随→岚岚二技能循环）", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_yilu(self):
        tasks = {
            "yilu_mode": True,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self._auto_start_rotation_after_daily = False
        self.log_message("🔄 启动依卢（重连→108→依卢橙点→机塔三次二技能→害怕捕捉）", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_light_mantis(self):
        tasks = {
            "light_mantis_mode": True,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self._auto_start_rotation_after_daily = False
        self.log_message("🔄 启动光螳螂（重连→102→入口→专用战斗）", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_bag_test(self):
        test_name = str(self.combo_bag_test.currentData() or "orange_skill3_primary")
        test_label = self.combo_bag_test.currentText()
        tasks = {
            "bag_putback_test_mode": test_name,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self._auto_start_rotation_after_daily = False
        self.log_message(f"🧪 启动测试模式：{test_label}", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_one_click_release(self):
        tasks = {
            "one_click_release_mode": True,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self._auto_start_rotation_after_daily = True
        self.log_message(
            "🐾 启动一键放生：扫描机械系→超能系→普通系→冰系→暗影系→水超能，"
            "放生青色精灵；完成后刷新并自动进入轮换模式",
            "SYSTEM",
        )
        self._lock_ui()
        self._emit_start(tasks)

    def _current_master_cup_settings(self) -> dict:
        cup_type = "水系"
        if hasattr(self, "combo_master_cup"):
            cup_type = str(
                self.combo_master_cup.currentData()
                or self.combo_master_cup.currentText()
                or "水系"
            )
        yellow_target = 36
        if hasattr(self, "master_cup_yellow_target_box"):
            raw = (self.master_cup_yellow_target_box.text() or "").strip()
            try:
                yellow_target = max(0, int(raw or "36"))
            except ValueError:
                yellow_target = 36
        norm_mode = cup_type == "诺姆"
        if norm_mode:
            yellow_target = 10
        return {
            "master_cup_type": cup_type,
            "master_cup_yellow_target": yellow_target,
            "master_cup_pre_setup": bool(
                getattr(self, "chk_master_cup_pre_setup", None)
                and self.chk_master_cup_pre_setup.isChecked()
            ),
            "master_cup_norm_mode": norm_mode,
        }

    def on_run_chip_gold_honor(self):
        raw_gacha_times = (self.task_repeat_box.text() or "").strip()
        try:
            gacha_filled_times = max(1, int(raw_gacha_times or "1"))
        except (TypeError, ValueError):
            gacha_filled_times = 1
        rotation_tasks = dict(self._build_rotation_mode_tasks())
        rotation_tasks.pop("rotation_mode", None)
        rotation_tasks["rotation_full_daily_maintenance"] = False
        tasks = {
            "chip_gold_honor_mode": True,
            "use_foreground": self.chk_foreground.isChecked(),
            "weekly_gacha_filled_times": gacha_filled_times,
            **self._current_master_cup_settings(),
            **rotation_tasks,
        }
        self._auto_start_rotation_after_daily = False
        self.log_message(
            "📅 启动一键周常：检查并按需补跑光螳螂→按本周记录检查大师杯→"
            "瞭望露台紫色跟随→"
            f"金豆×{gacha_filled_times}（扭蛋次数）→"
            "晶化气泡苏克×10→刷新→实验室紫色跟随→"
            "双芯片→回瞭望露台→荣誉兑换→首次重连后扭蛋99999次",
            "SYSTEM",
        )
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_happy_valley(self):
        tasks = {
            "happy_valley_daily": True,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self._auto_start_rotation_after_daily = False
        self.log_message("🎡 启动欢乐谷日常", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_new_daily(self):
        variant = self._current_new_daily_variant()
        if self._is_daily_special_start(variant):
            self._start_daily_special_mode(variant)
            return
        start_step = self._current_new_daily_start_step()
        include_tower = self.chk_daily_hero_tower.isChecked()
        tasks = {
            "new_daily_mode": True,
            "new_daily_variant": variant,
            "new_daily_start_step": start_step,
            "use_foreground": self.chk_foreground.isChecked(),
            "daily_include_hero_tower": include_tower,
        }
        if variant == "9":
            self._auto_start_rotation_after_daily = True
        if start_step > 1:
            self.log_message(
                f"📋 启动新日常方案 {variant}（从第 {start_step} 步）",
                "SYSTEM",
            )
        else:
            self.log_message(f"📋 启动新日常方案 {variant}", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_hero_tower(self):
        """执行勇者之塔（默认场次由引擎常量决定，当前为 2 场）"""
        tasks = {
            "hero_tower": True,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self.log_message(
            f"🗼 启动勇者之塔：{DEFAULT_HERO_TOWER_BATTLES}场（map102 五分钟保护）",
            "SYSTEM",
        )
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_1v1_chaos_rotation(self):
        """大乱斗×2 → 轮换/自定义"""
        tasks = self._build_rotation_mode_tasks()
        tasks["chaos_rotation_chain"] = True
        self.log_message(
            "⚔ 启动大乱斗×2 + 轮换/自定义（每场 30 分钟保护）",
            "SYSTEM",
        )
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_shanni_energy(self):
        tasks = {
            "shanni_energy_drain": True,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self.log_message("⚡ 启动闪尼吸能：删除 SWF 89、90，跳过前置重连，开局先切图", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_master_cup(self):
        master_cup_settings = self._current_master_cup_settings()
        cup_type = str(master_cup_settings["master_cup_type"])
        yellow_target = int(master_cup_settings["master_cup_yellow_target"])
        tasks = dict(self._build_rotation_mode_tasks())
        tasks.pop("rotation_mode", None)
        tasks.update({
            "master_cup_mode": True,
            **master_cup_settings,
            "master_cup_then_rotation": True,
            "use_foreground": self.chk_foreground.isChecked(),
        })
        pre_text = "；含前置" if tasks.get("master_cup_pre_setup") else ""
        norm_text = "；固定击败568十次" if cup_type == "诺姆" else ""
        self.log_message(f"🏆 启动大师杯：{cup_type}（还需黄胜 {yellow_target} 次后重连接轮换{pre_text}{norm_text}）", "SYSTEM")
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

    def on_run_psychic_exp_purple(self):
        exp_cfg = {}
        if hasattr(self, "combo_psychic_exp_type"):
            exp_cfg = self.combo_psychic_exp_type.currentData() or {}
        category_key = exp_cfg.get("category", "精灵仓库.超能系")
        exp_value = exp_cfg.get("value", "5820")
        exp_label = exp_cfg.get("label", "超能经验")
        tasks = {
            "psychic_exp_purple_mode": True,
            "psychic_exp_category_key": category_key,
            "psychic_exp_value": exp_value,
            "psychic_exp_label": exp_label,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self.log_message(f"[{exp_label}紫] 启动：仓库从前到后续扫，输入={exp_value}", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_nono_soul_fusion(self):
        tasks = {
            "nono_soul_fusion_check": True,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self.log_message("🧪 启动nono孵化检查", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_hatch_start(self):
        tasks = {
            "hatch_start_mode": True,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        self.log_message("🐣 启动孵化开始", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def on_run_collection_daily(self):
        self._start_daily_special_mode("preselect")

    def on_run_fusion_mode(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("融合模式")
        layout = QGridLayout(dialog)
        last_fusion_config = getattr(self, "_last_fusion_mode_config", {}) or {}

        scheme_combo = QComboBox(dialog)
        scheme_combo.addItem(
            "卡鲁耶克",
            userData={
                "scheme": "kalu",
                "label": "卡鲁耶克",
                "primary_category": "精灵仓库.飞行系",
                "primary_color": "purple",
                "secondary_category": "精灵仓库.超能系",
                "secondary_color": "cyan",
                "normal_soulbead": "1000008",
                "default_sequence": [3, 3, 3, 3],
            },
        )
        scheme_combo.addItem(
            "希格瑞特",
            userData={
                "scheme": "xigeruite",
                "label": "希格瑞特",
                "primary_category": "精灵仓库.暗影系",
                "primary_color": "purple",
                "secondary_category": "精灵仓库.草系",
                "secondary_color": "cyan",
                "normal_soulbead": "1000015",
                "default_sequence": [2, 2, 2, 2],
            },
        )
        last_scheme_key = str(last_fusion_config.get("scheme") or "")
        if last_scheme_key:
            for idx in range(scheme_combo.count()):
                data = scheme_combo.itemData(idx) or {}
                if data.get("scheme") == last_scheme_key:
                    scheme_combo.setCurrentIndex(idx)
                    break
        layout.addWidget(QLabel("方案", dialog), 0, 0)
        layout.addWidget(scheme_combo, 1, 0)

        resource_boxes = []
        for idx in range(4):
            box = QLineEdit(dialog)
            box.setFixedWidth(48)
            box.setValidator(QIntValidator(1, 24, dialog))
            resource_boxes.append(box)
            layout.addWidget(QLabel(f"资源{idx + 1}", dialog), 0, idx + 1)
            layout.addWidget(box, 1, idx + 1)

        pink_target_box = QLineEdit(dialog)
        pink_target_box.setFixedWidth(58)
        pink_target_box.setValidator(QIntValidator(1, 999, dialog))
        fusion_limit_box = QLineEdit(dialog)
        fusion_limit_box.setPlaceholderText("不限")
        fusion_limit_box.setFixedWidth(66)
        fusion_limit_box.setValidator(QIntValidator(1, 999999, dialog))
        layout.addWidget(QLabel("粉色数", dialog), 0, 5)
        layout.addWidget(pink_target_box, 1, 5)
        layout.addWidget(QLabel("融合次数", dialog), 0, 6)
        layout.addWidget(fusion_limit_box, 1, 6)

        def _apply_fusion_form_defaults(config=None):
            config = config or {}
            scheme_data = scheme_combo.currentData() or {}
            default_sequence = scheme_data.get("default_sequence") or [3, 3, 3, 3]
            sequence = config.get("sequence") or default_sequence
            for idx, box in enumerate(resource_boxes):
                try:
                    value = int(sequence[idx])
                except (TypeError, ValueError, IndexError):
                    try:
                        value = int(default_sequence[idx])
                    except (TypeError, ValueError, IndexError):
                        value = 3
                box.setText(str(max(1, min(24, value))))
            pink_target_box.setText(str(config.get("pink_target") or 4))
            fusion_limit_box.clear()
            fusion_limit = int(config.get("fusion_limit") or 0)
            if fusion_limit > 0:
                fusion_limit_box.setText(str(fusion_limit))

        def _on_scheme_changed(_index):
            _apply_fusion_form_defaults({})

        _apply_fusion_form_defaults(last_fusion_config)
        scheme_combo.currentIndexChanged.connect(_on_scheme_changed)

        btn_exec = QPushButton("执行", dialog)
        btn_cancel = QPushButton("取消", dialog)
        layout.addWidget(btn_exec, 1, 7)
        layout.addWidget(btn_cancel, 1, 8)

        selected = {}

        def _accept_fusion():
            seq = []
            for box in resource_boxes:
                raw = (box.text() or "").strip()
                try:
                    value = int(raw or "3")
                    if not 1 <= value <= 24:
                        raise ValueError()
                except ValueError:
                    self.log_message("❌ 融合资源编号必须是 1-24", "ERROR")
                    return
                seq.append(value)
            selected["scheme"] = scheme_combo.currentData() or {}
            selected["sequence"] = seq
            try:
                selected["pink_target"] = max(1, min(999, int((pink_target_box.text() or "4").strip() or "4")))
                raw_limit = (fusion_limit_box.text() or "").strip()
                selected["fusion_limit"] = max(1, min(999999, int(raw_limit))) if raw_limit else 0
            except ValueError:
                self.log_message("❌ 融合停止条件必须是正整数", "ERROR")
                return
            self._last_fusion_mode_config = {
                "scheme": (selected["scheme"].get("scheme") or "kalu"),
                "sequence": list(seq),
                "pink_target": selected["pink_target"],
                "fusion_limit": selected["fusion_limit"],
            }
            dialog.accept()

        btn_exec.clicked.connect(_accept_fusion)
        btn_cancel.clicked.connect(dialog.reject)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        scheme = selected.get("scheme") or {}
        sequence = selected.get("sequence") or [3, 3, 3, 3]
        pink_target = selected.get("pink_target", 4)
        fusion_limit = selected.get("fusion_limit", 0)
        label = scheme.get("label", "卡鲁耶克")
        tasks = {
            "fusion_mode": True,
            "fusion_then_rotation": True,
            "fusion_scheme": scheme.get("scheme", "kalu"),
            "fusion_label": label,
            "fusion_primary_category": scheme.get("primary_category", "精灵仓库.飞行系"),
            "fusion_primary_color": scheme.get("primary_color", "purple"),
            "fusion_secondary_category": scheme.get("secondary_category", "精灵仓库.超能系"),
            "fusion_secondary_color": scheme.get("secondary_color", "cyan"),
            "fusion_normal_soulbead": scheme.get("normal_soulbead", "1000008"),
            "fusion_sequence": sequence,
            "fusion_pink_target": pink_target,
            "fusion_limit": fusion_limit,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        rotation_tasks = dict(self._build_rotation_mode_tasks())
        rotation_tasks.pop("rotation_mode", None)
        tasks.update(rotation_tasks)
        seq_text = "-".join(str(v) for v in sequence)
        limit_text = str(fusion_limit) if fusion_limit else "不限"
        self.log_message(
            f"🧬 启动融合模式：{label}（资源序列 {seq_text}，仓库自动翻到底，粉色目标={pink_target}，融合次数={limit_text}）",
            "SYSTEM",
        )
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
        options = list_rare_select_options(self._project_root())
        options.sort(key=lambda item: 0 if (item[1] == "乌索" or "乌索" in item[0]) else 1)
        for label, key in options:
            combo.addItem(label, userData=key)
        pick = prev or default_key
        if pick:
            idx = combo.findData(pick)
            if idx >= 0:
                combo.setCurrentIndex(idx)

    def _load_rotation_resource_modes(self):
        """白天模式下拉：资源模式 + 全部野外稀有模式。"""
        if not hasattr(self, "rotation_resource_combo"):
            return
        from core.rotation_schedule import ROTATION_RESOURCE_LABELS, ROTATION_RESOURCE_SLUGS
        from core.nieo_mode_registry import list_nieo_select_options
        from core.wild_mode_registry import list_rare_select_options

        prev = self.rotation_resource_combo.currentData() if self.rotation_resource_combo.count() else None
        self.rotation_resource_combo.clear()
        rare_options = list_rare_select_options(self._project_root())
        rare_options.sort(key=lambda item: 0 if (item[1] == "乌索" or "乌索" in item[0]) else 1)
        for label, key in rare_options:
            if key == "乌索" or "乌索" in label:
                self.rotation_resource_combo.addItem(f"稀有｜{label}", f"rare:{key}")
        static_resource_slugs = set(ROTATION_RESOURCE_SLUGS)
        for label, slug in zip(ROTATION_RESOURCE_LABELS, ROTATION_RESOURCE_SLUGS):
            self.rotation_resource_combo.addItem(f"资源｜{label}", slug)
        for label, slug in list_nieo_select_options(self._project_root()):
            if slug in static_resource_slugs:
                continue
            self.rotation_resource_combo.addItem(f"资源｜{label}", slug)
        for label, key in rare_options:
            if key == "乌索" or "乌索" in label:
                continue
            self.rotation_resource_combo.addItem(f"稀有｜{label}", f"rare:{key}")
        idx = self.rotation_resource_combo.findData(prev or "rare:乌索")
        if idx >= 0:
            self.rotation_resource_combo.setCurrentIndex(idx)

    def _update_rotation_resource_combo_enabled(self):
        if hasattr(self, "rotation_resource_combo") and hasattr(self, "chk_rotation_resource"):
            self.rotation_resource_combo.setEnabled(self.chk_rotation_resource.isChecked())

    def _update_rotation_single_map_option_state(self) -> None:
        if not hasattr(self, "chk_rotation_nieo_single_map_escape"):
            return
        slug = ""
        if hasattr(self, "rotation_resource_combo"):
            slug = str(self.rotation_resource_combo.currentData() or "").strip()
        force_single_map = slug == "水生海草"
        if force_single_map:
            self.chk_rotation_nieo_single_map_escape.setChecked(True)
        self.chk_rotation_nieo_single_map_escape.setEnabled(not force_single_map)
        self.chk_rotation_nieo_single_map_escape.setToolTip(
            "水生海草固定为单图模式，不能取消。"
            if force_single_map
            else (
                "纯净能量与配置型资源按此开关执行；单图会避开上次选点。"
                "内置尼奥仍由时段固定：08:00-10:00 双图逃跑，10:00-12:00 单图逃跑。"
            )
        )

    def _update_rotation_schedule_hint(self):
        if not hasattr(self, "rotation_schedule_info_label"):
            return
        from core.rotation_schedule import RotationScheduleOptions, describe_rotation_day

        rare_slot = "shuangta"
        if hasattr(self, "rotation_rare_combo"):
            rare_slot = self.rotation_rare_combo.currentData() or "shuangta"
        opts = RotationScheduleOptions(
            resource_enabled=bool(self.chk_rotation_resource.isChecked()),
            resource_slug=self.rotation_resource_combo.currentData() or "rare:乌索",
            mantis_enabled=bool(self.chk_rotation_mantis.isChecked()),
            eit_enabled=bool(self.chk_rotation_eit.isChecked()),
        )
        self.rotation_schedule_info_label.setText(
            "当前日程：" + describe_rotation_day(opts, rare_slot=rare_slot)
        )

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
        combo.addItem("纯净能量（map=26/27）", "pure_energy")
        for label, key in list_nieo_select_options(self._project_root()):
            combo.addItem(label, userData=key)
        pick = prev or default_key or "nieo"
        idx = combo.findData(pick)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _load_event_pet_modes(self):
        """活动精灵下拉：assets/event_pet_modes。"""
        if not hasattr(self, "combo_event_pet"):
            return
        self.combo_event_pet.clear()
        try:
            from core.event_pet_mode_registry import list_event_pet_select_options

            for label, slug in list_event_pet_select_options(self._project_root()):
                self.combo_event_pet.addItem(label, userData=slug)
            if self.combo_event_pet.count() == 0:
                self.combo_event_pet.addItem("伊特(471)（map A=414）", userData="yite")
        except Exception as e:
            self.log_message(f"⚠ 加载活动精灵模式失败: {e}", "WARN")
            self.combo_event_pet.addItem("伊特(471)", userData="yite")

    def _load_nieo_modes(self):
        """尼奥下拉：内置 + assets/nieo_modes。"""
        if not hasattr(self, "combo_nieo_sub"):
            return
        try:
            self._fill_nieo_select_combo(self.combo_nieo_sub, default_key="nieo")
        except Exception as e:
            self.log_message(f"⚠ 加载自定义尼奥模式失败: {e}", "WARN")

    def _update_nieo_yellow60_to_rotation_enabled(self) -> None:
        if not hasattr(self, "chk_nieo_yellow60_to_rotation"):
            return
        sub = "nieo"
        if hasattr(self, "combo_nieo_sub"):
            sub = str(self.combo_nieo_sub.currentData() or "nieo").strip()
        is_resource_mode = sub != "nieo"
        self.chk_nieo_yellow60_to_rotation.setEnabled(is_resource_mode)
        if sub == "水之精华":
            self.chk_nieo_yellow60_to_rotation.setChecked(True)

    def _update_nieo_single_map_option_state(self) -> None:
        if not hasattr(self, "chk_nieo_single_map_escape"):
            return
        sub = "nieo"
        if hasattr(self, "combo_nieo_sub"):
            sub = str(self.combo_nieo_sub.currentData() or "nieo").strip()
        force_single_map = sub == "水生海草"
        if force_single_map:
            self.chk_nieo_single_map_escape.setChecked(True)
        self.chk_nieo_single_map_escape.setEnabled(not force_single_map)
        self.chk_nieo_single_map_escape.setToolTip(
            "水生海草固定为单图模式，不能取消。"
            if force_single_map
            else (
                "内置尼奥固定普通逃跑：08:00-10:00 强制双图，10:00-12:00 强制单图。"
                "其他时段及纯净能量/自定义资源按此开关执行；单图会避开上一轮选点。"
            )
        )

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

    def on_open_region_viewer(self):
        """打开区域显示器（截图 + 区域红点叠加）"""
        import subprocess
        import os
        import sys

        viewer_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tools",
            "region_viewer.py",
        )
        try:
            env = os.environ.copy()
            env.setdefault("PYTHONIOENCODING", "utf-8")
            command = [sys.executable, viewer_path]
            inherited_viewport = None
            if window_manager.find_window():
                inherited_viewport = window_manager.get_current_viewport()
            if inherited_viewport:
                command.extend(
                    [
                        "--viewport",
                        *(f"{float(value):.12g}" for value in inherited_viewport),
                        "--viewport-hwnd",
                        str(int(getattr(window_manager, "hwnd", 0) or 0)),
                    ]
                )
            subprocess.Popen(
                command,
                creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
                env=env,
            )
            viewport_note = "（已同步当前扫描视口）" if inherited_viewport else ""
            self.log_message(f"🔴 区域显示器已启动{viewport_note}", "SYSTEM")
        except Exception as e:
            self.log_message(f"❌ 启动区域显示器失败: {e}", "ERROR")

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
        if mode not in ("leiyi", "dudukala", "laokemengde"):
            mode = "leiyi"

        loop_count = 10
        if mode != "dudukala":
            try:
                loop_txt = (self.leiyi_loop_box.text() or "").strip()
                loop_count = 10 if loop_txt == "" else int(loop_txt)
                if loop_count <= 0:
                    raise ValueError()
                if loop_count > 999:
                    loop_count = 999
            except Exception:
                self.log_message("⚠ 对战训练：循环次数需为 1~999 的整数", "ERROR")
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
        mode_label = {
            "leiyi": "雷伊特训",
            "dudukala": "嘟嘟卡拉",
            "laokemengde": "劳克蒙德",
        }[mode]
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

    def on_export_logs(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        stamp = time.strftime("%Y%m%d_%H%M%S")
        export_dir = os.path.join(project_root, "log", "exports", stamp)
        files_dir = os.path.join(export_dir, "files")
        try:
            os.makedirs(files_dir, exist_ok=True)

            dashboard_text = self.log_box.toPlainText() if hasattr(self, "log_box") else ""
            try:
                kernel_window_text = self.kernel_log_window.log_box.toPlainText()
            except Exception:
                kernel_window_text = ""

            with open(os.path.join(export_dir, "dashboard_visible_log.txt"), "w", encoding="utf-8") as f:
                f.write(dashboard_text)
                if dashboard_text and not dashboard_text.endswith("\n"):
                    f.write("\n")

            with open(os.path.join(export_dir, "kernel_window_log.txt"), "w", encoding="utf-8") as f:
                f.write(kernel_window_text)
                if kernel_window_text and not kernel_window_text.endswith("\n"):
                    f.write("\n")

            rows = fetch_kernel_since(0, return_rows=True)
            with open(os.path.join(export_dir, "kernel_buffer_log.txt"), "w", encoding="utf-8") as f:
                for seq, ts, line in rows:
                    f.write(f"{seq}\t{line} [ts={self._format_log_timestamp(ts)}]\n")

            log_root = os.path.join(project_root, "log")
            copied = 0
            if os.path.isdir(log_root):
                for root, dirs, files in os.walk(log_root):
                    rel_root = os.path.relpath(root, log_root)
                    if rel_root == "exports" or rel_root.startswith("exports" + os.sep):
                        dirs[:] = []
                        continue
                    for name in files:
                        src = os.path.join(root, name)
                        rel = name if rel_root == "." else os.path.join(rel_root, name)
                        dst = os.path.join(files_dir, rel)
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        try:
                            shutil.copy2(src, dst)
                            copied += 1
                        except OSError as e:
                            with open(os.path.join(export_dir, "copy_errors.txt"), "a", encoding="utf-8") as f:
                                f.write(f"{src}: {e}\n")

            with open(os.path.join(export_dir, "README.txt"), "w", encoding="utf-8") as f:
                f.write("NieoPilot 日志导出\n")
                f.write(f"导出时间: {stamp}\n")
                f.write("dashboard_visible_log.txt: Dashboard 明面日志\n")
                f.write("kernel_window_log.txt: 内核日志窗口当前内容\n")
                f.write("kernel_buffer_log.txt: 运行中内核日志缓存\n")
                f.write("files/: log 目录文件副本（跳过 exports 自身）\n")
                f.write(f"复制文件数: {copied}\n")

            self.log_message(f"✅ 日志已导出：{export_dir}", "SUCCESS")
        except Exception as e:
            self.log_message(f"❌ 导出日志失败: {e}", "ERROR")

    # ------------------ UI状态 ------------------
    def _lock_ui_except_scheduled(self):
        """锁定UI但保持定时任务UI可用（允许定时任务与其他任务共存）"""
        if hasattr(self, "btn_new_daily"):
            self.btn_new_daily.setEnabled(False)
        if hasattr(self, "btn_pre_daily"):
            self.btn_pre_daily.setEnabled(False)
        if hasattr(self, "btn_lanlan"):
            self.btn_lanlan.setEnabled(False)
        if hasattr(self, "btn_yilu"):
            self.btn_yilu.setEnabled(False)
        if hasattr(self, "btn_light_mantis"):
            self.btn_light_mantis.setEnabled(False)
        if hasattr(self, "btn_one_click_release"):
            self.btn_one_click_release.setEnabled(False)
        if hasattr(self, "btn_chip_gold_honor"):
            self.btn_chip_gold_honor.setEnabled(False)
        if hasattr(self, "combo_bag_test"):
            self.combo_bag_test.setEnabled(False)
        if hasattr(self, "btn_bag_test"):
            self.btn_bag_test.setEnabled(False)
        if hasattr(self, "combo_new_daily"):
            self.combo_new_daily.setEnabled(False)
        if hasattr(self, "new_daily_start_step_box"):
            self.new_daily_start_step_box.setEnabled(False)
        if hasattr(self, "chk_skip_daily_exp_input"):
            self.chk_skip_daily_exp_input.setEnabled(False)
        self.btn_run_daily.setEnabled(False)
        self.btn_run_task.setEnabled(False)
        self.script_combo.setEnabled(False)
        self.task_repeat_box.setEnabled(False)
        self.btn_hero_tower.setEnabled(False)
        self.btn_1v1_chaos_rotation.setEnabled(False)
        if hasattr(self, "btn_shanni_energy"):
            self.btn_shanni_energy.setEnabled(False)
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
        if hasattr(self, "btn_psychic_exp_purple"):
            self.btn_psychic_exp_purple.setEnabled(False)
        if hasattr(self, "combo_psychic_exp_type"):
            self.combo_psychic_exp_type.setEnabled(False)
        if hasattr(self, "psychic_exp_pages_box"):
            self.psychic_exp_pages_box.setEnabled(False)
        if hasattr(self, "btn_fusion_mode"):
            self.btn_fusion_mode.setEnabled(False)
        if hasattr(self, "btn_nono_soul_fusion"):
            self.btn_nono_soul_fusion.setEnabled(False)
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
        if hasattr(self, "btn_refresh_trinity"):
            self.btn_refresh_trinity.setEnabled(False)
        if hasattr(self, "btn_pure_refresh"):
            self.btn_pure_refresh.setEnabled(False)
        if hasattr(self, "btn_master_cup"):
            self.btn_master_cup.setEnabled(False)
        if hasattr(self, "combo_master_cup"):
            self.combo_master_cup.setEnabled(False)
        if hasattr(self, "master_cup_yellow_target_box"):
            self.master_cup_yellow_target_box.setEnabled(False)
        if hasattr(self, "chk_master_cup_pre_setup"):
            self.chk_master_cup_pre_setup.setEnabled(False)
        if hasattr(self, "chk_resist_drain_logic"):
            self.chk_resist_drain_logic.setEnabled(False)
        if hasattr(self, "chk_rotation_nieo_single_map_escape"):
            self.chk_rotation_nieo_single_map_escape.setEnabled(False)
        if hasattr(self, "chk_rotation_nieo_follow_cyan"):
            self.chk_rotation_nieo_follow_cyan.setEnabled(False)
        # 录制器按钮保持可用（它们独立运行）
        # 定时任务UI保持可用（允许与其他任务共存）
        # 不锁定定时任务相关UI
    
    def _lock_ui(self):
        if hasattr(self, "btn_new_daily"):
            self.btn_new_daily.setEnabled(False)
        if hasattr(self, "btn_pre_daily"):
            self.btn_pre_daily.setEnabled(False)
        if hasattr(self, "btn_lanlan"):
            self.btn_lanlan.setEnabled(False)
        if hasattr(self, "btn_yilu"):
            self.btn_yilu.setEnabled(False)
        if hasattr(self, "btn_light_mantis"):
            self.btn_light_mantis.setEnabled(False)
        if hasattr(self, "btn_one_click_release"):
            self.btn_one_click_release.setEnabled(False)
        if hasattr(self, "btn_chip_gold_honor"):
            self.btn_chip_gold_honor.setEnabled(False)
        if hasattr(self, "combo_bag_test"):
            self.combo_bag_test.setEnabled(False)
        if hasattr(self, "btn_bag_test"):
            self.btn_bag_test.setEnabled(False)
        if hasattr(self, "combo_new_daily"):
            self.combo_new_daily.setEnabled(False)
        if hasattr(self, "new_daily_start_step_box"):
            self.new_daily_start_step_box.setEnabled(False)
        if hasattr(self, "chk_skip_daily_exp_input"):
            self.chk_skip_daily_exp_input.setEnabled(False)
        self.btn_run_daily.setEnabled(False)
        self.btn_run_task.setEnabled(False)
        self.script_combo.setEnabled(False)
        self.task_repeat_box.setEnabled(False)
        self.btn_hero_tower.setEnabled(False)
        self.btn_1v1_chaos_rotation.setEnabled(False)
        if hasattr(self, "btn_shanni_energy"):
            self.btn_shanni_energy.setEnabled(False)
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
        if hasattr(self, "btn_psychic_exp_purple"):
            self.btn_psychic_exp_purple.setEnabled(False)
        if hasattr(self, "combo_psychic_exp_type"):
            self.combo_psychic_exp_type.setEnabled(False)
        if hasattr(self, "psychic_exp_pages_box"):
            self.psychic_exp_pages_box.setEnabled(False)
        if hasattr(self, "btn_fusion_mode"):
            self.btn_fusion_mode.setEnabled(False)
        if hasattr(self, "btn_nono_soul_fusion"):
            self.btn_nono_soul_fusion.setEnabled(False)
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
        if hasattr(self, "btn_refresh_trinity"):
            self.btn_refresh_trinity.setEnabled(False)
        if hasattr(self, "btn_pure_refresh"):
            self.btn_pure_refresh.setEnabled(False)
        if hasattr(self, "btn_master_cup"):
            self.btn_master_cup.setEnabled(False)
        if hasattr(self, "combo_master_cup"):
            self.combo_master_cup.setEnabled(False)
        if hasattr(self, "master_cup_yellow_target_box"):
            self.master_cup_yellow_target_box.setEnabled(False)
        if hasattr(self, "chk_master_cup_pre_setup"):
            self.chk_master_cup_pre_setup.setEnabled(False)
        # ✅ 禁用轮换模式和尼奥模式的按钮和勾选框
        if hasattr(self, "btn_start_rotation"):
            self.btn_start_rotation.setEnabled(False)
        if hasattr(self, "btn_rotation_reconnect"):
            self.btn_rotation_reconnect.setEnabled(False)
        if hasattr(self, "btn_rotation_chain_test"):
            self.btn_rotation_chain_test.setEnabled(False)
        if hasattr(self, "rotation_rare_combo"):
            self.rotation_rare_combo.setEnabled(False)
        if hasattr(self, "chk_rotation_resource"):
            self.chk_rotation_resource.setEnabled(False)
        if hasattr(self, "rotation_resource_combo"):
            self.rotation_resource_combo.setEnabled(False)
        if hasattr(self, "chk_rotation_eit"):
            self.chk_rotation_eit.setEnabled(False)
        if hasattr(self, "chk_rotation_mantis"):
            self.chk_rotation_mantis.setEnabled(False)
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
        if hasattr(self, "chk_resist_drain_logic"):
            self.chk_resist_drain_logic.setEnabled(False)
        if hasattr(self, "chk_rotation_nieo_single_map_escape"):
            self.chk_rotation_nieo_single_map_escape.setEnabled(False)
        if hasattr(self, "chk_rotation_nieo_follow_cyan"):
            self.chk_rotation_nieo_follow_cyan.setEnabled(False)
        
        if hasattr(self, "btn_nieo"):
            self.btn_nieo.setEnabled(False)
        if hasattr(self, "btn_nieo_resource_chain"):
            self.btn_nieo_resource_chain.setEnabled(False)
        if hasattr(self, "combo_nieo_sub"):
            self.combo_nieo_sub.setEnabled(False)
        if hasattr(self, "chk_nieo_skip_pre_rotation"):
            self.chk_nieo_skip_pre_rotation.setEnabled(False)
        if hasattr(self, "chk_nieo_test_force_switch"):
            self.chk_nieo_test_force_switch.setEnabled(False)
        if hasattr(self, "chk_nieo_single_map_escape"):
            self.chk_nieo_single_map_escape.setEnabled(False)
        if hasattr(self, "chk_nieo_follow_cyan"):
            self.chk_nieo_follow_cyan.setEnabled(False)
        if hasattr(self, "chk_nieo_yellow60_to_rotation"):
            self.chk_nieo_yellow60_to_rotation.setEnabled(False)
        for _b in ("btn_afk_normal", "btn_afk_defeat", "btn_afk_rare", "btn_afk_nieo", "btn_event_pet"):
            if hasattr(self, _b):
                getattr(self, _b).setEnabled(False)
        for _b in ("btn_pinnacle_rank", "btn_pinnacle_fun"):
            if hasattr(self, _b):
                getattr(self, _b).setEnabled(False)
        if hasattr(self, "chk_pinnacle_small_account_mode"):
            self.chk_pinnacle_small_account_mode.setEnabled(False)
        
        # 录制器按钮保持可用（它们独立运行）


    @pyqtSlot()
    def _unlock_ui_stopped(self):
        if hasattr(self, "btn_new_daily"):
            self.btn_new_daily.setEnabled(True)
        if hasattr(self, "btn_pre_daily"):
            self.btn_pre_daily.setEnabled(True)
        if hasattr(self, "btn_lanlan"):
            self.btn_lanlan.setEnabled(True)
        if hasattr(self, "btn_yilu"):
            self.btn_yilu.setEnabled(True)
        if hasattr(self, "btn_light_mantis"):
            self.btn_light_mantis.setEnabled(True)
        if hasattr(self, "btn_one_click_release"):
            self.btn_one_click_release.setEnabled(True)
        if hasattr(self, "btn_chip_gold_honor"):
            self.btn_chip_gold_honor.setEnabled(True)
        if hasattr(self, "combo_bag_test"):
            self.combo_bag_test.setEnabled(True)
        if hasattr(self, "btn_bag_test"):
            self.btn_bag_test.setEnabled(True)
        if hasattr(self, "combo_new_daily"):
            self.combo_new_daily.setEnabled(True)
        if hasattr(self, "new_daily_start_step_box"):
            self.new_daily_start_step_box.setEnabled(True)
        if hasattr(self, "chk_skip_daily_exp_input"):
            self.chk_skip_daily_exp_input.setEnabled(True)
        self.btn_run_daily.setEnabled(True)
        self.btn_run_task.setEnabled(True)
        self.script_combo.setEnabled(True)
        self.task_repeat_box.setEnabled(True)
        self.btn_hero_tower.setEnabled(True)
        self.btn_1v1_chaos_rotation.setEnabled(True)
        if hasattr(self, "btn_shanni_energy"):
            self.btn_shanni_energy.setEnabled(True)
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
        if hasattr(self, "btn_psychic_exp_purple"):
            self.btn_psychic_exp_purple.setEnabled(True)
        if hasattr(self, "combo_psychic_exp_type"):
            self.combo_psychic_exp_type.setEnabled(True)
        if hasattr(self, "psychic_exp_pages_box"):
            self.psychic_exp_pages_box.setEnabled(True)
        if hasattr(self, "btn_fusion_mode"):
            self.btn_fusion_mode.setEnabled(True)
        if hasattr(self, "btn_nono_soul_fusion"):
            self.btn_nono_soul_fusion.setEnabled(True)
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
        if hasattr(self, "btn_refresh_trinity"):
            self.btn_refresh_trinity.setEnabled(True)
        if hasattr(self, "btn_pure_refresh"):
            self.btn_pure_refresh.setEnabled(True)
        if hasattr(self, "btn_master_cup"):
            self.btn_master_cup.setEnabled(True)
        if hasattr(self, "combo_master_cup"):
            self.combo_master_cup.setEnabled(True)
        if hasattr(self, "master_cup_yellow_target_box"):
            self.master_cup_yellow_target_box.setEnabled(True)
        if hasattr(self, "chk_master_cup_pre_setup"):
            self.chk_master_cup_pre_setup.setEnabled(True)
        if hasattr(self, "_update_master_cup_target_for_type"):
            self._update_master_cup_target_for_type()
        # 一键日常结束后自动启动轮换；内部失败/超时也交接，只有手动停止不交接。
        handoff_rotation = False
        bot = getattr(self, "bot", None)
        if self._auto_start_rotation_after_daily:
            self._auto_start_rotation_after_daily = False
            if bot is not None and getattr(bot, "user_stop_requested", False):
                self.log_message("⛔ 一键日常已手动停止，不自动启动轮换模式", "WARN")
            else:
                handoff_rotation = True
                self._auto_rotation_handoff_daily_completed = bool(
                    bot is not None
                    and getattr(bot, "new_daily_chain_completed", False)
                )
            if bot is not None:
                try:
                    bot.new_daily_chain_completed = False
                except Exception:
                    pass
        if bot is not None and getattr(bot, "rotation_handoff_after_chaos_timeout", False):
            try:
                bot.rotation_handoff_after_chaos_timeout = False
            except Exception:
                pass
            if not handoff_rotation and not getattr(bot, "user_stop_requested", False):
                handoff_rotation = True
                self._auto_rotation_handoff_daily_completed = None

        if handoff_rotation:
            # 等待1秒后自动启动轮换模式
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(1000, self._auto_start_rotation_mode)
            # 不解锁轮换模式和尼奥模式的UI，保持锁定状态
        else:
            # ✅ 只有在不需要自动启动时，才重新启用轮换模式和尼奥模式的按钮和勾选框
            if hasattr(self, "btn_start_rotation"):
                self.btn_start_rotation.setEnabled(True)
            if hasattr(self, "btn_rotation_reconnect"):
                self.btn_rotation_reconnect.setEnabled(True)
            if hasattr(self, "btn_rotation_chain_test"):
                self.btn_rotation_chain_test.setEnabled(True)
            if hasattr(self, "rotation_rare_combo"):
                self.rotation_rare_combo.setEnabled(True)
            if hasattr(self, "chk_rotation_resource"):
                self.chk_rotation_resource.setEnabled(True)
            if hasattr(self, "_update_rotation_resource_combo_enabled"):
                self._update_rotation_resource_combo_enabled()
            if hasattr(self, "chk_rotation_eit"):
                self.chk_rotation_eit.setEnabled(True)
            if hasattr(self, "chk_rotation_mantis"):
                self.chk_rotation_mantis.setEnabled(True)
            if hasattr(self, "chk_rotation_test_mode"):
                self.chk_rotation_test_mode.setEnabled(True)
                # 根据测试模式复选框状态更新输入框状态
                if hasattr(self, "_update_rotation_test_inputs_enabled"):
                    self._update_rotation_test_inputs_enabled()
            if hasattr(self, "chk_resist_drain_logic"):
                self.chk_resist_drain_logic.setEnabled(True)
            if hasattr(self, "chk_rotation_nieo_single_map_escape"):
                self.chk_rotation_nieo_single_map_escape.setEnabled(True)
            if hasattr(self, "chk_rotation_nieo_follow_cyan"):
                self.chk_rotation_nieo_follow_cyan.setEnabled(True)
            if hasattr(self, "combo_cap_tier"):
                self.combo_cap_tier.setEnabled(True)
            if hasattr(self, "_capsule_tier_label"):
                self._capsule_tier_label.setEnabled(True)
            
            if hasattr(self, "btn_nieo"):
                self.btn_nieo.setEnabled(True)
            if hasattr(self, "btn_nieo_resource_chain"):
                self.btn_nieo_resource_chain.setEnabled(True)
            if hasattr(self, "combo_nieo_sub"):
                self.combo_nieo_sub.setEnabled(True)
            if hasattr(self, "chk_nieo_skip_pre_rotation"):
                self.chk_nieo_skip_pre_rotation.setEnabled(True)
            if hasattr(self, "chk_nieo_test_force_switch"):
                self.chk_nieo_test_force_switch.setEnabled(True)
            if hasattr(self, "chk_nieo_single_map_escape"):
                self.chk_nieo_single_map_escape.setEnabled(True)
            if hasattr(self, "chk_nieo_follow_cyan"):
                self.chk_nieo_follow_cyan.setEnabled(True)
            if hasattr(self, "_update_nieo_yellow60_to_rotation_enabled"):
                self._update_nieo_yellow60_to_rotation_enabled()
            if hasattr(self, "_update_nieo_single_map_option_state"):
                self._update_nieo_single_map_option_state()
            if hasattr(self, "_update_rotation_single_map_option_state"):
                self._update_rotation_single_map_option_state()
            for _b in ("btn_afk_normal", "btn_afk_defeat", "btn_afk_rare", "btn_afk_nieo", "btn_event_pet"):
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
            Q_ARG(str, self._append_log_timestamp(f"[{level}] {text}"))
        )
    
    def on_clear_log(self):
        """清空日志"""
        self.log_box.clear()
        self.log_message("日志已清空", "SYSTEM")

    def _capsule_task_kv(self) -> dict:
        """螳螂遇野生 122 首回合无敌；其余捕捉投掷由本下拉（默认仅高级 / 循环备选）决定。"""
        tier = "high"
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
    
    def _build_rotation_mode_tasks(self, *, full_daily_maintenance: bool = False) -> dict:
        """Build rotation tasks; full daily maintenance is only for the test entry."""
        is_test_mode = self.chk_rotation_test_mode.isChecked()
        interval_minutes_nieo = self._parse_float_with_default(
            self.rotation_interval_minutes_nieo_input.text(), 60.0
        )
        interval_minutes_shuangta = self._parse_float_with_default(
            self.rotation_interval_minutes_shuangta_input.text(), 60.0
        )
        hard_limit_sec = self._parse_float_with_default(
            self.petswf_hard_limit_sec_input.text(), 8.5
        )
        rare_slot = "shuangta"
        if hasattr(self, "rotation_rare_combo"):
            rare_slot = self.rotation_rare_combo.currentData() or "shuangta"
        rotation_resource_slug = (
            self.rotation_resource_combo.currentData()
            if hasattr(self, "rotation_resource_combo")
            else "rare:乌索"
        ) or "rare:乌索"
        return {
            "rotation_mode": True,
            "rotation_full_daily_maintenance": bool(full_daily_maintenance),
            "use_foreground": self.chk_foreground.isChecked(),
            "rotation_test_mode": is_test_mode,
            "rotation_interval_minutes_nieo": interval_minutes_nieo,
            "rotation_interval_minutes_shuangta": interval_minutes_shuangta,
            "petswf_hard_limit_sec": hard_limit_sec,
            "rotation_rare_slot": rare_slot,
            "rotation_resource_enabled": bool(
                getattr(self, "chk_rotation_resource", None)
                and self.chk_rotation_resource.isChecked()
            ),
            "rotation_resource_slug": rotation_resource_slug,
            "rotation_mantis_enabled": bool(
                getattr(self, "chk_rotation_mantis", None)
                and self.chk_rotation_mantis.isChecked()
            ),
            "rotation_eit_enabled": bool(
                getattr(self, "chk_rotation_eit", None)
                and self.chk_rotation_eit.isChecked()
            ),
            "rotation_nieo_single_map_escape": bool(
                str(rotation_resource_slug).strip() == "水生海草"
                or (
                    getattr(self, "chk_rotation_nieo_single_map_escape", None)
                    and self.chk_rotation_nieo_single_map_escape.isChecked()
                )
            ),
            "rotation_nieo_follow_cyan": bool(
                getattr(self, "chk_rotation_nieo_follow_cyan", None)
                and self.chk_rotation_nieo_follow_cyan.isChecked()
            ),
        }

    def on_run_rotation_chain_test(self):
        """轮换链测试：白天模式→伊特→螳螂→尼奥→稀有，逐段进图校验"""
        resource_slug = (
            self.rotation_resource_combo.currentData()
            if hasattr(self, "rotation_resource_combo")
            else "rare:乌索"
        ) or "rare:乌索"
        rare_slot = (
            self.rotation_rare_combo.currentData()
            if hasattr(self, "rotation_rare_combo")
            else "shuangta"
        ) or "shuangta"
        tasks = {
            "rotation_chain_test": True,
            "use_foreground": self.chk_foreground.isChecked(),
            "rotation_resource_slug": resource_slug,
            "rotation_rare_slot": rare_slot,
            "rotation_nieo_follow_cyan": bool(
                getattr(self, "chk_rotation_nieo_follow_cyan", None)
                and self.chk_rotation_nieo_follow_cyan.isChecked()
            ),
        }
        follow_text = "；跟随青色" if tasks["rotation_nieo_follow_cyan"] else ""
        self.log_message(
            f"🧪 启动轮换链测试（白天模式={resource_slug} → 伊特 → 螳螂 → "
            f"尼奥 → 稀有={rare_slot}{follow_text}）",
            "SYSTEM",
        )
        self._lock_ui()
        self._emit_start(tasks)

    def _start_rotation_with_mode(self, *, full_daily_maintenance: bool) -> None:
        tasks = self._build_rotation_mode_tasks(
            full_daily_maintenance=full_daily_maintenance
        )
        tasks["skip_daily_exp_input"] = bool(
            getattr(self, "chk_skip_daily_exp_input", None)
            and self.chk_skip_daily_exp_input.isChecked()
        )
        is_test_mode = bool(tasks.get("rotation_test_mode"))
        mode_text = "测试模式（固定时间间隔切换）" if is_test_mode else "正式模式（根据北京时间自动切换）"
        slot = tasks.get("rotation_rare_slot") or "shuangta"
        rare_text = self._wild_profile_label(slot)
        entry_name = "测试完整轮换" if full_daily_maintenance else "轮换重连模式"
        self.log_message(
            f"🔄 启动{entry_name}（{mode_text}；非尼奥稀有={rare_text}）",
            "SYSTEM",
        )
        self._lock_ui()
        self._emit_start(tasks)

    def start_full_rotation_test(self):
        self._start_rotation_with_mode(full_daily_maintenance=True)

    def start_rotation_mode(self):
        """直接启动轮换重连模式，不执行首次一键日常前置。"""
        self._start_rotation_with_mode(full_daily_maintenance=False)
    
    def _auto_start_rotation_mode(self):
        """前序流程结束后自动启动轮换，并准确标注日常是否完整完成。"""
        tasks = self._build_rotation_mode_tasks(full_daily_maintenance=False)
        is_test_mode = bool(tasks.get("rotation_test_mode"))
        mode_text = "测试模式（固定时间间隔切换）" if is_test_mode else "正式模式（根据北京时间自动切换）"
        slot = tasks.get("rotation_rare_slot") or "shuangta"
        rare_text = self._wild_profile_label(slot)
        daily_completed = self._auto_rotation_handoff_daily_completed
        self._auto_rotation_handoff_daily_completed = None
        if daily_completed is True:
            handoff_text = "一键日常已完成，自动启动轮换模式"
            log_level = "SYSTEM"
        elif daily_completed is False:
            handoff_text = "一键日常未完整完成，按设定仍自动启动轮换模式"
            log_level = "WARN"
        else:
            handoff_text = "前序流程结束，自动启动轮换模式"
            log_level = "SYSTEM"
        self.log_message(
            f"🔄 {handoff_text}（{mode_text}；非尼奥稀有={rare_text}）",
            log_level,
        )
        # 前序流程结束后 _unlock_ui_stopped 已执行，UI已解锁，需重新锁定以匹配轮换模式运行状态。
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
        enabled = chk.isChecked() if chk is not None else False
        return {"enable_molecule_converter": enabled}

    def _emit_start(self, tasks: dict) -> None:
        self._ensure_newnpc_multi_90000_for_task(tasks, "任务启动")
        self._ensure_newnpc_multi_4(
            "任务启动",
            restore_yilu_90000=False,
        )
        self.start_signal.emit(
            {
                **tasks,
                **self._pick_task_kv(),
                **self._capsule_task_kv(),
                **self._molecule_converter_task_kv(),
            }
        )

    def _current_new_daily_variant(self) -> str:
        if hasattr(self, "combo_new_daily"):
            return str(self.combo_new_daily.currentData() or self.combo_new_daily.currentText() or "1")
        return "1"

    def _current_happy_valley_start_phase(self) -> str:
        if hasattr(self, "new_daily_start_step_box"):
            phase = str(
                self.new_daily_start_step_box.currentData()
                or self.new_daily_start_step_box.currentText()
                or "water"
            ).strip().lower()
            if phase in ("water", "fire", "grass"):
                return phase
        return "water"

    @staticmethod
    def _is_daily_special_start(variant: str) -> bool:
        return variant in ("hatch", "preselect", "happy_valley")

    def _populate_new_daily_start_steps(self, *_args) -> None:
        if not hasattr(self, "new_daily_start_step_box"):
            return
        variant = self._current_new_daily_variant()
        combo = self.new_daily_start_step_box
        if variant in ("hatch", "preselect"):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("—", None)
            combo.setEnabled(False)
            combo.blockSignals(False)
            return
        if variant == "happy_valley":
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("水系", "water")
            combo.addItem("火系", "fire")
            combo.addItem("草系", "grass")
            combo.setEnabled(True)
            combo.blockSignals(False)
            return

        max_step = int(NEW_DAILY_VARIANT_MAX_STEPS.get(variant, 1) or 1)
        prev = combo.currentData() if combo.count() else 1
        try:
            prev_int = int(prev or 1)
        except (TypeError, ValueError):
            prev_int = 1
        combo.blockSignals(True)
        combo.clear()
        combo.setEnabled(True)
        for step in range(1, max_step + 1):
            combo.addItem(str(step), step)
        combo.setCurrentIndex(max(0, min(prev_int, max_step) - 1))
        combo.blockSignals(False)

    def _current_new_daily_start_step(self) -> int:
        if not hasattr(self, "new_daily_start_step_box"):
            return 1
        combo = self.new_daily_start_step_box
        try:
            return max(1, int(combo.currentData() or combo.currentText() or 1))
        except (TypeError, ValueError):
            return 1
    
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

    def start_event_pet_mode(self):
        """启动活动精灵模式（含伊特）。"""
        slug = "yite"
        if hasattr(self, "combo_event_pet"):
            slug = self.combo_event_pet.currentData() or slug
        skip_pre = (
            self.chk_event_pet_skip_pre.isChecked()
            if hasattr(self, "chk_event_pet_skip_pre")
            else False
        )
        tasks = {
            "event_pet_mode": True,
            "event_pet_slug": slug,
            "event_pet_skip_pre_rotation": skip_pre,
            "use_foreground": self.chk_foreground.isChecked(),
        }
        pre_msg = " [跳过前置]" if skip_pre else " [启动前置重连·to埃尔特→map414]"
        self.log_message(f"🌿 启动活动精灵 slug={slug}{pre_msg}", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def start_eit_mode(self):
        """兼容旧入口：等同启动活动精灵·伊特。"""
        if hasattr(self, "combo_event_pet"):
            idx = self.combo_event_pet.findData("yite")
            if idx >= 0:
                self.combo_event_pet.setCurrentIndex(idx)
        self.start_event_pet_mode()

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
        """启动尼奥模式、纯净能量或自定义三图尼奥（下拉选择）"""
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
            "nieo_single_map_escape": (
                str(sub).strip() == "水生海草"
                or (
                    hasattr(self, "chk_nieo_single_map_escape")
                    and self.chk_nieo_single_map_escape.isChecked()
                )
            ),
            "nieo_follow_cyan": bool(
                hasattr(self, "chk_nieo_follow_cyan")
                and self.chk_nieo_follow_cyan.isChecked()
            ),
            "nieo_yellow60_to_rotation": (
                hasattr(self, "chk_nieo_yellow60_to_rotation")
                and self.chk_nieo_yellow60_to_rotation.isEnabled()
                and self.chk_nieo_yellow60_to_rotation.isChecked()
            ),
        }
        handoff_rotation_tasks = dict(self._build_rotation_mode_tasks())
        handoff_rotation_tasks.pop("rotation_mode", None)
        handoff_rotation_tasks["rotation_full_daily_maintenance"] = False
        tasks.update(handoff_rotation_tasks)

        test_msg = ""
        if tasks.get("nieo_skip_pre_rotation"):
            test_msg += " [跳过前置]"
        else:
            test_msg += " [启动前置重连]"
        if tasks.get("nieo_test_force_switch"):
            test_msg += " [测试·10图机塔/11图艾菲德斯]"
        if tasks.get("nieo_single_map_escape"):
            test_msg += " [单图]"
        if tasks.get("nieo_follow_cyan"):
            test_msg += " [跟随青色]"
        if sub == "pure_energy":
            self.log_message(f"⚡ 启动纯净能量模式（26/27，技能四战胜）{test_msg}", "SYSTEM")
        elif sub not in ("nieo", "pure_energy"):
            tasks["nieo_custom_slug"] = sub
            self.log_message(f"🌊 启动自定义尼奥模式（slug={sub}）{test_msg}", "SYSTEM")
        else:
            self.log_message(f"🌊 启动尼奥模式（10/11地图循环）{test_msg}", "SYSTEM")
        self._lock_ui()
        self._emit_start(tasks)

    def start_nieo_resource_chain(self):
        """依次跑五个尼奥资源模式，每个黄色胜利 60 次后进入普通轮换。"""
        tasks = {
            "nieo_resource_chain": True,
            "use_foreground": self.chk_foreground.isChecked(),
            "nieo_single_map_escape": bool(
                hasattr(self, "chk_nieo_single_map_escape")
                and self.chk_nieo_single_map_escape.isChecked()
            ),
            "nieo_follow_cyan": bool(
                hasattr(self, "chk_nieo_follow_cyan")
                and self.chk_nieo_follow_cyan.isChecked()
            ),
        }
        rotation_tasks = dict(self._build_rotation_mode_tasks())
        rotation_tasks.pop("rotation_mode", None)
        rotation_tasks["rotation_full_daily_maintenance"] = False
        tasks.update(rotation_tasks)
        self.log_message(
            "🔗 启动尼奥资源五连：晶化气泡 → 露西之核 → 水生海草 → "
            "贝壳精华 → 水之精华（每项黄色胜利 60 次）→ 普通轮换"
            f"{'；跟随青色' if tasks.get('nieo_follow_cyan') else ''}",
            "SYSTEM",
        )
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
