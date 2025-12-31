# core/post_battle_cleaner.py
import os
import time
from typing import Optional, Tuple, Dict

import numpy as np
from PIL import Image

from core.utils import window_manager


def _mean_rgb(img: Image.Image) -> Tuple[int, int, int]:
    arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
    if arr.size == 0:
        return (0, 0, 0)
    m = arr.reshape(-1, 3).mean(axis=0)
    return int(m[0]), int(m[1]), int(m[2])


def _yellow_pixel_mask(arr: np.ndarray, tol: int = 10) -> np.ndarray:
    """
    纯黄探针（你用取色器看到 255,255,0）：
    允许一点点误差（tol 默认 10）
    """
    r = arr[..., 0].astype(np.int16)
    g = arr[..., 1].astype(np.int16)
    b = arr[..., 2].astype(np.int16)

    return (
        (r >= 255 - tol) &
        (g >= 255 - tol) &
        (b <= tol)
    )


class PostBattleCleaner:
    """
    Stage3（训练室）：
    - 等“对战.胜利探针”变黄（2px×2px 纯色探针 → 直接像素判定）
    - 等不到黄也要执行一次“补点流程”（只执行一次）
    - 点击确认：严格固定序列，每个区域只点一次（不能多点不能少点）
    """

    def __init__(self, bot, regions, template_root: str):
        self.bot = bot
        self.regions = regions
        self.template_root = template_root
        self._click_log_throttle: Dict[str, float] = {}  # 点击日志节流

        self.KEY_VICTORY_PROBE = "对战.胜利探针"

        # 训练室：你要求的固定点击流程（每次都点一次，不循环）
        self.CLICK_PLAN_TRAINING = [
            ("对话框.对战胜利确认", 0.12),
            ("对话框.升级确认", 0.12),
            ("对话框.技能替换取消", 0.12),
            ("对话框.普通确认", 0.12),
        ]

        # ✅ 勇者之塔：胜利确认后连点 4 次普通确认
        self.CLICK_PLAN_HERO_TOWER = [
            ("对话框.对战胜利确认", 0.12),
            ("对话框.普通确认", 0.12),
            ("对话框.普通确认", 0.12),
            ("对话框.普通确认", 0.12),
            ("对话框.普通确认", 0.12),
        ]

    # -------------------------
    # region helpers
    # -------------------------
    def _require_region(self, key: str):
        r = self.regions.get(key)
        if not r:
            self.bot.emit_and_log(f"❌ 找不到区域：{key}", "ERROR")
            raise KeyError(key)
        return r

    def _grab_region_img(self, key: str) -> Optional[Image.Image]:
        r = self._require_region(key)
        x1, y1, x2, y2 = r.outer_bbox()
        # 注意：grab_game_bbox 已经支持 2px 探针并做 min_size 扩张
        return window_manager.grab_game_bbox(x1, y1, x2, y2, min_size_px=2)

    def _click_region(self, key: str, use_foreground: bool):
        r = self._require_region(key)
        gx, gy = r.sample_click_point()
        if use_foreground:
            window_manager.click(gx, gy)
        else:
            window_manager.click_background(gx, gy)
        # 点击日志节流：相同区域每1秒最多输出一次
        now = time.time()
        throttle_key = f"click_{key}"
        last_time = self._click_log_throttle.get(throttle_key, 0)
        if now - last_time >= 1.0:
            self.bot.emit_and_log(f"🖱 Stage3 点击区域 {key} -> ({gx},{gy})", "DEBUG")
            self._click_log_throttle[throttle_key] = now

    # -------------------------
    # probe detect (pure yellow)
    # -------------------------
    def detect_victory_probe_yellow(
        self,
        tol: int = 10,
        ratio_th: float = 0.75,      # 兼容保留：不用于判定
        use_foreground: bool = False # 兼容保留：不用于判定
    ) -> Tuple[bool, float, Tuple[int, int, int]]:
        """
        胜利黄点：只看“中心附近”是否出现纯黄（255,255,0）即可通过。
        - 不用 mean 做判定
        - 只要中心附近(3x3)里有一个像素满足黄点条件 => True
        返回 (match, score, rgb)
        - match: 是否检测到黄点
        - score: 中心附近黄像素占比(仅用于日志 best_score)
        - rgb: 命中的黄像素rgb（或中心rgb用于调试）
        """
        img = self._grab_region_img(self.KEY_VICTORY_PROBE)
        if img is None:
            return False, 0.0, (0, 0, 0)

        arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
        h, w = arr.shape[:2]
        cy, cx = h // 2, w // 2

        # 只取中心附近 3x3（更符合“只检测中间一个黄点”的意图，也更抗偏移）
        y1, y2 = max(cy - 1, 0), min(cy + 2, h)
        x1, x2 = max(cx - 1, 0), min(cx + 2, w)
        patch = arr[y1:y2, x1:x2, :]

        mask = _yellow_pixel_mask(patch, tol=tol)
        yellow_cnt = int(mask.sum())
        total = int(mask.size)  # patch_h * patch_w
        score = (yellow_cnt / total) if total else 0.0

        if yellow_cnt > 0:
            py, px = np.argwhere(mask)[0]
            hit_rgb = tuple(int(v) for v in patch[py, px, :])
            return True, score, hit_rgb

        center_rgb = tuple(int(v) for v in arr[cy, cx, :])
        return False, score, center_rgb

    def _run_click_plan(self, click_plan, use_foreground: bool):
        """按顺序点击一次，不循环。"""
        for key, dt in click_plan:
            if getattr(self.bot, "stop_current", False):
                return
            try:
                self._click_region(key, use_foreground=use_foreground)
            except Exception as e:
                # 某些对话框可能不存在，允许跳过
                self.bot.emit_and_log(f"⚠ Stage3 点击 {key} 失败: {e}", "WARN")
            time.sleep(float(dt))
    # -------------------------
    # Stage3 for training room
    # -------------------------
    def run_stage3_training_room(
        self,
        use_foreground: bool,
        max_wait_s: float = 8.0,
        tol: int = 10,
        ratio_th: float = 0.75,
        min_detect_delay_s: float = 2.5,
        click_plan=None,  # ✅ 新增：允许外部传点击序列
    ):
        # 默认：训练室点击序列
        if click_plan is None:
            click_plan = self.CLICK_PLAN_TRAINING

        # ✅ 先等 UI 稳定
        t_delay0 = time.time()
        while time.time() - t_delay0 < min_detect_delay_s:
            if getattr(self.bot, "stop_current", False):
                return False
            try:
                self.bot.wait_if_paused()
            except Exception:
                pass
            time.sleep(0.05)

        self.bot.emit_and_log("🟡 Stage3：等待胜利黄色探针出现…", "DEBUG")

        t0 = time.time()
        best_score = 0.0
        best_rgb = (0, 0, 0)
        got_yellow = False

        while time.time() - t0 < max_wait_s and (not getattr(self.bot, "stop_current", False)):
            try:
                self.bot.wait_if_paused()
            except Exception:
                pass

            got_yellow, score, rgb = self.detect_victory_probe_yellow(
                use_foreground=use_foreground,
                tol=tol,
                ratio_th=ratio_th,
            )
            if score > best_score:
                best_score = score
                best_rgb = rgb

            self.bot.emit_and_log(
                f"🟡 Stage3 探针检测：match={got_yellow} score={score:.3f} best={best_score:.3f} rgb={rgb}",
                "DEBUG",
            )

            if got_yellow:
                break
            time.sleep(0.08)

        if not got_yellow:
            # ✅ 跟训练室一致：等不到也执行一次“补点点击流程”，避免卡死
            self.bot.emit_and_log(
                f"⚠ Stage3 未等到黄色探针（best_score={best_score:.3f}, best_rgb={best_rgb}），执行一次补点流程（按 click_plan 点一遍）",
                "WARN",
            )

        # ✅ 按传入的 click_plan 点（训练室/勇者之塔都走这里）
        self._run_click_plan(click_plan, use_foreground=use_foreground)
        return got_yellow



