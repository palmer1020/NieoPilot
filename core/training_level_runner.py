# core/training_level_runner.py
import os
import re
import time
from typing import List, Optional, Tuple, Dict

from core.utils import window_manager
from core.battle_runner import BattleRunner
from core.post_battle_cleaner import PostBattleCleaner
from core.unified_battle_framework import UnifiedBattleFramework, BattleConfig, BattleMode
from core.fixed_mode_adapter import FixedModeAdapter
from PIL import ImageOps, ImageEnhance

try:
    import pytesseract
except Exception:
    pytesseract = None


class TrainingLevelRunner:
    """
    训练室练级总流程（Stage0~Stage4） + 你新增需求：
    1) Dashboard 输入 recover_every：每 N 场执行一次 Stage0 恢复（默认5）
    2) OCR 满级/调试停：第一回合任意技能点击后触发 OCR，输出等级到日志
       - 若 OCR >= 100：本场结束后停止整个练级，并最大化前置窗口
       - 若 debug_stop_level 设置：OCR >= debug_stop_level 本场结束后停止
    3) “训练室连接脚本”仅在完整打满 30 场且清理完弹窗后执行（不允许每场触发）
    """

    OPEN_BAG_KEYS = ["训练室.精灵背包", "精灵背包.打开精灵背包", "训练室.打开精灵背包"]
    CLOSE_BAG_KEYS = ["训练室.关闭精灵背包", "精灵背包.关闭精灵背包", "训练室.精灵背包.关闭"]

    KEY_RECOVER = "训练室.精灵恢复"
    KEY_NORMAL_CONFIRM = "对话框.普通确认"
    CLICK_BATTLE_KEYS = ["训练室.点击对战", "训练室.点击对战.按钮", "训练室.对战"]

    CONNECT_SCRIPT_FILENAME = "训练室连接.json"  # 放在 fix_script 下
    LEVEL_KEY_CONTAINS = "我方精灵等级"          # assets/regions/对战信息/我方精灵等级.json

    def __init__(self, bot, regions, template_root: str, battle_runner: Optional[BattleRunner] = None, use_unified_framework: bool = True):
        self.bot = bot
        self.regions = regions
        self.template_root = template_root
        self.save_level_ocr_debug = True
        self.use_unified_framework = use_unified_framework  # 是否使用新的统一框架

        self.battle_runner = battle_runner or BattleRunner(bot, regions, template_root)
        self.cleaner = PostBattleCleaner(bot, regions, template_root)
        
        # 初始化统一框架（如果启用）
        if self.use_unified_framework:
            try:
                self.unified_framework = UnifiedBattleFramework(bot, regions, template_root)
                self.fixed_adapter = FixedModeAdapter(self.unified_framework)
            except Exception as e:
                self.bot.emit_and_log(f"⚠️ 统一框架初始化失败，回退到旧实现: {e}", "WARN")
                self.use_unified_framework = False

        self._level_key_cache: Optional[str] = None
        self._click_log_throttle: Dict[str, float] = {}  # 点击日志节流

    # -------------------------
    # helpers
    # -------------------------
    def _pick_existing_key(self, candidates: List[str]) -> Optional[str]:
        for k in candidates:
            if self.regions.get(k):
                return k
        return None

    def _click(self, key: str, use_foreground: bool):
        r = self.regions.get(key)
        if not r:
            self.bot.emit_and_log(f"❌ 找不到区域：{key}", "ERROR")
            raise KeyError(key)

        gx, gy = r.sample_click_point()
        if use_foreground:
            window_manager.click(gx, gy)
        else:
            window_manager.click_background(gx, gy)

        # 点击日志节流：相同区域每1秒最多输出一次
        now = time.time()
        throttle_key = f"click_{r.key}"
        last_time = self._click_log_throttle.get(throttle_key, 0)
        if now - last_time >= 1.0:
            self.bot.emit_and_log(f"🖱 点击区域 {r.key} -> ({int(gx)},{int(gy)})", "DEBUG")
            self._click_log_throttle[throttle_key] = now

    def _find_level_key(self) -> Optional[str]:
        if self._level_key_cache and self.regions.get(self._level_key_cache):
            return self._level_key_cache

        try:
            for k in self.regions.keys():
                if self.LEVEL_KEY_CONTAINS in k:
                    self._level_key_cache = k
                    return k
        except Exception:
            pass

        # 兜底常见名
        for cand in ["对战信息.我方精灵等级", "对战信息.我方等级", "对战信息.等级"]:
            if self.regions.get(cand):
                self._level_key_cache = cand
                return cand

        return None

    def _ocr_level_from_region(self, level_key: str, debug_tag: str = ""):
        # 1) region 存在吗
        r = self.regions.get(level_key)
        if not r:
            self.bot.emit_and_log(f"📟 OCR失败：找不到region: {level_key}", "WARN")
            return None

        x1, y1, x2, y2 = r.outer_bbox()
        self.bot.emit_and_log(f"📟 OCR bbox({level_key})=({x1},{y1},{x2},{y2})", "DEBUG")

        # 2) 能截到图吗
        try:
            img = window_manager.grab_game_bbox(x1, y1, x2, y2)
        except Exception as e:
            self.bot.emit_and_log(f"📟 OCR失败：grab_game_bbox异常: {e}", "WARN")
            return None

        if img is None:
            self.bot.emit_and_log("📟 OCR失败：截图为空(None)（很可能是坐标映射/窗口捕获问题）", "WARN")
            return None

        # 3) tesseract 可用吗（如果这里抛错，你之前是被 try/except 吞掉了）
        if pytesseract is None:
            self.bot.emit_and_log("📟 OCR失败：pytesseract不可用（未安装/未配置tesseract）", "WARN")
            return None

        # 4) 保存原图，看看你截到的到底是不是等级数字
        if self.save_level_ocr_debug:
            try:
                os.makedirs(os.path.join(self.bot.project_root, "debug_ocr"), exist_ok=True)
                ts = int(time.time() * 1000)
                tag = debug_tag or "level"
                raw_path = os.path.join(self.bot.project_root, "debug_ocr", f"{tag}_{ts}_raw.png")
                img.save(raw_path)
                self.bot.emit_and_log(f"📟 OCR原图已保存: {raw_path}", "DEBUG")
            except Exception as e:
                self.bot.emit_and_log(f"📟 保存OCR原图失败: {e}", "DEBUG")

        # 5) 多种预处理 + 多种 config 尝试（只要抓到数字就返回）
        def _try_ocr(pil_img, config):
            try:
                txt = pytesseract.image_to_string(pil_img, lang="eng", config=config)
                nums = re.findall(r"\d{1,3}", (txt or ""))
                return txt, (int(nums[0]) if nums else None)
            except Exception as e:
                return f"[tesseract_error]{e}", None

        gray = img.convert("L")
        gray = ImageOps.autocontrast(gray)
        w, h = gray.size
        gray = gray.resize((max(1, w * 4), max(1, h * 4)))

        # 两种阈值/反色都试一遍（很多UI是浅字深底，或深字浅底）
        variants = []
        variants.append(gray.point(lambda p: 255 if p > 160 else 0))  # normal
        inv = ImageOps.invert(gray)
        variants.append(inv.point(lambda p: 255 if p > 160 else 0))   # inverted

        configs = [
            "--psm 7 -c tessedit_char_whitelist=0123456789",
            "--psm 8 -c tessedit_char_whitelist=0123456789",
            "--psm 6 -c tessedit_char_whitelist=0123456789",
        ]

        best_txt = ""
        for i, v in enumerate(variants):
            for cfg in configs:
                txt, val = _try_ocr(v, cfg)
                best_txt = txt
                if val is not None and 1 <= val <= 120:
                    return val

        # 没识别到数字：把原文打出来，方便你看差在哪
        self.bot.emit_and_log(f"📟 OCR未出数字，raw_text={best_txt!r}", "DEBUG")
        return None

    def _run_connect_script(self) -> bool:
        # 只负责执行一次连接脚本（脚本格式由 DailyRunner 兼容）
        script_path = os.path.join(self.bot.project_root, "fix_script", self.CONNECT_SCRIPT_FILENAME)
        if not os.path.exists(script_path):
            self.bot.emit_and_log(f"⚠ 未找到连接脚本，跳过：{script_path}", "WARN")
            return False

        if not hasattr(self.bot, "daily_runner"):
            self.bot.emit_and_log("⚠ bot.daily_runner 不存在，无法执行连接脚本", "WARN")
            return False

        ok = self.bot.daily_runner.run_script(script_path, bg_override=None)
        self.bot.emit_and_log(f"🔗 执行训练室连接脚本：{self.CONNECT_SCRIPT_FILENAME}", "SYSTEM")
        time.sleep(3.0)
        # bg_override=None => 使用脚本每步自己的 bg（没有 bg 就默认后台，兼容 script_record 输出）
        return ok

    # -------------------------
    # stage0
    # -------------------------
    def stage0_recover_once(self, use_foreground: bool = False):
        open_key = self._pick_existing_key(self.OPEN_BAG_KEYS)
        close_key = self._pick_existing_key(self.CLOSE_BAG_KEYS)

        if not open_key or not close_key:
            self.bot.emit_and_log(
                f"❌ 训练室背包区域缺失：open={open_key} close={close_key}（检查 regions/训练室/*.json）",
                "ERROR",
            )
            raise KeyError("training_room_bag_keys_missing")

        self.bot.emit_and_log("🧰 Stage0：打开背包并恢复精灵", "SYSTEM")
        self._click(open_key, use_foreground)
        time.sleep(2.5)

        self._click(self.KEY_RECOVER, use_foreground)
        time.sleep(0.5)

        self._click(self.KEY_NORMAL_CONFIRM, use_foreground)
        time.sleep(0.5)

        self._click(close_key, use_foreground)
        time.sleep(0.5)

    # -------------------------
    # stage1
    # -------------------------
    def stage1_click_battle(self, use_foreground: bool):
        key = self._pick_existing_key(self.CLICK_BATTLE_KEYS)
        if not key:
            self.bot.emit_and_log("❌ 找不到训练室对战按钮区域：训练室.点击对战", "ERROR")
            raise KeyError("训练室.点击对战")

        self.bot.emit_and_log("⚔ Stage1：点击训练室【对战】", "SYSTEM")
        self._click(key, use_foreground)
        time.sleep(0.5)

    # -------------------------
    # stage2
    # -------------------------
    def stage2_defeat(self, use_foreground: bool, on_round1_skill_used=None):
        self.bot.emit_and_log("⚔ Stage2：自动击败（循环技能）", "SYSTEM")
        try:
            self.battle_runner.run_defeat_mode(
                use_foreground=use_foreground,
                on_round1_skill_used=on_round1_skill_used,
            )
        except TypeError:
            # 兼容旧签名
            self.battle_runner.run_defeat_mode(use_foreground=use_foreground)

    # -------------------------
    # stage3
    # -------------------------
    def stage3_clear_dialogs(self, use_foreground: bool):
        self.bot.emit_and_log("🧹 Stage3：清理胜利/升级/确认弹窗", "SYSTEM")
        self.cleaner.run_stage3_training_room(use_foreground=use_foreground)

    # -------------------------
    # stage4
    # -------------------------
    def stage4_wait(self):
        self.bot.emit_and_log("⏳ Stage4：等待 3s 进入下一轮", "SYSTEM")
        time.sleep(3.0)

    # -------------------------
    # 单批次：最多 30 场
    # -------------------------
    def run_training_level(
        self,
        max_battles: int = 30,
        recover_every: int = 5,
        debug_stop_level: Optional[int] = None,
        use_foreground: bool = False,
    ) -> Tuple[int, Optional[int], bool]:
        """
        返回：(已完成场数, 最后一次OCR等级, 是否触发停止条件)
        停止条件：
          - OCR >= 100
          - 或 debug_stop_level 设置且 OCR >= debug_stop_level
        """
        if max_battles <= 0:
            max_battles = 1
        if max_battles > 30:
            max_battles = 30  # 训练室单轮上限

        if recover_every is None or recover_every <= 0:
            recover_every = 0  # 0 表示不做间隔恢复

        if not window_manager.find_window():
            self.bot.emit_and_log("❌ 未检测到游戏窗口：请先点【启动游戏】", "ERROR")
            return 0, None, False

        level_key = self._find_level_key()
        if not level_key:
            self.bot.emit_and_log("⚠ 未找到【我方精灵等级】区域：仍会练级，但不会OCR停", "WARN")

        self.bot.emit_and_log(
            f"🏫 训练室练级启动：max_battles={max_battles} recover_every={recover_every or 'OFF'} "
            f"debug_stop_level={debug_stop_level or '-'} 前台={use_foreground}",
            "SYSTEM",
        )

        # Stage0：开头恢复一次
        self.stage0_recover_once(use_foreground=use_foreground)

        battles_done = 0
        last_level = None
        stopped_by_level = False

        while battles_done < max_battles:
            if getattr(self.bot, "stop_current", False):
                self.bot.emit_and_log("⛔ 训练室练级：检测到中止请求，退出", "WARN")
                return battles_done, last_level, False

            try:
                self.bot.wait_if_paused()
            except Exception:
                pass

            # ✅ 每 N 场之间做一次恢复（在"下一场开始前"做）- 统一框架和旧实现都需要
            if recover_every and battles_done > 0 and (battles_done % recover_every == 0):
                self.bot.emit_and_log(f"🧃 已完成 {battles_done} 场：执行一次 Stage0 恢复", "SYSTEM")
                self.stage0_recover_once(use_foreground=use_foreground)

            battles_done += 1
            self.bot.emit_and_log(f"🔁 训练室练级：第 {battles_done}/{max_battles} 场", "INFO")

            stop_after_this_battle = False

            # ✅ OCR 回调：检查等级并设置停止标志
            def _check_level_and_set_stop():
                nonlocal last_level, stop_after_this_battle, stopped_by_level
                if not level_key:
                    return
                lvl = self._ocr_level_from_region(level_key)
                if lvl is None:
                    self.bot.emit_and_log("📟 我方精灵等级OCR：识别失败", "WARN")
                    return

                last_level = lvl
                self.bot.emit_and_log(f"📟 我方精灵等级OCR：{lvl}", "INFO")

                # 满级强制停
                if lvl in (10, 100):
                    stopped_by_level = True
                    stop_after_this_battle = True
                    self.bot.emit_and_log("🎉 检测到已满级(>=100)：本场结束后停止练级，并前置窗口", "SUCCESS")
                    return

                # 调试停
                if debug_stop_level is not None and lvl >= int(debug_stop_level):
                    stopped_by_level = True
                    stop_after_this_battle = True
                    self.bot.emit_and_log(f"🧪 调试：检测到等级>={debug_stop_level}，本场结束后停止", "SYSTEM")

            # ✅ OCR 回调：第一回合"任意技能点击后"触发一次（旧实现用）
            def _on_round1_skill_used(_skill_key: str):
                _check_level_and_set_stop()

            # Stage1~3：根据配置选择新旧实现
            if self.use_unified_framework:
                # 使用新的统一框架
                try:
                    # 配置OCR回调（在第一回合技能使用后触发）
                    def action_callback(round_idx: int) -> str:
                        if round_idx == 1:
                            # 第一回合使用技能后触发OCR检查
                            _check_level_and_set_stop()
                        return "skill"  # 永远使用技能四
                    
                    # 使用固定模式适配器执行单场对战
                    config = BattleConfig(
                        mode=BattleMode.FIXED,
                        use_foreground=use_foreground,
                        skill_key="对战.使用技能四",
                        trigger_callback=self.fixed_adapter._trigger_training_room,
                        action_callback=action_callback,
                        abort_check=lambda: getattr(self.bot, "stop_current", False),
                    )
                    
                    success = self.unified_framework.run_battle(config, is_training_room=True)
                    if not success:
                        self.bot.emit_and_log("⚠️ 对战失败或跳过", "WARN")
                    
                    # ✅ 统一框架模式下也需要等待3.5s（框架内部已处理，这里不需要额外等待）
                    # 但连接脚本需要在框架完成后执行
                    
                except Exception as e:
                    self.bot.emit_and_log(f"⚠️ 统一框架执行失败，回退到旧实现: {e}", "WARN")
                    import traceback
                    self.bot.emit_and_log(traceback.format_exc(), "ERROR")
                    # 回退到旧实现
                    self.stage1_click_battle(use_foreground=use_foreground)
                    self.stage2_defeat(use_foreground=use_foreground, on_round1_skill_used=_on_round1_skill_used)
                    self.stage3_clear_dialogs(use_foreground=use_foreground)
            else:
                # 使用旧的实现
                self.stage1_click_battle(use_foreground=use_foreground)
                self.stage2_defeat(use_foreground=use_foreground, on_round1_skill_used=_on_round1_skill_used)
                self.stage3_clear_dialogs(use_foreground=use_foreground)

            # ✅ 若本场触发停止条件：直接收尾退出（不跑连接脚本）
            if stop_after_this_battle:
                try:
                    window_manager.maximize_window()
                except Exception:
                    pass
                self.bot.emit_and_log(f"✅ 训练室练级停止：已完成 {battles_done} 场", "SUCCESS")
                return battles_done, last_level, True

            # ✅ 仅当"完整打满30场"才执行连接脚本（并且是在清完弹窗之后）- 统一框架和旧实现都需要
            if (max_battles == 30) and (battles_done == 30):
                self._run_connect_script()
                self.bot.emit_and_log("✅ 训练室单批次(30场)完成", "SUCCESS")
                return battles_done, last_level, False

            # Stage4：等待3.5s（统一框架内部已处理，只有旧实现需要额外等待）
            if not self.use_unified_framework:
                self.stage4_wait()

        self.bot.emit_and_log(f"✅ 训练室练级完成：共 {battles_done} 场", "SUCCESS")
        return battles_done, last_level, False

    # -------------------------
    # 多批次：升级直到目标等级（默认100）
    # -------------------------
    def run_training_until_level(
        self,
        target_level: int = 100,
        battles_per_batch: int = 30,
        recover_every: int = 5,
        debug_stop_level: Optional[int] = None,
        use_foreground: bool = False,
    ):
        # 训练室直升100模式：强制battles_per_batch=30
        battles_per_batch = 30

        # debug_stop_level 优先（用于你说的"调试输入xx级后停"）
        stop_level = debug_stop_level if debug_stop_level is not None else int(target_level)

        batch_idx = 0
        while True:
            if getattr(self.bot, "stop_current", False):
                self.bot.emit_and_log("⛔ 已中止：退出【升级直到目标等级】", "WARN")
                return

            batch_idx += 1
            self.bot.emit_and_log(f"📦 Batch {batch_idx}：最多 {battles_per_batch} 场，目标停级={stop_level}", "SYSTEM")

            done, last_lvl, stopped = self.run_training_level(
                max_battles=battles_per_batch,
                recover_every=recover_every,
                debug_stop_level=stop_level,   # 用同一套停条件
                use_foreground=use_foreground,
            )

            if stopped:
                self.bot.emit_and_log(f"🎯 已达到停止等级：{last_lvl}（Batch={batch_idx}）", "SUCCESS")
                return

            # 没停，说明这批打满了 battles_per_batch（通常=30）并且连接脚本已在批末执行
            # 直接继续下一批

    HV_KEY_CONTAINS = "人机验证信息"
    HV_TRIGGER_WORDS = ("正面", "侧面", "背面")

    def _find_key_contains(self, s: str):
        for k in self.regions.keys():
            if s in k:
                return k
        return None