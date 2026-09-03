# core/event_pet_mode_registry.py
"""活动精灵模式：单地图 A 挂机，遇敌/战斗策略白盒注册。"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

SCHEMA = "nieopilot_event_pet_mode_v1"
EVENT_PET_MODES_DIR = os.path.join("assets", "event_pet_modes")

_cache: Optional[Dict[str, "EventPetModeProfile"]] = None


def slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "event_pet"


def _parse_int_list(raw: Any) -> Tuple[int, ...]:
    if not raw:
        return ()
    out: List[int] = []
    items = raw if isinstance(raw, (list, tuple)) else str(raw).replace("，", ",").split(",")
    for item in items:
        try:
            out.append(int(str(item).strip()))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(set(out)))


@dataclass(frozen=True)
class EventPetModeProfile:
    """活动精灵：to 到地图 A 后门控通过即开干（无地图 B）。"""

    name: str
    slug: str
    route_hint: str
    to_script: str
    map_a_id: int
    entry_pet_id: int
    region_prefix: str
    hunt_strategy: str
    battle_strategy: str
    min_cycles_before_entry: int = 5
    delete_swf_ids: Tuple[int, ...] = ()
    pick_flight_reverse: int = 1
    pick_flight_pet_id: int = 10


def manifest_to_profile(data: Dict[str, Any]) -> EventPetModeProfile:
    route_hint = str(data.get("route_hint") or data.get("name") or "").strip()
    slug = str(data.get("slug") or slugify(route_hint)).strip()
    region_prefix = str(data.get("region_prefix") or route_hint).strip()
    return EventPetModeProfile(
        name=str(data.get("name") or route_hint).strip(),
        slug=slug,
        route_hint=route_hint,
        to_script=str(data.get("to_script") or f"to{route_hint}").strip(),
        map_a_id=int(data["map_a_id"]),
        entry_pet_id=int(data["entry_pet_id"]),
        region_prefix=region_prefix,
        hunt_strategy=str(data.get("hunt_strategy") or "custom").strip(),
        battle_strategy=str(data.get("battle_strategy") or "custom").strip(),
        min_cycles_before_entry=int(data.get("min_cycles_before_entry") or 5),
        delete_swf_ids=_parse_int_list(data.get("delete_swf_ids")),
        pick_flight_reverse=int(data.get("pick_flight_reverse") or 1),
        pick_flight_pet_id=int(data.get("pick_flight_pet_id") or 10),
    )


def load_all_event_pet_modes(
    project_root: str, *, reload: bool = False
) -> Dict[str, EventPetModeProfile]:
    global _cache
    if _cache is not None and not reload:
        return _cache

    root = os.path.join(os.path.abspath(project_root), EVENT_PET_MODES_DIR)
    profiles: Dict[str, EventPetModeProfile] = {}
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
            except Exception:
                continue
    _cache = profiles
    return profiles


def get_profile(project_root: str, slug: str) -> Optional[EventPetModeProfile]:
    return load_all_event_pet_modes(project_root).get(slug)


def list_event_pet_select_options(project_root: str) -> List[Tuple[str, str]]:
    items: List[Tuple[str, str]] = []
    for slug, pf in sorted(
        load_all_event_pet_modes(project_root).items(), key=lambda x: x[1].name
    ):
        items.append((f"{pf.name}（map A={pf.map_a_id}）", slug))
    return items


def build_event_pet_manifest(
    *,
    route_hint: str,
    map_a_id: int,
    entry_pet_id: int,
    to_script: Optional[str] = None,
    region_prefix: Optional[str] = None,
    hunt_strategy: str = "custom",
    battle_strategy: str = "custom",
    min_cycles_before_entry: int = 5,
    delete_swf_ids: Optional[Tuple[int, ...]] = None,
) -> Dict[str, Any]:
    slug = slugify(route_hint)
    prefix = region_prefix or route_hint
    return {
        "schema": SCHEMA,
        "slug": slug,
        "name": route_hint,
        "route_hint": route_hint,
        "to_script": to_script or "to埃尔特",
        "map_a_id": int(map_a_id),
        "entry_pet_id": int(entry_pet_id),
        "region_prefix": prefix,
        "hunt_strategy": hunt_strategy,
        "battle_strategy": battle_strategy,
        "min_cycles_before_entry": int(min_cycles_before_entry),
        "delete_swf_ids": list(delete_swf_ids or ()),
        "create_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_event_pet_manifest(project_root: str, manifest: Dict[str, Any]) -> str:
    slug = str(manifest.get("slug") or slugify(str(manifest.get("route_hint", ""))))
    manifest["slug"] = slug
    root = os.path.join(os.path.abspath(project_root), EVENT_PET_MODES_DIR)
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, f"{slug}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=4, ensure_ascii=False)
    load_all_event_pet_modes(project_root, reload=True)
    return path
