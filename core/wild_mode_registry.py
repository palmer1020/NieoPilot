# core/wild_mode_registry.py
"""从 assets/wild_modes/*.json 加载用户自定义野外稀有模式。"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from core.dar_route_runner import WildCaptureProfile

SCHEMA = "nieopilot_wild_mode_v1"
WILD_MODES_DIR = os.path.join("assets", "wild_modes")

_cache: Optional[Dict[str, WildCaptureProfile]] = None
_manifest_cache: Optional[Dict[str, Dict[str, Any]]] = None

# 历史任务可能仍保存旧的乌索312方案键。统一迁移到唯一的乌索528方案，
# 使 312 走击败逻辑、528 走稀有捕捉逻辑。
_LEGACY_PROFILE_ALIASES = {
    "wusuo_312": "乌索",
}


def slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "mode"


def parse_int_list(raw: Any) -> Tuple[int, ...]:
    return _parse_int_list(raw)


def _parse_int_list(raw: Any) -> Tuple[int, ...]:
    if raw is None:
        return ()
    if isinstance(raw, int):
        return (raw,)
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
        out: List[int] = []
        for p in parts:
            try:
                out.append(int(p))
            except ValueError:
                pass
        return tuple(out)
    if isinstance(raw, (list, tuple)):
        out = []
        for item in raw:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                pass
        return tuple(out)
    return ()


def manifest_to_profile(data: Dict[str, Any]) -> WildCaptureProfile:
    route_hint = str(data.get("route_hint") or data.get("name") or "").strip()
    slug = str(data.get("slug") or slugify(route_hint)).strip()
    map_b = int(data["map_b_id"])
    target_pets = _parse_int_list(data.get("target_pet_ids"))
    target_mp3s = _parse_int_list(data.get("target_mp3_ids"))
    if not target_pets:
        raise ValueError(f"manifest {slug}: target_pet_ids 为空")
    if not target_mp3s:
        target_mp3s = target_pets
    delete_swf = _parse_int_list(data.get("delete_swf_ids"))
    if not delete_swf:
        delete_swf = _parse_int_list(data.get("excluded_pet_ids"))  # 旧字段，含义相同
    display_name = str(data.get("name") or f"{route_hint}({target_pets[0]})").strip()
    map_a = data.get("map_a_id")
    map_a_int = int(map_a) if map_a is not None else None
    entry_stem = str(data.get("map_entry_stem") or "").strip()
    if not entry_stem and map_a_int is not None:
        entry_stem = f"{map_a_int}to{map_b}"
    to_script = str(data.get("to_script") or f"to{route_hint}").strip()

    return WildCaptureProfile(
        name=display_name,
        route_hint=route_hint,
        map_swf_id=map_b,
        target_mp3_id=int(target_mp3s[0]),
        target_pet_id=int(target_pets[0]),
        target_mp3_ids=target_mp3s if len(target_mp3s) > 1 else None,
        target_pet_ids=target_pets if len(target_pets) > 1 else None,
        delete_swf_ids=delete_swf,
        slug=slug,
        to_script=to_script,
        map_zero_id=map_a_int,
        map_entry_stem=entry_stem or None,
    )


def load_all_wild_modes(project_root: str, *, reload: bool = False) -> Dict[str, WildCaptureProfile]:
    global _cache, _manifest_cache
    if _cache is not None and not reload:
        return _cache

    root = os.path.join(os.path.abspath(project_root), WILD_MODES_DIR)
    profiles: Dict[str, WildCaptureProfile] = {}
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
                slug = pf.slug or slugify(pf.route_hint)
                profiles[slug] = pf
                manifests[slug] = data
            except Exception:
                continue

    _cache = profiles
    _manifest_cache = manifests
    return profiles


def list_modes(project_root: str) -> List[Tuple[str, WildCaptureProfile]]:
    modes = load_all_wild_modes(project_root)
    return sorted(modes.items(), key=lambda x: x[1].name)


def get_profile(project_root: str, slug: str) -> Optional[WildCaptureProfile]:
    key = (slug or "").strip()
    canonical_key = _LEGACY_PROFILE_ALIASES.get(key.lower(), key)
    return load_all_wild_modes(project_root).get(canonical_key)


def build_manifest(
    *,
    route_hint: str,
    map_a_id: int,
    map_b_id: int,
    target_pet_ids: Tuple[int, ...],
    target_mp3_ids: Tuple[int, ...],
    delete_swf_ids: Tuple[int, ...],
    import_from: Optional[str] = None,
) -> Dict[str, Any]:
    slug = slugify(route_hint)
    primary = target_pet_ids[0] if target_pet_ids else 0
    return {
        "schema": SCHEMA,
        "slug": slug,
        "name": f"{route_hint}({primary})",
        "route_hint": route_hint,
        "to_script": f"to{route_hint}",
        "map_a_id": int(map_a_id),
        "map_b_id": int(map_b_id),
        "map_entry_stem": f"{int(map_a_id)}to{int(map_b_id)}",
        "target_pet_ids": list(target_pet_ids),
        "target_mp3_ids": list(target_mp3_ids) if target_mp3_ids else list(target_pet_ids),
        "delete_swf_ids": list(delete_swf_ids),
        "import_from": import_from,
    }


def save_manifest(project_root: str, manifest: Dict[str, Any]) -> str:
    import time

    slug = str(manifest.get("slug") or slugify(str(manifest.get("route_hint", ""))))
    manifest["slug"] = slug
    manifest.setdefault("create_time", time.strftime("%Y-%m-%d %H:%M:%S"))
    root = os.path.join(os.path.abspath(project_root), WILD_MODES_DIR)
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, f"{slug}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=4, ensure_ascii=False)
    load_all_wild_modes(project_root, reload=True)
    return path


BUILTIN_WILD_PROFILE_KEYS = frozenset(
    {"mantis", "dugulu", "shuangta", "xiaodouya", "flash_pipi", "eyeball", "埃尔特"}
)

_BUILTIN_RARE_KEYS: Tuple[str, ...] = (
    "shuangta",
    "mantis",
    "dugulu",
    "flash_pipi",
    "xiaodouya",
    "eyeball",
    "埃尔特",
)

def wild_mode_select_label(pf: WildCaptureProfile) -> str:
    """野外/轮换下拉统一显示：嘟拉(618)（map=429）"""
    return f"{pf.name}（map={pf.map_swf_id}）"


def list_rare_select_options(project_root: str) -> List[Tuple[str, str]]:
    """野外/轮换下拉：内置 + assets/wild_modes（去重，标签格式统一）。"""
    items: List[Tuple[str, str]] = []
    seen: set[str] = set()
    for key in _BUILTIN_RARE_KEYS:
        pf = resolve_wild_capture_profile(project_root, key)
        items.append((wild_mode_select_label(pf), key))
        seen.add(key)
        if pf.slug:
            seen.add(pf.slug)
    for slug, pf in list_modes(project_root):
        if slug in seen:
            continue
        items.append((wild_mode_select_label(pf), slug))
        seen.add(slug)
    return items


def resolve_wild_capture_profile(project_root: str, profile_name: str) -> WildCaptureProfile:
    """内置 slug 或 assets/wild_modes 自定义 slug → WildCaptureProfile。"""
    from core.dar_route_runner import (
        DEFAULT_PROFILE_DUGULU,
        DEFAULT_PROFILE_FLASH_PIPI,
        DEFAULT_PROFILE_MANTIS,
        DEFAULT_PROFILE_SHUANGTA,
        DEFAULT_PROFILE_XIAODOUYA,
        EYEBALL_PROFILE,
    )

    key = (profile_name or "mantis").strip()
    key_lower = key.lower()
    key = _LEGACY_PROFILE_ALIASES.get(key_lower, key)
    key_lower = key.lower()
    _builtin = {
        "mantis": DEFAULT_PROFILE_MANTIS,
        "dugulu": DEFAULT_PROFILE_DUGULU,
        "shuangta": DEFAULT_PROFILE_SHUANGTA,
        "xiaodouya": DEFAULT_PROFILE_XIAODOUYA,
        "flash_pipi": DEFAULT_PROFILE_FLASH_PIPI,
        "eyeball": EYEBALL_PROFILE,
    }
    if key_lower in _builtin:
        return _builtin[key_lower]
    custom = get_profile(project_root, key) or get_profile(project_root, key_lower)
    if custom is not None:
        return custom
    return DEFAULT_PROFILE_SHUANGTA
