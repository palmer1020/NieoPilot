# 统一对战框架集成总结

## 已完成的工作

### 1. 核心框架
- ✅ `core/unified_battle_framework.py` - 统一对战流程框架
  - Stage 1: 触发对战（可扩展回调）
  - Stage 2: PetItem检测 + 校准逻辑（大探针小探针 + X1-X4扫描）
  - Stage 3: 战斗循环（回合检测 + 出招）
  - Stage 4: 战斗结束处理（胜利/捕捉/逃跑）

### 2. 固定模式适配器
- ✅ `core/fixed_mode_adapter.py` - 固定模式适配器
  - 训练室固定次数模式
  - 训练室直升100模式
  - 勇者之塔模式

### 3. 集成到现有代码
- ✅ `core/training_level_runner.py` - 已集成统一框架（可通过 `use_unified_framework` 参数切换）
- ✅ `core/daily_runner.py` - 已集成统一框架到 `run_hero_tower` 方法

## 使用方法

### 启用统一框架

#### 训练室模式
```python
# 默认启用统一框架
runner = TrainingLevelRunner(bot, regions, template_root, use_unified_framework=True)

# 或者显式禁用，使用旧实现
runner = TrainingLevelRunner(bot, regions, template_root, use_unified_framework=False)
```

#### 勇者之塔
```python
# DailyRunner 的 run_hero_tower 默认启用统一框架
daily_runner.run_hero_tower(times=10, background_mode=False, use_unified_framework=True)
```

## 配置说明

### 统一框架特性
1. **自动校准**: Stage 2会自动检测并执行屏幕校准
2. **异常处理**: 校准异常时会自动暂停并发送邮件
3. **灵活配置**: 通过BattleConfig可以自定义各Stage行为
4. **向后兼容**: 保留了旧实现作为备用

### 关键参数
- `use_unified_framework`: 是否使用统一框架（默认True）
- `use_foreground`: 是否前台运行
- `skill_key`: 技能按钮region键

## 注意事项

1. **OCR回调**: 在统一框架中，OCR等级检测通过 `action_callback` 在第一回合触发
2. **异常回退**: 如果统一框架执行失败，会自动回退到旧实现
3. **邮件通知**: 校准异常时会记录日志（实际发送需要配置SMTP）

## 后续工作

- [ ] 测试实际运行，确保所有功能正常
- [ ] 完善野外模式适配器
- [ ] 配置SMTP服务器以启用邮件发送
- [ ] 优化性能（如有必要）
- [ ] 添加更多日志和调试信息

## 文件变更列表

### 新增文件
- `core/unified_battle_framework.py` - 统一对战流程框架
- `core/fixed_mode_adapter.py` - 固定模式适配器
- `UNIFIED_BATTLE_FRAMEWORK.md` - 框架文档
- `INTEGRATION_SUMMARY.md` - 本文件

### 修改文件
- `core/training_level_runner.py` - 集成统一框架
- `core/daily_runner.py` - 集成统一框架到勇者之塔







