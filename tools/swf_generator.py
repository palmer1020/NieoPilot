import os
import shutil
from pathlib import Path

SRC_DIR = Path(r"C:\Users\dayuz\AppData\Local\Programs\nieoasset\resource\pet\swf_og")
DST_DIR = Path(r"C:\Users\dayuz\AppData\Local\Programs\nieoasset\resource\pet\swf")

def parse_excludes(s: str) -> set[str]:
    """
    支持输入：
    - 空：直接回车
    - 逗号分隔：16,27,252
    - 带大括号：{16,27,252}
    - 允许有空格
    返回：不带 .swf 的文件名集合（字符串）
    """
    s = s.strip()
    if not s:
        return set()

    # 去掉可能的 {} [] ()
    for ch in "{}[]()":
        s = s.replace(ch, "")

    items = []
    for part in s.split(","):
        part = part.strip()
        if part:
            items.append(part)

    return set(items)

def main():
    if not SRC_DIR.exists():
        raise FileNotFoundError(f"源目录不存在：{SRC_DIR}")

    template_id = input("1) 输入要复制的 swf 文件名（不带后缀），例如 254：").strip()
    if not template_id:
        print("未输入模板文件名，退出。")
        return

    template_file = SRC_DIR / f"{template_id}.swf"
    if not template_file.exists():
        raise FileNotFoundError(f"模板文件不存在：{template_file}")

    excludes_raw = input("2) 输入不希望生成的文件名（例如 16,27,252 或 {16,27,252}；可直接回车表示不排除）：")
    excludes = parse_excludes(excludes_raw)

    DST_DIR.mkdir(parents=True, exist_ok=True)

    src_files = sorted(SRC_DIR.glob("*.swf"))
    if not src_files:
        print(f"源目录下没有 swf 文件：{SRC_DIR}")
        return

    copied = 0
    skipped = 0

    for f in src_files:
        stem = f.stem  # 文件名不带后缀
        if stem in excludes:
            skipped += 1
            continue

        dst_path = DST_DIR / f.name  # 保留原文件名（例如 16.swf）
        # 用模板文件内容覆盖写入
        shutil.copyfile(template_file, dst_path)
        copied += 1

    print("\n✅ 完成")
    print(f"模板文件：{template_file}")
    print(f"输出目录：{DST_DIR}")
    print(f"生成数量：{copied}")
    print(f"跳过数量：{skipped}")
    if excludes:
        print(f"跳过列表：{sorted(excludes, key=lambda x: (len(x), x))}")

if __name__ == "__main__":
    main()
