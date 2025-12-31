# core/battle_runner.py
import os
import re
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Tuple, List, Callable, Dict, Any, Sequence, Set
from core.region_store import Region, RegionStore

import numpy as np
from PIL import Image

from core.logger import add_kernel_log_callback, remove_kernel_log_callback
from core.logger import fetch_kernel_since, kernel_cursor, wait_kernel_contains
from core.utils import window_manager
from core.human_verify_watcher import HumanVerifyWatcher

import logging
log = logging.getLogger(__name__)



# ======================================================
# Probe helpers (分辨率不敏感)
# ======================================================

def _ahash_bits(img: Image.Image, size: int = 10) -> np.ndarray:
    g = img.convert("L").resize((size, size), Image.BILINEAR)
    arr = np.asarray(g, dtype=np.float32)
    m = arr.mean()
    bits = (arr > m).astype(np.uint8).flatten()
    return bits


def _sim_bits(a: np.ndarray, b: np.ndarray) -> float:
    if a is None or b is None:
        return 0.0
    if a.shape != b.shape:
        return 0.0
    diff = np.count_nonzero(a != b)
    return 1.0 - diff / float(a.size)


def _blue_strength(img: Image.Image) -> float:
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    r = float(arr[:, :, 0].mean())
    g = float(arr[:, :, 1].mean())
    b = float(arr[:, :, 2].mean())
    return float(b - 0.5 * (r + g))


@dataclass
class ProbeModel:
    mode: str  # "COLOR" or "AHASH"
    blue_ref: float = 0.0
    gray_ref: float = 0.0
    span: float = 10.0
    blue_bits: Optional[np.ndarray] = None
    gray_bits: Optional[np.ndarray] = None
    ahash_size: int = 10
    tie_eps: float = 0.05


# ======================================================
# BattleRunner
# ======================================================

class BattleRunner:
    """
    击败模式（第二环节）+ 你新增的两件事：
    A) 每 5 场自动训练室恢复一次（Stage0）
    B) Stage1.5 人机验证初步：检测 pet/swf burst -> OCR + 截图入库 + 自动暂停
    """

    # ---------- 你已有/约定的 region key ----------
    KEY_SKILL4 = "对战.使用技能四"
    KEY_PROBE = "对战.回合探针"

    # ---------- 人机验证 region key ----------
    KEY_HV_PANEL = "人机验证.人机验证"
    KEY_HV_TEXT = "人机验证.人机验证信息"

    # ---------- 训练室恢复（每 5 场一次）的 region key（你按自己的实际命名改这里即可） ----------
    # 这四个动作就是你说的 Stage0：“打开背包 -> 恢复 -> 确认 -> 关闭背包”
    RECOVER_KEYS_CANDIDATES = {
        "open_bag": ["训练室.精灵背包"],
        "recover":  [ "训练室.精灵恢复"],
        "confirm":  ["对话框.普通确认"],
        "close_bag":["精灵背包.关闭精灵背包"],
    }

    # ---------- 模板路径（回合探针 blue/gray） ----------
    PROBE_BLUE_REL = os.path.join("对战", "回合探针", "blue.png")
    PROBE_GRAY_REL = os.path.join("对战", "回合探针", "gray.png")

    # ---------- 人机验证样本保存 ----------
    HV_SAVE_ROOT_REL = os.path.join("assets", "human_verify")
    HV_UNLABELED = "未处理"
    HV_LABELED = "已处理"

    # ---------- pet swf burst 判定 ----------
    _PET_SWF_RE = re.compile(r"/resource/pet/swf/(\d+)\.swf", re.IGNORECASE)

    def __init__(self, bot, regions, template_root: str):
        self.bot = bot
        self.regions = regions
        self.template_root = template_root

        self._kernel_q = deque(maxlen=6000)
        from config import HV_SAMPLES_PATH
        self.hv_watcher = HumanVerifyWatcher(self.bot, self.regions, save_root=HV_SAMPLES_PATH)
        self._hv_prebattle = True  # 用于限制只在“入战前”判定 burst
        self._kernel_cb = None

        self.PROBE_BLUE = os.path.join(self.template_root, self.PROBE_BLUE_REL)
        self.PROBE_GRAY = os.path.join(self.template_root, self.PROBE_GRAY_REL)

        # 人机验证 burst 缓冲
        self._pet_burst = deque()  # (ts, pet_id)
        self._hv_cooldown_until = 0.0

        # 统计：用于“每5场恢复一次”
        self._battle_count = 0
        from config import HV_SAMPLES_PATH
        self.hv_watcher = HumanVerifyWatcher(self.bot, self.regions, save_root=HV_SAMPLES_PATH)
        self._click_log_throttle: Dict[str, float] = {}  # 点击日志节流


    # -------------------------
    # kernel log 监听
    # -------------------------
    def _start_kernel_listen(self):
        self._kernel_q.clear()

        def _on_line(line: str):
            self._kernel_q.append(line)

        self._kernel_cb = _on_line
        add_kernel_log_callback(self._kernel_cb)

    def _stop_kernel_listen(self):
        if self._kernel_cb:
            try:
                remove_kernel_log_callback(self._kernel_cb)
            except Exception:
                pass
        self._kernel_cb = None

    # -------------------------
    # region access (兼容 RegionStore / dict / __getitem__)
    # -------------------------
    def _rs_get(self, key: str):
        # 1) RegionStore.get(key)
        if hasattr(self.regions, "get") and callable(getattr(self.regions, "get")):
            return self.regions.get(key)
        # 2) dict-like
        if isinstance(self.regions, dict):
            return self.regions.get(key)
        # 3) __getitem__
        if hasattr(self.regions, "__getitem__"):
            try:
                return self.regions[key]
            except Exception:
                return None
        return None

    def _require_region(self, key: str):
        r = self._rs_get(key)
        if not r:
            self.bot.emit_and_log(f"❌ 找不到区域：{key}", "ERROR")
            raise KeyError(key)
        return r

    def _region_points(self, r) -> List[Tuple[float, float]]:
        # Region 对象
        if hasattr(r, "points"):
            pts = r.points
            return [(float(x), float(y)) for x, y in pts]
        # dict
        if isinstance(r, dict):
            pts = r.get("points") or []
            return [(float(x), float(y)) for x, y in pts]
        return []

    def _outer_bbox(self, r) -> Tuple[float, float, float, float]:
        # Region 对象
        if hasattr(r, "outer_bbox") and callable(getattr(r, "outer_bbox")):
            x1, y1, x2, y2 = r.outer_bbox()
            return float(x1), float(y1), float(x2), float(y2)

        pts = self._region_points(r)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        if not xs or not ys:
            raise ValueError("region has no points/bbox")
        return min(xs), min(ys), max(xs), max(ys)

    def _inner_bbox(self, r) -> Tuple[float, float, float, float]:
        """
        你要求：内接长方形不要消减（2px 也要保留）
        所以这里默认：inner_bbox = outer_bbox（不做 shrink）
        """
        if hasattr(r, "inner_bbox") and callable(getattr(r, "inner_bbox")):
            x1, y1, x2, y2 = r.inner_bbox()
            return float(x1), float(y1), float(x2), float(y2)
        return self._outer_bbox(r)

    def _sample_point_in_bbox(self, x1, y1, x2, y2) -> Tuple[float, float]:
        # bbox 可能是 2px：必须允许极小范围
        if x2 < x1:
            x1, x2 = x2, x1
        if y2 < y1:
            y1, y2 = y2, y1
        # 如果是极小 bbox，直接取中心
        if (x2 - x1) < 1.0 and (y2 - y1) < 1.0:
            return (0.5 * (x1 + x2), 0.5 * (y1 + y2))
        gx = np.random.uniform(x1, x2)
        gy = np.random.uniform(y1, y2)
        return float(gx), float(gy)

    def _click_region(self, key: str, use_foreground: bool):
        r = self._require_region(key)
        x1, y1, x2, y2 = self._inner_bbox(r)  # 点击用“内接”
        gx, gy = self._sample_point_in_bbox(x1, y1, x2, y2)

        if use_foreground:
            window_manager.click(gx, gy)
        else:
            window_manager.click_background(gx, gy)

        # 点击日志节流：相同区域每1秒最多输出一次
        now = time.time()
        throttle_key = f"click_{key}"
        last_time = self._click_log_throttle.get(throttle_key, 0)
        if now - last_time >= 1.0:
            self.bot.emit_and_log(f"🖱 点击区域 {key} -> ({gx:.0f},{gy:.0f})", "DEBUG")
            self._click_log_throttle[throttle_key] = now

    def _grab_region_outer(self, key: str) -> Optional[Image.Image]:
        r = self._require_region(key)
        x1, y1, x2, y2 = self._outer_bbox(r)  # OCR/模板匹配用“外接”
        return window_manager.grab_game_bbox(x1, y1, x2, y2)

    def _grab_probe_image(self) -> Optional[Image.Image]:
        r = self._require_region(self.KEY_PROBE)
        x1, y1, x2, y2 = self._outer_bbox(r)
        return window_manager.grab_game_bbox(x1, y1, x2, y2)
    
    def _hv_consume_kernel_line(self, line: str):
        # 只在入战前监听（你要求的“发起对战到 petItem 前”）
        if not getattr(self, "_hv_prebattle", True):
            return

        hv = getattr(self, "hv_watcher", None)
        if not hv:
            return

        evt = hv.observe_lines([line])
        if not evt:
            return

        self.bot.emit_and_log(
            f"🧩 检测到疑似人机验证 pet/swf burst：stack={evt.get('stack_pid')} opts={evt.get('option_pids')}",
            "WARN",
        )

        hv.capture_and_save(evt)

        # 跟你以前一样：自动暂停等待人工处理（空格继续 / ESC 中止）
        self.bot.is_paused = True

    def _click_region_twice(self, key: str, use_foreground: bool, gap: float = 0.06):
        # 连续点两次，提升“胶囊点不到”的成功率
        self._click_region(key, use_foreground)
        time.sleep(max(0.0, gap))
        self._click_region(key, use_foreground)

    # -------------------------
    # Screen calibration (点击触发对战后的颜色校准)
    # -------------------------
    _GAME_SMALL_PROBE_CANDS = ["游戏.小探针", "游戏.探针.小探针", "游戏.小探针区域"]
    _GAME_BIG_PROBE_CANDS = ["游戏.大探针", "游戏.探针.大探针", "游戏.大探针区域"]

    @staticmethod
    def _hex_rgb(hex_str: str) -> Tuple[int, int, int]:
        s = (hex_str or "").strip().lstrip("#")
        if len(s) != 6:
            raise ValueError(f"bad hex rgb: {hex_str!r}")
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)

    def _region_center(self, reg_or_key: str) -> Tuple[float, float]:
        if isinstance(reg_or_key, str):
            r = self._require_region(reg_or_key)
            x1, y1, x2, y2 = self._outer_bbox(r)
        else:
            r = reg_or_key
            try:
                x1, y1, x2, y2 = r.outer_bbox()
            except Exception:
                # dict/其他结构兜底
                x1, y1, x2, y2 = self._outer_bbox(r)
        return (x1 + x2) / 2.0, (y1 + y2) / 2.0

    def _click_xy(self, x: float, y: float, use_foreground: bool):
        if use_foreground:
            window_manager.click(x, y)
        else:
            window_manager.click_background(x, y)

    def _mean_rgb_of_region(self, key: str) -> Optional[Tuple[int, int, int]]:
        try:
            img = self._grab_region_outer(key)
        except Exception:
            return None
        if img is None:
            return None
        arr = np.asarray(img.convert("RGB"), dtype=np.float32)
        if arr.size == 0:
            return None
        mean = np.round(arr.mean(axis=(0, 1))).astype(int)
        return int(mean[0]), int(mean[1]), int(mean[2])

    def _color_ratio_of_region(self, key: str, target_rgb: Tuple[int, int, int], tol: int = 10) -> float:
        try:
            img = self._grab_region_outer(key)
        except Exception:
            return 0.0
        if img is None:
            return 0.0
        tr, tg, tb = target_rgb
        arr = np.asarray(img.convert("RGB"), dtype=np.int16)
        if arr.size == 0:
            return 0.0
        dr = np.abs(arr[:, :, 0] - tr)
        dg = np.abs(arr[:, :, 1] - tg)
        db = np.abs(arr[:, :, 2] - tb)
        ok = (dr <= tol) & (dg <= tol) & (db <= tol)
        return float(ok.mean())

    def _find_existing_region_key(self, candidates: List[str]) -> Optional[str]:
        for k in candidates:
            if self._rs_get(k):
                return k
        return None

    def _find_game_cell_keys(self) -> Dict[str, str]:
        """返回 {"1a": "游戏.1a", ...}，尽量兼容大小写。"""
        out: Dict[str, str] = {}
        keys: List[str] = []
        try:
            if hasattr(self.regions, "keys"):
                keys = list(self.regions.keys())
            elif isinstance(self.regions, dict):
                keys = list(self.regions.keys())
        except Exception:
            keys = []

        pat = re.compile(r"^游戏\.(?P<i>[1-4])(?P<ab>[aAbB])$")
        for k in keys:
            m = pat.match(str(k))
            if not m:
                continue
            kk = f"{m.group('i')}{m.group('ab').lower()}"
            if kk not in out:
                out[kk] = str(k)

        # 兜底：如果 keys() 不可用，直接尝试 8 个标准 key
        for i in range(1, 5):
            for ab in ("a", "b"):
                kk = f"{i}{ab}"
                cand = f"游戏.{i}{ab}"
                if kk not in out and self._rs_get(cand):
                    out[kk] = cand

        return out

    


    # -------------------------
    # probe model
    # -------------------------
    def _load_probe_templates(self) -> ProbeModel:
        if not os.path.exists(self.PROBE_BLUE) or not os.path.exists(self.PROBE_GRAY):
            self.bot.emit_and_log(
                f"❌ 探针模板不存在：\n- {self.PROBE_BLUE}\n- {self.PROBE_GRAY}",
                "ERROR",
            )
            raise FileNotFoundError("probe templates missing")

        blue_img = Image.open(self.PROBE_BLUE).convert("RGB")
        gray_img = Image.open(self.PROBE_GRAY).convert("RGB")

        blue_ref = _blue_strength(blue_img)
        gray_ref = _blue_strength(gray_img)
        span = max(10.0, abs(blue_ref - gray_ref))

        ahash_size = 10
        blue_bits = _ahash_bits(blue_img, size=ahash_size)
        gray_bits = _ahash_bits(gray_img, size=ahash_size)

        force = os.environ.get("NIEO_PROBE_MODE", "").strip().upper()
        color_gap = abs(blue_ref - gray_ref)
        if force in ("COLOR", "AHASH"):
            mode = force
        else:
            mode = "COLOR" if color_gap >= 6.0 else "AHASH"

        if mode == "COLOR":
            self.bot.emit_and_log(
                f"🧪 探针模式=COLOR (blue_ref={blue_ref:.2f}, gray_ref={gray_ref:.2f}, gap={color_gap:.2f})",
                "DEBUG",
            )
            return ProbeModel(mode="COLOR", blue_ref=blue_ref, gray_ref=gray_ref, span=span, tie_eps=0.05)

        self.bot.emit_and_log(
            f"🧪 探针模式=AHASH (gap={color_gap:.2f}) size={ahash_size}",
            "DEBUG",
        )
        return ProbeModel(mode="AHASH", blue_bits=blue_bits, gray_bits=gray_bits, ahash_size=ahash_size, tie_eps=0.03)

    def _detect_probe(self, model: ProbeModel) -> Tuple[str, float, float]:
        img = self._grab_probe_image()
        if img is None:
            return ("UNKNOWN", 0.0, 0.0)

        try:
            if model.mode == "COLOR":
                v = _blue_strength(img)
                s_blue = max(0.0, 1.0 - abs(v - model.blue_ref) / float(model.span))
                s_gray = max(0.0, 1.0 - abs(v - model.gray_ref) / float(model.span))
                if abs(s_blue - s_gray) < model.tie_eps:
                    return ("UNKNOWN", s_blue, s_gray)
                return ("BLUE", s_blue, s_gray) if s_blue > s_gray else ("GRAY", s_blue, s_gray)

            bits = _ahash_bits(img, size=model.ahash_size)
            s_blue = _sim_bits(bits, model.blue_bits)
            s_gray = _sim_bits(bits, model.gray_bits)
            if abs(s_blue - s_gray) < model.tie_eps:
                return ("UNKNOWN", s_blue, s_gray)
            return ("BLUE", s_blue, s_gray) if s_blue > s_gray else ("GRAY", s_blue, s_gray)

        except Exception:
            return ("UNKNOWN", 0.0, 0.0)

    # -------------------------
    # kernel pattern
    # -------------------------
    @staticmethod
    def _has_peticon(line: str) -> bool:
        return "/resource/item/petItem/icon/" in line

    @staticmethod
    def _has_map(line: str) -> bool:
        return "/resource/map/" in line

    @staticmethod
    def _has_newnpc(line: str) -> bool:
        return "/resource/newNpc/multi/0.swf" in line

    def _extract_pet_swf_id(self, line: str) -> Optional[int]:
        m = self._PET_SWF_RE.search(line)
        if not m:
            return None
        try:
            return int(m.group(1))
        except Exception:
            return None

    # -------------------------
    # OCR (可选：没装 pytesseract 也不崩)
    # -------------------------
    def _ocr_text(self, img: Image.Image) -> str:
        try:
            import pytesseract  # pip install pytesseract
        except Exception:
            return ""

        try:
            # 中文需要你本机装 Tesseract + chi_sim 语言包
            txt = pytesseract.image_to_string(img, lang="chi_sim")
            return (txt or "").strip()
        except Exception:
            # 有些环境没装 chi_sim，会异常
            try:
                txt = pytesseract.image_to_string(img)
                return (txt or "").strip()
            except Exception:
                return ""

    def _parse_view_side(self, text: str) -> str:
        # 你要的三种：正面 / 侧面 / 背面
        if "正面" in text:
            return "正面"
        if "侧面" in text:
            return "侧面"
        if "背面" in text:
            return "背面"
        return "未知"

    # -------------------------
    # Stage1.5 人机验证：截图入库 + 自动暂停
    # -------------------------
    def _ensure_hv_dirs(self) -> Tuple[str, str]:
        from config import HV_SAMPLES_PATH
        unlab = os.path.join(HV_SAMPLES_PATH, self.HV_UNLABELED)
        lab = os.path.join(HV_SAMPLES_PATH, self.HV_LABELED)
        os.makedirs(unlab, exist_ok=True)
        os.makedirs(lab, exist_ok=True)
        return unlab, lab

    def _capture_human_verify(self, pet_ids_last4: List[int]):
        # 1) 最大化窗口
        try:
            window_manager.maximize_window()
        except Exception:
            pass

        # 2) OCR 文字信息
        ocr_text = ""
        side = "未知"
        try:
            info_img = self._grab_region_outer(self.KEY_HV_TEXT)
            if info_img is not None:
                ocr_text = self._ocr_text(info_img)
                side = self._parse_view_side(ocr_text)
        except Exception:
            pass

        self.bot.emit_and_log(f"🧩 人机验证：OCR={ocr_text or '(空)'} | 解析={side}", "WARN")

        # 3) 截图整个验证面板
        panel_img = None
        try:
            panel_img = self._grab_region_outer(self.KEY_HV_PANEL)
        except Exception as e:
            self.bot.emit_and_log(f"❌ 人机验证截图失败：{e}", "ERROR")
            return

        if panel_img is None:
            self.bot.emit_and_log("❌ 人机验证截图失败：grab 返回 None", "ERROR")
            return

        unlab_dir, _ = self._ensure_hv_dirs()
        ts = time.strftime("%Y%m%d_%H%M%S")
        pet_str = "-".join(str(x) for x in pet_ids_last4)
        fname = f"{ts}__{side}__{pet_str}.png"
        img_path = os.path.join(unlab_dir, fname)
        meta_path = os.path.join(unlab_dir, f"{ts}__{side}__{pet_str}.json")

        panel_img.save(img_path)

        meta = {
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "side": side,
            "pet_ids": pet_ids_last4,
            "ocr_text": ocr_text,
            "image": os.path.basename(img_path),
        }
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        self.bot.emit_and_log(f"💾 人机验证样本已保存：{img_path}", "SUCCESS")

        # 4) 自动暂停，等你手动处理；空格继续 / ESC 中止
        self.bot.is_paused = True
        self.bot.emit_and_log("⏸ 已自动暂停：请手动完成人机验证，完成后按【空格】继续", "SYSTEM")

    def _hv_burst_update_and_maybe_trigger(self, line: str):
        now = time.time()

        # cooldown：避免同一波重复触发
        if now < self._hv_cooldown_until:
            return

        pid = self._extract_pet_swf_id(line)
        if pid is None:
            return

        self._pet_burst.append((now, pid))

        # 只保留最近 1.2 秒的 burst
        while self._pet_burst and (now - self._pet_burst[0][0]) > 1.2:
            self._pet_burst.popleft()

        # 至少 4 条才认为可能是人机
        if len(self._pet_burst) < 4:
            return

        ids = [x[1] for x in self._pet_burst]
        last4 = ids[-4:]

        # 触发一次
        self._hv_cooldown_until = now + 6.0
        self._pet_burst.clear()

        self.bot.emit_and_log(f"🧩 检测到疑似人机验证 pet/swf burst：{last4}", "WARN")
        # 捕获 + 暂停
        self._capture_human_verify(last4)

    # -------------------------
    # 每 5 场训练室恢复一次（Stage0）
    # -------------------------
    def _first_existing_key(self, candidates: List[str]) -> Optional[str]:
        for k in candidates:
            try:
                if self._rs_get(k):
                    return k
            except Exception:
                continue
        return None

    def _recover_training_room_once(self, use_foreground: bool):
        keys = {
            "open_bag": self._first_existing_key(self.RECOVER_KEYS_CANDIDATES["open_bag"]),
            "recover":  self._first_existing_key(self.RECOVER_KEYS_CANDIDATES["recover"]),
            "confirm":  self._first_existing_key(self.RECOVER_KEYS_CANDIDATES["confirm"]),
            "close_bag":self._first_existing_key(self.RECOVER_KEYS_CANDIDATES["close_bag"]),
        }

        if not all(keys.values()):
            self.bot.emit_and_log(f"⚠ 训练室恢复：缺少 region（请在 BattleRunner.RECOVER_KEYS_CANDIDATES 里对齐你的命名）", "WARN")
            return

        self.bot.emit_and_log("🩹 训练室恢复：打开背包 → 恢复 → 确认 → 关闭", "SYSTEM")

        self._click_region(keys["open_bag"], use_foreground=use_foreground)
        time.sleep(1.5)

        self._click_region(keys["recover"], use_foreground=use_foreground)
        time.sleep(0.5)

        self._click_region(keys["confirm"], use_foreground=use_foreground)
        time.sleep(0.2)

        self._click_region(keys["close_bag"], use_foreground=use_foreground)
        time.sleep(0.2)



    # -------------------------
    # 主逻辑：单场击败（你现有的第二环节）
    # -------------------------
    def run_defeat_mode(
        self,
        use_foreground: bool = False,
        skill_key: Optional[str] = None,
        on_round1_skill_used: Optional[Callable[[str], None]] = None,
    ):
        """
        - skill_key: 允许外部传“对战.使用技能一/二/三/四”，默认 KEY_SKILL4
        - on_round1_skill_used: 第一回合点了任意技能后触发一次（用于 OCR 等）
        """
        # ✅ 允许别的逻辑用技能1也能触发
        skill_key = skill_key or self.KEY_SKILL4

        # 基本检查
        self._require_region(skill_key)
        self._require_region(self.KEY_PROBE)

        probe_model = self._load_probe_templates()

        if not window_manager.find_window():
            self.bot.emit_and_log("❌ 未检测到游戏窗口：请先点【启动游戏】", "ERROR")
            return

        self.bot.emit_and_log("⚔ 自动击败：已启动（请先点击精灵进入对战）", "SYSTEM")
        self.bot.emit_and_log("⏳ 第一回合：等待内核日志 /resource/item/petItem/icon/ ...", "SYSTEM")

        # 监听内核
        self._start_kernel_listen()

        round_idx = 0
        map_seen_at: Optional[float] = None
        npc_seen = False

        blue_streak = 0
        armed = False
        last_probe_log = 0.0

        # ✅ 关键：只在“第一回合任意技能点击后”触发一次
        round1_hook_fired = False

        t0 = time.time()
        # --- 人机验证：每次触发对战后，入战前循环 OCR 扫描，直到 petItem/fallback 入战 ---
        hv_found = False
        hv_last_text = ""
        hv_next_scan = t0 + 1.5      # 你之前定的 1.5s
        hv_interval = 0.25
        hv_finalized = False

        def _hv_finalize_once():
            nonlocal hv_finalized
            if hv_finalized:
                return
            hv_finalized = True
            if not hv_found:
                self.bot.emit_and_log("🤖 无人机验证", "INFO")



        def _fire_round1_hook_if_needed():
            nonlocal round1_hook_fired
            if round1_hook_fired:
                return
            if on_round1_skill_used is None:
                return
            # round_idx==1 才是第一回合技能已点
            if round_idx != 1:
                return
            round1_hook_fired = True
            try:
                # ✅ 给 UI 一点时间：你说的“petItem加载后 + 第一回合技能执行完那个时机”
                time.sleep(0.12)
                on_round1_skill_used(skill_key)
            except Exception as e:
                self.bot.emit_and_log(f"⚠ Round1 hook 执行异常: {e}", "WARN")

        try:
            while True:
                if getattr(self.bot, "stop_current", False):
                    self.bot.emit_and_log("⛔ 已中止：退出击败模式循环", "WARN")
                    return

                while getattr(self.bot, "is_paused", False) and not getattr(self.bot, "stop_current", False):
                    time.sleep(0.05)

                # 1) 内核队列
                self._hv_prebattle = (round_idx == 0)

                # 入战前扫描（直到 round_idx 变成 1 才停止）
                if (round_idx == 0) and (not hv_found) and getattr(self, "hv_watcher", None):
                    now = time.time()
                    if now >= hv_next_scan:
                        hv_next_scan = now + hv_interval
                        try:
                            txt = self.hv_watcher.scan_info_text()
                            if txt:
                                hv_last_text = txt
                            if txt and self.hv_watcher.contains_keywords(txt):
                                hv_found = True
                                self.bot.emit_and_log(f"🧩 人机验证信息OCR：{txt!r}", "WARN")
                                meta = self.hv_watcher.capture_and_save_text(txt)
                                if meta:
                                    self.bot.emit_and_log(f"📸 人机验证已截图入库：{meta}", "SYSTEM")
                        except Exception as e:
                            self.bot.emit_and_log(f"⚠ 人机验证OCR扫描异常: {e}", "WARN")

                while self._kernel_q:
                    line = self._kernel_q.popleft()

                    if self._has_map(line):
                        if map_seen_at is None:
                            map_seen_at = time.time()
                            self.bot.emit_and_log("🗺 检测到 map 加载信号 /resource/map/", "INFO")

                    if self._has_newnpc(line):
                        if not npc_seen:
                            npc_seen = True
                            self.bot.emit_and_log("📡 检测到 NPC 加载信号 /resource/newNpc/multi/0.swf", "INFO")

                    # ✅ 第一回合信号：petItem/icon
                    if round_idx == 0 and self._has_peticon(line):
                        round_idx = 1
                        _hv_finalize_once()
                        self.bot.emit_and_log("✅ 已入对战：请选择技能 回合数1", "INFO")

                        # 自动点技能（不管是1还是4）
                        self._click_region(skill_key, use_foreground=use_foreground)

                        # ✅ 触发 OCR hook：第一回合“任意技能已点”
                        _fire_round1_hook_if_needed()

                        blue_streak = 0
                        armed = False

                # 结束：map + newNpc
                if map_seen_at is not None and npc_seen:
                    cost = time.time() - t0
                    self.bot.emit_and_log(
                        f"🏁 对战结束：map + newNpc，用时 {cost:.1f}s，总回合={round_idx}",
                        "SUCCESS",
                    )
                    self._battle_count += 1

                    # （你原本 battle_count%5 恢复逻辑如果在这里也保留即可）
                    return

                # 2) fallback：探针判断第一回合
                if round_idx == 0:
                    state, s_blue, s_gray = self._detect_probe(probe_model)
                    if state == "BLUE" and s_blue >= 0.90:
                        round_idx = 1
                        _hv_finalize_once()
                        self.bot.emit_and_log(
                            f"✅ 已入对战：请选择技能（fallback=探针BLUE {s_blue:.3f}） 回合数1",
                            "INFO",
                        )
                        self._click_region(skill_key, use_foreground=use_foreground)

                        # ✅ 同样触发 OCR hook
                        _fire_round1_hook_if_needed()

                        blue_streak = 0
                        armed = False

                    time.sleep(0.03)
                    continue

                # 3) 后续回合：非蓝->连续蓝触发
                state, s_blue, s_gray = self._detect_probe(probe_model)

                now = time.time()
                if now - last_probe_log >= 2.5:
                    last_probe_log = now
                    self.bot.emit_and_log(
                        f"🔎 探针={state} blue={s_blue:.3f} gray={s_gray:.3f} 回合={round_idx}",
                        "DEBUG",
                    )

                if state == "BLUE":
                    blue_streak += 1
                else:
                    blue_streak = 0
                    armed = True

                if armed and state == "BLUE" and blue_streak >= 2:
                    round_idx += 1
                    self.bot.emit_and_log(f"🎯 回合数{round_idx}：检测到可选技能（非蓝→蓝）", "INFO")
                    self._click_region(skill_key, use_foreground=use_foreground)

                    armed = False
                    blue_streak = 0
                    time.sleep(0.05)
                    continue

                time.sleep(0.03)

        finally:
            self._stop_kernel_listen()


    # =========================
    # 校准：触发后检查 (小探针=FFFFFF, 大探针=FE6700)
    # =========================

    _CAL_SMALL_PROBE_KEYS = ("游戏.小探针", "游戏.小探针.json", "小探针", "小探针.json")
    _CAL_BIG_PROBE_KEYS = ("游戏.大探针", "游戏.大探针.json", "大探针", "大探针.json")

    # 1a/1b ... 4a/4b（都在 assets/regions/游戏 目录下）
    _CAL_AREA_KEYS: Tuple[Tuple[str, str], ...] = (
        ("游戏.1a", "游戏.1b"),
        ("游戏.2a", "游戏.2b"),
        ("游戏.3a", "游戏.3b"),
        ("游戏.4a", "游戏.4b"),
    )

    _PETITEM_TOKEN = "/resource/item/petItem/icon/"
    _FIGHT_PET_SWF_TOKEN = "/resource/fightResource/pet/swf/"

    def _emit(self, text: str, level: str = "INFO") -> None:
        if hasattr(self.bot, "emit_and_log") and callable(getattr(self.bot, "emit_and_log")):
            try:
                self.bot.emit_and_log(text, level)
                return
            except Exception:
                pass
        if level == "ERROR":
            log.error(text)
        elif level in ("WARN", "WARNING"):
            log.warning(text)
        else:
            log.info(text)

    def _fetch_kernel_lines(self, cursor: int) -> Tuple[int, List[str]]:
        """兼容 fetch_kernel_since 的多种返回形态。"""
        try:
            res: Any = fetch_kernel_since(cursor)
        except Exception:
            return kernel_cursor(), []

        if isinstance(res, tuple) and len(res) == 2 and isinstance(res[0], int):
            new_cursor, lines = res
            return int(new_cursor), self._coerce_lines(lines)

        return kernel_cursor(), self._coerce_lines(res)

    @staticmethod
    def _coerce_lines(obj: Any) -> List[str]:
        if obj is None:
            return []
        if isinstance(obj, str):
            return [obj]
        if isinstance(obj, list):
            if not obj:
                return []
            if isinstance(obj[0], tuple) and len(obj[0]) >= 3:
                out: List[str] = []
                for t in obj:
                    try:
                        out.append(str(t[-1]))
                    except Exception:
                        pass
                return out
            return [str(x) for x in obj]
        try:
            return [str(x) for x in obj]
        except Exception:
            return []

    def _find_first_region(self, keys: Sequence[str]) -> Optional[Region]:
        for k in keys:
            try:
                reg = self.regions.get(k)
            except Exception:
                reg = None
            if reg:
                return reg
        return None

    def _probe_match(self, reg: Region, target_rgb: Tuple[int, int, int], tol: int, min_ratio: float) -> bool:
        gx1, gy1, gx2, gy2 = reg.outer_bbox()
        img = window_manager.grab_game_bbox(gx1, gy1, gx2, gy2)
        pixels = list(img.getdata())
        if not pixels:
            return False
        tr, tg, tb = target_rgb
        ok = 0
        for r, g, b in pixels:
            if abs(r - tr) <= tol and abs(g - tg) <= tol and abs(b - tb) <= tol:
                ok += 1
        return (ok / len(pixels)) >= float(min_ratio)

    def _mean_rgb(self, reg: Region) -> Tuple[int, int, int]:
        gx1, gy1, gx2, gy2 = reg.outer_bbox()
        img = window_manager.grab_game_bbox(gx1, gy1, gx2, gy2)
        pixels = list(img.getdata())
        if not pixels:
            return (0, 0, 0)
        r = int(round(sum(p[0] for p in pixels) / len(pixels)))
        g = int(round(sum(p[1] for p in pixels) / len(pixels)))
        b = int(round(sum(p[2] for p in pixels) / len(pixels)))
        return (r, g, b)

    def _mean_is_color_strict(self, reg: Region, rgb: Tuple[int, int, int]) -> bool:
        return self._mean_rgb(reg) == rgb

    def _need_screen_calibration(self) -> bool:
        """当 小探针(白) && 大探针(蓝) 同时出现时返回 True。"""
        small_reg = self._find_first_region(self._CAL_SMALL_PROBE_KEYS)
        big_reg = self._find_first_region(self._CAL_BIG_PROBE_KEYS)
        if not small_reg or not big_reg:
            return False

        small_ok = self._probe_match(small_reg, (255, 255, 255), tol=18, min_ratio=0.60)
        big_ok = self._probe_match(big_reg, (47, 167, 238), tol=22, min_ratio=0.55)  # 2FA7EE
        return bool(small_ok and big_ok)

    def _calibrate_click_once(self, use_foreground: bool, use_fallback: bool = False) -> Tuple[Optional[int], bool]:
        """
        执行一次 X1..X4 计算并点击对应组的中点
        
        Returns:
            (点击的组号(1~4)或None, 是否检测到异常pattern)
        """
        # 计算每组的值：0/1/2（1a/1b 区域 mean 是否严格等于 FE6700）
        values: List[int] = []
        regs: List[Tuple[Region, Region]] = []
        for a_key, b_key in self._CAL_AREA_KEYS:
            ra = self.regions.get(a_key)
            rb = self.regions.get(b_key)
            if not ra or not rb:
                values.append(0)
                regs.append((None, None))  # type: ignore
                continue
            va = 1 if self._mean_is_color_strict(ra, (254, 103, 0)) else 0
            vb = 1 if self._mean_is_color_strict(rb, (254, 103, 0)) else 0
            values.append(va + vb)
            regs.append((ra, rb))

        if not regs or all(v == 0 for v in values):
            return None, False

        c1 = sum(1 for v in values if v == 1)
        c2 = sum(1 for v in values if v == 2)
        c0 = sum(1 for v in values if v == 0)
        
        # ✅ 检测异常pattern：2+2+0、4+0+0、或2+1+1
        pattern_str = f"{c2}+{c1}+{c0}"
        is_abnormal_pattern = pattern_str in ("2+2+0", "4+0+0", "2+1+1")
        
        # 如果检测到异常pattern且需要使用fallback逻辑
        if is_abnormal_pattern and use_fallback:
            self._emit(f"⚠️ 检测到异常pattern：{pattern_str}，使用fallback逻辑", "WARN")
            
            # ✅ 尝试排除A、B均为RGB严格为(0,0,0)的点
            filtered_indices = []
            filtered_values = []
            filtered_regs = []
            
            for i, (ra, rb) in enumerate(regs):
                if ra is None or rb is None:
                    continue
                rgb_a = self._mean_rgb(ra)
                rgb_b = self._mean_rgb(rb)
                # 如果A和B都不是(0,0,0)，则保留这个点
                if rgb_a != (0, 0, 0) or rgb_b != (0, 0, 0):
                    filtered_indices.append(i)
                    filtered_values.append(values[i])
                    filtered_regs.append((ra, rb))
            
            if filtered_values:
                # 重新计算过滤后的统计
                f_c1 = sum(1 for v in filtered_values if v == 1)
                f_c2 = sum(1 for v in filtered_values if v == 2)
                f_c0 = sum(1 for v in filtered_values if v == 0)
                f_pattern = f"{f_c2}+{f_c1}+{f_c0}"
                
                self._emit(f"⚠️ 排除(0,0,0)后变为 {f_pattern}", "WARN")
                
                # 如果排除后变成了2+1+0，点击那个值为1的点
                if f_pattern == "2+1+0":
                    idx_in_filtered = filtered_values.index(1)
                    original_idx = filtered_indices[idx_in_filtered]
                    ra, rb = filtered_regs[idx_in_filtered]
                    if ra is None or rb is None:
                        return None, True
                    
                    ax, ay = self._region_center(ra)
                    bx, by = self._region_center(rb)
                    mx = (ax + bx) / 2.0
                    my = (ay + by) / 2.0
                    
                    if use_foreground:
                        window_manager.click(mx, my)
                    else:
                        window_manager.click_background(mx, my)
                    
                    self._emit(f"🧭 Fallback：点击X{original_idx + 1}（排除(0,0,0)后唯一值为1的点）", "WARN")
                    return original_idx + 1, True
                
                # 如果无法排除或排除后为3+0+0，随便点击一个A、B中至少有一个是橙色的点
                # 从原始的4个点中选择（不排除(0,0,0)的点，因为需要点击至少有一个橙色的点）
                orange_candidates = []
                for i, (ra, rb) in enumerate(regs):
                    if ra is None or rb is None:
                        continue
                    rgb_a = self._mean_rgb(ra)
                    rgb_b = self._mean_rgb(rb)
                    # 至少有一个是橙色(254, 103, 0)
                    if rgb_a == (254, 103, 0) or rgb_b == (254, 103, 0):
                        orange_candidates.append(i)
                
                if orange_candidates:
                    idx = orange_candidates[0]  # 随便选第一个
                    ra, rb = regs[idx]
                    if ra is None or rb is None:
                        return None, True
                    
                    ax, ay = self._region_center(ra)
                    bx, by = self._region_center(rb)
                    mx = (ax + bx) / 2.0
                    my = (ay + by) / 2.0
                    
                    if use_foreground:
                        window_manager.click(mx, my)
                    else:
                        window_manager.click_background(mx, my)
                    
                    self._emit(f"🧭 Fallback：点击X{idx + 1}（至少有一个橙色的点）", "WARN")
                    return idx + 1, True
            
            # Fallback逻辑无法解决，返回None但标记为异常pattern
            return None, True
        
        # ✅ 正常逻辑（如果检测到异常pattern但不使用fallback，也走正常逻辑）
        idx: int
        if c2 == 1:
            idx = values.index(2)
        elif c1 == 1:
            idx = values.index(1)
        else:
            # 兜底：点击"最大值"的第一组
            mx = max(values)
            idx = values.index(mx)

        ra, rb = regs[idx]
        if ra is None or rb is None:
            return None, is_abnormal_pattern

        ax, ay = self._region_center(ra)
        bx, by = self._region_center(rb)
        mx = (ax + bx) / 2.0
        my = (ay + by) / 2.0

        if use_foreground:
            window_manager.click(mx, my)
        else:
            window_manager.click_background(mx, my)

        return idx + 1, is_abnormal_pattern

    def _click_any_valid_point(self, use_foreground: bool) -> bool:
        """
        随便点击一个有效的点（非两点都是000的）
        返回True表示成功点击，False表示没有找到有效点
        """
        for a_key, b_key in self._CAL_AREA_KEYS:
            ra = self.regions.get(a_key)
            rb = self.regions.get(b_key)
            if not ra or not rb:
                continue
            
            rgb_a = self._mean_rgb(ra)
            rgb_b = self._mean_rgb(rb)
            
            # 如果A和B不都是(0,0,0)（即至少有一个不是(0,0,0)），则这是一个有效点
            if rgb_a != (0, 0, 0) or rgb_b != (0, 0, 0):
                ax, ay = self._region_center(ra)
                bx, by = self._region_center(rb)
                mx = (ax + bx) / 2.0
                my = (ay + by) / 2.0
                
                if use_foreground:
                    window_manager.click(mx, my)
                else:
                    window_manager.click_background(mx, my)
                
                self._emit(f"🧭 点击有效点（非(0,0,0)的点）", "WARN")
                return True
        
        return False

    def calibrate_after_trigger(
        self,
        trigger_xy: Tuple[float, float],
        use_foreground: bool,
        abort: Optional[Any] = None,
        timeout_s: float = 10.0,
    ) -> bool:
        """
        点击触发对战后统一调用：
        - 若出现校准探针(白+橙=11) -> 执行校准逻辑，直到 11 消失
        - 若没有任何输出(既无 fightResource/pet/swf，也无校准探针) -> 每 0.1s 复点一次触发点
        - 最终等待 PetItem 信号（/resource/item/petItem/icon/）

        返回 True：已进入对战；False：超时未入战（上层应回 A 跳过）。
        """
        abort = abort or (lambda: False)
        cursor = kernel_cursor()
        t0 = time.time()
        last_retry_click = 0.0
        saw_any_output = False

        while (time.time() - t0) < float(timeout_s):
            if abort():
                return False

            # 1) 校准探针：必须先检测到 11，然后持续校准直到 11 消失
            if self._need_screen_calibration():
                self._emit("🧭 检测到屏幕校准探针(白+橙=11) -> 开始校准", "WARN")
                
                # ✅ 两次机会机制
                for chance_num in range(1, 3):  # 机会1和机会2
                    if abort():
                        return False
                    
                    self._emit(f"🧭 机会{chance_num}开始", "INFO")
                    chance_success = False
                    
                    # ✅ 每次机会有两轮：Round 1（正常逻辑）和 Round 2（fallback逻辑）
                    for round_num in range(1, 3):  # Round 1和Round 2
                        if abort():
                            return False
                        
                        # 提前检查：如果已经不需要校准了，直接成功
                        if not self._need_screen_calibration():
                            chance_success = True
                            break
                        
                        self._emit(f"🧭 机会{chance_num} - Round {round_num}开始", "INFO")
                        use_fallback = (round_num == 2)  # Round 2使用fallback逻辑
                        round_success = False
                        round_has_abnormal = False
                        
                        # 执行校准循环（最多30次尝试）
                        for attempt in range(30):
                            if abort():
                                return False
                            
                            if not self._need_screen_calibration():
                                round_success = True
                                chance_success = True
                                break
                            
                            idx, has_abnormal = self._calibrate_click_once(use_foreground, use_fallback=use_fallback)
                            if idx is not None:
                                self._emit(f"🧭 校准点击：X{idx}", "DEBUG")
                                if has_abnormal:
                                    round_has_abnormal = True
                            
                            time.sleep(0.05)
                        
                        if round_success:
                            self._emit(f"✅ 机会{chance_num} - Round {round_num}成功", "SUCCESS")
                            break
                        
                        if round_num == 1:
                            if round_has_abnormal:
                                self._emit(f"⚠️ 机会{chance_num} - Round 1检测到异常pattern，进入Round 2", "WARN")
                            else:
                                self._emit(f"⚠️ 机会{chance_num} - Round 1失败，进入Round 2", "WARN")
                        else:
                            # Round 2也失败了
                            self._emit(f"❌ 机会{chance_num} - Round 2失败", "WARN")
                    
                    # ✅ 机会1的特殊处理：即使Round 2都失败，仍可以随便点击一个有效点
                    if not chance_success and chance_num == 1:
                        self._emit("⚠️ 机会1失败，尝试点击任意有效点", "WARN")
                        if self._click_any_valid_point(use_foreground):
                            time.sleep(0.1)  # 等待一下
                            # 检查1AND1是否消失
                            if not self._need_screen_calibration():
                                self._emit("✅ 点击有效点后1AND1消失，机会1成功", "SUCCESS")
                                chance_success = True
                            else:
                                self._emit("❌ 点击有效点后1AND1仍在，进入机会2", "WARN")
                        else:
                            self._emit("❌ 未找到有效点，进入机会2", "WARN")
                    
                    # ✅ 机会2的特殊处理：如果Round 2还不行，发邮件
                    if not chance_success and chance_num == 2:
                        self._emit("❌ 机会2也失败，发送邮件通知", "ERROR")
                        self._send_calibration_failure_email()
                    
                    if chance_success:
                        break

                # 校准结束后，快速再点一次触发点
                tx, ty = trigger_xy
                if use_foreground:
                    window_manager.click(tx, ty)
                else:
                    window_manager.click_background(tx, ty)
                time.sleep(0.05)

            # 2) kernel 输出检测（PetItem / fightResource）
            cursor, lines = self._fetch_kernel_lines(cursor)
            for ln in lines:
                s = str(ln)
                if self._PETITEM_TOKEN in s:
                    return True
                if (not saw_any_output) and (self._FIGHT_PET_SWF_TOKEN in s):
                    saw_any_output = True

            # 3) 如果什么也没输出（且没触发校准），就每 0.1s 复点一次
            now = time.time()
            if (not saw_any_output) and (not self._need_screen_calibration()):
                if (now - last_retry_click) >= 0.1:
                    tx, ty = trigger_xy
                    if use_foreground:
                        window_manager.click(tx, ty)
                    else:
                        window_manager.click_background(tx, ty)
                    last_retry_click = now

            time.sleep(0.02)

        return False

    def _send_calibration_failure_email(self):
        """发送校准失败邮件"""
        try:
            import smtplib
            from email.mime.text import MIMEText
            from email.header import Header
            
            # 构建邮件内容
            msg = MIMEText("校准机制失败：两次机会（每机会两轮）都无法解决异常pattern（2+2+0、4+0+0或2+1+1）", "plain", "utf-8")
            msg["Subject"] = Header("NieoPilot校准失败通知", "utf-8")
            msg["From"] = "NieoPilot <noreply@nieopilot>"
            msg["To"] = "1713518932qqcom@gmail.com"
            
            # 注意：这里需要配置SMTP服务器
            # 实际使用时需要配置SMTP服务器信息
            self._emit("📧 邮件发送功能需要配置SMTP服务器", "WARN")
            # 如果需要实际发送邮件，需要添加SMTP配置
            # smtp = smtplib.SMTP("smtp.gmail.com", 587)
            # smtp.starttls()
            # smtp.login("your_email@gmail.com", "your_password")
            # smtp.sendmail("your_email@gmail.com", ["1713518932qqcom@gmail.com"], msg.as_string())
            # smtp.quit()
            
        except Exception as e:
            self._emit(f"❌ 发送邮件失败: {e}", "ERROR")

    def run_mantis_capture_mode(
        self,
        map_swf_id: int = 11,
        use_foreground=False,   # bool 或 callable -> bool
        skill1_key: str = "对战.使用技能一",
        invincible_first_round: bool = False,
    ):
        """
        捕捉模式：
        - 第1回合：点技能1（或 invincible_first_round 直接无敌胶囊）
        - 第2回合开始：每回合 先切换捕捉面板 -> 0.25s -> 点胶囊（连续点击两次，提升命中率）
        胶囊节奏：中级(回合2) / 高级(回合3) / 中级(回合4) / 高级...
        - 结束判定：map(+优先 map/{id}.swf) + newNpc/multi/0.swf
        """

        def _uf() -> bool:
            try:
                return bool(use_foreground()) if callable(use_foreground) else bool(use_foreground)
            except Exception:
                return False

        # ✅ 双保险：无敌胶囊只允许螳螂地图（达尔 map=11）
        try:
            if invincible_first_round and int(map_swf_id) != 11:
                self.bot.emit_and_log("⚠ invincible_first_round 已忽略：仅螳螂(map=11)允许无敌胶囊", "WARN")
                invincible_first_round = False
        except Exception:
            invincible_first_round = False

        # --- 关键 region：技能1 + 探针 ---
        self._require_region(skill1_key)
        self._require_region(self.KEY_PROBE)

        # --- 捕捉相关 region（兼容“分开录制”与“合并录制”） ---
        panel = self._first_existing_key([
            "对战.捕捉.切换捕捉面板",
            "对战.捕捉.切换捕捉面板+精灵胶囊",
        ])

        mid = self._first_existing_key([
            "对战.捕捉.中级精灵胶囊",
            "对战.捕捉.中级胶囊",
        ])
        high = self._first_existing_key([
            "对战.捕捉.高级精灵胶囊",
            "对战.捕捉.高级胶囊",
        ])

        combo_mid = self._first_existing_key([
            "对战.捕捉.切换捕捉面板+中级精灵胶囊",
        ])
        combo_high = self._first_existing_key([
            "对战.捕捉.切换捕捉面板+高级精灵胶囊",
        ])

        has_split = bool(panel and mid and high)
        has_combo = bool(combo_mid and combo_high)
        if (not has_split) and (not has_combo):
            raise KeyError(
                "缺少捕捉相关 regions：需要(切换捕捉面板 + 中级/高级胶囊) 或 (切换捕捉面板+中级 / +高级)"
            )

        inv_key = "对战.捕捉.无敌精灵胶囊"
        inv_panel = self._first_existing_key(["对战.捕捉.切换捕捉面板"]) or panel

        probe_model = self._load_probe_templates()

        if not window_manager.find_window():
            self.bot.emit_and_log("❌ 未检测到游戏窗口：请先点【启动游戏】", "ERROR")
            return

        map_sub = f"/resource/map/{int(map_swf_id)}.swf"
        self.bot.emit_and_log(f"🪲 捕捉战斗：启动（map={map_sub}）", "SYSTEM")

        self._start_kernel_listen()

        round_idx = 0
        map_seen_at = None
        any_map_seen_at = None
        npc_seen = False

        blue_streak = 0
        armed = False
        last_probe_log = 0.0
        t0 = time.time()
        last_action_at = 0.0
        
        # 超时保护常量
        MAX_BATTLE_DURATION = 300  # 5分钟总超时
        MAX_ROUND_DURATION = 120   # 2分钟单回合超时
        MAX_ENTRY_DURATION = 30    # 30秒入战超时
        
        # #region agent log
        try:
            with open(r"c:\Users\dayuz\OneDrive\Desktop\nieo\NieoPilot\.cursor\debug.log", "a", encoding="utf-8") as f:
                f.write(json.dumps({"sessionId":"debug-session","runId":"run1","hypothesisId":"B","location":"battle_runner.py:1381","message":"螳螂模式启动","data":{"map_swf_id":map_swf_id,"invincible_first_round":invincible_first_round,"t0":t0},"timestamp":int(time.time()*1000)})+"\n")
        except: pass
        # #endregion

        def _do_capture_round(ridx: int):
            nonlocal last_action_at
            if ridx < 2:
                return

            use_mid = (not ridx % 3 == 0)

            # split：面板 + 胶囊（胶囊连点2次）
            if has_split:
                # 你要求：切换捕捉面板点两次，然后等待约0.5s
                self._click_region_twice(panel, use_foreground=_uf(), gap=0.10)
                time.sleep(0.50)
                cap_key = mid if use_mid else high
                self._click_region_twice(cap_key, use_foreground=_uf(), gap=0.08)
                last_action_at = time.time()
                self.bot.emit_and_log(
                    f"🎯 回合{ridx} 捕捉：面板 -> {'中级' if use_mid else '高级'}胶囊(×2)",
                    "INFO",
                )
                return

            # combo：直接点 combo 两次
            ck = combo_mid if use_mid else combo_high
            self._click_region_twice(ck, use_foreground=_uf(), gap=0.50)
            last_action_at = time.time()
            self.bot.emit_and_log(
                f"🎯 回合{ridx} 捕捉：{'中级' if use_mid else '高级'}（combo×2）",
                "INFO",
            )

        try:
            while True:
                # #region agent log
                elapsed = time.time() - t0
                try:
                    with open(r"c:\Users\dayuz\OneDrive\Desktop\nieo\NieoPilot\.cursor\debug.log", "a", encoding="utf-8") as f:
                        f.write(json.dumps({"sessionId":"debug-session","runId":"run1","hypothesisId":"B","location":"battle_runner.py:1415","message":"螳螂模式主循环迭代","data":{"elapsed":elapsed,"round_idx":round_idx,"npc_seen":npc_seen,"map_seen_at":map_seen_at,"any_map_seen_at":any_map_seen_at,"last_action_at":last_action_at,"time_since_last_action":time.time()-last_action_at if last_action_at > 0 else 0},"timestamp":int(time.time()*1000)})+"\n")
                except: pass
                # #endregion
                
                # 总超时检查
                if elapsed > MAX_BATTLE_DURATION:
                    self.bot.emit_and_log(f"⏱️ 战斗总时长超时（{MAX_BATTLE_DURATION}s），强制退出", "ERROR")
                    # #region agent log
                    try:
                        with open(r"c:\Users\dayuz\OneDrive\Desktop\nieo\NieoPilot\.cursor\debug.log", "a", encoding="utf-8") as f:
                            f.write(json.dumps({"sessionId":"debug-session","runId":"run1","hypothesisId":"B","location":"battle_runner.py:1420","message":"螳螂模式总超时退出","data":{"elapsed":elapsed,"round_idx":round_idx},"timestamp":int(time.time()*1000)})+"\n")
                    except: pass
                    # #endregion
                    return
                
                if getattr(self.bot, "stop_current", False):
                    self.bot.emit_and_log("⛔ 已中止：退出捕捉战斗循环", "WARN")
                    return

                while getattr(self.bot, "is_paused", False) and not getattr(self.bot, "stop_current", False):
                    time.sleep(0.05)

                # 1) 处理内核队列（入战、结束）
                while self._kernel_q:
                    line = self._kernel_q.popleft()

                    if map_sub in line:
                        map_seen_at = time.time()
                    elif self._has_map(line):
                        any_map_seen_at = time.time()

                    if self._has_newnpc(line):
                        npc_seen = True

                    # 第一回合入战信号
                    if round_idx == 0 and self._has_peticon(line):
                        round_idx = 1
                        self.bot.emit_and_log("✅ 已入对战：回合1", "INFO")

                        if invincible_first_round:
                            if inv_panel and self._rs_get(inv_key):
                                # 你要求：切换捕捉面板点两次，然后等待约0.5s
                                self._click_region_twice(inv_panel, use_foreground=_uf(), gap=0.10)
                                time.sleep(0.50)
                                self._click_region_twice(inv_key, use_foreground=_uf(), gap=0.08)
                                time.sleep(0.55)
                                self.bot.emit_and_log("🛡 回合1：无敌精灵胶囊(×2)", "INFO")
                            else:
                                self.bot.emit_and_log("⚠ 无敌胶囊 region 缺失：回退为技能1", "WARN")
                                self._click_region(skill1_key, use_foreground=_uf())
                                time.sleep(0.55)
                        else:
                            self._click_region(skill1_key, use_foreground=_uf())
                            time.sleep(0.55)

                        blue_streak = 0
                        armed = False
                        last_action_at = time.time()

                # 结束判定：map + newNpc
                if npc_seen and (map_seen_at is not None or any_map_seen_at is not None):
                    cost = time.time() - t0
                    self.bot.emit_and_log(
                        f"🏁 对战结束：map + newNpc，用时 {cost:.1f}s，总回合={round_idx}",
                        "SUCCESS",
                    )
                    return

                # 2) round_idx==0 fallback：探针蓝色视为已入战
                if round_idx == 0:
                    # round_idx=0 入战超时
                    entry_elapsed = time.time() - t0
                    if entry_elapsed > MAX_ENTRY_DURATION:
                        self.bot.emit_and_log(f"⏱️ {MAX_ENTRY_DURATION}秒未检测到入战信号，可能卡死", "ERROR")
                        # #region agent log
                        try:
                            with open(r"c:\Users\dayuz\OneDrive\Desktop\nieo\NieoPilot\.cursor\debug.log", "a", encoding="utf-8") as f:
                                f.write(json.dumps({"sessionId":"debug-session","runId":"run1","hypothesisId":"B","location":"battle_runner.py:1502","message":"螳螂模式入战超时退出","data":{"entry_elapsed":entry_elapsed},"timestamp":int(time.time()*1000)})+"\n")
                        except: pass
                        # #endregion
                        return
                    
                    state, s_blue, s_gray = self._detect_probe(probe_model)
                    if state == "BLUE" and s_blue >= 0.90:
                        round_idx = 1
                        self.bot.emit_and_log("✅ 已入对战(fallback探针)：回合1", "INFO")
                        self._click_region(skill1_key, use_foreground=_uf())
                        last_action_at = time.time()
                    time.sleep(0.03)
                    continue

                # 3) 后续回合：非蓝->蓝 触发"可操作"
                state, s_blue, s_gray = self._detect_probe(probe_model)

                now = time.time()
                
                # 回合超时检查
                if round_idx > 0 and (now - last_action_at) > MAX_ROUND_DURATION:
                    self.bot.emit_and_log(f"⏱️ 回合{round_idx}超过{MAX_ROUND_DURATION}秒无动作，可能卡死", "ERROR")
                    # #region agent log
                    try:
                        with open(r"c:\Users\dayuz\OneDrive\Desktop\nieo\NieoPilot\.cursor\debug.log", "a", encoding="utf-8") as f:
                            f.write(json.dumps({"sessionId":"debug-session","runId":"run1","hypothesisId":"B","location":"battle_runner.py:1481","message":"螳螂模式回合超时退出","data":{"round_idx":round_idx,"time_since_last_action":now-last_action_at},"timestamp":int(time.time()*1000)})+"\n")
                    except: pass
                    # #endregion
                    return
                
                if now - last_probe_log >= 2.0:
                    last_probe_log = now
                    self.bot.emit_and_log(
                        f"🔎 探针={state} blue={s_blue:.3f} gray={s_gray:.3f} 回合={round_idx}",
                        "DEBUG",
                    )

                if state == "BLUE":
                    blue_streak += 1
                else:
                    blue_streak = 0
                    armed = True

                # ✅ 更灵敏：blue_streak >= 1 就触发（再加动作间隔避免连点）
                if armed and state == "BLUE" and blue_streak >= 1 and (now - last_action_at) >= 0.12:
                    round_idx += 1
                    self.bot.emit_and_log(f"🎯 回合数{round_idx}：进入捕捉动作", "INFO")
                    _do_capture_round(round_idx)

                    armed = False
                    blue_streak = 0
                    time.sleep(0.05)
                    continue

                time.sleep(0.03)

        finally:
            self._stop_kernel_listen()
