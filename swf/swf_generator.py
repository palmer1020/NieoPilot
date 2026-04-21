#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SWF 文件批量替换与补齐脚本

功能：
1. 查找模板文件（优先本目录下的 254.swf 等，其次游戏 NieoData/resource/pet 下）
2. 步骤1：将游戏 pet/swf 目录下每个已有文件的内容替换为模板内容
3. 步骤2：补齐 1.swf ~ 5000.swf（缺失的创建，已有的覆盖）

用法（在项目根目录）：
  python swf/swf_generator.py
  python swf/swf_generator.py 254
  python swf/swf_generator.py --dry-run
"""

import os
import sys
import shutil
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config_bootstrap import ensure_config_py

ensure_config_py(str(_PROJECT_ROOT))

try:
    from config import GAME_SWF_FOLDER, GAME_ASSET_BASE_PATH
except ImportError:
    GAME_ASSET_BASE_PATH = r"E:\1\NieoGame\NieoData\resource"
    GAME_SWF_FOLDER = os.path.join(GAME_ASSET_BASE_PATH, "pet", "swf")

DST_DIR = Path(GAME_SWF_FOLDER)
PET_BASE = Path(GAME_ASSET_BASE_PATH) / "pet"
_SCRIPT_DIR = Path(__file__).resolve().parent


def find_template_file(template_id: str) -> Path:
    """
    依次尝试：
    1) 本脚本同目录（项目 swf/254.swf）
    2) pet/254.swf, pet/swf/254.swf, pet/swf_og/254.swf
    """
    candidates = [
        _SCRIPT_DIR / f"{template_id}.swf",
        PET_BASE / f"{template_id}.swf",
        PET_BASE / "swf" / f"{template_id}.swf",
        PET_BASE / "swf_og" / f"{template_id}.swf",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        f"模板文件 {template_id}.swf 未找到，已尝试：{[str(c) for c in candidates]}"
    )


def parse_excludes(s: str) -> set:
    """
    支持输入：
    - 空：直接回车
    - 逗号分隔：16,27,252
    - 带大括号：{16,27,252}
    - 允许有空格
    返回：不带 .swf 的文件名集合（字符串）
    """
    s = (s or "").strip()
    if not s:
        return set()
    for ch in "{}[]()":
        s = s.replace(ch, "")
    return {p.strip() for p in s.split(",") if p.strip()}


def run(template_id: str = "254", excludes: set = None, dry_run: bool = False):
    excludes = excludes or set()
    template_file = find_template_file(template_id)

    if not DST_DIR.exists():
        DST_DIR.mkdir(parents=True, exist_ok=True)
        print(f"[INFO] 已创建目标目录：{DST_DIR}")

    # 步骤1：将 swf/ 目录下每个已有文件替换为模板内容
    swf_files = sorted(DST_DIR.glob("*.swf"))
    copied = 0
    skipped = 0
    for f in swf_files:
        stem = f.stem
        if stem in excludes:
            skipped += 1
            continue
        if dry_run and copied < 5:  # 仅预览时前几个打印详情
            print(f"  [预览] 替换 {f.name} <- {template_file.name}")
        else:
            shutil.copyfile(template_file, f)
        copied += 1

    if not swf_files:
        print(f"[步骤1] swf 目录下没有 swf 文件，跳过")

    # 步骤2：补齐 1.swf ~ 5000.swf
    fill_count = 0
    fill_skipped = 0
    for i in range(1, 5001):
        stem = str(i)
        if stem in excludes:
            fill_skipped += 1
            continue
        dst_path = DST_DIR / f"{stem}.swf"
        if dry_run and fill_count < 5:  # 仅预览时前几个打印详情
            exists = dst_path.exists()
            print(f"  [预览] {'覆盖' if exists else '新建'} {stem}.swf")
        else:
            shutil.copyfile(template_file, dst_path)
        fill_count += 1

    print("\n✅ 完成")
    print(f"模板文件：{template_file}")
    print(f"输出目录：{DST_DIR}")
    print(f"[步骤1] swf 目录替换：{copied}，跳过：{skipped}")
    print(f"[步骤2] 1-5000 填充：{fill_count}，跳过：{fill_skipped}")
    if excludes:
        print(f"跳过列表：{sorted(excludes, key=lambda x: (len(x), x))}")
    return copied, fill_count


def main():
    # Windows 控制台 UTF-8
    if sys.platform == "win32":
        try:
            import io
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
        except Exception:
            pass
    dry_run = "--dry-run" in sys.argv or "-n" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("-")]

    if args:
        # 非交互：python swf/swf_generator.py 254 或 ... 254 16,27
        template_id = args[0]
        excludes = parse_excludes(args[1]) if len(args) > 1 else set()
    else:
        # 交互式
        template_id = input("1) 输入要复制的 swf 文件名（不带后缀），例如 254：").strip()
        if not template_id:
            print("未输入模板文件名，退出。")
            return
        excludes_raw = input("2) 输入不希望生成的文件名（例如 16,27,252；可直接回车表示不排除）：")
        excludes = parse_excludes(excludes_raw)

    run(template_id=template_id, excludes=excludes, dry_run=dry_run)


if __name__ == "__main__":
    main()
