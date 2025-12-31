# regions_structure_reader.py  (放在项目根目录运行)
from pathlib import Path
import json
import time

def build_tree_lines(root: Path):
    lines = [f"{root.name}/"]

    def walk(dir_path: Path, prefix=""):
        items = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name))
        for i, p in enumerate(items):
            last = (i == len(items) - 1)
            branch = "└─ " if last else "├─ "
            if p.is_dir():
                lines.append(f"{prefix}{branch}{p.name}/")
                walk(p, prefix + ("   " if last else "│  "))
            else:
                lines.append(f"{prefix}{branch}{p.name}")

    walk(root)
    return lines

def build_structure_dict(root: Path):
    """递归构建结构：只包含文件夹名和文件名"""
    def helper(p: Path):
        if p.is_dir():
            return {
                "type": "dir",
                "name": p.name,
                "children": [helper(x) for x in sorted(p.iterdir(), key=lambda z: (z.is_file(), z.name))]
            }
        else:
            return {
                "type": "file",
                "name": p.name
            }
    return helper(root)

def main():
    project_root = Path(__file__).resolve().parent
    region_root = project_root / "assets" / "regions"

    if not region_root.exists():
        print(f"❌ 找不到目录：{region_root}")
        return

    # 1) txt 树
    tree_lines = build_tree_lines(region_root)
    out_txt = project_root / "regions_structure.txt"
    out_txt.write_text("\n".join(tree_lines), encoding="utf-8")

    # 2) json 结构
    struct = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "root": str(region_root),
        "structure": build_structure_dict(region_root)
    }
    out_json = project_root / "regions_structure.json"
    out_json.write_text(json.dumps(struct, indent=2, ensure_ascii=False), encoding="utf-8")

    print("✅ 已生成：")
    print(f" - {out_txt.name}")
    print(f" - {out_json.name}")

if __name__ == "__main__":
    main()


