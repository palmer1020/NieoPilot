#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
显示稀有精灵文件夹内1-9点的分布（在屏幕上显示红点）

用法：
    python tools/show_route_points.py
    然后输入文件夹名称，如：尼奥一
"""

import json
import os
import sys
from pathlib import Path
from typing import List, Tuple, Optional

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Windows控制台编码修复
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except:
        pass

from core.region_store import RegionStore, Region
from core.utils import window_manager
from PIL import Image, ImageDraw, ImageFont

def load_route_points(folder_name: str, region_root: str) -> List[Tuple[int, Region, Tuple[float, float]]]:
    """
    加载指定文件夹内的1-9点区域
    
    Returns:
        List of (point_number, region, center_point)
    """
    points = []
    
    # 构建文件夹路径
    folder_path = Path(region_root) / folder_name
    
    if not folder_path.exists():
        print(f"[ERROR] 文件夹不存在: {folder_path}")
        return points
    
    # 加载1-9点
    for i in range(1, 10):
        json_file = folder_path / f"{i}.json"
        if not json_file.exists():
            print(f"[WARN] 文件不存在: {json_file}")
            continue
        
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 解析points
            points_data = data.get("points", [])
            if not points_data:
                print(f"[WARN] {json_file} 没有points数据")
                continue
            
            pts = [(float(p[0]), float(p[1])) for p in points_data if len(p) >= 2]
            if not pts:
                continue
            
            # 创建Region对象
            click_config = data.get("click", {"random": True})
            region = Region(key=f"{folder_name}.{i}", points=pts, click=click_config)
            
            # 计算中心点
            x1, y1, x2, y2 = region.outer_bbox()
            center = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
            
            points.append((i, region, center))
            print(f"[OK] 加载点{i}: 中心=({center[0]:.1f}, {center[1]:.1f})")
            
        except Exception as e:
            print(f"[ERROR] 加载{json_file}失败: {e}")
            continue
    
    return points

def draw_points_on_screen(points: List[Tuple[int, Region, Tuple[float, float]]], folder_name: str):
    """
    在游戏窗口上绘制红点标记1-9点的位置
    """
    # 获取游戏窗口
    if not window_manager.find_window():
        print("[ERROR] 未找到游戏窗口，请先启动游戏")
        return
    
    # 获取游戏窗口位置和大小
    try:
        hwnd = window_manager.hwnd  # 使用hwnd而不是game_hwnd
        if not hwnd:
            print("[ERROR] 未找到游戏窗口句柄")
            return
        
        import win32gui
        rect = win32gui.GetWindowRect(hwnd)
        window_x, window_y, window_right, window_bottom = rect
        window_width = window_right - window_x
        window_height = window_bottom - window_y
        
        print(f"[INFO] 游戏窗口: 位置=({window_x}, {window_y}), 大小=({window_width}, {window_height})")
    except Exception as e:
        print(f"[ERROR] 获取窗口信息失败: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # 截图 - 使用grab_game_bbox截取整个游戏区域
    try:
        # 从config获取游戏逻辑大小
        from config import GAME_LOGIC_W, GAME_LOGIC_H
        
        # 使用grab_game_bbox截取整个游戏区域
        img = window_manager.grab_game_bbox(0, 0, GAME_LOGIC_W, GAME_LOGIC_H)
        if img is None:
            print("[ERROR] 截图失败（grab_game_bbox返回None）")
            return
        
        print(f"[INFO] 截图成功: 大小={img.size}")
    except Exception as e:
        print(f"[ERROR] 截图失败: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # 创建绘图对象
    draw = ImageDraw.Draw(img)
    
    # 绘制每个点
    point_radius = 10  # 红点半径
    for point_num, region, center in points:
        cx, cy = center
        
        # 绘制红色圆点
        draw.ellipse(
            [cx - point_radius, cy - point_radius, cx + point_radius, cy + point_radius],
            fill='red',
            outline='darkred',
            width=3
        )
        
        # 绘制点编号（白色背景，黑色文字）
        try:
            # 尝试使用中文字体
            font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 16)  # 微软雅黑
        except:
            try:
                font = ImageFont.truetype("arial.ttf", 16)
            except:
                font = ImageFont.load_default()
        
        text = str(point_num)
        # 获取文字大小
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        
        # 绘制文字背景（白色矩形）
        text_x = cx - text_width // 2
        text_y = cy - text_height // 2 - point_radius - 8
        draw.rectangle(
            [text_x - 3, text_y - 3, text_x + text_width + 3, text_y + text_height + 3],
            fill='white',
            outline='black',
            width=2
        )
        
        # 绘制文字
        draw.text(
            (text_x, text_y),
            text,
            fill='black',
            font=font
        )
    
    # 显示图像
    try:
        img.show(title=f"Route Points - {folder_name}")
        print(f"[OK] 已显示路由点分布图（共{len(points)}个点）")
        print("[TIP] 提示：关闭图像窗口即可")
    except Exception as e:
        print(f"[ERROR] 显示图像失败: {e}")
        print("[TIP] 正在保存图像到项目根目录...")
        
        # 保存图像
        output_path = project_root / f"route_points_{folder_name}.png"
        img.save(output_path)
        print(f"[OK] 图像已保存到: {output_path}")

def main():
    print("=" * 60)
    print("路由点分布显示工具")
    print("=" * 60)
    
    # 获取region根目录
    region_root = project_root / "assets" / "regions"
    if not region_root.exists():
        print(f"[ERROR] Region目录不存在: {region_root}")
        return
    
    print(f"[INFO] Region目录: {region_root}")
    
    # 列出可用的文件夹
    print("\n可用的文件夹:")
    folders = [d.name for d in region_root.iterdir() if d.is_dir()]
    folders.sort()
    for i, folder in enumerate(folders, 1):
        # 检查是否有1.json文件
        has_points = (region_root / folder / "1.json").exists()
        marker = "[OK]" if has_points else "[  ]"
        print(f"  {marker} {folder}")
    
    # 输入文件夹名称
    print("\n" + "-" * 60)
    folder_name = input("请输入文件夹名称（如：尼奥一、尼奥二、嘟咕噜等）: ").strip()
    
    if not folder_name:
        print("[ERROR] 文件夹名称不能为空")
        return
    
    # 加载路由点
    print(f"\n[INFO] 加载文件夹: {folder_name}")
    points = load_route_points(folder_name, str(region_root))
    
    if not points:
        print(f"[ERROR] 未找到任何路由点（请确认文件夹{folder_name}内存在1-9.json文件）")
        return
    
    print(f"\n[OK] 成功加载{len(points)}个路由点")
    
    # 显示点信息
    print("\n路由点信息:")
    for point_num, region, center in points:
        x1, y1, x2, y2 = region.outer_bbox()
        print(f"  点{point_num}: 中心=({center[0]:.1f}, {center[1]:.1f}), "
              f"区域=({x1:.1f}, {y1:.1f}) ~ ({x2:.1f}, {y2:.1f})")
    
    # 在屏幕上绘制
    print("\n[INFO] 正在截图并绘制路由点...")
    draw_points_on_screen(points, folder_name)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n[INFO] 用户中断")
    except Exception as e:
        import traceback
        print(f"\n[ERROR] 发生错误: {e}")
        traceback.print_exc()

