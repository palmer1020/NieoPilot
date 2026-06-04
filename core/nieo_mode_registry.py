# core/nieo_mode_registry.py
"""从 assets/nieo_modes/*.json 加载用户自定义尼奥三图模式。"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

SCHEMA = "nieopilot_nieo_mode_v1"
NIEO_MODES_DIR = os.path.join("assets", "nieo_modes")
REGION_ROOT_NAME = "assets/regions"
VALID_ACTIONS = frozenset({"capture", "defeat", "skip"})

_cache: Optional[Dict[str, "NieoModeProfile"]] = None
_manifest_cache: Optional[Dict[str, Dict[str, Any]]] = None


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

    return NieoModeProfile(
        name=str(data.get("name") or route_hint).strip(),
        route_hint=route_hint,
        slug=slug,
        to_script=str(data.get("to_script") or f"to{route_hint}").strip(),
        map_a_id=map_a,
        map_b_id=map_b,
        map_c_id=map_c,
        entry_stem=str(data.get("entry_stem") or f"{map_a}to{map_b}").strip(),
        transition_b_to_c=str(
            data.get("transition_b_to_c") or f"{map_b}to{map_c}"
        ).strip(),
        transition_c_to_b=str(
            data.get("transition_c_to_b") or f"{map_c}to{map_b}"
        ).strip(),
        prefix_b=str(data.get("prefix_b") or f"{route_hint}一").strip(),
        prefix_c=str(data.get("prefix_c") or f"{route_hint}二").strip(),
        action_b=action_b,
        action_c=action_c,
        import_from_b=data.get("import_from_b"),
        import_from_c=data.get("import_from_c"),
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
) -> Dict[str, Any]:
    slug = slugify(route_hint)
    prefix_b = f"{route_hint}一"
    prefix_c = f"{route_hint}二"
    return {
        "schema": SCHEMA,
        "slug": slug,
        "name": route_hint,
        "route_hint": route_hint,
        "to_script": f"to{route_hint}",
        "map_a_id": int(map_a_id),
        "map_b_id": int(map_b_id),
        "map_c_id": int(map_c_id),
        "entry_stem": f"{int(map_a_id)}to{int(map_b_id)}",
        "transition_b_to_c": f"{int(map_b_id)}to{int(map_c_id)}",
        "transition_c_to_b": f"{int(map_c_id)}to{int(map_b_id)}",
        "prefix_b": prefix_b,
        "prefix_c": prefix_c,
        "action_b": action_b,
        "action_c": action_c,
        "import_from_b": import_from_b,
        "import_from_c": import_from_c,
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
