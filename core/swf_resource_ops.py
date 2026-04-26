# core/swf_resource_ops.py
"""GUI 与 CLI 共用的微端 SWF 同步；返回 (是否成功, 说明文本)。

覆盖前：若目标文件已存在且尚无 OG 备份，则自动生成 OG（与 fight 目录的 swf_og、PetStorage 同目录下的 .og 文件规则一致）。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Callable, List, Tuple

_ROOT = Path(__file__).resolve().parent.parent


def _ensure_project_path() -> None:
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from config_bootstrap import ensure_config_py

    ensure_config_py(str(_ROOT))


def _nono_super_action_paths() -> Tuple[Path, Path]:
    """resource\\nono\\super 下的 action 目录与改名后的 super_og 目录。"""
    _ensure_project_path()
    import config as cfg

    super_dir = Path(cfg.GAME_ASSET_BASE_PATH) / "nono" / "super"
    return super_dir / "action", super_dir / "super_og"


def rename_nono_super_action_to_super_og() -> Tuple[bool, str]:
    """
    将 action 改名为 super_og（与 PetStorage 写入配套）。
    若无 action 目录则跳过（已成功）；若 super_og 已存在且 action 仍存在则失败。
    """
    action, super_og = _nono_super_action_paths()
    if not action.is_dir():
        return True, f"nono/super：无 action，跳过改名（{action}）"
    if super_og.exists():
        return (
            False,
            f"nono/super：已存在 {super_og.name}，无法将 action 改名（请先手动处理）",
        )
    try:
        action.rename(super_og)
        return True, f"nono/super：已将 action → super_og（{super_og}）"
    except OSError as e:
        return False, f"nono/super：action→super_og 失败: {e}"


def rename_nono_super_super_og_to_action() -> Tuple[bool, str]:
    """
    将 super_og 改回 action（与 PetStorage OG 还原配套）。
    若无 super_og 目录则跳过；若 action 已存在则失败。
    """
    action, super_og = _nono_super_action_paths()
    if not super_og.is_dir():
        return True, f"nono/super：无 super_og，跳过还原改名（{super_og}）"
    if action.exists():
        return (
            False,
            f"nono/super：已存在 action，无法将 super_og 改回（请先手动处理）",
        )
    try:
        super_og.rename(action)
        return True, f"nono/super：已将 super_og → action（{action}）"
    except OSError as e:
        return False, f"nono/super：super_og→action 失败: {e}"


def _restore_dir_from_og(live_dir: Path, og_dir: Path) -> Tuple[int, int]:
    """将 og_dir 中同名 .swf 还原到 live_dir。返回 (已还原数量, 无 OG 跳过的数量)。"""
    restored = 0
    skipped = 0
    if not live_dir.is_dir():
        return 0, 0
    for t in sorted(live_dir.glob("*.swf")):
        og = og_dir / t.name
        if og.is_file():
            shutil.copy2(og, t)
            restored += 1
        else:
            skipped += 1
    return restored, skipped


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
        ok2, msg2 = rename_nono_super_action_to_super_og()
        if not ok2:
            return (
                False,
                f"已写入 PetStorage.swf -> {live_path}；但 {msg2}",
            )
        return True, f"已写入 PetStorage.swf -> {live_path}；{msg2}"
    except OSError as e:
        return False, str(e)


def sync_pet_254() -> Tuple[bool, str]:
    _ensure_project_path()
    from swf.replace_fight_swfs import _ensure_og_backup

    from config import GAME_SWF_FOLDER, GAME_SWF_OG_FOLDER, PROJECT_TEMPLATE_254_SWF

    template = Path(PROJECT_TEMPLATE_254_SWF)
    if not template.is_file():
        return False, f"源文件不存在: {PROJECT_TEMPLATE_254_SWF}"

    dest_dir = Path(GAME_SWF_FOLDER)
    og_dir = Path(GAME_SWF_OG_FOLDER)
    try:
        os.makedirs(dest_dir, exist_ok=True)
        os.makedirs(og_dir, exist_ok=True)
        targets = sorted(dest_dir.glob("*.swf"))
        if not targets:
            return False, "pet/swf 目录下没有 .swf，无法批量替换"

        n_ok = 0
        errs: List[str] = []
        for t in targets:
            try:
                _ensure_og_backup(t, og_dir / t.name)
                shutil.copy2(template, t)
                n_ok += 1
            except OSError as e:
                errs.append(f"{t.name}: {e}")
        if errs:
            return False, f"已写入 {n_ok} 个，失败 {len(errs)}（首条：{errs[0]}）"
        return True, f"已用 254.swf 模板覆盖 pet/swf 下共 {n_ok} 个文件（已按需生成 swf_og 备份）"
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
        ok2, msg2 = rename_nono_super_super_og_to_action()
        if not ok2:
            return (
                False,
                f"已从 OG 还原 PetStorage.swf <- {og}；但 {msg2}",
            )
        return True, f"已从 OG 还原 PetStorage.swf <- {og}；{msg2}"
    except OSError as e:
        return False, str(e)


def restore_pet_254_from_og() -> Tuple[bool, str]:
    _ensure_project_path()
    from config import GAME_SWF_FOLDER, GAME_SWF_OG_FOLDER

    live = Path(GAME_SWF_FOLDER)
    if not live.is_dir():
        return False, "pet/swf 目录不存在"
    r, s = _restore_dir_from_og(live, Path(GAME_SWF_OG_FOLDER))
    if r == 0 and s == 0:
        return False, "pet/swf 下没有 .swf 文件"
    return True, f"已从 swf_og 还原 {r} 个 .swf（无备份未覆盖: {s}）"


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
    if not lp.is_dir():
        return False, "fight pet 目录不存在"
    r, s = _restore_dir_from_og(lp, Path(og))
    if r == 0 and s == 0:
        return False, "fight pet 目录下无 .swf"
    return True, f"已从 swf_og 还原 {r} 个（无备份未覆盖: {s}）"


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
    if not lp.is_dir():
        return False, "fight skill 目录不存在"
    r, s = _restore_dir_from_og(lp, Path(og))
    if r == 0 and s == 0:
        return False, "fight skill 目录下无 .swf"
    return True, f"已从 swf_og 还原 {r} 个（无备份未覆盖: {s}）"


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
