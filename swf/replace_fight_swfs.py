#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用本目录下的模板批量覆盖游戏资源中的对战 SWF：

- fightpet.swf  → GAME_FIGHT_PET_SWF_DIR 下每一个已有的 *.swf
- fightskill.swf → GAME_FIGHT_SKILL_SWF_DIR 下每一个已有的 *.swf

路径来自 config（GAME_ASSET_BASE_PATH = …/NieoData/resource）。在项目根目录执行：

  python swf/replace_fight_swfs.py
  python swf/replace_fight_swfs.py --dry-run
  python swf/replace_fight_swfs.py --pet-only
  python swf/replace_fight_swfs.py --skill-only
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from config import (
        GAME_ASSET_BASE_PATH,
        GAME_FIGHT_PET_SWF_DIR,
        GAME_FIGHT_SKILL_SWF_DIR,
    )
    import config as _cfg

    _def_pet_og = os.path.join(GAME_ASSET_BASE_PATH, "fightResource", "pet", "swf_og")
    _def_skill_og = os.path.join(GAME_ASSET_BASE_PATH, "fightResource", "skill", "swf_og")
    GAME_FIGHT_PET_SWF_OG_DIR = getattr(_cfg, "GAME_FIGHT_PET_SWF_OG_DIR", _def_pet_og)
    GAME_FIGHT_SKILL_SWF_OG_DIR = getattr(_cfg, "GAME_FIGHT_SKILL_SWF_OG_DIR", _def_skill_og)
except ImportError:
    _here = Path(__file__).resolve().parent.parent
    BASE_PATH = str(_here)
    GAME_ASSET_BASE_PATH = r"E:\1\NieoGame\NieoData\resource"
    GAME_FIGHT_PET_SWF_DIR = os.path.join(
        GAME_ASSET_BASE_PATH, "fightResource", "pet", "swf"
    )
    GAME_FIGHT_SKILL_SWF_DIR = os.path.join(
        GAME_ASSET_BASE_PATH, "fightResource", "skill", "swf"
    )
    GAME_FIGHT_PET_SWF_OG_DIR = os.path.join(
        GAME_ASSET_BASE_PATH, "fightResource", "pet", "swf_og"
    )
    GAME_FIGHT_SKILL_SWF_OG_DIR = os.path.join(
        GAME_ASSET_BASE_PATH, "fightResource", "skill", "swf_og"
    )


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def _swf_name_sort_key(p: Path) -> Tuple:
    """按数字文件名排序（如 1.swf … 10.swf）；非数字名放后并按小写序。"""
    stem = p.stem
    try:
        return (0, int(stem))
    except ValueError:
        return (1, stem.lower())


def fill_live_swf_from_og_dir(live_dir: Path, og_dir: Path) -> Tuple[int, int]:
    """
    按 og 目录内已有 *.swf，将 live 侧缺失的同名文件从 og 复制补齐（按数字序号排序遍历）。
    用于 Pet 254 覆盖前：先用 og 集合对齐 live，再对已存在文件做 _ensure_og_backup。
    返回 (补齐文件数, og 内 .swf 数量)。
    """
    if not og_dir.is_dir():
        return 0, 0
    og_files = sorted(og_dir.glob("*.swf"), key=_swf_name_sort_key)
    if not og_files:
        return 0, 0
    live_dir.mkdir(parents=True, exist_ok=True)
    filled = 0
    for og in og_files:
        dest = live_dir / og.name
        if dest.is_file():
            continue
        shutil.copy2(og, dest)
        filled += 1
    return filled, len(og_files)


def _ensure_og_backup(live: Path, og_path: Path) -> None:
    """覆盖或删除前：若当前 live 存在且尚无对应 og 副本，则写入 og。"""
    if not live.is_file():
        return
    if og_path.is_file():
        return
    og_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(live, og_path)


def _copy_template_to_targets(
    template: Path,
    dest_dir: Path,
    dry_run: bool,
    label: str,
    *,
    quiet: bool = False,
    og_dir: Optional[Path] = None,
) -> Tuple[int, List[str]]:
    if not template.is_file():
        raise FileNotFoundError(f"Template missing: {template}")
    if not dest_dir.is_dir():
        raise FileNotFoundError(f"Destination missing: {dest_dir}")

    targets = sorted(dest_dir.glob("*.swf"))
    errors: List[str] = []
    ok = 0
    for t in targets:
        if dry_run:
            if not quiet:
                print(f"  [dry-run] {t.name} <- {template.name}")
            ok += 1
            continue
        try:
            if og_dir is not None:
                _ensure_og_backup(t, og_dir / t.name)
            shutil.copy2(template, t)
            ok += 1
            if not quiet:
                print(f"  [OK] {t.name}")
        except OSError as e:
            errors.append(f"{t.name}: {e}")
            if not quiet:
                print(f"  [FAIL] {t.name}: {e}", file=sys.stderr)
    if not targets:
        if not quiet:
            print(f"  [{label}] no .swf files in: {dest_dir}")
    return ok, errors


def replace_pet_swfs(dry_run: bool = False, *, quiet: bool = False) -> Tuple[int, List[str]]:
    tpl = _script_dir() / "fightpet.swf"
    dest = Path(GAME_FIGHT_PET_SWF_DIR)
    og = Path(GAME_FIGHT_PET_SWF_OG_DIR)
    if not quiet:
        print(f"[pet] template: {tpl}")
        print(f"[pet] dest: {dest}")
        print(f"[pet] og: {og}")
    return _copy_template_to_targets(tpl, dest, dry_run, "pet", quiet=quiet, og_dir=og)


def replace_skill_swfs(dry_run: bool = False, *, quiet: bool = False) -> Tuple[int, List[str]]:
    tpl = _script_dir() / "fightskill.swf"
    dest = Path(GAME_FIGHT_SKILL_SWF_DIR)
    og = Path(GAME_FIGHT_SKILL_SWF_OG_DIR)
    if not quiet:
        print(f"[skill] template: {tpl}")
        print(f"[skill] dest: {dest}")
        print(f"[skill] og: {og}")
    return _copy_template_to_targets(tpl, dest, dry_run, "skill", quiet=quiet, og_dir=og)


def main(argv: Optional[Iterable[str]] = None) -> int:
    if sys.platform == "win32":
        try:
            import io

            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
        except Exception:
            pass

    p = argparse.ArgumentParser(description="用 fightpet.swf / fightskill.swf 覆盖对战资源目录")
    p.add_argument("--dry-run", "-n", action="store_true", help="只列出将要覆盖的文件，不写入")
    p.add_argument("--pet-only", action="store_true", help="只处理 pet/swf")
    p.add_argument("--skill-only", action="store_true", help="只处理 skill/swf")
    args = p.parse_args(list(argv) if argv is not None else None)

    if args.pet_only and args.skill_only:
        print("Cannot use --pet-only and --skill-only together.", file=sys.stderr)
        return 2

    all_errors: List[str] = []
    try:
        if args.skill_only:
            _, err = replace_skill_swfs(dry_run=args.dry_run, quiet=False)
            all_errors.extend(err)
        elif args.pet_only:
            _, err = replace_pet_swfs(dry_run=args.dry_run, quiet=False)
            all_errors.extend(err)
        else:
            print("=== pet ===")
            _, err_p = replace_pet_swfs(dry_run=args.dry_run, quiet=False)
            all_errors.extend(err_p)
            print()
            print("=== skill ===")
            _, err_s = replace_skill_swfs(dry_run=args.dry_run, quiet=False)
            all_errors.extend(err_s)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1

    if all_errors:
        print(f"\nDone with {len(all_errors)} error(s).", file=sys.stderr)
        return 1
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
