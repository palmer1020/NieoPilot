# core/swf_resource_ops.py
"""GUI 与 CLI 共用的微端 SWF 同步；返回 (是否成功, 说明文本)。

覆盖前：若目标文件已存在且尚无 OG 备份，则自动生成 OG（与 fight 目录的 swf_og、PetStorage 同目录下的 .og 文件规则一致）。

普通PetSWF：对 ``pet/swf`` 与 ``groupFightResource/pet`` 两套目录同步执行——
先从各自 ``*_og`` 补齐 live 缺失序号，再对已有 ``*.swf`` 先备份 OG 后覆盖。
普通 Pet254：全量 254 橙色，再将 ``1337.swf`` 置为 252 紫色、``197.swf`` 置为 519 青色。
尼奥 Pet254：全量 254 橙色，再将 ``1337.swf`` 置为 252 紫色、``1459.swf`` 置为 519 青色。
融合SWF：全量 254 橙色，再将 ``77/164/471/480`` 置为 252 紫色、``79/473`` 置为 519 青色。

PetStorage 同步时另处理 ``resource/nono/super``：``nono_1``~``nono_4`` 备份为 ``*.og.swf`` 后用 ``nono_5`` 覆盖，
并将 ``action`` / ``exp`` 改为对应的 ``action_og`` / ``exp_og``；还原 PetStorage OG 时反向还原。

``resource/newNpc/multi`` 下 ``4``/``4.swf`` 应始终隐藏为 ``4_og``/``4_og.swf``（PetStorage 同步、开仓库前、各任务/按钮入口均会幂等检查；兼容旧名 ``og`` 自动迁移）。
PetStorage OG 还原后同样保持 ``4_og``，不再改回 ``4``。

删除 ``delete_swf_ids`` 时：两个 live 目录均检查同名 ``{id}.swf``，存在则先备份 OG 再删。

删除 ``pet/swf`` / ``groupFightResource/pet`` 下文件前：应调用对应的 ``ensure_*_og_before_delete``。
"""
from __future__ import annotations

import json
import os
import filecmp
import shutil
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parent.parent
_FLY_PET_1337_LOCK = threading.Lock()

# 每个用户任务首次仍做一次全目录 254。后续模式切换只重置这个保守并集：
# 颜色编辑目标 + 内置模式目标/删除项 + assets 中所有候选模式的精灵/删除项。
PET_SWF_POTENTIAL_COLOR_IDS = frozenset({
    40, 67, 77, 79, 95, 102, 143, 164, 197, 268, 303, 309, 347, 403,
    416, 471, 473, 480, 528, 568, 606, 631, 667, 683, 1337, 1459,
})
PET_SWF_POTENTIAL_BUILTIN_PET_IDS = frozenset({
    27, 77, 102, 122, 143, 164, 166, 197, 254, 269, 310, 416, 471,
    480, 606, 618, 799, 1337, 1459,
})
PET_SWF_POTENTIAL_BUILTIN_DELETE_IDS = frozenset({
    10, 16, 27, 89, 90, 104, 144, 198, 206, 207, 209, 210, 212, 232,
    233, 234, 252, 278, 279, 280, 491, 492, 493, 523, 524, 525,
    557, 558, 559, 574, 575,
})
PET_SWF_POTENTIAL_STATIC_IDS = frozenset(
    PET_SWF_POTENTIAL_COLOR_IDS
    | PET_SWF_POTENTIAL_BUILTIN_PET_IDS
    | PET_SWF_POTENTIAL_BUILTIN_DELETE_IDS
)
# 兼容旧引用；实际同步入口使用 potential_pet_swf_union_ids() 动态补入模式 JSON。
PET_SWF_ROTATION_COLOR_IDS = PET_SWF_POTENTIAL_COLOR_IDS
PET_SWF_ROTATION_BUILTIN_PET_IDS = PET_SWF_POTENTIAL_BUILTIN_PET_IDS
PET_SWF_ROTATION_BUILTIN_DELETE_IDS = PET_SWF_POTENTIAL_BUILTIN_DELETE_IDS
PET_SWF_ROTATION_STATIC_IDS = PET_SWF_POTENTIAL_STATIC_IDS
PET_SWF_RUNTIME_COLOR_IDS = PET_SWF_POTENTIAL_COLOR_IDS
PET_SWF_RUNTIME_DELETE_IDS = PET_SWF_POTENTIAL_BUILTIN_DELETE_IDS
PET_SWF_RUNTIME_MANAGED_IDS = PET_SWF_POTENTIAL_STATIC_IDS

_POTENTIAL_MANIFEST_DIRS = (
    "wild_modes",
    "nieo_modes",
    "event_pet_modes",
)
_POTENTIAL_MANIFEST_PET_FIELDS = frozenset({
    "delete_swf_ids",
    "excluded_pet_ids",
    "target_pet_ids",
    "target_mp3_ids",
    "entry_pet_id",
    "pick_flight_pet_id",
    "battle_pet_ids_a",
    "battle_pet_ids_b",
    "battle_pet_ids_c",
    "rare_capture_pets_a",
    "rare_capture_pets_b",
    "rare_capture_pets_c",
})


def _ensure_project_path() -> None:
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))


def ensure_fly_pet_483_and_1337_from_50(
    fly_pet_dir: Optional[Path] = None,
) -> Tuple[bool, str]:
    """
    准备 groupFightResource/flyPet：
    483/1337 分别保留自身原版为 *_og，再用 50.swf 覆盖两个活动本体。

    兼容旧状态：1337_og 已是原始 1337，而活动 1337 是 483 的副本；
    此时保留现有 1337_og，只补建原始 483 的 483_og。
    """
    _ensure_project_path()
    import config as cfg

    root = Path(
        fly_pet_dir
        if fly_pet_dir is not None
        else getattr(
            cfg,
            "GAME_GROUP_FIGHT_FLY_PET_SWF_DIR",
            Path(cfg.GAME_ASSET_BASE_PATH) / "groupFightResource" / "flyPet",
        )
    )
    source = root / "50.swf"
    live_paths = {
        483: root / "483.swf",
        1337: root / "1337.swf",
    }
    backup_paths = {
        483: root / "483_og.swf",
        1337: root / "1337_og.swf",
    }

    with _FLY_PET_1337_LOCK:
        if not root.is_dir():
            return False, f"flyPet 目录不存在：{root}"
        if not source.is_file():
            return False, f"flyPet 缺少 50 替换源：{source}"
        for pet_id, live in live_paths.items():
            backup = backup_paths[pet_id]
            if backup.is_file():
                continue
            if not live.is_file():
                return False, f"flyPet 缺少待备份文件：{live}"
            try:
                if filecmp.cmp(live, source, shallow=False):
                    return (
                        False,
                        f"flyPet：{live.name} 已是 50 内容但缺少 {backup.name}，"
                        "无法安全重建原版 OG",
                    )
            except OSError as e:
                return False, f"flyPet 比较 {live.name}/50.swf 失败：{e}"
            try:
                shutil.copy2(live, backup)
            except OSError as e:
                return False, f"flyPet 备份失败（{live.name} → {backup.name}）：{e}"

        try:
            for live in live_paths.values():
                shutil.copy2(source, live)
        except OSError as e:
            rollback_errors = []
            try:
                for pet_id, live in live_paths.items():
                    backup = backup_paths[pet_id]
                    if backup.is_file():
                        try:
                            shutil.copy2(backup, live)
                        except OSError as rollback_error:
                            rollback_errors.append(
                                f"{backup.name}→{live.name}: {rollback_error}"
                            )
            finally:
                rollback_msg = (
                    f"；回滚失败：{'；'.join(rollback_errors)}"
                    if rollback_errors
                    else "；已从各自 OG 回滚"
                )
            return False, f"flyPet 用 50.swf 覆盖 483/1337 失败：{e}{rollback_msg}"

        return (
            True,
            "flyPet：483/1337 原版本已分别保存为 483_og/1337_og，"
            "两个活动本体均已更新为 50.swf",
        )


def ensure_fly_pet_1337_from_483(
    fly_pet_dir: Optional[Path] = None,
) -> Tuple[bool, str]:
    """兼容旧调用名；当前规则为 483/1337 各自备份 OG 后均用 50 覆盖。"""
    return ensure_fly_pet_483_and_1337_from_50(fly_pet_dir)


def _nono_super_action_paths() -> Tuple[Path, Path]:
    """resource\\nono\\super 下的 action 目录与改名后的 action_og 目录。"""
    _ensure_project_path()
    import config as cfg

    super_dir = Path(cfg.GAME_ASSET_BASE_PATH) / "nono" / "super"
    return super_dir / "action", super_dir / "action_og"


def _nono_super_exp_paths() -> Tuple[Path, Path]:
    """resource/nono/super 下的 exp 目录与改名后的 exp_og 目录。"""
    _ensure_project_path()
    import config as cfg

    super_dir = Path(cfg.GAME_ASSET_BASE_PATH) / "nono" / "super"
    return super_dir / "exp", super_dir / "exp_og"


def _resolve_nono_super_action_og_dir(super_dir: Path) -> Optional[Path]:
    """当前用于还原的 OG 目录：优先 action_og，兼容旧版 super_og。"""
    action_og = super_dir / "action_og"
    if action_og.is_dir():
        return action_og
    legacy = super_dir / "super_og"
    if legacy.is_dir():
        return legacy
    return None


_NEWNPC_MULTI_SLOT_LIVE = "4"
_NEWNPC_MULTI_SLOT_OG = "4_og"
_NEWNPC_MULTI_SLOT_OG_LEGACY = "og"
_NEWNPC_MULTI_YILU_SLOT_LIVE = "90000"
_NEWNPC_MULTI_YILU_SLOT_OG = "90000_og"


def _newnpc_multi_dir() -> Path:
    _ensure_project_path()
    import config as cfg

    return Path(getattr(cfg, "GAME_NEWNPC_MULTI_DIR", Path(cfg.GAME_ASSET_BASE_PATH) / "newNpc" / "multi"))


def _find_newnpc_multi_live(multi_dir: Path) -> Optional[Path]:
    for suffix in (".swf", ""):
        p = multi_dir / f"{_NEWNPC_MULTI_SLOT_LIVE}{suffix}"
        if p.is_file():
            return p
    return None


def _find_newnpc_multi_og(multi_dir: Path) -> Optional[Path]:
    for stem in (_NEWNPC_MULTI_SLOT_OG, _NEWNPC_MULTI_SLOT_OG_LEGACY):
        for suffix in (".swf", ""):
            p = multi_dir / f"{stem}{suffix}"
            if p.is_file():
                return p
    return None


def _newnpc_multi_og_target_for_live(live: Path) -> Path:
    if live.suffix.lower() == ".swf":
        return live.with_name(f"{_NEWNPC_MULTI_SLOT_OG}.swf")
    return live.with_name(_NEWNPC_MULTI_SLOT_OG)


def _migrate_legacy_newnpc_multi_og_to_4_og(multi_dir: Path) -> Optional[str]:
    for suffix in (".swf", ""):
        legacy = multi_dir / f"{_NEWNPC_MULTI_SLOT_OG_LEGACY}{suffix}"
        new = multi_dir / f"{_NEWNPC_MULTI_SLOT_OG}{suffix}"
        if legacy.is_file() and not new.is_file():
            try:
                legacy.rename(new)
                return f"newNpc/multi：已将旧名 {legacy.name} → {new.name}"
            except OSError as e:
                return f"newNpc/multi：旧名 og→4_og 迁移失败: {e}"
    return None


def _newnpc_multi_slot_paths(multi_dir: Path) -> Tuple[Path, Path]:
    """解析 4 与 4_og 路径（优先 .swf，否则无扩展名）。"""
    live = _find_newnpc_multi_live(multi_dir)
    og = _find_newnpc_multi_og(multi_dir)
    if live is not None:
        return live, _newnpc_multi_og_target_for_live(live)
    if og is not None:
        if og.suffix.lower() == ".swf":
            return multi_dir / f"{_NEWNPC_MULTI_SLOT_LIVE}.swf", og
        return multi_dir / _NEWNPC_MULTI_SLOT_LIVE, og
    return (
        multi_dir / f"{_NEWNPC_MULTI_SLOT_LIVE}.swf",
        multi_dir / f"{_NEWNPC_MULTI_SLOT_OG}.swf",
    )


def rename_newnpc_multi_4_to_4_og() -> Tuple[bool, str]:
    """
    将 newNpc/multi 下的 4（4.swf 或无扩展名）改名为 4_og。
    已无 4 且已有 4_og（或旧名 og，会先迁移）时跳过；4 与 4_og 并存则失败。
    """
    multi_dir = _newnpc_multi_dir()
    migrate_msg = _migrate_legacy_newnpc_multi_og_to_4_og(multi_dir)
    live = _find_newnpc_multi_live(multi_dir)
    og = _find_newnpc_multi_og(multi_dir)
    if live is None:
        if og is not None:
            msg = f"newNpc/multi：已是 {og.name}，跳过 4→4_og"
        else:
            msg = f"newNpc/multi：无 {_NEWNPC_MULTI_SLOT_LIVE}，跳过改名"
        if migrate_msg:
            msg = f"{migrate_msg}；{msg}"
        return True, msg
    og_target = _newnpc_multi_og_target_for_live(live)
    if og is not None or og_target.is_file():
        existing = og.name if og is not None else og_target.name
        return (
            False,
            f"newNpc/multi：{_NEWNPC_MULTI_SLOT_LIVE} 与 {existing} 同时存在，无法改名",
        )
    try:
        live.rename(og_target)
        msg = f"newNpc/multi：已将 {live.name} → {og_target.name}"
        if migrate_msg:
            msg = f"{migrate_msg}；{msg}"
        return True, msg
    except OSError as e:
        return False, f"newNpc/multi：4→4_og 失败: {e}"


def restore_newnpc_multi_4_og_to_4() -> Tuple[bool, str]:
    """将 newNpc/multi 下的 4_og（或旧名 og）改回 4。"""
    multi_dir = _newnpc_multi_dir()
    live = _find_newnpc_multi_live(multi_dir)
    og = _find_newnpc_multi_og(multi_dir)
    if og is None:
        if live is not None:
            return True, f"newNpc/multi：已是 {live.name}，跳过 4_og→4"
        return True, f"newNpc/multi：无 4_og，跳过还原"
    if og.suffix.lower() == ".swf":
        live_target = og.with_name(f"{_NEWNPC_MULTI_SLOT_LIVE}.swf")
    else:
        live_target = og.with_name(_NEWNPC_MULTI_SLOT_LIVE)
    if live is not None:
        return (
            False,
            f"newNpc/multi：{og.name} 与 {live.name} 同时存在，无法还原",
        )
    try:
        og.rename(live_target)
        return True, f"newNpc/multi：已将 {og.name} → {live_target.name}"
    except OSError as e:
        return False, f"newNpc/multi：4_og→4 失败: {e}"


def rename_newnpc_multi_4_to_og() -> Tuple[bool, str]:
    """兼容旧函数名。"""
    return rename_newnpc_multi_4_to_4_og()


def restore_newnpc_multi_og_to_4() -> Tuple[bool, str]:
    """兼容旧函数名。"""
    return restore_newnpc_multi_4_og_to_4()


def ensure_newnpc_multi_4_to_4_og() -> Tuple[bool, str]:
    """幂等：若 multi/4 存在则改名为 4_og（已是 4_og 则跳过）。"""
    return rename_newnpc_multi_4_to_4_og()


def ensure_newnpc_multi_4_hidden_before_daily() -> Tuple[bool, str]:
    """兼容旧名。"""
    return ensure_newnpc_multi_4_to_4_og()


def _find_newnpc_multi_named(multi_dir: Path, stem: str) -> Optional[Path]:
    for suffix in (".swf", ""):
        p = multi_dir / f"{stem}{suffix}"
        if p.is_file():
            return p
    return None


def _newnpc_multi_named_target(src: Path, target_stem: str) -> Path:
    if src.suffix.lower() == ".swf":
        return src.with_name(f"{target_stem}.swf")
    return src.with_name(target_stem)


def rename_newnpc_multi_90000_to_90000_og() -> Tuple[bool, str]:
    """依卢前置：将 newNpc/multi/90000.swf 隐藏为 90000_og.swf。"""
    multi_dir = _newnpc_multi_dir()
    live = _find_newnpc_multi_named(multi_dir, _NEWNPC_MULTI_YILU_SLOT_LIVE)
    og = _find_newnpc_multi_named(multi_dir, _NEWNPC_MULTI_YILU_SLOT_OG)
    if live is None:
        if og is not None:
            return True, f"newNpc/multi：已是 {og.name}，跳过 90000→90000_og"
        return True, "newNpc/multi：无 90000，跳过改名"
    og_target = _newnpc_multi_named_target(live, _NEWNPC_MULTI_YILU_SLOT_OG)
    if og is not None or og_target.is_file():
        existing = og.name if og is not None else og_target.name
        return False, f"newNpc/multi：90000 与 {existing} 同时存在，无法改名"
    try:
        live.rename(og_target)
        return True, f"newNpc/multi：已将 {live.name} → {og_target.name}"
    except OSError as e:
        return False, f"newNpc/multi：90000→90000_og 失败: {e}"


def restore_newnpc_multi_90000_og_to_90000() -> Tuple[bool, str]:
    """非依卢入口：若 90000_og.swf 存在，则还原为 90000.swf。"""
    multi_dir = _newnpc_multi_dir()
    live = _find_newnpc_multi_named(multi_dir, _NEWNPC_MULTI_YILU_SLOT_LIVE)
    og = _find_newnpc_multi_named(multi_dir, _NEWNPC_MULTI_YILU_SLOT_OG)
    if og is None:
        if live is not None:
            return True, f"newNpc/multi：已是 {live.name}，跳过 90000_og→90000"
        return True, "newNpc/multi：无 90000_og，跳过还原"
    if live is not None:
        return False, f"newNpc/multi：{og.name} 与 {live.name} 同时存在，无法还原"
    live_target = _newnpc_multi_named_target(og, _NEWNPC_MULTI_YILU_SLOT_LIVE)
    try:
        og.rename(live_target)
        return True, f"newNpc/multi：已将 {og.name} → {live_target.name}"
    except OSError as e:
        return False, f"newNpc/multi：90000_og→90000 失败: {e}"


def ensure_newnpc_multi_90000_hidden_for_yilu() -> Tuple[bool, str]:
    """依卢按钮专用：保证 90000 被隐藏。"""
    return rename_newnpc_multi_90000_to_90000_og()


def ensure_newnpc_multi_90000_restored_for_non_yilu() -> Tuple[bool, str]:
    """非依卢按钮专用：如果 90000 当前是 og，就还原。"""
    return restore_newnpc_multi_90000_og_to_90000()


def verify_newnpc_multi_90000_live() -> Tuple[bool, str]:
    """岚岚启动门控：确认 90000 已恢复且没有与 90000_og 并存。"""
    multi_dir = _newnpc_multi_dir()
    live = _find_newnpc_multi_named(multi_dir, _NEWNPC_MULTI_YILU_SLOT_LIVE)
    og = _find_newnpc_multi_named(multi_dir, _NEWNPC_MULTI_YILU_SLOT_OG)
    if live is not None and og is None:
        return True, f"newNpc/multi：已确认 {live.name} 可用"
    if live is not None and og is not None:
        return False, f"newNpc/multi：{live.name} 与 {og.name} 同时存在"
    if og is not None:
        return False, f"newNpc/multi：仍为 {og.name}，90000 未恢复"
    return False, "newNpc/multi：90000 与 90000_og 均不存在"


def rename_nono_super_action_to_action_og() -> Tuple[bool, str]:
    """
    将 action 改名为 action_og（与 PetStorage 写入配套）。
    若无 action 且已有 action_og（或旧版 super_og）则跳过；两者并存则失败。
    """
    action, action_og = _nono_super_action_paths()
    super_dir = action.parent
    if not action.is_dir():
        og = _resolve_nono_super_action_og_dir(super_dir)
        if og is not None:
            if og.name == "super_og" and not action_og.exists():
                try:
                    og.rename(action_og)
                    return True, f"nono/super：已将旧版 super_og → action_og（{action_og}）"
                except OSError as e:
                    return False, f"nono/super：super_og→action_og 迁移失败: {e}"
            return True, f"nono/super：无 action，已有 {og.name}，跳过改名"
        return True, f"nono/super：无 action，跳过改名（{action}）"
    if action_og.exists() or (super_dir / "super_og").exists():
        return (
            False,
            f"nono/super：已存在 action_og/super_og，无法将 action 改名（请先手动处理）",
        )
    try:
        action.rename(action_og)
        return True, f"nono/super：已将 action → action_og（{action_og}）"
    except OSError as e:
        return False, f"nono/super：action→action_og 失败: {e}"


def rename_nono_super_exp_to_exp_og() -> Tuple[bool, str]:
    """将 exp 改名为 exp_og；已有单独 exp_og 时幂等跳过。"""
    exp_dir, exp_og = _nono_super_exp_paths()
    if not exp_dir.is_dir():
        if exp_og.is_dir():
            return True, "nono/super：无 exp，已有 exp_og，跳过改名"
        return True, f"nono/super：无 exp，跳过改名（{exp_dir}）"
    if exp_og.exists():
        return False, "nono/super：exp 与 exp_og 同时存在，无法改名（请先手动处理）"
    try:
        exp_dir.rename(exp_og)
        return True, f"nono/super：已将 exp → exp_og（{exp_og}）"
    except OSError as e:
        return False, f"nono/super：exp→exp_og 失败: {e}"


_NONO_SUPER_SLOT_STEMS: Tuple[str, ...] = ("nono_1", "nono_2", "nono_3", "nono_4")
_NONO_SUPER_REPLACE_TEMPLATE_STEM = "nono_5"


def _nono_super_dir() -> Path:
    _ensure_project_path()
    import config as cfg

    return Path(getattr(cfg, "GAME_NONO_SUPER_DIR", Path(cfg.GAME_ASSET_BASE_PATH) / "nono" / "super"))


def sync_nono_super_slots_to_nono5() -> Tuple[bool, str]:
    """
    将 nono_1~4.swf 备份为同名 .og.swf（仅当尚无 OG 时复制），再用 nono_5.swf 覆盖 live。
    """
    from swf.replace_fight_swfs import _ensure_og_backup

    super_dir = _nono_super_dir()
    template = super_dir / f"{_NONO_SUPER_REPLACE_TEMPLATE_STEM}.swf"
    if not template.is_file():
        return False, f"nono/super：缺少模板 {template.name}"

    backed: List[str] = []
    replaced: List[str] = []
    errs: List[str] = []
    for stem in _NONO_SUPER_SLOT_STEMS:
        live = super_dir / f"{stem}.swf"
        og = super_dir / f"{stem}.og.swf"
        try:
            if live.is_file():
                _ensure_og_backup(live, og)
                backed.append(stem)
            shutil.copy2(template, live)
            replaced.append(stem)
        except OSError as e:
            errs.append(f"{stem}: {e}")

    if errs:
        return False, f"nono/super：失败 {len(errs)}（首条 {errs[0]}）"
    if not replaced:
        return False, "nono/super：未处理任何 nono_1~4"
    msg = f"nono_1~4 已用 {_NONO_SUPER_REPLACE_TEMPLATE_STEM} 覆盖（{len(replaced)}）"
    if backed:
        msg += f"，已备份 OG：{', '.join(backed)}"
    else:
        msg += "（live 原不存在，未新建 OG）"
    return True, msg


def restore_nono_super_slots_from_og() -> Tuple[bool, str]:
    """从 nono_N.og.swf 还原 nono_N.swf；无 OG 的序号跳过。"""
    super_dir = _nono_super_dir()
    restored: List[str] = []
    skipped: List[str] = []
    errs: List[str] = []
    for stem in _NONO_SUPER_SLOT_STEMS:
        live = super_dir / f"{stem}.swf"
        og = super_dir / f"{stem}.og.swf"
        if not og.is_file():
            skipped.append(stem)
            continue
        try:
            shutil.copy2(og, live)
            restored.append(stem)
        except OSError as e:
            errs.append(f"{stem}: {e}")

    if errs:
        return False, f"nono/super 还原失败：{errs[0]}"
    if not restored:
        return True, "nono/super：无 nono_1~4.og.swf，跳过 nono 还原"
    msg = f"nono/super：已还原 {', '.join(restored)}"
    if skipped:
        msg += f"（无 OG 跳过：{', '.join(skipped)}）"
    return True, msg


def rename_nono_super_action_og_to_action() -> Tuple[bool, str]:
    """
    将 action_og（或旧版 super_og）改回 action（与 PetStorage OG 还原配套）。
    若无 OG 目录则跳过；action 已存在则失败。
    """
    action, action_og = _nono_super_action_paths()
    super_dir = action.parent
    og_dir = _resolve_nono_super_action_og_dir(super_dir)
    if og_dir is None:
        return True, f"nono/super：无 action_og/super_og，跳过还原改名（{action_og}）"
    if action.exists():
        return (
            False,
            f"nono/super：已存在 action，无法将 {og_dir.name} 改回（请先手动处理）",
        )
    try:
        og_dir.rename(action)
        return True, f"nono/super：已将 {og_dir.name} → action（{action}）"
    except OSError as e:
        return False, f"nono/super：{og_dir.name}→action 失败: {e}"


def rename_nono_super_exp_og_to_exp() -> Tuple[bool, str]:
    """将 exp_og 改回 exp；无 exp_og 时幂等跳过。"""
    exp_dir, exp_og = _nono_super_exp_paths()
    if not exp_og.is_dir():
        return True, f"nono/super：无 exp_og，跳过还原改名（{exp_og}）"
    if exp_dir.exists():
        return False, "nono/super：exp 与 exp_og 同时存在，无法还原（请先手动处理）"
    try:
        exp_og.rename(exp_dir)
        return True, f"nono/super：已将 exp_og → exp（{exp_dir}）"
    except OSError as e:
        return False, f"nono/super：exp_og→exp 失败: {e}"


def _restore_dir_from_og(
    live_dir: Path,
    og_dir: Path,
    *,
    delete_live_not_in_og: bool = False,
) -> Tuple[int, int]:
    """
    以 og_dir 为准：将其下每个 *.swf 复制到 live_dir（同名覆盖；live 中已删的序号会重新出现）。

    delete_live_not_in_og=False（默认）：
        第二项 = live 里无 OG 对应、且仍保留的文件数。
    delete_live_not_in_og=True：
        删除 live 中无 OG 的 *.swf；第二项 = 已删除数量。
    """
    from swf.replace_fight_swfs import _swf_name_sort_key

    if not og_dir.is_dir():
        return 0, 0
    og_files = sorted(og_dir.glob("*.swf"), key=_swf_name_sort_key)
    og_names = {og.name for og in og_files}
    live_dir.mkdir(parents=True, exist_ok=True)

    restored = 0
    for og in og_files:
        dest = live_dir / og.name
        shutil.copy2(og, dest)
        restored += 1

    if delete_live_not_in_og:
        removed = 0
        for t in sorted(live_dir.glob("*.swf"), key=_swf_name_sort_key):
            if t.name in og_names:
                continue
            try:
                t.unlink()
                removed += 1
            except OSError:
                pass
        return restored, removed

    skipped = 0
    for t in sorted(live_dir.glob("*.swf"), key=_swf_name_sort_key):
        if t.name not in og_names:
            skipped += 1
    return restored, skipped


def _ensure_live_swf_og_before_delete(live_swf_path: os.PathLike[str] | str, og_dir: os.PathLike[str] | str) -> None:
    """删除 live 下某个 ``*.swf`` 前：若 og 目录尚无同名备份，则复制一份。"""
    _ensure_project_path()
    from swf.replace_fight_swfs import _ensure_og_backup

    live = Path(live_swf_path)
    og = Path(og_dir) / live.name
    _ensure_og_backup(live, og)


def ensure_pet_swf_og_before_delete(live_swf_path: os.PathLike[str] | str) -> None:
    """删除 ``pet/swf`` 下某个 ``*.swf`` 之前调用。"""
    _ensure_project_path()
    from config import GAME_SWF_OG_FOLDER

    _ensure_live_swf_og_before_delete(live_swf_path, GAME_SWF_OG_FOLDER)


def ensure_group_fight_pet_swf_og_before_delete(live_swf_path: os.PathLike[str] | str) -> None:
    """删除 ``groupFightResource/pet`` 下某个 ``*.swf`` 之前调用。"""
    _ensure_project_path()
    from config import GAME_GROUP_FIGHT_PET_SWF_OG_DIR

    _ensure_live_swf_og_before_delete(live_swf_path, GAME_GROUP_FIGHT_PET_SWF_OG_DIR)


def delete_pet_swf_ids(pet_ids: Sequence[int]) -> Tuple[bool, str]:
    """Safely delete pet SWFs from both live directories after preserving OG files."""
    _ensure_project_path()
    import config as cfg

    ids = sorted({int(pet_id) for pet_id in pet_ids if int(pet_id) > 0})
    if not ids:
        return True, "未指定需要删除的 PetSWF"

    targets = (
        (
            Path(cfg.GAME_SWF_FOLDER),
            ensure_pet_swf_og_before_delete,
            "pet/swf",
        ),
        (
            Path(cfg.GAME_GROUP_FIGHT_PET_SWF_DIR),
            ensure_group_fight_pet_swf_og_before_delete,
            "groupFightResource/pet",
        ),
    )
    removed: List[str] = []
    already_absent: List[str] = []
    errors: List[str] = []
    for live_dir, ensure_og, label in targets:
        if not live_dir.is_dir():
            errors.append(f"{label}目录不存在：{live_dir}")
            continue
        for pet_id in ids:
            path = live_dir / f"{pet_id}.swf"
            if not path.exists():
                already_absent.append(f"{label}/{path.name}")
                continue
            try:
                ensure_og(path)
                path.unlink()
                removed.append(f"{label}/{path.name}")
            except OSError as exc:
                errors.append(f"{label}/{path.name}: {exc}")

    parts = []
    if removed:
        parts.append("已删除 " + "、".join(removed))
    if already_absent:
        parts.append("原本不存在 " + "、".join(already_absent))
    if errors:
        parts.append("失败 " + "；".join(errors))
    return not errors, "；".join(parts)


def _project_pet_template_paths() -> Tuple[Path, Path, Path]:
    _ensure_project_path()
    import config as cfg

    assets = Path(getattr(cfg, "ASSETS_PATH", _ROOT / "assets"))
    tpl254 = assets / "254.swf"
    if not tpl254.is_file():
        tpl254 = Path(getattr(cfg, "PROJECT_TEMPLATE_254_SWF", _ROOT / "swf" / "254.swf"))
    return assets / "252.swf", tpl254, assets / "519.swf"


_FUSION_PURPLE_PET_IDS = ("77", "164", "471", "480")
_FUSION_CYAN_PET_IDS = ("79", "473")


def _fusion_pet_template_for_target(stem: str, templates: dict[str, Path]) -> Optional[Path]:
    if stem in {"77", "164", "471", "480"}:
        return templates["252"]
    if stem in {"79", "473"}:
        return templates["519"]
    return None


def _pet_swf_candidate_names(stem: str) -> Tuple[str, ...]:
    candidates = [f"{stem}.swf"]
    try:
        n = int(stem)
    except ValueError:
        n = None
    if n is not None:
        for width in (3, 4):
            padded = f"{n:0{width}d}.swf"
            if padded not in candidates:
                candidates.append(padded)
    return tuple(candidates)


def _sync_fusion_pet_templates_in_directory(
    dest_dir: Path,
    og_dir: Path,
    templates: dict[str, Path],
    label: str,
) -> Tuple[int, Dict[str, int], int, List[str]]:
    """
    融合SWF在全量 254 后，只追加覆盖紫色/青色目标。

    Returns (covered, counts, skipped_missing, errors).
    """
    from swf.replace_fight_swfs import _ensure_og_backup

    os.makedirs(dest_dir, exist_ok=True)
    os.makedirs(og_dir, exist_ok=True)

    n_ok = 0
    counts = {"252": 0, "519": 0}
    skipped_missing = 0
    errs: List[str] = []
    stems = _FUSION_PURPLE_PET_IDS + _FUSION_CYAN_PET_IDS
    for stem in stems:
        template = _fusion_pet_template_for_target(stem, templates)
        if template is None:
            continue
        template_name = "252" if stem in _FUSION_PURPLE_PET_IDS else "519"
        matched = False
        for name in _pet_swf_candidate_names(stem):
            target = dest_dir / name
            og = og_dir / name
            try:
                if target.is_file():
                    _ensure_og_backup(target, og)
                elif og.is_file():
                    shutil.copy2(og, target)
                else:
                    continue
                matched = True
                shutil.copy2(template, target)
                counts[template_name] = counts.get(template_name, 0) + 1
                n_ok += 1
            except OSError as e:
                errs.append(f"{label}/{target.name}: {e}")
        if not matched:
            skipped_missing += 1
    return n_ok, counts, skipped_missing, errs


def _sync_pet_template_map_in_directory(
    dest_dir: Path,
    og_dir: Path,
    templates: dict[str, Path],
    label: str,
    stem_to_template: dict[str, str],
) -> Tuple[int, Dict[str, int], int, List[str]]:
    from swf.replace_fight_swfs import _ensure_og_backup

    os.makedirs(dest_dir, exist_ok=True)
    os.makedirs(og_dir, exist_ok=True)

    n_ok = 0
    counts = {"254": 0, "252": 0, "519": 0}
    skipped_missing = 0
    errs: List[str] = []
    for stem, template_name in stem_to_template.items():
        template = templates.get(template_name)
        if template is None:
            continue
        matched = False
        for name in _pet_swf_candidate_names(stem):
            target = dest_dir / name
            og = og_dir / name
            try:
                if target.is_file():
                    _ensure_og_backup(target, og)
                elif og.is_file():
                    shutil.copy2(og, target)
                else:
                    continue
                matched = True
                shutil.copy2(template, target)
                counts[template_name] = counts.get(template_name, 0) + 1
                n_ok += 1
            except OSError as e:
                errs.append(f"{label}/{target.name}: {e}")
        if not matched:
            skipped_missing += 1
    return n_ok, counts, skipped_missing, errs


def _sync_all_pet_254_in_directory(
    dest_dir: Path,
    og_dir: Path,
    template_254: Path,
    label: str,
) -> Tuple[int, int, int, List[str]]:
    from swf.replace_fight_swfs import _ensure_og_backup, _swf_name_sort_key, fill_live_swf_from_og_dir

    os.makedirs(dest_dir, exist_ok=True)
    os.makedirs(og_dir, exist_ok=True)
    filled_og, og_total = fill_live_swf_from_og_dir(dest_dir, og_dir)
    targets = sorted(dest_dir.glob("*.swf"), key=_swf_name_sort_key)
    if not targets:
        return 0, filled_og, og_total, []

    n_ok = 0
    errs: List[str] = []
    for t in targets:
        try:
            _ensure_og_backup(t, og_dir / t.name)
            shutil.copy2(template_254, t)
            n_ok += 1
        except OSError as e:
            errs.append(f"{label}/{t.name}: {e}")
    return n_ok, filled_og, og_total, errs


def sync_petstorage() -> Tuple[bool, str]:
    _ensure_project_path()
    from swf.replace_fight_swfs import _ensure_og_backup

    import config as cfg

    live_path = cfg.GAME_PETSTORAGE_SWF
    og_path = getattr(
        cfg,
        "GAME_PETSTORAGE_OG_SWF",
        os.path.join(os.path.dirname(live_path), "PetStorage.og.swf"),
    )
    PROJECT_PETSTORAGE_SWF = cfg.PROJECT_PETSTORAGE_SWF

    if not os.path.isfile(PROJECT_PETSTORAGE_SWF):
        return False, f"源文件不存在: {PROJECT_PETSTORAGE_SWF}"
    try:
        live = Path(live_path)
        og = Path(og_path)
        os.makedirs(live.parent, exist_ok=True)
        _ensure_og_backup(live, og)
        shutil.copy2(PROJECT_PETSTORAGE_SWF, live)
        ok_nono, msg_nono = sync_nono_super_slots_to_nono5()
        if not ok_nono:
            return (
                False,
                f"已写入 PetStorage.swf -> {live_path}；但 {msg_nono}",
            )
        ok2, msg2 = rename_nono_super_action_to_action_og()
        if not ok2:
            return (
                False,
                f"已写入 PetStorage.swf -> {live_path}；{msg_nono}；但 {msg2}",
            )
        ok_exp, msg_exp = rename_nono_super_exp_to_exp_og()
        if not ok_exp:
            return (
                False,
                f"已写入 PetStorage.swf -> {live_path}；{msg_nono}；{msg2}；但 {msg_exp}",
            )
        ok3, msg3 = rename_newnpc_multi_4_to_4_og()
        if not ok3:
            return (
                False,
                f"已写入 PetStorage.swf -> {live_path}；{msg_nono}；{msg2}；{msg_exp}；但 {msg3}",
            )
        return True, f"已写入 PetStorage.swf -> {live_path}；{msg_nono}；{msg2}；{msg_exp}；{msg3}"
    except OSError as e:
        return False, str(e)


def sync_pet_254(*, runtime_subset: bool = False) -> Tuple[bool, str]:
    base_ok, base_msg = (
        sync_runtime_pet_254_base() if runtime_subset else sync_all_pet_254_base()
    )
    if not base_ok:
        return False, base_msg
    edit_ok, edit_msg = _sync_named_pet_template_map(
        "普通Pet254",
        {"1337": "252", "197": "519"},
    )
    return bool(edit_ok), f"{base_msg} | {edit_msg}"


def sync_nieo_pet_254(*, runtime_subset: bool = False) -> Tuple[bool, str]:
    base_ok, base_msg = (
        sync_runtime_pet_254_base() if runtime_subset else sync_all_pet_254_base()
    )
    if not base_ok:
        return False, base_msg
    edit_ok, edit_msg = _sync_named_pet_template_map(
        "尼奥Pet254",
        {"1337": "252", "1459": "519"},
    )
    return bool(edit_ok), f"{base_msg} | {edit_msg}"


def sync_all_pet_254_base() -> Tuple[bool, str]:
    _ensure_project_path()
    import config as cfg

    tpl252, tpl254, tpl519 = _project_pet_template_paths()
    templates = {"252": tpl252, "254": tpl254, "519": tpl519}
    missing_templates = [str(p) for p in templates.values() if not p.is_file()]
    if missing_templates:
        return False, "缺少模板文件: " + "; ".join(missing_templates)

    pairs = (
        (Path(cfg.GAME_SWF_FOLDER), Path(cfg.GAME_SWF_OG_FOLDER), "pet/swf"),
        (
            Path(cfg.GAME_GROUP_FIGHT_PET_SWF_DIR),
            Path(cfg.GAME_GROUP_FIGHT_PET_SWF_OG_DIR),
            "groupFightResource/pet",
        ),
    )
    try:
        parts: List[str] = []
        all_errs: List[str] = []
        any_work = False
        for dest_dir, og_dir, label in pairs:
            n_ok, filled_og, og_total, errs = _sync_all_pet_254_in_directory(
                dest_dir,
                og_dir,
                tpl254,
                label,
            )
            all_errs.extend(errs)
            if n_ok == 0 and filled_og == 0:
                parts.append(f"{label}: 未找到 .swf，跳过")
                continue
            any_work = True
            seg = f"{label}: 全量橙色254覆盖 {n_ok} 个"
            if filled_og:
                seg += f"（从OG补齐缺失 {filled_og}/{og_total}）"
            parts.append(seg)
        if not any_work:
            return False, "pet/swf 和 groupFightResource/pet 都没有可覆盖的 .swf"
        msg = " | ".join(parts)
        if all_errs:
            return False, f"{msg}；失败 {len(all_errs)} 个（第一个：{all_errs[0]}）"
        return True, msg
    except OSError as e:
        return False, str(e)


def _collect_positive_pet_ids(value: Any, output: set[int]) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _collect_positive_pet_ids(item, output)
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_positive_pet_ids(item, output)
        return
    try:
        pet_id = int(value)
    except (TypeError, ValueError):
        return
    if pet_id > 0:
        output.add(pet_id)


def potential_pet_swf_union_ids(
    project_root: Optional[Path] = None,
) -> frozenset[int]:
    """Return all PetSWF ids potentially changed by app mode transitions."""
    ids = set(PET_SWF_POTENTIAL_STATIC_IDS)
    assets_root = Path(project_root or _ROOT) / "assets"
    for folder_name in _POTENTIAL_MANIFEST_DIRS:
        folder = assets_root / folder_name
        if not folder.is_dir():
            continue
        for manifest_path in folder.glob("*.json"):
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            for field in _POTENTIAL_MANIFEST_PET_FIELDS:
                _collect_positive_pet_ids(payload.get(field), ids)
    return frozenset(ids)


def rotation_pet_swf_union_ids(
    project_root: Optional[Path] = None,
) -> frozenset[int]:
    """Backward-compatible name for the shared potential-impact union."""
    return potential_pet_swf_union_ids(project_root)


def sync_runtime_pet_254_base() -> Tuple[bool, str]:
    """同一任务后续切换：只将潜在影响并集重置为 254 橙色。"""
    union_ids = potential_pet_swf_union_ids()
    mapping = {
        str(pet_id): "254"
        for pet_id in sorted(union_ids)
    }
    return _sync_named_pet_template_map(
        f"潜在影响并集Pet254({len(union_ids)}个ID)",
        mapping,
    )


def sync_special_pet_254_set() -> Tuple[bool, str]:
    return sync_fusion_pet_254_set()


def sync_fusion_pet_254_set(*, runtime_subset: bool = False) -> Tuple[bool, str]:
    _ensure_project_path()
    import config as cfg

    base_ok, base_msg = (
        sync_runtime_pet_254_base() if runtime_subset else sync_all_pet_254_base()
    )
    if not base_ok:
        return False, f"融合SWF全量橙色失败：{base_msg}"

    tpl252, tpl254, tpl519 = _project_pet_template_paths()
    templates = {"252": tpl252, "254": tpl254, "519": tpl519}
    missing_templates = [str(p) for p in templates.values() if not p.is_file()]
    if missing_templates:
        return False, "缺少模板文件: " + "; ".join(missing_templates)

    pairs = (
        (Path(cfg.GAME_SWF_FOLDER), Path(cfg.GAME_SWF_OG_FOLDER), "pet/swf"),
        (
            Path(cfg.GAME_GROUP_FIGHT_PET_SWF_DIR),
            Path(cfg.GAME_GROUP_FIGHT_PET_SWF_OG_DIR),
            "groupFightResource/pet",
        ),
    )
    try:
        parts: List[str] = []
        all_errs: List[str] = []
        any_work = False
        for dest_dir, og_dir, label in pairs:
            n_ok, counts, skipped_missing, errs = _sync_fusion_pet_templates_in_directory(
                dest_dir, og_dir, templates, label
            )
            all_errs.extend(errs)
            if n_ok:
                any_work = True
            seg = (
                f"{label}: 融合覆盖 {n_ok} 个"
                f"（紫色252:{counts.get('252', 0)}，青色519:{counts.get('519', 0)}）"
            )
            if skipped_missing:
                seg += f"，未找到live/OG目标 {skipped_missing} 个"
            parts.append(seg)
        if not any_work:
            return False, f"{base_msg} | 未找到融合SWF紫色/青色目标"
        msg = f"{base_msg} | " + " | ".join(parts)
        if all_errs:
            return False, f"{msg}；失败 {len(all_errs)} 个（第一个：{all_errs[0]}）"
        return True, msg
    except OSError as e:
        return False, str(e)


def _sync_named_pet_template_map(name: str, stem_to_template: dict[str, str]) -> Tuple[bool, str]:
    _ensure_project_path()
    import config as cfg

    tpl252, tpl254, tpl519 = _project_pet_template_paths()
    templates = {"252": tpl252, "254": tpl254, "519": tpl519}
    missing_templates = [str(p) for p in templates.values() if not p.is_file()]
    if missing_templates:
        return False, "缺少模板文件: " + "; ".join(missing_templates)

    pairs = (
        (Path(cfg.GAME_SWF_FOLDER), Path(cfg.GAME_SWF_OG_FOLDER), "pet/swf"),
        (
            Path(cfg.GAME_GROUP_FIGHT_PET_SWF_DIR),
            Path(cfg.GAME_GROUP_FIGHT_PET_SWF_OG_DIR),
            "groupFightResource/pet",
        ),
    )
    try:
        parts: List[str] = []
        all_errs: List[str] = []
        any_work = False
        for dest_dir, og_dir, label in pairs:
            n_ok, counts, skipped_missing, errs = _sync_pet_template_map_in_directory(
                dest_dir,
                og_dir,
                templates,
                label,
                stem_to_template,
            )
            all_errs.extend(errs)
            if n_ok:
                any_work = True
            seg = (
                f"{label}: {name} 覆盖 {n_ok} 个"
                f"（橙色254:{counts.get('254', 0)}，紫色252:{counts.get('252', 0)}，青色519:{counts.get('519', 0)}）"
            )
            if skipped_missing:
                seg += f"，未找到live/OG目标 {skipped_missing} 个"
            parts.append(seg)
        if not any_work:
            return False, f"未找到 {name} 的live/OG目标"
        msg = " | ".join(parts)
        if all_errs:
            return False, f"{msg}；失败 {len(all_errs)} 个（第一个：{all_errs[0]}）"
        return True, msg
    except OSError as e:
        return False, str(e)


def sync_yameisi_pet_254_swap_set() -> Tuple[bool, str]:
    """亚梅丝交换：197->252(紫)，1337->254(橙)，1459->519(青蓝)。"""
    return _sync_named_pet_template_map(
        "yameisi-swap",
        {"197": "252", "1337": "254", "1459": "519"},
    )


def sync_light_mantis_pet_254_set() -> Tuple[bool, str]:
    """兼容入口：光螳螂已统一使用普通 Pet254 配色。"""
    return _sync_named_pet_template_map(
        "normal",
        {"1337": "252", "197": "519"},
    )


def sync_yilu_pet_254_set() -> Tuple[bool, str]:
    """依卢：1337->252(紫)，197->519(青色)，其余保持基线254橙色。"""
    return _sync_named_pet_template_map("yilu", {"1337": "252", "197": "519"})


def sync_weekly_purple_follow_pet_254_set() -> Tuple[bool, str]:
    """一键周常瞭望露台：只保留 1337 紫色跟随目标。"""
    return _sync_named_pet_template_map("weekly-purple-follow", {"1337": "252"})


def sync_one_click_release_pet_254_set() -> Tuple[bool, str]:
    """一键放生：1337 紫色，指定待放生精灵为青色。"""
    return _sync_named_pet_template_map(
        "one-click-release",
        {
            "1337": "252",
            "65": "519",
            "95": "519",
            "102": "519",
            "143": "519",
            "416": "519",
            "528": "519",
            "604": "519",
            "607": "519",
            "650": "519",
            "667": "519",
        },
    )


def sync_lanlan_pet_254_set(cyan_pet_id: int) -> Tuple[bool, str]:
    """岚岚：1337->252(紫)；周二/四/六用 67 与目标作青色，周日仅 1459。"""
    cyan = str(int(cyan_pet_id))
    allowed = {"347", "1459", "683"}
    if cyan not in allowed:
        return False, f"unsupported lanlan cyan pet id: {cyan}"
    mapping = {"1337": "252"}
    if cyan != "1459":
        mapping["67"] = "519"
    mapping[cyan] = "519"
    return _sync_named_pet_template_map(f"lanlan-cyan-{cyan}", mapping)


def sync_master_cup_pet_254_set(cyan_pet_ids: Sequence[int]) -> Tuple[bool, str]:
    """大师杯：1337->252(紫)；67 与请求目标 ->519(青蓝)，目标含1459时不标67。"""
    cyan_ids = {str(int(pid)) for pid in cyan_pet_ids if int(pid) > 0}
    mapping = {"1337": "252"}
    if "1459" not in cyan_ids:
        mapping["67"] = "519"
    for pid in sorted(cyan_ids, key=lambda x: int(x)):
        mapping[pid] = "519"
    return _sync_named_pet_template_map(
        "master-cup-cyan-" + "-".join(sorted(set(mapping) - {"1337"}, key=lambda x: int(x))),
        mapping,
    )


def sync_happy_valley_pet_254_set() -> Tuple[bool, str]:
    """兼容入口：欢乐谷已统一使用孵化 Pet254 配色。"""
    return _sync_named_pet_template_map(
        "hatch-606-67-309-purple-303-cyan",
        {"606": "252", "67": "252", "309": "252", "303": "519"},
    )


def sync_hatch_pet_254_set() -> Tuple[bool, str]:
    """Hatch start: 606/67/309 purple and 303 cyan."""
    return _sync_named_pet_template_map(
        "hatch-606-67-309-purple-303-cyan",
        {"606": "252", "67": "252", "309": "252", "303": "519"},
    )


def sync_hatch_exp_pet_254_set() -> Tuple[bool, str]:
    """兼容入口：孵化经验与孵化共用同一套 Pet254 配色。"""
    return _sync_named_pet_template_map(
        "hatch-606-67-309-purple-303-cyan",
        {"606": "252", "67": "252", "309": "252", "303": "519"},
    )


def sync_fight_pet() -> Tuple[bool, str]:
    _ensure_project_path()
    from swf.replace_fight_swfs import replace_pet_swfs

    try:
        n, errs = replace_pet_swfs(dry_run=False, quiet=True)
        if errs:
            return False, f"共 {len(errs)} 个错误（首条：{errs[0]}）"
    except FileNotFoundError as e:
        return False, str(e)
    if n == 0:
        return True, "fight pet：目标目录下无 .swf，未覆盖"
    return True, f"fight pet：已用 fightpet.swf 覆盖 {n} 个文件（已按需生成 swf_og 备份）"


def sync_fight_skill() -> Tuple[bool, str]:
    _ensure_project_path()
    from swf.replace_fight_swfs import replace_skill_swfs

    try:
        n, errs = replace_skill_swfs(dry_run=False, quiet=True)
        if errs:
            return False, f"共 {len(errs)} 个错误（首条：{errs[0]}）"
    except FileNotFoundError as e:
        return False, str(e)
    if n == 0:
        return True, "fight skill：目标目录下无 .swf，未覆盖"
    return True, f"fight skill：已用 fightskill.swf 覆盖 {n} 个文件（已按需生成 swf_og 备份）"


def restore_petstorage_from_og() -> Tuple[bool, str]:
    _ensure_project_path()
    import config as cfg

    live_path = cfg.GAME_PETSTORAGE_SWF
    og_path = getattr(
        cfg,
        "GAME_PETSTORAGE_OG_SWF",
        os.path.join(os.path.dirname(live_path), "PetStorage.og.swf"),
    )
    live = Path(live_path)
    og = Path(og_path)
    if not og.is_file():
        return False, f"无 OG 备份: {og}"
    try:
        os.makedirs(live.parent, exist_ok=True)
        shutil.copy2(og, live)
        ok2, msg2 = rename_nono_super_action_og_to_action()
        if not ok2:
            return (
                False,
                f"已从 OG 还原 PetStorage.swf <- {og}；但 {msg2}",
            )
        ok_exp, msg_exp = rename_nono_super_exp_og_to_exp()
        if not ok_exp:
            return (
                False,
                f"已从 OG 还原 PetStorage.swf <- {og}；{msg2}；但 {msg_exp}",
            )
        ok_nono, msg_nono = restore_nono_super_slots_from_og()
        if not ok_nono:
            return (
                False,
                f"已从 OG 还原 PetStorage.swf <- {og}；{msg2}；{msg_exp}；但 {msg_nono}",
            )
        ok3, msg3 = ensure_newnpc_multi_4_to_4_og()
        if not ok3:
            return (
                False,
                f"已从 OG 还原 PetStorage.swf <- {og}；{msg2}；{msg_exp}；{msg_nono}；但 {msg3}",
            )
        return True, f"已从 OG 还原 PetStorage.swf <- {og}；{msg2}；{msg_exp}；{msg_nono}；{msg3}"
    except OSError as e:
        return False, str(e)


def restore_pet_254_from_og() -> Tuple[bool, str]:
    _ensure_project_path()
    import config as cfg

    pairs = (
        (Path(cfg.GAME_SWF_FOLDER), Path(cfg.GAME_SWF_OG_FOLDER), "pet/swf"),
        (
            Path(cfg.GAME_GROUP_FIGHT_PET_SWF_DIR),
            Path(cfg.GAME_GROUP_FIGHT_PET_SWF_OG_DIR),
            "groupFightResource/pet",
        ),
    )
    parts: List[str] = []
    any_restored = False
    for live, og_dir, label in pairs:
        if not og_dir.is_dir() or not any(og_dir.glob("*.swf")):
            parts.append(f"{label}：无 og 备份，跳过")
            continue
        live.mkdir(parents=True, exist_ok=True)
        r, skipped = _restore_dir_from_og(live, og_dir, delete_live_not_in_og=False)
        if r == 0:
            parts.append(f"{label}：og 无有效文件")
            continue
        any_restored = True
        parts.append(f"{label}：还原 {r} 个（live 无 og 对应未覆盖: {skipped}）")
    if not any_restored:
        return False, "；".join(parts) if parts else "swf_og 下无 .swf 备份，无法还原"
    return True, "；".join(parts)


def restore_fight_pet_from_og() -> Tuple[bool, str]:
    _ensure_project_path()
    import config as cfg

    base = cfg.GAME_ASSET_BASE_PATH
    live = cfg.GAME_FIGHT_PET_SWF_DIR
    og = getattr(
        cfg,
        "GAME_FIGHT_PET_SWF_OG_DIR",
        os.path.join(base, "fightResource", "pet", "swf_og"),
    )
    lp = Path(live)
    ogp = Path(og)
    if not ogp.is_dir():
        return False, "fight pet swf_og 目录不存在"
    if not any(ogp.glob("*.swf")):
        return False, "fight pet swf_og 下无 .swf 备份，无法还原"
    lp.mkdir(parents=True, exist_ok=True)
    r, s = _restore_dir_from_og(lp, ogp)
    if r == 0:
        return False, "未能从 swf_og 写入任何文件"
    return True, f"已从 swf_og 还原 {r} 个（含已删序号；live 中无 OG 对应未覆盖: {s}）"


def restore_fight_skill_from_og() -> Tuple[bool, str]:
    _ensure_project_path()
    import config as cfg

    base = cfg.GAME_ASSET_BASE_PATH
    live = cfg.GAME_FIGHT_SKILL_SWF_DIR
    og = getattr(
        cfg,
        "GAME_FIGHT_SKILL_SWF_OG_DIR",
        os.path.join(base, "fightResource", "skill", "swf_og"),
    )
    lp = Path(live)
    ogp = Path(og)
    if not ogp.is_dir():
        return False, "fight skill swf_og 目录不存在"
    if not any(ogp.glob("*.swf")):
        return False, "fight skill swf_og 下无 .swf 备份，无法还原"
    lp.mkdir(parents=True, exist_ok=True)
    r, s = _restore_dir_from_og(lp, ogp)
    if r == 0:
        return False, "未能从 swf_og 写入任何文件"
    return True, f"已从 swf_og 还原 {r} 个（含已删序号；live 中无 OG 对应未覆盖: {s}）"


def sync_all_four() -> Tuple[bool, str]:
    """依次执行四项，遇到失败即停止并汇总信息。"""
    steps: List[Tuple[str, Callable[[], Tuple[bool, str]]]] = [
        ("PetStorage", sync_petstorage),
        ("普通PetSWF", sync_pet_254),
        ("战斗精灵SWF", sync_fight_pet),
        ("战斗技能SWF", sync_fight_skill),
    ]
    lines = []
    for name, fn in steps:
        ok, msg = fn()
        lines.append(f"{name}: {msg}")
        if not ok:
            return False, " | ".join(lines)
    return True, " | ".join(lines)
