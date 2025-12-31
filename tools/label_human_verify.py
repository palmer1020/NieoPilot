# tools/label_human_verify.py
import os
import sys
import json
import shutil
from glob import glob

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)

DATA_ROOT = os.path.join(PROJECT_ROOT, "human_verify")
TODO_DIR = os.path.join(DATA_ROOT, "未处理")
DONE_DIR = os.path.join(DATA_ROOT, "已处理")
os.makedirs(DONE_DIR, exist_ok=True)

def main():
    metas = sorted(glob(os.path.join(TODO_DIR, "*_meta.json")))
    if not metas:
        print("✅ 未处理为空")
        return

    print(f"发现未处理样本：{len(metas)} 个")
    print("输入格式：对 opt1~opt4 依次输入 4 个标签，用逗号分隔：正面/侧面/背面")
    print("例：正面,侧面,背面,侧面   或  z,s,b,s")
    print("输入 q 退出\n")

    def norm(x: str) -> str:
        x = (x or "").strip()
        m = {"z": "正面", "s": "侧面", "b": "背面", "正": "正面", "侧": "侧面", "背": "背面"}
        return m.get(x, x)

    for meta_path in metas:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        print("=" * 60)
        print("meta:", os.path.basename(meta_path))
        print("target face:", meta.get("face"))
        print("ocr:", meta.get("ocr_text"))
        print("option_pids:", meta.get("option_pids"))
        print("files:", meta.get("files", {}))

        ans = input("为 opt1~opt4 输入朝向标签：").strip()
        if ans.lower() == "q":
            break

        parts = [p.strip() for p in ans.split(",") if p.strip()]
        if len(parts) != 4:
            print("❌ 必须输入 4 个标签，用逗号隔开。跳过该样本。")
            continue

        labels = [norm(p) for p in parts]
        meta["labels"] = {
            "opt1": labels[0],
            "opt2": labels[1],
            "opt3": labels[2],
            "opt4": labels[3],
        }

        # 写入新 meta 到 已处理
        base = os.path.basename(meta_path).replace("_meta.json", "")
        new_meta = os.path.join(DONE_DIR, base + "_meta.json")
        with open(new_meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        # 移动/清理关联文件
        files = meta.get("files", {})
        rels = []
        if files.get("panel"): rels.append(files["panel"])
        if files.get("info"): rels.append(files["info"])
        for x in files.get("opts", []) or []:
            rels.append(x)

        # meta 自己也移动
        rels.append(os.path.basename(meta_path))

        for fn in rels:
            src = os.path.join(TODO_DIR, fn)
            if os.path.exists(src):
                dst = os.path.join(DONE_DIR, fn)
                shutil.move(src, dst)

        print("✅ 已处理并迁移到 已处理（未处理已删除）")

if __name__ == "__main__":
    main()


