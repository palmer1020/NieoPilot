import os
import sys
import time
from pathlib import Path

from PIL import ImageGrab

# ===== 注入项目根目录 =====
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.utils import window_manager  # 你现有的 window_manager


def load_region_points(region_json_path: Path):
    import json
    data = json.loads(region_json_path.read_text(encoding="utf-8"))
    pts = data.get("points", [])
    if not pts or not isinstance(pts, list):
        raise ValueError("region json 里没有 points")
    points = [(int(p[0]), int(p[1])) for p in pts]
    return points


def bbox_from_game_points(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def grab_region_bbox(points):
    """按外接 bbox 截图（用于模板/OCR）"""
    # 把 bbox 的四个角转换到屏幕坐标，再取屏幕外接矩形
    minx, miny, maxx, maxy = bbox_from_game_points(points)
    corners = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)]
    screen_pts = []
    for gx, gy in corners:
        s = window_manager.game_to_screen(gx, gy)
        if not s:
            return None
        screen_pts.append(s)

    sx = [p[0] for p in screen_pts]
    sy = [p[1] for p in screen_pts]
    left, top, right, bottom = min(sx), min(sy), max(sx), max(sy)

    # bbox 需要 right/bottom 是“边界外一像素”，这里稍微 +1 防止裁剪少一列/行
    return ImageGrab.grab(bbox=(left, top, right + 1, bottom + 1), all_screens=True)


def main():
    import sys
    
    region_root = PROJECT_ROOT / "assets" / "regions"
    template_root = PROJECT_ROOT / "assets" / "templates"
    print("\n=== Template Capture Tool ===\n")

    # 支持命令行参数，如果没有则从输入获取
    if len(sys.argv) >= 3:
        rel_input = sys.argv[1].strip()
        state = sys.argv[2].strip()
    else:
        # 支持 xx.xx.xx 格式输入，自动转换为路径并添加 .json
        rel_input = input("输入 region 路径（例如：对战.逃跑.切换逃跑面板 或 对战/逃跑/切换逃跑面板.json）：").strip()
        if not rel_input:
            print("❌ 取消")
            return
        
        state = input("输入状态名（例如：灰色 / 蓝色）：").strip()
        if not state:
            print("❌ 状态名不能为空")
            return
    
    # 如果输入是点号分隔的格式（xx.xx.xx），转换为路径格式
    if "." in rel_input and "/" not in rel_input:
        # 点号格式：将点号替换为斜杠，并添加 .json
        rel = rel_input.replace(".", "/") + ".json"
    elif not rel_input.endswith(".json"):
        # 如果既不是点号格式也不是以 .json 结尾，添加 .json
        rel = rel_input + ".json"
    else:
        # 已经是路径格式
        rel = rel_input

    region_path = region_root / rel
    if not region_path.exists():
        print(f"❌ 找不到：{region_path}")
        return

    try:
        points = load_region_points(region_path)
    except Exception as e:
        print(f"❌ 解析 region 失败：{e}")
        return

    # 不强制启动游戏：只尝试连接
    if not window_manager.find_window():
        print("❌ 未找到游戏窗口，请先手动打开游戏并显示窗口后再运行")
        return

    # 不再创建状态文件夹，直接在模板目录下创建文件
    out_dir = template_root / rel.replace(".json", "")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n开始自动截图（状态：{state}）...")
    print(f"文件将保存到：{out_dir}，文件名格式：序号_时间戳_{state}.png")
    print("提示：请确保游戏窗口已显示正确的状态，截图将在 2 秒后开始...")
    time.sleep(2.0)  # 给用户2秒时间切换到正确的状态
    
    # 自动截图固定数量（5张）
    capture_count = 5
    for idx in range(1, capture_count + 1):
        print(f"[{state}] 正在截图第 {idx}/{capture_count} 张...")
        
        img = grab_region_bbox(points)
        if img is None:
            print(f"❌ 第 {idx} 张截图失败：视口未就绪")
            time.sleep(0.5)  # 等待一下再重试
            continue

        ts = time.strftime("%H%M%S")
        # 文件名格式：序号_时间戳_状态名.png
        out_path = out_dir / f"{idx:02d}_{ts}_{state}.png"
        img.save(out_path)
        print(f"✅ 已保存：{out_path}")
        
        # 每张截图之间稍作延迟（0.3秒）
        if idx < capture_count:
            time.sleep(0.3)

    print(f"\n✅ 完成！已成功采集 {capture_count} 张图片（状态：{state}）。")


if __name__ == "__main__":
    main()


