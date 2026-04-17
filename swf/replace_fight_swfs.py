#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用本目录下的模板批量覆盖游戏资源中的对战 SWF：

- fightpet.swf  → GAME_FIGHT_PET_SWF_DIR 下每一个已有的 *.swf
- fightskill.swf → GAME_FIGHT_SKILL_SWF_DIR 下每一个已有的 *.swf

路径来自 config（GAME_ASSET_BASE_PATH）。在项目根目录执行：

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

try:
    from config import (
        GAME_ASSET_BASE_PATH,
        GAME_FIGHT_PET_SWF_DIR,
        GAME_FIGHT_SKILL_SWF_DIR,
    )
except ImportError:
    _here = Path(__file__).resolve().parent.parent
    BASE_PATH = str(_here)
    GAME_ASSET_BASE_PATH = r"E:\1\nieoasset"
    GAME_FIGHT_PET_SWF_DIR = os.path.join(
        GAME_ASSET_BASE_PATH, "resource", "fightResource", "pet", "swf"
    )
    GAME_FIGHT_SKILL_SWF_DIR = os.path.join(
        GAME_ASSET_BASE_PATH, "resource", "fightResource", "skill", "swf"
    )


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def _copy_template_to_targets(
    template: Path,
    dest_dir: Path,
    dry_run: bool,
    label: str,
) -> Tuple[int, List[str]]:
    if not template.is_file():
        raise FileNotFoundError(f"模板不存在: {template}")
    if not dest_dir.is_dir():
        raise FileNotFoundError(f"目标目录不存在: {dest_dir}")

    targets = sorted(dest_dir.glob("*.swf"))
    errors: List[str] = []
    ok = 0
    for t in targets:
        if dry_run:
            print(f"  [预览] {t.name} <- {template.name}")
            ok += 1
            continue
        try:
            shutil.copy2(template, t)
            ok += 1
            print(f"  [OK] {t.name}")
        except OSError as e:
            errors.append(f"{t.name}: {e}")
            print(f"  [FAIL] {t.name}: {e}", file=sys.stderr)
    if not targets:
        print(f"  [{label}] 目录下没有 .swf 文件: {dest_dir}")
    return ok, errors


def replace_pet_swfs(dry_run: bool = False) -> Tuple[int, List[str]]:
    tpl = _script_dir() / "fightpet.swf"
    dest = Path(GAME_FIGHT_PET_SWF_DIR)
    print(f"[pet] 模板: {tpl}")
    print(f"[pet] 目标: {dest}")
    return _copy_template_to_targets(tpl, dest, dry_run, "pet")


def replace_skill_swfs(dry_run: bool = False) -> Tuple[int, List[str]]:
    tpl = _script_dir() / "fightskill.swf"
    dest = Path(GAME_FIGHT_SKILL_SWF_DIR)
    print(f"[skill] 模板: {tpl}")
    print(f"[skill] 目标: {dest}")
    return _copy_template_to_targets(tpl, dest, dry_run, "skill")


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
        print("不能同时指定 --pet-only 与 --skill-only", file=sys.stderr)
        return 2

    all_errors: List[str] = []
    try:
        if args.skill_only:
            _, err = replace_skill_swfs(dry_run=args.dry_run)
            all_errors.extend(err)
        elif args.pet_only:
            _, err = replace_pet_swfs(dry_run=args.dry_run)
            all_errors.extend(err)
        else:
            print("=== pet ===")
            _, err_p = replace_pet_swfs(dry_run=args.dry_run)
            all_errors.extend(err_p)
            print()
            print("=== skill ===")
            _, err_s = replace_skill_swfs(dry_run=args.dry_run)
            all_errors.extend(err_s)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1

    if all_errors:
        print(f"\n完成，有 {len(all_errors)} 个错误。", file=sys.stderr)
        return 1
    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
