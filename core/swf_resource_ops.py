# core/swf_resource_ops.py
"""GUI 与 CLI 共用的微端 SWF 同步；返回 (是否成功, 说明文本)。

覆盖前：若目标文件已存在且尚无 OG 备份，则自动生成 OG（与 fight 目录的 swf_og、PetStorage 同目录下的 .og 文件规则一致）。

Pet 254：对 ``pet/swf`` 与 ``groupFightResource/pet`` 两套目录同步执行——
先从各自 ``*_og`` 补齐 live 缺失序号，再对已有 ``*.swf`` 先备份 OG 后用 254 模板覆盖。

PetStorage 同步时另处理 ``resource/nono/super``：``nono_1``~``nono_4`` 备份为 ``*.og.swf`` 后用 ``nono_5`` 覆盖；
还原 PetStorage OG 时写回 ``nono_1``~``nono_4``，并将 ``action_og`` 改回 ``action``。

删除 ``delete_swf_ids`` 时：两个 live 目录均检查同名 ``{id}.swf``，存在则先备份 OG 再删。

删除 ``pet/swf`` / ``groupFightResource/pet`` 下文件前：应调用对应的 ``ensure_*_og_before_delete``。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent.parent


def _ensure_project_path() -> None:
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))


def _nono_super_action_paths() -> Tuple[Path, Path]:
    """resource\\nono\\super 下的 action 目录与改名后的 action_og 目录。"""
    _ensure_project_path()
    import config as cfg

    super_dir = Path(cfg.GAME_ASSET_BASE_PATH) / "nono" / "super"
    return super_dir / "action", super_dir / "action_og"


def _resolve_nono_super_action_og_dir(super_dir: Path) -> Optional[Path]:
    """当前用于还原的 OG 目录：优先 action_og，兼容旧版 super_og。"""
    action_og = super_dir / "action_og"
    if action_og.is_dir():
        return action_og
    legacy = super_dir / "super_og"
    if legacy.is_dir():
        return legacy
    return None


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


def _sync_pet_254_in_directory(
    dest_dir: Path,
    og_dir: Path,
    template: Path,
    label: str,
) -> Tuple[int, int, int, List[str]]:
    """
    单目录 Pet 254 同步：og 补齐缺失 → 对已有 swf 备份 OG → 254 覆盖。
    返回 (覆盖成功数, 从 og 补齐数, og 文件总数, 错误列表)。
    """
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
            shutil.copy2(template, t)
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
        return True, f"已写入 PetStorage.swf -> {live_path}；{msg_nono}；{msg2}"
    except OSError as e:
        return False, str(e)


def sync_pet_254() -> Tuple[bool, str]:
    _ensure_project_path()
    import config as cfg

    template = Path(cfg.PROJECT_TEMPLATE_254_SWF)
    if not template.is_file():
        return False, f"源文件不存在: {cfg.PROJECT_TEMPLATE_254_SWF}"

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
            n_ok, filled_og, og_total, errs = _sync_pet_254_in_directory(
                dest_dir, og_dir, template, label
            )
            all_errs.extend(errs)
            if n_ok == 0 and filled_og == 0:
                parts.append(f"{label}：无 .swf，跳过")
                continue
            any_work = True
            seg = f"{label}：254 覆盖 {n_ok} 个"
            if filled_og:
                seg += f"（从 og 补齐缺失 {filled_og}/{og_total}）"
            parts.append(seg)
        if not any_work:
            return False, "pet/swf 与 groupFightResource/pet 均无 .swf，无法批量替换"
        msg = "；".join(parts)
        if all_errs:
            return False, f"{msg}；失败 {len(all_errs)}（首条：{all_errs[0]}）"
        return True, msg
    except OSError as e:
        return False, str(e)


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
        ok_nono, msg_nono = restore_nono_super_slots_from_og()
        if not ok_nono:
            return (
                False,
                f"已从 OG 还原 PetStorage.swf <- {og}；{msg2}；但 {msg_nono}",
            )
        return True, f"已从 OG 还原 PetStorage.swf <- {og}；{msg2}；{msg_nono}"
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
        ("Pet 254", sync_pet_254),
        ("Fight pet", sync_fight_pet),
        ("Fight skill", sync_fight_skill),
    ]
    lines = []
    for name, fn in steps:
        ok, msg = fn()
        lines.append(f"{name}: {msg}")
        if not ok:
            return False, " | ".join(lines)
    return True, " | ".join(lines)
