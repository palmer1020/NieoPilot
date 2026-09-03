# core/rotation_schedule.py
"""尼奥·稀有轮换：按北京时间分段调度（资源 / 稀有 / 伊特 / 螳螂 / 尼奥）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

try:
    import pytz

    _BEIJING_TZ = pytz.timezone("Asia/Shanghai")
except ImportError:
    _BEIJING_TZ = timezone(timedelta(hours=8))

# 白天模式下拉：内置纯净能量 + 自定义类尼奥资源模式；Dashboard 还会追加 rare:{slug}
ROTATION_RESOURCE_SLUGS: Tuple[str, ...] = (
    "nieo_resource",
    "pure_energy",
    "海洋能量",
    "大地之核",
    "熔岩晶体",
    "露西之核",
)

ROTATION_RESOURCE_LABELS: Tuple[str, ...] = (
    "尼奥四技能",
    "纯净能量",
    "海洋能量",
    "大地之核",
    "熔岩晶体",
    "露西之核",
)


@dataclass(frozen=True)
class RotationScheduleOptions:
    resource_enabled: bool = False
    resource_slug: str = "rare:乌索"
    mantis_enabled: bool = False
    eit_enabled: bool = False


def normalize_daytime_mode(selection: str) -> str:
    """Dashboard 白天模式选择值 -> 日程 mode。"""
    s = (selection or "rare:乌索").strip()
    if s.startswith("rare:") or s.startswith("resource:"):
        return s
    return f"resource:{s}"


def beijing_now() -> datetime:
    return datetime.now(_BEIJING_TZ)


def _localize_beijing_datetime(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(_BEIJING_TZ)
    localize = getattr(_BEIJING_TZ, "localize", None)
    if callable(localize):
        return localize(dt)
    return dt.replace(tzinfo=_BEIJING_TZ)


def _ensure_beijing(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return _localize_beijing_datetime(dt)
    return dt.astimezone(_BEIJING_TZ)


def build_rotation_day_segments(
    opts: RotationScheduleOptions,
) -> List[Tuple[time, str]]:
    """
    单日分段（按开始时刻升序）。模式：
    rare | rare:{slug} | resource:{slug} | eit | mantis | nieo
    """
    segs: List[Tuple[time, str]] = []
    if opts.resource_enabled:
        segs.append((time(0, 0), "rare"))
        segs.append((time(6, 0), normalize_daytime_mode(opts.resource_slug)))
        segs.append((time(18, 0), "rare"))
    else:
        segs.append((time(0, 0), "rare"))

    if opts.eit_enabled:
        segs.append((time(20, 0), "eit"))
    if opts.mantis_enabled:
        mantis_start = 21 if opts.eit_enabled else 20
        segs.append((time(mantis_start, 0), "mantis"))

    nieo_h = 20
    if opts.eit_enabled:
        nieo_h += 1
    if opts.mantis_enabled:
        nieo_h += 1 if opts.eit_enabled else 2
    segs.append((time(nieo_h, 0), "nieo"))
    return segs


def _segment_datetimes(
    base: date, segs: Sequence[Tuple[time, str]]
) -> List[Tuple[datetime, str]]:
    out: List[Tuple[datetime, str]] = []
    for t, mode in segs:
        out.append((_localize_beijing_datetime(datetime.combine(base, t)), mode))
    midnight_next = datetime.combine(
        base + timedelta(days=1), time(0, 0)
    )
    midnight_next = _localize_beijing_datetime(midnight_next)
    out.append((midnight_next, segs[0][1]))
    return out


def detect_rotation_mode_at(
    now: datetime,
    opts: RotationScheduleOptions,
    *,
    quiet: bool = False,
) -> Tuple[str, datetime]:
    """返回 (current_mode, next_switch_datetime)。"""
    now = _ensure_beijing(now)
    segs = build_rotation_day_segments(opts)
    today = _segment_datetimes(now.date(), segs)
    yesterday = _segment_datetimes(now.date() - timedelta(days=1), segs)
    timeline = yesterday + today

    current_mode = segs[0][1]
    for i, (start, mode) in enumerate(timeline):
        if start <= now:
            current_mode = mode
        if start > now:
            return current_mode, start

    # 不应到达：fallback 次日 0 点
    nxt = _localize_beijing_datetime(datetime.combine(now.date() + timedelta(days=1), time(0, 0)))
    return current_mode, nxt


def rotation_mode_label(mode: str, *, rare_slot: str = "shuangta") -> str:
    if mode == "nieo":
        return "尼奥"
    if mode == "rare":
        return f"稀有({rare_slot})"
    if mode.startswith("rare:"):
        return f"稀有({mode.split(':', 1)[1]})"
    if mode == "mantis":
        return "螳螂"
    if mode == "eit":
        return "伊特"
    if mode == "pure_energy":
        return "纯净能量"
    if mode == "nieo_resource":
        return "尼奥四技能"
    if mode.startswith("resource:"):
        slug = mode.split(":", 1)[1]
        if slug == "pure_energy":
            return "纯净能量"
        if slug == "nieo_resource":
            return "尼奥四技能"
        return slug
    if mode == "shuangta":
        return f"稀有({rare_slot})"
    return mode


def describe_rotation_day(opts: RotationScheduleOptions, *, rare_slot: str = "shuangta") -> str:
    """人类可读的一日时间表（用于 UI 提示）。"""
    segs = build_rotation_day_segments(opts)
    parts: List[str] = []
    for i, (start, mode) in enumerate(segs):
        end = segs[i + 1][0] if i + 1 < len(segs) else time(0, 0)
        end_s = "24:00" if end == time(0, 0) else end.strftime("%H:%M")
        parts.append(
            f"{start.strftime('%H:%M')}-{end_s} {rotation_mode_label(mode, rare_slot=rare_slot)}"
        )
    return "；".join(parts)


def list_all_rotation_combinations(*, rare_slot: str = "双塔") -> List[str]:
    """8 种勾选组合的完整日程说明。"""
    lines: List[str] = []
    flags = (
        (False, False, False, "无附加勾选"),
        (True, False, False, "仅白天模式"),
        (False, True, False, "仅螳螂"),
        (False, False, True, "仅伊特"),
        (True, True, False, "白天模式 + 螳螂"),
        (True, False, True, "白天模式 + 伊特"),
        (False, True, True, "伊特 + 螳螂"),
        (True, True, True, "白天模式 + 伊特 + 螳螂"),
    )
    for res, mantis, eit, title in flags:
        opts = RotationScheduleOptions(
            resource_enabled=res,
            resource_slug="rare:乌索",
            mantis_enabled=mantis,
            eit_enabled=eit,
        )
        lines.append(f"【{title}】{describe_rotation_day(opts, rare_slot=rare_slot)}")
    return lines


def rotation_pick_compat_mode(schedule_mode: str) -> str:
    """Step3 历史参数：nieo 系 vs 稀有系。"""
    if schedule_mode in ("nieo", "pure_energy") or schedule_mode.startswith("resource:"):
        return "nieo"
    return "shuangta"


def rotation_swf_calendar_bucket(schedule_mode: str) -> str:
    mode = str(schedule_mode or "").strip()
    if mode in ("nieo", "nieo_resource", "resource:nieo_resource"):
        return "nieo"
    return "shuangta"
