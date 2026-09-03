# core/nieo_mode_registry.py
"""从 assets/nieo_modes/*.json 加载用户自定义尼奥三图模式。"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA = "nieopilot_nieo_mode_v1"
NIEO_MODES_DIR = os.path.join("assets", "nieo_modes")
REGION_ROOT_NAME = "assets/regions"
VALID_ACTIONS = frozenset({"capture", "defeat", "skip"})

# 不在 UI 下拉中展示（manifest 仍保留加载）；slug / route_hint / name 任一匹配即隐藏
NIEO_MODE_HIDDEN_FROM_SELECT: frozenset = frozenset({"旧大地之核"})


def is_nieo_mode_visible_in_select(profile: "NieoModeProfile") -> bool:
    """自定义尼奥模式是否应在 Dashboard / 建立器等下拉中展示。"""
    for key in (profile.slug, profile.route_hint, profile.name):
        k = (key or "").strip()
        if k in NIEO_MODE_HIDDEN_FROM_SELECT:
            return False
    return True

# 类尼奥入战允许 ID：全模式公共我方 + 尼尔家族，再并各模式特有遇敌/目标
# 类尼奥 / Pick 标准六宠我方 id。
# 347、683 仅用于岚岚的时段 SWF 目标，不属于当前出战六宠。
NIEO_LIKE_MY_PET_IDS = frozenset({67, 166, 197, 606, 1337, 1459})
NIEO_LIKE_NIE_FAMILY_IDS = frozenset({77, 310, 416})
NIEO_LIKE_GLOBAL_RARE_CAPTURE_IDS = frozenset({799})
NIEO_LIKE_BASE_ALLOWED_PET_IDS = (
    NIEO_LIKE_MY_PET_IDS
    | NIEO_LIKE_NIE_FAMILY_IDS
    | NIEO_LIKE_GLOBAL_RARE_CAPTURE_IDS
    | {1100000}
)
NIEO_LIKE_EXTRA_BY_SLUG: Dict[str, frozenset] = {
    "海洋能量": frozenset({33, 108}),
    "旧大地之核": frozenset({105, 106, 107}),
    "大地之核": frozenset({105, 106, 107}),
    "熔岩晶体": frozenset({35, 36, 37, 135}),
    "露西之核": frozenset({203, 208}),
}
NIEO_LIKE_PURE_ENERGY_EXTRA = frozenset({249, 250, 251, 25, 26, 370})
# 内置尼奥 10/11：各 map 正常会遇见的野怪/稀有目标（逃跑或捕捉），均属预期入战，不应触发重连。
#   map10：10、162    map11：16、27、122
NIEO_BUILTIN_NIEO_MAP_EXTRAS: Dict[int, frozenset] = {
    10: frozenset({10, 162}),
    11: frozenset({16, 27, 122}),
}


def nieo_like_allowed_pet_ids(
    *,
    profile: Optional["NieoModeProfile"] = None,
    pure_energy_resource: bool = False,
    current_map_id: Optional[int] = None,
    built_in_nieo: bool = False,
) -> frozenset:
    """类尼奥模式入战 pet ID 白名单（我方 + 尼尔族 + 模式特有）。"""
    allowed = set(NIEO_LIKE_BASE_ALLOWED_PET_IDS)
    if pure_energy_resource:
        allowed |= NIEO_LIKE_PURE_ENERGY_EXTRA
    elif profile is not None:
        for key in (
            (profile.slug or "").strip(),
            (profile.route_hint or "").strip(),
            (profile.name or "").strip(),
        ):
            if key and key in NIEO_LIKE_EXTRA_BY_SLUG:
                allowed |= NIEO_LIKE_EXTRA_BY_SLUG[key]
        allowed |= set(profile.rare_capture_pets_a)
        allowed |= set(profile.rare_capture_pets_b)
        allowed |= set(profile.rare_capture_pets_c)
        allowed |= set(profile.battle_pet_ids_a)
        allowed |= set(profile.battle_pet_ids_b)
        allowed |= set(profile.battle_pet_ids_c)
    elif built_in_nieo and current_map_id is not None:
        allowed |= NIEO_BUILTIN_NIEO_MAP_EXTRAS.get(int(current_map_id), frozenset())
    return frozenset(allowed)


def nieo_like_allowlist_label(
    *,
    profile: Optional["NieoModeProfile"] = None,
    pure_energy_resource: bool = False,
) -> str:
    if pure_energy_resource:
        return "纯净能量"
    if profile is not None:
        return profile.name or profile.slug or "自定义尼奥"
    return "尼奥"

_manifest_cache: Optional[Dict[str, Dict[str, Any]]] = None
_cache: Optional[Dict[str, "NieoModeProfile"]] = None


def slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "nieo_mode"


@dataclass(frozen=True)
class NieoModeProfile:
    name: str
    route_hint: str
    slug: str
    to_script: str
    map_a_id: int
    map_b_id: int
    map_c_id: int
    entry_stem: str
    transition_b_to_c: str
    transition_c_to_b: str
    prefix_b: str
    prefix_c: str
    action_b: str
    action_c: str
    import_from_b: Optional[str] = None
    import_from_c: Optional[str] = None
    skip_route_points_b: Tuple[str, ...] = ()
    skip_route_points_c: Tuple[str, ...] = ()
    # 默认沿用落图后直接扫描；设为 False 时先点击 Z 点离开刷新位置。
    skip_z_click: bool = True
    # B 图刷新点存在绕行地形时，按该链从相邻点逐段累计距离；Z 为判距基准。
    route_distance_chain_b: Tuple[str, ...] = ()
    prefix_a: str = ""
    action_a: str = "skip"
    transition_b_to_a: str = ""
    transition_a_to_b: str = ""
    rare_capture_pets_a: Tuple[int, ...] = ()
    rare_capture_pets_b: Tuple[int, ...] = ()
    rare_capture_pets_c: Tuple[int, ...] = ()
    # 各图普通战胜目标；仅加入允许入战 ID，不触发稀有捕捉。
    battle_pet_ids_a: Tuple[int, ...] = ()
    battle_pet_ids_b: Tuple[int, ...] = ()
    battle_pet_ids_c: Tuple[int, ...] = ()
    # 自定义资源模式默认使用普通 Pet254（197 青、1337 紫）。
    pet254_mode: str = "normal"
    # 启动时需删除（屏蔽）的 swf id（如背景/干扰精灵），与野外 delete_swf_ids 同义。
    delete_swf_ids: Tuple[int, ...] = ()
    # B 图可能不刷精灵：进图后该秒数内未检测到突变点则放弃本轮 B，切回 A（0=不启用）。
    b_no_spawn_giveup_s: float = 0.0
    # B 图进图认证用白色探针 region key（该图无 newNPC 时，由白→非白代替 newNPC）。
    b_entry_white_probe_key: str = ""
    # 指定任意无 newNpc 地图使用的白探针；未配置时仍兼容上面的 B 图字段。
    white_probe_key: str = ""
    white_probe_map_ids: Tuple[int, ...] = ()
    # 进入 B 图前的可选专用流程：到达 A 图后召唤 NONO，恢复一次，再执行第二段 to 脚本。
    pre_entry_summon_until_action: bool = False
    post_recovery_to_script: str = ""
    # 单 B 图模式不依赖 A/C 传送区域；需要切图或恢复时统一刷新重连。
    stay_on_b_map: bool = False
    recovery_via_reconnect: bool = False
    # 技能累计恢复阈值；sequential_skill_recovery 开启时按技能四→技能三顺序耗尽后恢复。
    skill3_recovery_uses: int = 10
    skill4_recovery_uses: int = 25
    sequential_skill_recovery: bool = False


def nieo_b_to_c_region_key(prefix_b: str) -> str:
    """B 图 → C 图传送点（新命名，较 toC 更易辨识）。"""
    return f"{prefix_b}.BtoC"


def nieo_c_to_b_region_key(prefix_c: str) -> str:
    """C 图 → B 图传送点（新命名，较 toB 更易辨识）。"""
    return f"{prefix_c}.CtoB"


def nieo_b_to_a_region_key(prefix_b: str) -> str:
    """B 图 → A 图传送点。"""
    return f"{prefix_b}.BtoA"


def nieo_a_to_b_region_key(prefix_a: str) -> str:
    """A 图 → B 图传送点。"""
    return f"{prefix_a}.AtoB"


def nieo_legacy_b_to_c_region_key(prefix_b: str) -> str:
    return f"{prefix_b}.toC"


def nieo_legacy_c_to_b_region_key(prefix_c: str) -> str:
    return f"{prefix_c}.toB"


_ABC_BTOC_CTOB_ONLY = frozenset({"海洋能量", "旧大地之核", "大地之核"})


def nieo_btoc_ctob_only_reference(profile: "NieoModeProfile") -> bool:
    """海洋能量 / 旧大地之核 / 大地之核：扫点参考固定 B 图 BtoC、C 图 CtoB（不看上一轮地图）。"""
    slug = (profile.slug or "").strip()
    hint = (profile.route_hint or profile.name or "").strip()
    return slug in _ABC_BTOC_CTOB_ONLY or hint in _ABC_BTOC_CTOB_ONLY


def nieo_scan_reference_keys(
    profile: "NieoModeProfile",
    current_map_id: int,
    previous_map_id: Optional[int] = None,
) -> Tuple[str, str]:
    """
    自定义 ABC 三图：变化点「最近」判距用的邻图传送参考点。

    海洋能量 / 旧大地之核：固定 B 图 → BtoC，C 图 → CtoB。
    其它 ABC 模式：取决于上一轮所在地图（B 图 BtoA 或 BtoC 等）。
    """
    cur = int(current_map_id)

    if nieo_btoc_ctob_only_reference(profile):
        if cur == int(profile.map_b_id):
            return (
                nieo_b_to_c_region_key(profile.prefix_b),
                nieo_legacy_b_to_c_region_key(profile.prefix_b),
            )
        if cur == int(profile.map_c_id):
            return (
                nieo_c_to_b_region_key(profile.prefix_c),
                nieo_legacy_c_to_b_region_key(profile.prefix_c),
            )
        return ("", "")

    prev = int(previous_map_id) if previous_map_id is not None else None

    if cur == int(profile.map_b_id):
        if prev == int(profile.map_a_id):
            return (
                nieo_b_to_a_region_key(profile.prefix_b),
                f"{profile.prefix_b}.toA",
            )
        if prev == int(profile.map_c_id):
            return (
                nieo_b_to_c_region_key(profile.prefix_b),
                nieo_legacy_b_to_c_region_key(profile.prefix_b),
            )
        # 首开 A→B、或 prev 未记录/异常：有 A 分叉则默认 BtoA（海洋能量/旧大地之核 A skip 循环）
        if profile.transition_b_to_a:
            return (
                nieo_b_to_a_region_key(profile.prefix_b),
                f"{profile.prefix_b}.toA",
            )
        return (
            nieo_b_to_c_region_key(profile.prefix_b),
            nieo_legacy_b_to_c_region_key(profile.prefix_b),
        )

    if cur == int(profile.map_c_id):
        return (
            nieo_c_to_b_region_key(profile.prefix_c),
            nieo_legacy_c_to_b_region_key(profile.prefix_c),
        )

    if cur == int(profile.map_a_id):
        return (
            nieo_a_to_b_region_key(profile.prefix_a),
            f"{profile.prefix_a}.toB",
        )

    return ("", "")


def nieo_skip_z_click(profile: "NieoModeProfile") -> bool:
    """Return whether this configured mode scans immediately without clicking Z."""
    return bool(profile.skip_z_click)


def nieo_neighbor_transition_keys(profile: "NieoModeProfile", current_map_id: int) -> Tuple[str, str]:
    """返回 (首选 key, 旧版 key)。"""
    if int(current_map_id) == int(profile.map_b_id):
        return (
            nieo_b_to_c_region_key(profile.prefix_b),
            nieo_legacy_b_to_c_region_key(profile.prefix_b),
        )
    if int(current_map_id) == int(profile.map_c_id):
        return (
            nieo_c_to_b_region_key(profile.prefix_c),
            nieo_legacy_c_to_b_region_key(profile.prefix_c),
        )
    return ("", "")


def nieo_transition_to_target(
    profile: "NieoModeProfile", current_map_id: int, target_map_id: int
) -> Tuple[str, str]:
    """返回 (region_key, map_script_stem)。"""
    cur, tgt = int(current_map_id), int(target_map_id)
    if cur == int(profile.map_b_id) and tgt == int(profile.map_c_id):
        return nieo_b_to_c_region_key(profile.prefix_b), profile.transition_b_to_c
    if cur == int(profile.map_c_id) and tgt == int(profile.map_b_id):
        return nieo_c_to_b_region_key(profile.prefix_c), profile.transition_c_to_b
    if cur == int(profile.map_b_id) and tgt == int(profile.map_a_id):
        return nieo_b_to_a_region_key(profile.prefix_b), profile.transition_b_to_a
    if cur == int(profile.map_a_id) and tgt == int(profile.map_b_id):
        return nieo_a_to_b_region_key(profile.prefix_a), profile.transition_a_to_b
    return "", ""


def _parse_rare_capture_pet_ids(raw: Any) -> Tuple[int, ...]:
    if not raw:
        return ()
    out: List[int] = []
    for item in raw if isinstance(raw, (list, tuple)) else str(raw).replace("，", ",").split(","):
        s = str(item).strip()
        if not s:
            continue
        try:
            out.append(int(s))
        except ValueError:
            continue
    return tuple(sorted(set(out)))


def nieo_rare_capture_pets_on_map(profile: "NieoModeProfile", map_id: int) -> frozenset:
    mid = int(map_id)
    if mid == int(profile.map_a_id):
        return frozenset(profile.rare_capture_pets_a)
    if mid == int(profile.map_b_id):
        return frozenset(profile.rare_capture_pets_b)
    if mid == int(profile.map_c_id):
        return frozenset(profile.rare_capture_pets_c)
    return frozenset()


def nieo_skip_route_keys(prefix: str, suffixes: Sequence[str]) -> frozenset:
    """manifest 中可写 \"4\" 或 \"前缀.4\"。"""
    out: set = set()
    for raw in suffixes or ():
        s = str(raw).strip()
        if not s:
            continue
        out.add(s if "." in s else f"{prefix}.{s}")
    return frozenset(out)


def _regions_dir(project_root: str) -> str:
    return os.path.join(os.path.abspath(project_root), "assets", "regions")


def has_complete_route_1_to_9(project_root: str, prefix: str) -> bool:
    base = os.path.join(_regions_dir(project_root), prefix)
    if not os.path.isdir(base):
        return False
    for i in range(1, 10):
        if not os.path.isfile(os.path.join(base, f"{i}.json")):
            return False
    return True


def find_rare_archives_for_map(
    project_root: str, map_id: int
) -> List[Tuple[str, str]]:
    """返回 [(route_hint, prefix)]：wild_modes.map_b_id 匹配且 1-9 齐全。"""
    from core.wild_mode_registry import load_all_wild_modes

    out: List[Tuple[str, str]] = []
    for _slug, pf in load_all_wild_modes(project_root).items():
        if int(pf.map_swf_id) != int(map_id):
            continue
        prefix = pf.route_hint
        if has_complete_route_1_to_9(project_root, prefix):
            out.append((pf.route_hint, prefix))
    return out


def copy_route_points_1_to_9(
    project_root: str,
    src_prefix: str,
    dst_prefix: str,
    *,
    builder_name: str = "建立器",
) -> List[str]:
    src_dir = os.path.join(_regions_dir(project_root), src_prefix)
    dst_dir = os.path.join(_regions_dir(project_root), dst_prefix)
    os.makedirs(dst_dir, exist_ok=True)
    written: List[str] = []
    for i in range(1, 10):
        src = os.path.join(src_dir, f"{i}.json")
        dst = os.path.join(dst_dir, f"{i}.json")
        if not os.path.isfile(src):
            raise FileNotFoundError(f"缺少 {src}")
        with open(src, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["key"] = f"{dst_prefix}.{i}"
        data["category"] = dst_prefix
        meta = data.get("meta") or {}
        meta["desc"] = f"从 {src_prefix} 复制（{builder_name}）"
        meta["copied_from"] = src_prefix
        data["meta"] = meta
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        written.append(dst)
    return written


def write_region_point(
    project_root: str,
    prefix: str,
    label: str,
    gx: int,
    gy: int,
    *,
    desc: str = "",
) -> str:
    from tools.map_recorder import expand_single_point

    save_dir = os.path.join(_regions_dir(project_root), prefix)
    os.makedirs(save_dir, exist_ok=True)
    region = {
        "key": f"{prefix}.{label}",
        "category": prefix,
        "name": label,
        "shape": "polygon",
        "points": expand_single_point(gx, gy),
        "click": {"random": True},
        "meta": {
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "desc": desc or f"nieo_mode_builder {label}",
        },
    }
    path = os.path.join(save_dir, f"{label}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(region, f, indent=4, ensure_ascii=False)
    return path


def copy_map_transition_to_prefix(
    project_root: str,
    map_stem: str,
    prefix: str,
    label: str,
) -> Optional[str]:
    """从 assets/regions/地图/{stem}.json 复制坐标到 prefix/{label}.json。"""
    src = os.path.join(_regions_dir(project_root), "地图", f"{map_stem}.json")
    if not os.path.isfile(src):
        return None
    with open(src, "r", encoding="utf-8") as f:
        data = json.load(f)
    pts = data.get("points") or []
    if not pts:
        return None
    gx = int(round(sum(p[0] for p in pts) / len(pts)))
    gy = int(round(sum(p[1] for p in pts) / len(pts)))
    return write_region_point(
        project_root, prefix, label, gx, gy, desc=f"来自 地图/{map_stem}.json"
    )


def duplicate_region_label(
    project_root: str,
    prefix: str,
    src_label: str,
    dst_label: str,
    *,
    desc: str = "",
) -> Optional[str]:
    """同前缀下复制 region 文件并改名（如 BtoA→BtoC、AtoB→CtoB）。"""
    src_path = os.path.join(_regions_dir(project_root), prefix, f"{src_label}.json")
    if not os.path.isfile(src_path):
        return None
    with open(src_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    pts = data.get("points") or []
    if not pts:
        return None
    gx = int(round(sum(p[0] for p in pts) / len(pts)))
    gy = int(round(sum(p[1] for p in pts) / len(pts)))
    note = desc or f"复制自 {prefix}.{src_label}"
    return write_region_point(project_root, prefix, dst_label, gx, gy, desc=note)


# 内置尼奥/纯净能量：map_id → regions 前缀（1-9 刷新点）
_BUILTIN_NIEO_MAP_PREFIXES: Tuple[Tuple[int, str, str], ...] = (
    (10, "内置尼奥", "尼奥一"),
    (11, "内置尼奥", "尼奥二"),
    (27, "纯净能量", "纯净能量一"),
    (26, "纯净能量", "纯净能量二"),
)


def find_nieo_archives_for_map(
    project_root: str, map_id: int
) -> List[Tuple[str, str]]:
    """返回 [(显示名, prefix)]：尼奥 manifest 或内置前缀，且 1-9 齐全。"""
    out: List[Tuple[str, str]] = []
    seen: set[str] = set()

    for mid, kind, prefix in _BUILTIN_NIEO_MAP_PREFIXES:
        if int(mid) != int(map_id):
            continue
        if prefix in seen:
            continue
        if has_complete_route_1_to_9(project_root, prefix):
            out.append((f"{kind}·{prefix}", prefix))
            seen.add(prefix)

    for _slug, pf in load_all_nieo_modes(project_root).items():
        if not is_nieo_mode_visible_in_select(pf):
            continue
        for map_match, prefix, tag in (
            (pf.map_b_id, pf.prefix_b, "B"),
            (pf.map_c_id, pf.prefix_c, "C"),
        ):
            if int(map_match) != int(map_id):
                continue
            if prefix in seen:
                continue
            if has_complete_route_1_to_9(project_root, prefix):
                label = f"{pf.route_hint}·{tag}（{prefix}）"
                out.append((label, prefix))
                seen.add(prefix)

    return out


def find_route_archives_for_map(
    project_root: str, map_id: int
) -> List[Tuple[str, str, str]]:
    """合并稀有 + 尼奥档案 [(kind, 显示名, prefix)]，去重 prefix。"""
    merged: List[Tuple[str, str, str]] = []
    seen: set[str] = set()
    for route_hint, prefix in find_rare_archives_for_map(project_root, map_id):
        if prefix in seen:
            continue
        merged.append(("稀有", route_hint, prefix))
        seen.add(prefix)
    for label, prefix in find_nieo_archives_for_map(project_root, map_id):
        if prefix in seen:
            continue
        merged.append(("尼奥", label, prefix))
        seen.add(prefix)
    return merged


def manifest_to_profile(data: Dict[str, Any]) -> NieoModeProfile:
    route_hint = str(data.get("route_hint") or data.get("name") or "").strip()
    slug = str(data.get("slug") or slugify(route_hint)).strip()
    map_a = int(data["map_a_id"])
    map_b = int(data["map_b_id"])
    map_c = int(data["map_c_id"])
    action_b = str(data.get("action_b") or "capture").strip().lower()
    action_c = str(data.get("action_c") or "capture").strip().lower()
    if action_b not in VALID_ACTIONS:
        raise ValueError(f"manifest {slug}: action_b 无效 {action_b!r}")
    if action_c not in VALID_ACTIONS:
        raise ValueError(f"manifest {slug}: action_c 无效 {action_c!r}")

    entry_stem = str(data.get("entry_stem") or f"{map_a}to{map_b}").strip()
    prefix_b = str(data.get("prefix_b") or f"{route_hint}一").strip()
    prefix_c = str(data.get("prefix_c") or f"{route_hint}二").strip()
    prefix_a = str(data.get("prefix_a") or prefix_c).strip()
    action_a = str(data.get("action_a") or "skip").strip().lower()
    if action_a not in VALID_ACTIONS:
        raise ValueError(f"manifest {slug}: action_a 无效 {action_a!r}")

    return NieoModeProfile(
        name=str(data.get("name") or route_hint).strip(),
        route_hint=route_hint,
        slug=slug,
        to_script=str(data.get("to_script") or f"to{route_hint}").strip(),
        map_a_id=map_a,
        map_b_id=map_b,
        map_c_id=map_c,
        entry_stem=entry_stem,
        transition_b_to_c=str(
            data.get("transition_b_to_c") or f"{map_b}to{map_c}"
        ).strip(),
        transition_c_to_b=str(
            data.get("transition_c_to_b") or f"{map_c}to{map_b}"
        ).strip(),
        prefix_b=prefix_b,
        prefix_c=prefix_c,
        action_b=action_b,
        action_c=action_c,
        import_from_b=data.get("import_from_b"),
        import_from_c=data.get("import_from_c"),
        skip_route_points_b=tuple(str(x) for x in (data.get("skip_route_points_b") or [])),
        skip_route_points_c=tuple(str(x) for x in (data.get("skip_route_points_c") or [])),
        skip_z_click=bool(data.get("skip_z_click", True)),
        route_distance_chain_b=tuple(
            str(x).strip()
            for x in (data.get("route_distance_chain_b") or [])
            if str(x).strip()
        ),
        prefix_a=prefix_a,
        action_a=action_a,
        transition_b_to_a=str(
            data.get("transition_b_to_a") or f"{map_b}to{map_a}"
        ).strip(),
        transition_a_to_b=str(data.get("transition_a_to_b") or entry_stem).strip(),
        rare_capture_pets_a=_parse_rare_capture_pet_ids(data.get("rare_capture_pets_a")),
        rare_capture_pets_b=_parse_rare_capture_pet_ids(data.get("rare_capture_pets_b")),
        rare_capture_pets_c=_parse_rare_capture_pet_ids(data.get("rare_capture_pets_c")),
        battle_pet_ids_a=_parse_rare_capture_pet_ids(data.get("battle_pet_ids_a")),
        battle_pet_ids_b=_parse_rare_capture_pet_ids(data.get("battle_pet_ids_b")),
        battle_pet_ids_c=_parse_rare_capture_pet_ids(data.get("battle_pet_ids_c")),
        pet254_mode=str(data.get("pet254_mode") or "normal").strip().lower(),
        delete_swf_ids=_parse_rare_capture_pet_ids(data.get("delete_swf_ids")),
        b_no_spawn_giveup_s=float(data.get("b_no_spawn_giveup_s") or 0.0),
        b_entry_white_probe_key=str(data.get("b_entry_white_probe_key") or "").strip(),
        white_probe_key=str(data.get("white_probe_key") or "").strip(),
        white_probe_map_ids=_parse_rare_capture_pet_ids(data.get("white_probe_map_ids")),
        pre_entry_summon_until_action=bool(data.get("pre_entry_summon_until_action", False)),
        post_recovery_to_script=str(data.get("post_recovery_to_script") or "").strip(),
        stay_on_b_map=bool(data.get("stay_on_b_map", False)),
        recovery_via_reconnect=bool(data.get("recovery_via_reconnect", False)),
        skill3_recovery_uses=max(1, int(data.get("skill3_recovery_uses") or 10)),
        skill4_recovery_uses=max(1, int(data.get("skill4_recovery_uses") or 25)),
        sequential_skill_recovery=bool(data.get("sequential_skill_recovery", False)),
    )


def load_all_nieo_modes(
    project_root: str, *, reload: bool = False
) -> Dict[str, NieoModeProfile]:
    global _cache, _manifest_cache
    if _cache is not None and not reload:
        return _cache

    root = os.path.join(os.path.abspath(project_root), NIEO_MODES_DIR)
    profiles: Dict[str, NieoModeProfile] = {}
    manifests: Dict[str, Dict[str, Any]] = {}
    if os.path.isdir(root):
        for fn in sorted(os.listdir(root)):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if str(data.get("schema", "")) != SCHEMA:
                    continue
                pf = manifest_to_profile(data)
                profiles[pf.slug] = pf
                manifests[pf.slug] = data
            except Exception:
                continue

    _cache = profiles
    _manifest_cache = manifests
    return profiles


def get_profile(project_root: str, slug: str) -> Optional[NieoModeProfile]:
    return load_all_nieo_modes(project_root).get(slug)


def nieo_mode_select_label(pf: NieoModeProfile) -> str:
    """尼奥下拉统一显示：与野外稀有嘟拉格式一致。"""
    b, c = int(pf.map_b_id), int(pf.map_c_id)
    map_part = str(b) if b == c else f"{b}/{c}"
    return f"{pf.name}（map={map_part}）"


def list_nieo_select_options(project_root: str) -> List[Tuple[str, str]]:
    """返回 [(显示名, slug), ...] 供 Dashboard 下拉。"""
    items: List[Tuple[str, str]] = []
    for slug, pf in sorted(
        load_all_nieo_modes(project_root).items(), key=lambda x: x[1].name
    ):
        if not is_nieo_mode_visible_in_select(pf):
            continue
        items.append((nieo_mode_select_label(pf), slug))
    return items


def build_nieo_manifest(
    *,
    route_hint: str,
    map_a_id: int,
    map_b_id: int,
    map_c_id: int,
    action_b: str,
    action_c: str,
    import_from_b: Optional[str] = None,
    import_from_c: Optional[str] = None,
    skip_route_points_b: Optional[Sequence[str]] = None,
    skip_route_points_c: Optional[Sequence[str]] = None,
    prefix_b: Optional[str] = None,
    prefix_c: Optional[str] = None,
    prefix_a: Optional[str] = None,
    action_a: str = "skip",
    rare_capture_pets_a: Optional[Sequence[int]] = None,
    rare_capture_pets_b: Optional[Sequence[int]] = None,
    rare_capture_pets_c: Optional[Sequence[int]] = None,
    battle_pet_ids_a: Optional[Sequence[int]] = None,
    battle_pet_ids_b: Optional[Sequence[int]] = None,
    battle_pet_ids_c: Optional[Sequence[int]] = None,
    delete_swf_ids: Optional[Sequence[int]] = None,
    pet254_mode: str = "normal",
    b_no_spawn_giveup_s: float = 0.0,
    b_entry_white_probe_key: str = "",
    white_probe_key: str = "",
    white_probe_map_ids: Optional[Sequence[int]] = None,
) -> Dict[str, Any]:
    slug = slugify(route_hint)
    prefix_b_val = prefix_b or f"{route_hint}一"
    prefix_c_val = prefix_c or f"{route_hint}二"
    prefix_a_val = prefix_a or prefix_c_val
    entry_stem = f"{int(map_a_id)}to{int(map_b_id)}"
    return {
        "schema": SCHEMA,
        "slug": slug,
        "name": route_hint,
        "route_hint": route_hint,
        "to_script": f"to{route_hint}",
        "map_a_id": int(map_a_id),
        "map_b_id": int(map_b_id),
        "map_c_id": int(map_c_id),
        "entry_stem": entry_stem,
        "transition_b_to_c": f"{int(map_b_id)}to{int(map_c_id)}",
        "transition_c_to_b": f"{int(map_c_id)}to{int(map_b_id)}",
        "transition_b_to_a": f"{int(map_b_id)}to{int(map_a_id)}",
        "transition_a_to_b": entry_stem,
        "prefix_b": prefix_b_val,
        "prefix_c": prefix_c_val,
        "prefix_a": prefix_a_val,
        "action_b": action_b,
        "action_c": action_c,
        "action_a": action_a,
        "import_from_b": import_from_b,
        "import_from_c": import_from_c,
        "skip_route_points_b": list(skip_route_points_b or []),
        "skip_route_points_c": list(skip_route_points_c or []),
        "maintenance_every_battles": 25,
        "maintenance_external_map": "a",
        "nie_capture_switch": "aisifeige",
        "rare_capture_pets_a": [int(x) for x in (rare_capture_pets_a or [])],
        "rare_capture_pets_b": [int(x) for x in (rare_capture_pets_b or [])],
        "rare_capture_pets_c": [int(x) for x in (rare_capture_pets_c or [])],
        "battle_pet_ids_a": [int(x) for x in (battle_pet_ids_a or [])],
        "battle_pet_ids_b": [int(x) for x in (battle_pet_ids_b or [])],
        "battle_pet_ids_c": [int(x) for x in (battle_pet_ids_c or [])],
        "delete_swf_ids": [int(x) for x in (delete_swf_ids or [])],
        "pet254_mode": str(pet254_mode or "normal").strip().lower(),
        "b_no_spawn_giveup_s": max(0.0, float(b_no_spawn_giveup_s or 0.0)),
        "b_entry_white_probe_key": str(b_entry_white_probe_key or "").strip(),
        "white_probe_key": str(white_probe_key or "").strip(),
        "white_probe_map_ids": [int(x) for x in (white_probe_map_ids or [])],
    }


def save_nieo_manifest(project_root: str, manifest: Dict[str, Any]) -> str:
    slug = str(manifest.get("slug") or slugify(str(manifest.get("route_hint", ""))))
    manifest["slug"] = slug
    manifest.setdefault("create_time", time.strftime("%Y-%m-%d %H:%M:%S"))
    root = os.path.join(os.path.abspath(project_root), NIEO_MODES_DIR)
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, f"{slug}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=4, ensure_ascii=False)
    load_all_nieo_modes(project_root, reload=True)
    return path
