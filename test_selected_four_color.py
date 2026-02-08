#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试选中四区域颜色检测
使用template中的黄色和蓝色图片进行测试
"""

import os
import numpy as np
from PIL import Image

# 定义参考颜色（基于template图片实际测试结果）
YELLOW_MIXED_REF = (230, 235, 26)  # 黄色混合参考RGB（基于01_123111_黄.png实际测试值）
BLUE_REF = (26, 115, 178)  # 蓝色参考RGB（基于01_123737_蓝.png实际测试值）

def euclidean_distance(rgb1, rgb2):
    """计算两个RGB颜色的欧氏距离"""
    return np.sqrt(sum((a - b) ** 2 for a, b in zip(rgb1, rgb2)))

def check_selected_four_color(img_path):
    """
    检测选中四区域的颜色
    
    Returns:
        1: 黄色混合（继续执行放回程序）
        0: 蓝色（执行刷新重连）
        None: 检测失败
    """
    try:
        # 读取图片
        img = Image.open(img_path)
        if img is None:
            print(f"[WARN] 无法读取图片: {img_path}")
            return None
        
        # 转换为RGB数组
        arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
        if arr.size == 0:
            print(f"[WARN] 图片为空: {img_path}")
            return None
        
        # 计算平均RGB
        mean_rgb = np.round(arr.mean(axis=(0, 1))).astype(int)
        r, g, b = int(mean_rgb[0]), int(mean_rgb[1]), int(mean_rgb[2])
        print(f"[INFO] 图片: {os.path.basename(img_path)}")
        print(f"   平均RGB: ({r}, {g}, {b})")
        
        # 计算欧氏距离
        dist_to_yellow_mixed = euclidean_distance((r, g, b), YELLOW_MIXED_REF)
        dist_to_blue = euclidean_distance((r, g, b), BLUE_REF)
        
        print(f"   距离黄色混合参考: {dist_to_yellow_mixed:.2f}")
        print(f"   距离蓝色参考: {dist_to_blue:.2f}")
        
        # 使用RGB特征辅助判断
        rgb_yellow_score = (r + g) / (b + 1)  # 黄色得分（越高越黄）
        rgb_blue_score = b / (r + g + 1)       # 蓝色得分（越高越蓝）
        
        print(f"   RGB黄色得分: {rgb_yellow_score:.2f}")
        print(f"   RGB蓝色得分: {rgb_blue_score:.2f}")
        
        # 综合判断
        is_yellow_mixed = False
        is_blue = False
        
        if dist_to_yellow_mixed < dist_to_blue:
            # 距离黄色混合更近
            if rgb_yellow_score > 1.5:
                is_yellow_mixed = True
                print(f"   [OK] 判定：黄色混合（距离更近且RGB特征偏向黄色）")
            elif rgb_blue_score > 0.8:
                is_blue = True
                print(f"   [OK] 判定：蓝色（虽然距离黄色混合更近，但RGB特征明显偏向蓝色）")
            else:
                is_yellow_mixed = True
                print(f"   [OK] 判定：黄色混合（距离更近）")
        else:
            # 距离蓝色更近
            if rgb_blue_score > 0.6:
                is_blue = True
                print(f"   [OK] 判定：蓝色（距离更近且RGB特征偏向蓝色）")
            elif rgb_yellow_score > 2.0:
                is_yellow_mixed = True
                print(f"   [OK] 判定：黄色混合（虽然距离蓝色更近，但RGB特征明显偏向黄色）")
            else:
                is_blue = True
                print(f"   [OK] 判定：蓝色（距离更近）")
        
        # 返回结果
        if is_yellow_mixed:
            print(f"   [RESULT] 最终结果: 1 (黄色混合，继续执行放回程序)")
            return 1
        elif is_blue:
            print(f"   [RESULT] 最终结果: 0 (蓝色，执行刷新重连)")
            return 0
        else:
            print(f"   [RESULT] 最终结果: None (无法判断)")
            return None
            
    except Exception as e:
        print(f"[ERROR] 检测异常: {e}")
        import traceback
        print(traceback.format_exc())
        return None

def main():
    """主函数"""
    print("=" * 60)
    print("选中四区域颜色检测测试")
    print("=" * 60)
    print()
    
    # 测试图片路径
    template_dir = "assets/templates/精灵背包/选中四"
    yellow_img = os.path.join(template_dir, "01_123111_黄.png")
    blue_img = os.path.join(template_dir, "01_123737_蓝.png")
    
    # 检查文件是否存在
    if not os.path.exists(yellow_img):
        print(f"[ERROR] 找不到黄色图片: {yellow_img}")
        return
    if not os.path.exists(blue_img):
        print(f"[ERROR] 找不到蓝色图片: {blue_img}")
        return
    
    print("参考颜色定义:")
    print(f"  黄色混合参考RGB: {YELLOW_MIXED_REF}")
    print(f"  蓝色参考RGB: {BLUE_REF}")
    print()
    
    # 测试黄色图片
    print("-" * 60)
    print("测试1: 黄色图片")
    print("-" * 60)
    result1 = check_selected_four_color(yellow_img)
    print()
    
    # 测试蓝色图片
    print("-" * 60)
    print("测试2: 蓝色图片")
    print("-" * 60)
    result2 = check_selected_four_color(blue_img)
    print()
    
    # 总结
    print("=" * 60)
    print("测试总结")
    print("=" * 60)
    print(f"黄色图片结果: {result1} (期望: 1)")
    print(f"蓝色图片结果: {result2} (期望: 0)")
    
    if result1 == 1 and result2 == 0:
        print("[SUCCESS] 测试通过！")
    else:
        print("[FAIL] 测试失败！")
        if result1 != 1:
            print(f"   黄色图片判断错误，期望1，实际{result1}")
        if result2 != 0:
            print(f"   蓝色图片判断错误，期望0，实际{result2}")

if __name__ == "__main__":
    main()

