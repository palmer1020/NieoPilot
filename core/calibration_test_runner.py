# core/calibration_test_runner.py
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from core.logger import fetch_kernel_since, kernel_cursor, wait_kernel_contains
from core.kernel_log_match import RE_NEWNPC_MULTI, first_map_id_in_line, re_map_swf_exact_id
from core.region_store import Region, RegionStore
from core.utils import window_manager

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CalibrationTestConfig:
    expected_map_id: int = 438
    newnpc_substr: str = "/resource/newNpc/multi/0.swf"

    # regions keys (对应 assets/regions/游戏/*.json)
    # 你说的是 游戏\test.json -> 通常 key 为 "游戏.test"
    key_test_click: str = "游戏.test"
    key_probe_small: str = "游戏.小探针"  # #FFFFFF
    key_probe_big: str = "游戏.大探针"    # #2FA7EE （注意：不是 FE6700）

    # 点击强度
    click_interval_sec: float = 0.01  # 高强度点点点
    probe_check_window_sec: float = 0.18
    probe_check_tick_sec: float = 0.02

    # 探针判定参数：11 = 小白(FFFFFF) + 大蓝(2FA7EE)
    small_rgb: Tuple[int, int, int] = (255, 255, 255)        # FFFFFF
    big_rgb: Tuple[int, int, int] = (47, 167, 238)           # 2FA7EE

    tol_small: int = 18
    tol_big: int = 22

    # 建议跟 battle_runner 对齐（你那边 big 用 0.55）
    min_ratio_small: float = 0.60
    min_ratio_big: float = 0.55

    # kernel poll
    kernel_poll_sec: float = 0.05


class CalibrationTestRunner:
    """
    测试脚本：
    - 等待 map=438 + newNpc/multi/0
    - 高强度点击 游戏.test
    - 若某次点击后探针(小白+大蓝)变成 1&1 -> 调用 battle_runner.calibrate_after_trigger(trigger_xy)
    - 全程检测 map 变化，若 map != 438 -> 立刻中止
    """

    def __init__(self, bot, regions: RegionStore, battle_runner, config: Optional[CalibrationTestConfig] = None):
        self.bot = bot
        self.regions = regions
        self.battle_runner = battle_runner
        self.cfg = config or CalibrationTestConfig()

    # ---------------------------
    # public
    # ---------------------------
    def run(self, stop_event: threading.Event, use_foreground: bool) -> None:
        self._emit("🧪 CalibrationTestRunner 启动", "SYSTEM")

        if not window_manager.ensure_game_hwnd():
            self._emit("❌ 未检测到游戏窗口：请先在 Dashboard 点【启动游戏】", "ERROR")
            return

        # 1) 等待进入地图 438 + newNpc/multi/0
        self._emit(
            f"⏳ 等待进入地图：/resource/map/{self.cfg.expected_map_id}.swf + {self.cfg.newnpc_substr}",
            "SYSTEM",
        )
        if not self._wait_kernel_contains_compat(
            re_map_swf_exact_id(self.cfg.expected_map_id), timeout_s=90.0, poll=0.05
        ):
            self._emit("⛔ 等待 map 超时/已停止", "WARN")
            return
        if not self._wait_kernel_contains_compat(RE_NEWNPC_MULTI, timeout_s=90.0, poll=0.05):
            self._emit("⛔ 等待 newNpc 超时/已停止", "WARN")
            return
        self._emit("✅ 已进入测试地图：开始高强度点击 + 探针触发校准", "SUCCESS")

        # 2) regions
        reg_test = self._require_region(self.cfg.key_test_click, hint="游戏/test.json")
        reg_small = self._require_region(self.cfg.key_probe_small, hint="游戏/小探针.json")
        reg_big = self._require_region(self.cfg.key_probe_big, hint="游戏/大探针.json")

        # 3) kernel guard（用于“地图变化中止”，并可被 calibrate 的 abort 回调复用）
        guard = _KernelMapGuard(
            expected_map_id=self.cfg.expected_map_id,
            bot=self.bot,
            stop_event=stop_event,
            poll_sec=self.cfg.kernel_poll_sec,
        )

        # 4) 主循环：高强度点击 test，探针变 11 -> calibrate
        self._emit(
            f"🚀 开始高强度点击：{self.cfg.key_test_click} | interval={self.cfg.click_interval_sec:.2f}s",
            "SYSTEM",
        )

        while not stop_event.is_set() and (not getattr(self.bot, "stop_current", False)):
            self._wait_if_paused(stop_event)

            # (a) 地图变化检测（任何时刻 map!=438 立即中止）
            bad_map = guard.poll_and_get_bad_map()
            if bad_map is not None:
                self._emit(
                    f"❌ 检测到地图变化：/resource/map/{bad_map}.swf（期望 {self.cfg.expected_map_id}）-> 中止测试",
                    "ERROR",
                )
                return

            # (b) 点击 test
            tx, ty = self._click_region(reg_test, use_foreground)

            # (c) 点击后短窗口内检查探针 11（小白+大蓝）
            if self._wait_probe_11(reg_small, reg_big, stop_event):
                self._emit("🟦⬜ 探针 1&1 命中(小白+大蓝) -> 调用 calibrate_after_trigger()", "WARN")

                if not hasattr(self.battle_runner, "calibrate_after_trigger"):
                    self._emit("⚠ battle_runner 未实现 calibrate_after_trigger：跳过调用", "ERROR")
                else:
                    abort_fn = guard.make_abort_fn()
                    try:
                        ok = bool(
                            self.battle_runner.calibrate_after_trigger(
                                trigger_xy=(tx, ty),
                                use_foreground=use_foreground,
                                abort=abort_fn,
                            )
                        )
                    except TypeError:
                        # 兼容旧签名（位置参数）
                        ok = bool(self.battle_runner.calibrate_after_trigger((tx, ty), use_foreground, abort_fn))

                    if guard.bad_map_id is not None:
                        self._emit(
                            f"❌ 校准过程中检测到地图变化：/resource/map/{guard.bad_map_id}.swf -> 中止测试",
                            "ERROR",
                        )
                        return

                    self._emit(f"✅ calibrate_after_trigger 返回：{ok}", "SUCCESS" if ok else "WARN")

            # (d) 节流
            self._sleep_abortable(stop_event, self.cfg.click_interval_sec, tick=0.02)

        self._emit("🛑 CalibrationTestRunner 停止（stop_event / stop_current）", "WARN")

    # ---------------------------
    # probe helpers
    # ---------------------------
    def _wait_probe_11(self, reg_small: Region, reg_big: Region, stop_event: threading.Event) -> bool:
        deadline = time.time() + float(self.cfg.probe_check_window_sec)
        while time.time() < deadline:
            if stop_event.is_set() or getattr(self.bot, "stop_current", False):
                return False
            self._wait_if_paused(stop_event)

            small_ok = self._probe_match(
                reg_small,
                target_rgb=self.cfg.small_rgb,
                tol=self.cfg.tol_small,
                min_ratio=self.cfg.min_ratio_small,
            )
            big_ok = self._probe_match(
                reg_big,
                target_rgb=self.cfg.big_rgb,
                tol=self.cfg.tol_big,
                min_ratio=self.cfg.min_ratio_big,
            )
            if small_ok and big_ok:
                return True

            time.sleep(float(self.cfg.probe_check_tick_sec))
        return False

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

    # ---------------------------
    # region/click helpers
    # ---------------------------
    def _require_region(self, key: str, hint: str = "") -> Region:
        reg = self.regions.get(key)
        if reg:
            return reg
        sug = []
        try:
            if hasattr(self.regions, "suggest"):
                sug = self.regions.suggest(key.split(".")[0], limit=30)
        except Exception:
            pass
        raise KeyError(f"缺少区域 key={key}（{hint}）| suggest={sug}")

    def _click_region(self, reg: Region, use_foreground: bool) -> Tuple[float, float]:
        x, y = reg.sample_click_point()
        if use_foreground:
            window_manager.click(x, y)
        else:
            window_manager.click_background(x, y)
        return float(x), float(y)

    # ---------------------------
    # wait/pause/log helpers
    # ---------------------------
    def _wait_kernel_contains_compat(self, substr, timeout_s: float, poll: float) -> bool:
        try:
            return bool(wait_kernel_contains(substr, timeout_s=timeout_s, poll=poll, cursor=None))
        except TypeError:
            try:
                return bool(wait_kernel_contains(substr, timeout=timeout_s, poll=poll))
            except Exception:
                return False
        except Exception:
            return False

    def _sleep_abortable(self, stop_event: Optional[threading.Event], seconds: float, tick: float = 0.1):
        end = time.time() + float(max(0.0, seconds))
        while time.time() < end:
            if stop_event is not None and stop_event.is_set():
                return
            if getattr(self.bot, "stop_current", False):
                return
            if getattr(self.bot, "is_paused", False):
                time.sleep(0.05)
                continue
            time.sleep(tick)

    def _wait_if_paused(self, stop_event: threading.Event):
        if hasattr(self.bot, "wait_if_paused") and callable(getattr(self.bot, "wait_if_paused")):
            self.bot.wait_if_paused()
            return
        while getattr(self.bot, "is_paused", False) and (not stop_event.is_set()) and (not getattr(self.bot, "stop_current", False)):
            time.sleep(0.05)

    def _emit(self, text: str, level: str = "INFO"):
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


class _KernelMapGuard:
    """
    给主循环和 calibrate_after_trigger 的 abort 回调共用的“地图变化检测器”：
    - 任何时刻发现 /resource/map/{id}.swf 且 id != expected -> bad_map_id 置位
    - abort_fn(): stop_event/stop_current/bad_map -> True
    """

    def __init__(self, expected_map_id: int, bot, stop_event: threading.Event, poll_sec: float = 0.05):
        self.expected_map_id = int(expected_map_id)
        self.bot = bot
        self.stop_event = stop_event
        self.poll_sec = float(poll_sec)

        self.cursor = kernel_cursor()
        self.last_poll = 0.0
        self.bad_map_id: Optional[int] = None

    def poll_and_get_bad_map(self) -> Optional[int]:
        now = time.time()
        if now - self.last_poll < self.poll_sec:
            return self.bad_map_id
        self.last_poll = now

        try:
            res: Any = fetch_kernel_since(self.cursor)
        except Exception:
            return self.bad_map_id

        # 兼容返回形态
        if isinstance(res, tuple) and len(res) == 2 and isinstance(res[0], int):
            self.cursor = int(res[0])
            lines = self._coerce_lines(res[1])
        else:
            self.cursor = kernel_cursor()
            lines = self._coerce_lines(res)

        for ln in lines or []:
            mid = first_map_id_in_line(str(ln))
            if mid is None:
                continue
            if mid != self.expected_map_id:
                self.bad_map_id = mid
                return self.bad_map_id

        return self.bad_map_id

    def make_abort_fn(self):
        def abort() -> bool:
            if self.stop_event.is_set() or getattr(self.bot, "stop_current", False):
                return True
            self.poll_and_get_bad_map()
            return self.bad_map_id is not None
        return abort

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
