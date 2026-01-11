# 统一对战流程框架文档

## 概述

重构后的代码采用统一的对战流程框架，所有对战模式（固定、野外、玩家对战）都遵循相同的4个Stage流程。

## 架构设计

### 核心文件

1. **`core/unified_battle_framework.py`** - 统一对战流程框架核心
2. **`core/fixed_mode_adapter.py`** - 固定模式适配器（训练室、勇者之塔等）

### Stage 流程

#### Stage 1: 触发对战
- **功能**: 根据各种条件判断并点击触发对战
- **实现**: 通过 `BattleConfig.trigger_callback` 回调函数实现
- **返回**: 触发坐标 `(x, y)`

#### Stage 2: PetItem检测 + 校准
- **功能**: 
  1. 检测PetItem信号（`/resource/item/petItem/icon/`）
  2. 检测校准探针（大探针=2FA7EE AND 小探针=FFFFFF）
  3. 如果出现校准探针，执行校准流程：
     - 扫描 `游戏.1a`, `游戏.1b`, `游戏.2a`, `游戏.2b`, `游戏.3a`, `游戏.3b`, `游戏.4a`, `游戏.4b`
     - 计算 X1-X4（每个X值为对应a和b区域中颜色严格为FE6700的数量，取值0/1/2）
     - 分析分布：正常分布为 310, 301, 031, 130, 103, 013（表示1的个数+0的个数+2的个数）
     - 找到值为1的X，点击对应组的a和b区域各自中心的中点
     - 点击后检测：
       - 如果仍然1 AND 1或分布异常（4+0或2+2），暂停并发邮件
       - 如果不是1 AND 1，重新执行Stage 1
  4. 等待PetItem信号（10s超时）

#### Stage 3: 战斗循环
- **功能**:
  1. 检测回合探针（蓝/灰变化）
  2. 根据配置回调获取当前回合应该执行的动作
  3. 执行动作（技能/胶囊/逃跑）
  4. 循环直到检测到战斗结束信号

#### Stage 4: 战斗结束处理
- **功能**: 根据上一回合动作类型处理：
  - **SKILL（技能）**: 
    1. 检测黄色探针（胜利探针）
    2. 点击胜利确认
    3. 训练室模式：升级确认 + 技能替换取消
    4. 检测 通用探针白色 + 普通确认探针蓝色 1 AND 1，出现一个点一次
  - **CAPSULE（胶囊）**: 直接检测 通用探针白色 + 普通确认探针蓝色 1 AND 1
  - **ESCAPE（逃跑）**: 直接检测 通用探针白色 + 普通确认探针蓝色 1 AND 1

## 使用示例

### 固定模式（训练室）

```python
from core.unified_battle_framework import UnifiedBattleFramework, BattleConfig, BattleMode
from core.fixed_mode_adapter import FixedModeAdapter
from core.region_store import RegionStore

# 初始化
framework = UnifiedBattleFramework(bot, regions, template_root)
adapter = FixedModeAdapter(framework)

# 训练室固定次数
adapter.run_training_room(
    use_foreground=False,
    skill_key="对战.使用技能四",
    max_battles=30,
    recover_every=5
)

# 训练室直升100
adapter.run_training_room_until_level(
    target_level=100,
    use_foreground=False,
    skill_key="对战.使用技能四",
    battles_per_batch=30,
    recover_every=5
)

# 勇者之塔
adapter.run_hero_tower(
    times=10,
    use_foreground=False,
    skill_key="对战.使用技能四"
)
```

### 自定义模式

```python
# 自定义Stage 1触发
def my_trigger():
    # 自定义触发逻辑
    return (x, y)

# 自定义Stage 3动作选择
def my_action(round_idx: int) -> str:
    if round_idx == 1:
        return "skill"
    elif round_idx >= 2:
        return "capsule"
    return "skill"

config = BattleConfig(
    mode=BattleMode.WILD,
    use_foreground=False,
    skill_key="对战.使用技能四",
    trigger_callback=my_trigger,
    action_callback=my_action
)

success = framework.run_battle(config)
```

## 配置项说明

### BattleConfig

- `mode`: 对战模式（FIXED/WILD/PVP）
- `use_foreground`: 是否前台运行
- `skill_key`: 默认技能region键
- `trigger_callback`: Stage 1触发回调
- `action_callback`: Stage 3动作选择回调（返回"skill"/"capsule"/"escape"）
- `victory_callback`: Stage 4胜利后回调
- `capture_callback`: Stage 4捕捉后回调
- `escape_callback`: Stage 4逃跑后回调
- `abort_check`: 中止检查函数

## 关键Region键

### 校准相关
- `游戏.大探针` - 大探针区域（期望颜色：2FA7EE）
- `游戏.小探针` - 小探针区域（期望颜色：FFFFFF）
- `游戏.1a`, `游戏.1b`, `游戏.2a`, `游戏.2b`, `游戏.3a`, `游戏.3b`, `游戏.4a`, `游戏.4b` - 校准点击区域

### 战斗相关
- `对战.回合探针` - 回合检测探针
- `对战.使用技能四` - 技能按钮
- `对战.逃跑.切换逃跑面板` - 逃跑面板
- `对战.逃跑.确认逃跑` - 确认逃跑

### 结束处理相关
- `对话框.对战胜利确认` - 胜利确认（黄色探针）
- `对话框.对战胜利确认按钮` - 胜利确认按钮
- `对话框.升级确认` - 升级确认
- `对话框.升级确认按钮` - 升级确认按钮
- `对话框.技能替换取消` - 技能替换取消
- `对话框.技能替换取消按钮` - 技能替换取消按钮
- `对话框.通用探针` - 通用探针（白色）
- `对话框.普通确认探针` - 普通确认探针（蓝色）
- `对话框.普通确认按钮` - 普通确认按钮

## 邮件通知

当校准异常时，会自动发送邮件到 `1713518932qqcom@gmail.com`。

注意：目前邮件功能仅记录日志，实际发送需要配置SMTP服务器。

## 注意事项

1. **校准逻辑**: Stage 2的校准逻辑会在点击后检测探针状态，如果仍然异常会暂停并发送邮件
2. **重新触发**: 校准成功后会自动重新执行Stage 1（重新触发对战）
3. **超时处理**: PetItem检测超时（10s）会跳过本次对战
4. **分布异常**: 如果X值分布不是正常模式（310/301等），会暂停并发送邮件

## 后续工作

- [ ] 实现野外模式适配器
- [ ] 实现玩家对战模式适配器
- [ ] 完善邮件发送功能（SMTP配置）
- [ ] 实现OCR等级检测（训练室直升模式）
- [ ] 实现连接脚本播放功能
- [ ] 集成到现有BotWorker中








