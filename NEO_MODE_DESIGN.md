# 尼奥模式（Neo Mode）设计方案

## 一、需求概述

### 1.1 核心目标
- **专门捕捉尼尔家族**：77（尼尔）、310（闪光尼尔）、416（尼奥）
- **两个地图来回切换**：地图一 ↔ 地图二
- **双重目标**：
  - 优先捕捉稀有精灵（橙色毛球）
  - 对战普通精灵（紫色毛球）寻找尼尔家族变身

### 1.2 精灵识别规则
- **普通精灵**：紫色毛球（可能变身尼尔家族）
- **稀有精灵**：橙色毛球（优先对战）
- **尼尔家族**：77、310、416（通过战斗中的 `/resource/fightResource/pet/swf/` 检测）

## 二、架构设计

### 2.1 新增数据结构

#### 2.1.1 NeoModeProfile
```python
@dataclass(frozen=True)
class NeoModeProfile:
    """尼奥模式配置"""
    name: str = "尼奥模式"
    map1_swf_id: int  # 地图一ID
    map2_swf_id: int  # 地图二ID
    route_hint_map1: str = "尼奥一"  # 地图一路线提示
    route_hint_map2: str = "尼奥二"  # 地图二路线提示
    
    # 目标精灵
    target_rare_mp3_ids: Tuple[int, ...]  # 稀有精灵mp3 ID列表
    target_rare_pet_ids: Tuple[int, ...]  # 稀有精灵pet ID列表
    target_nie_family_ids: Tuple[int, ...] = (77, 310, 416)  # 尼尔家族ID
    
    # 扫描参数
    scan_step_interval_sec: float = 0.25
    ab_cooldown_sec: float = 40.0
    mp3_trigger_window_sec: float = 2.0
    
    # 颜色检测
    orange_color: Tuple[int, int, int] = (254, 103, 0)  # FE6700 - 橙色（稀有精灵）
    purple_color: Tuple[int, int, int] = (?, ?, ?)  # 待确认 - 紫色（普通精灵）
    color_tolerance: int = 15
```

#### 2.1.2 NeoModeState
```python
@dataclass
class NeoModeState:
    """尼奥模式运行时状态"""
    current_map_id: int  # 当前所在地图ID（1或2）
    current_map_swf_id: int  # 当前地图的swf ID
    current_route_points: List[Tuple[str, Region]]  # 当前地图的9个刷新点
    current_reg_a: Region  # 当前地图的A点
    current_reg_b: Region  # 当前地图的B点
    current_map_entrances: List[Region]  # 当前地图的出口（切换到另一地图的入口）
    last_anchor: str  # 'A' or 'B'
    current_pos: Optional[Tuple[float, float]]  # 当前位置
```

### 2.2 核心方法设计

#### 2.2.1 `run_neo_mode()` - 主循环
```
1. 初始化状态（默认从地图一开始）
2. 进入当前地图（执行地图进入脚本）
3. 等待地图就绪（检测map+npc）
4. 解析当前地图的路线区域（9个点、A、B、出口）
5. 点击A点，执行初始稳态标定
6. 进入主循环：
   - AB移动
   - 扫描检测（优先稀有精灵，其次普通精灵）
   - 触发对战
   - 战斗处理（检测尼尔家族 / 逃跑 / 捕捉）
   - 地图切换（如果需要）
```

#### 2.2.2 `_scan_for_pets()` - 扫描精灵
```
扫描逻辑：
1. 扫描当前地图的9个刷新点
2. 检测每个点的颜色：
   - 橙色（FE6700）：稀有精灵
   - 紫色（待确认RGB）：普通精灵
3. 返回结果：
   - 优先返回橙色精灵（如果存在）
   - 如果没有橙色，返回最近的紫色精灵
   - 如果没有精灵，返回None
```

#### 2.2.3 `_handle_battle_neo_mode()` - 战斗处理
```
战斗逻辑：
1. 进入战斗后，检测 `/resource/fightResource/pet/swf/` 获取所有精灵ID
2. 判断是否有尼尔家族（77/310/416）：
   - 如果有尼尔家族：
     * 执行正常的尼尔家族捕捉逻辑（技能一 → 切换精灵 → 高级胶囊）
     * 捕捉成功后：放回仓库 + 恢复 + 切换到下一个地图
   - 如果没有尼尔家族：
     * 执行逃跑逻辑：
       a. 双击"对战.逃跑.切换逃跑面板"
       b. 等待0.3s
       c. 双击"对战.逃跑.确认逃跑"
       d. 等待战斗结束（Map+NPC）
       e. 使用1AND1确认残留对话框
     * 逃跑成功后：切换到下一个地图
```

#### 2.2.4 `_switch_map()` - 地图切换
```
切换逻辑：
1. 确定目标地图（当前地图是1则切换到2，反之亦然）
2. 获取当前地图的出口列表：
   - 地图一 → 地图二：只有一个出口
   - 地图二 → 地图一：有三个出口，选择最近的
3. 点击最近的出口
4. 等待进入目标地图（检测map+npc）
5. 更新NeoModeState状态
6. 重新解析路线区域
7. 点击A点，执行稳态标定
```

#### 2.2.5 `_find_nearest_entrance()` - 找最近出口
```
1. 获取当前位置（current_pos）
2. 计算当前位置到每个出口的距离
3. 返回距离最近的出口Region
```

## 三、实现细节

### 3.1 区域文件结构
```
assets/regions/
├── 尼奥一/
│   ├── 1.json ... 9.json  (9个刷新点)
│   ├── A.json
│   ├── B.json
│   └── 切换.json  (切换到地图二的出口)
└── 尼奥二/
    ├── 1.json ... 9.json  (9个刷新点)
    ├── A.json
    ├── B.json
    ├── 切换一.json  (切换到地图一的出口1)
    ├── 切换二.json  (切换到地图一的出口2)
    └── 切换三.json  (切换到地图一的出口3)
```

### 3.2 颜色检测实现
```python
def _detect_pet_color(self, region: Region) -> Optional[str]:
    """
    检测指定区域的精灵颜色
    Returns: 'orange' (稀有), 'purple' (普通), None (无精灵)
    """
    # 截取区域图像
    img = self._grab_region(region)
    
    # 转换为numpy数组
    arr = np.array(img)
    
    # 检测橙色（FE6700，容差15）
    orange_mask = self._color_match(arr, (254, 103, 0), tolerance=15)
    if np.sum(orange_mask) > threshold:  # threshold待测试
        return 'orange'
    
    # 检测紫色（RGB待确认）
    purple_mask = self._color_match(arr, purple_rgb, tolerance=15)
    if np.sum(purple_mask) > threshold:
        return 'purple'
    
    return None
```

### 3.3 扫描优先逻辑
```python
def _scan_for_pets(self, route_points, current_pos) -> Optional[Tuple[str, Region, str]]:
    """
    扫描9个点，优先返回稀有精灵，其次返回最近的普通精灵
    Returns: (point_key, region, color_type) or None
    """
    orange_hits = []  # 稀有精灵
    purple_hits = []  # 普通精灵
    
    for point_key, region in route_points:
        color = self._detect_pet_color(region)
        if color == 'orange':
            orange_hits.append((point_key, region))
        elif color == 'purple':
            purple_hits.append((point_key, region))
    
    # 优先返回橙色
    if orange_hits:
        # 选择最近的橙色精灵
        best = min(orange_hits, key=lambda x: self._dist_to_pos(self._region_center(x[1]), current_pos))
        return (best[0], best[1], 'orange')
    
    # 如果没有橙色，返回最近的紫色
    if purple_hits:
        best = min(purple_hits, key=lambda x: self._dist_to_pos(self._region_center(x[1]), current_pos))
        return (best[0], best[1], 'purple')
    
    return None
```

### 3.4 战斗流程整合
```python
def _handle_battle_neo_mode(self, ...):
    """
    尼奥模式的战斗处理
    """
    # 1. 执行战斗（使用统一框架）
    battle_result = self._do_battle_capture(...)
    
    # 2. 战斗结束后，检测是否有尼尔家族
    pet_ids = self._collect_fight_pet_ids(...)
    has_nie_family = any(pid in (77, 310, 416) for pid in pet_ids)
    
    if has_nie_family:
        # 有尼尔家族：使用正常的捕捉逻辑（已在_do_battle_capture中处理）
        if battle_result == "captured":
            # 捕捉成功：恢复 + 切换地图
            self._recover_pets(...)
            self._switch_map(...)
        else:
            # 未捕捉成功：直接切换地图
            self._switch_map(...)
    else:
        # 没有尼尔家族：执行逃跑
        self._execute_escape(...)
        # 逃跑后：切换地图
        self._switch_map(...)
```

### 3.5 逃跑逻辑实现
```python
def _execute_escape(self, use_foreground, stop_event):
    """
    执行逃跑逻辑：切换逃跑面板 + 确认逃跑 + 1AND1确认
    """
    # 1. 双击切换逃跑面板
    escape_panel_key = "对战.逃跑.切换逃跑面板"
    self._click_region_twice(escape_panel_key, use_foreground)
    self._sleep_abortable(stop_event, 0.3)
    
    # 2. 双击确认逃跑
    escape_confirm_key = "对战.逃跑.确认逃跑"
    self._click_region_twice(escape_confirm_key, use_foreground)
    
    # 3. 等待战斗结束（Map+NPC）
    # 使用统一框架的_check_battle_end逻辑
    
    # 4. 1AND1确认残留对话框
    # 使用统一框架的_wait_for_confirm_probes逻辑
```

## 四、与现有代码的集成

### 4.1 复用现有功能
- ✅ `_recalibrate_all()` - 稳态标定
- ✅ `_execute_map_entry_script()` - 地图进入脚本
- ✅ `_wait_for_map_ready()` - 等待地图就绪
- ✅ `_do_battle_capture()` - 战斗捕捉（需要适配）
- ✅ `_recover_pets()` - 恢复流程
- ✅ `_check_fight_pet_pattern()` - 检测战斗精灵模式
- ✅ `_switch_pet_for_nie_family()` - 尼尔家族切换精灵
- ✅ 统一框架的逃跑逻辑（`_execute_action("escape")`）

### 4.2 需要新增的功能
- ❌ `_scan_for_pets()` - 颜色扫描（橙色/紫色）
- ❌ `_detect_pet_color()` - 单点颜色检测
- ❌ `_find_nearest_entrance()` - 找最近出口
- ❌ `_switch_map()` - 地图切换逻辑
- ❌ `_execute_escape()` - 逃跑流程（可能可以复用统一框架）
- ❌ `NeoModeProfile` - 配置类
- ❌ `NeoModeState` - 状态管理

### 4.3 Dashboard集成
```python
# gui/dashboard.py
# 在"野外捕捉"组中添加：
- QCheckBox: "尼奥模式"
- QComboBox: 选择稀有精灵目标（如果需要）
```

## 五、关键问题待确认

1. **紫色RGB值**：需要用户提供紫色的RGB值用于颜色检测
2. **地图ID**：需要用户提供地图一和地图二的map_swf_id
3. **颜色检测阈值**：需要测试确定橙色/紫色检测的像素数量阈值
4. **稀有精灵目标**：是否需要配置特定的稀有精灵mp3/pet ID，还是所有橙色都算？
5. **逃跑后的等待时间**：逃跑成功后是否需要等待一段时间再切换地图？
6. **稳态标定**：地图切换后是否需要在切换后立即标定，还是等回到A点后再标定？

## 六、实现步骤建议

### Phase 1: 基础框架
1. 创建 `NeoModeProfile` 和 `NeoModeState`
2. 实现 `_resolve_neo_route_regions()` - 解析尼奥模式路线
3. 实现 `_find_nearest_entrance()` - 找最近出口
4. 实现 `_switch_map()` - 地图切换

### Phase 2: 扫描逻辑
5. 实现 `_detect_pet_color()` - 颜色检测
6. 实现 `_scan_for_pets()` - 扫描优先逻辑
7. 集成到主循环

### Phase 3: 战斗逻辑
8. 实现 `_execute_escape()` - 逃跑流程
9. 实现 `_handle_battle_neo_mode()` - 战斗处理
10. 集成尼尔家族检测和切换逻辑

### Phase 4: 测试和优化
11. Dashboard集成
12. 测试两个地图的切换
13. 测试颜色检测准确性
14. 测试战斗流程（有/无尼尔家族）
15. 优化等待时间和参数

## 七、注意事项

1. **状态管理**：需要仔细管理 `NeoModeState`，确保地图切换时状态正确更新
2. **错误处理**：地图切换失败、战斗异常等情况需要妥善处理
3. **性能优化**：颜色检测可能比较耗时，需要考虑扫描频率
4. **向后兼容**：确保新增代码不影响现有的野外捕捉模式






