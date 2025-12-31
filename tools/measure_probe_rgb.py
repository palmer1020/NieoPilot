#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测量切换探针图片的平均RGB值"""

import sys
import os
from PIL import Image
import numpy as np

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def measure_image_rgb(image_path: str) -> tuple:
    """测量图片的平均RGB值"""
    try:
        img = Image.open(image_path)
        # 转换为RGB模式（如果不是）
        if img.mode != 'RGB':
            img = img.convert('RGB')
        
        # 转换为numpy数组
        arr = np.array(img)
        
        # 计算平均RGB
        mean_rgb = arr.mean(axis=(0, 1))
        
        return tuple(mean_rgb.astype(int))
    except Exception as e:
        print(f"❌ 读取图片失败 {image_path}: {e}")
        return None

if __name__ == "__main__":
    # 图片路径
    img1_path = r"C:\Users\dayuz\OneDrive\Desktop\nieo\NieoPilot\assets\templates\对战\切换精灵\切换探针\01_154137_a.png"
    img2_path = r"C:\Users\dayuz\OneDrive\Desktop\nieo\NieoPilot\assets\templates\对战\切换精灵\切换探针\01_154155_b.png"
    
    import sys
    if sys.platform == 'win32':
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    
    print("测量切换探针图片的平均RGB值...")
    print()
    
    # 测量第一张图片（艾斯菲格-深蓝）
    rgb1 = measure_image_rgb(img1_path)
    if rgb1:
        print(f"01_154137_a.png (艾斯菲格-深蓝):")
        print(f"   R={rgb1[0]}, G={rgb1[1]}, B={rgb1[2]}")
        print(f"   十六进制: #{rgb1[0]:02X}{rgb1[1]:02X}{rgb1[2]:02X}")
        print()
    
    # 测量第二张图片（闪光艾菲亚-黄混深蓝）
    rgb2 = measure_image_rgb(img2_path)
    if rgb2:
        print(f"01_154155_b.png (闪光艾菲亚-黄混深蓝):")
        print(f"   R={rgb2[0]}, G={rgb2[1]}, B={rgb2[2]}")
        print(f"   十六进制: #{rgb2[0]:02X}{rgb2[1]:02X}{rgb2[2]:02X}")
        print()
    
    if rgb1 and rgb2:
        print("代码中使用:")
        print(f"   AISIFEIGE_PROBE_RGB = {rgb1}  # 艾斯菲格-深蓝")
        print(f"   FLASH_AIFEIA_PROBE_RGB = {rgb2}  # 闪光艾菲亚-黄混深蓝")

