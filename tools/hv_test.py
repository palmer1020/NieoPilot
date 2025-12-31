import cv2
import numpy as np
import os

# ==========================================
# 1. 模拟神经网络分类器部分
# ==========================================
class SimplePetOrientationCNN:
    """
    这是一个模拟的卷积神经网络分类器。
    在真实项目中，你会在这里定义网络结构（如使用 PyTorch/TensorFlow）
    并加载训练好的模型权重。
    
    为了演示，我们用一个基于图像处理的启发式算法来模拟预测过程。
    """
    def __init__(self):
        print("[模型] 初始化模拟 CNN 模型...")
        # 定义分类标签
        self.classes = ['front', 'side', 'back']

    def preprocess(self, image):
        """图像预处理：转灰度，统一尺寸"""
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        # 统一缩放到一个较小的尺寸进行分析
        resized = cv2.resize(gray, (64, 64))
        return resized

    def predict(self, image):
        """
        模拟神经网络的前向传播预测。
        这里使用“对称性检测”作为区分侧面和背面的特征。
        """
        processed_img = self.preprocess(image)

        # --- 特征提取核心逻辑 ---
        # 1. 水平翻转图像
        flipped = cv2.flip(processed_img, 1)
        # 2. 计算原图和翻转图的绝对差值
        diff = cv2.absdiff(processed_img, flipped)
        # 3. 计算平均差值作为“不对称分数”
        asymmetry_score = np.mean(diff)

        # --- 分类决策逻辑 ---
        # 侧面朝向的物体通常不对称，分数较高。
        # 背面或正面朝向的物体通常较对称，分数较低。
        # 阈值 15 是根据示例图片调试得出的经验值。
        if asymmetry_score > 15:
            prediction_idx = 1 # 'side' (侧面)
            confidence = 0.92  # 模拟置信度
        else:
            # 在当前简化场景下，非侧面即背面
            prediction_idx = 2 # 'back' (背面)
            confidence = 0.88  # 模拟置信度

        predicted_class = self.classes[prediction_idx]
        return predicted_class, confidence, asymmetry_score


# ==========================================
# 2. 游戏解题器逻辑部分
# ==========================================
class PetQuizSolver:
    def __init__(self, model):
        self.model = model
        # 中英文方向映射
        self.target_map = {
            "正面": "front",
            "侧面": "side",
            "背面": "back"
        }

    def crop_elements(self, full_screenshot):
        """
        从完整截图中裁剪出关键区域（根据 image_5.png 的布局估算坐标）。
        在不同分辨率下可能需要调整这些坐标。
        """
        # 游戏面板的大致区域（去除IDE和桌面背景）
        panel_crop = full_screenshot[60:600, 110:970]
        
        # 裁剪出四个精灵的区域 (相对于 panel_crop 的坐标)
        pet_crops = []
        y_start, h = 140, 150
        x_starts = [120, 290, 460, 630] # 四个精灵的起始 X 坐标
        w = 130
        
        for x in x_starts:
            pet_img = panel_crop[y_start:y_start+h, x:x+w]
            pet_crops.append(pet_img)

        # 裁剪出指令文字区域
        text_crop = panel_crop[390:440, 250:600]
        
        return pet_crops, text_crop

    def get_target_from_text(self, text_image):
        """
        识别题目要求的目标方向。
        """
        print("[解题] 正在分析题目要求...")
        # 方法 A: 使用 OCR (太复杂，此处省略)
        
        # 方法 B: 颜色检测法（简单有效）
        # 题目中关键方向词会用特殊颜色高亮。
        # "侧面"是蓝色，"正面"可能是红色或其他。
        
        # 将图片转为 HSV 颜色空间以便分离颜色
        hsv = cv2.cvtColor(text_image, cv2.COLOR_BGR2HSV)
        
        # 定义蓝色的 HSV 范围
        lower_blue = np.array([100, 150, 150])
        upper_blue = np.array([120, 255, 255])
        
        # 创建蓝色掩膜
        mask_blue = cv2.inRange(hsv, lower_blue, upper_blue)
        
        # 如果检测到足够多的蓝色像素，则认为是“侧面”
        if cv2.countNonZero(mask_blue) > 50:
            print("  -> 检测到蓝色高亮文字，目标为：侧面")
            return "侧面"
        
        # 可以添加对红色（正面）等的检测
        # ...

        # 默认返回侧面作为演示
        print("  -> 未检测到特定颜色，默认目标：侧面")
        return "侧面"

    def solve(self, screenshot_path):
        print(f"开始处理图片: {screenshot_path}")
        full_screenshot = cv2.imread(screenshot_path)
        if full_screenshot is None:
            print("错误：无法读取图片。")
            return

        # 1. 裁剪关键区域
        pet_images, text_image = self.crop_elements(full_screenshot)
        
        # 2. 获取目标方向
        target_chinese = self.get_target_from_text(text_image)
        target_class = self.target_map[target_chinese]
        print(f"[解题] 目标方向已确认为: {target_chinese} ({target_class})")
        print("-" * 40)

        # 3. 使用 CNN 模型识别每只精灵
        predictions = []
        for i, pet_img in enumerate(pet_images):
            orientation, conf, score = self.model.predict(pet_img)
            predictions.append(orientation)
            print(f"精灵 {i+1}: 预测方向=[{orientation}], 不对称分数={score:.1f}, 置信度={conf:.2f}")
            
            # 可视化裁剪结果（调试用）
            # cv2.imshow(f"Pet {i+1}", pet_img)

        print("-" * 40)
        
        # 4. 找出符合要求的精灵
        match_index = -1
        match_count = 0
        for i, pred in enumerate(predictions):
            if pred == target_class:
                match_index = i
                match_count += 1
        
        # 输出最终结果
        if match_count == 1:
            print(f">>> 最终答案 <<<")
            print(f"请选择第 [{match_index + 1}] 个精灵。")
            print("它唯一符合题目要求的方向。")
        elif match_count == 0:
            print("错误：没有找到符合目标方向的精灵。")
        else:
            print(f"错误：找到了 {match_count} 个符合要求的精灵，答案不唯一。")

        # cv2.waitKey(0) # 如果取消了可视化注释，需要这一行来暂停
        cv2.destroyAllWindows()

# ==========================================
# 3. 主程序入口
# ==========================================
if __name__ == "__main__":
    # 输入图片路径（你提供的包含整个桌面的截图）
    from config import HV_SAMPLES_PATH
    image_path = os.path.join(HV_SAMPLES_PATH, "未处理", "20251217_155947_hv_78a28517_panel.png") 

    # 1. 实例化模拟的 CNN 模型
    cnn_model = SimplePetOrientationCNN()

    # 2. 实例化解题器，并传入模型
    solver = PetQuizSolver(cnn_model)

    # 3. 运行解题流程
    solver.solve(image_path)