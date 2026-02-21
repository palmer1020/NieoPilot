#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SWF文件批量替换脚本

功能：
1. 从skill/skillset文件夹读取文件名（格式：数字+中文.swf），提取数字部分
2. 根据数字前缀选择源文件：
   - 1开头的数字：使用17903.swf
   - 2开头的数字：使用20660.swf
3. 将fightResource/pet/swf下所有.swf文件复制成1020.swf的内容
"""

import os
import shutil
import re
from pathlib import Path
from typing import List, Tuple


class SWFReplacer:
    def __init__(self, base_path: str):
        """
        初始化替换器
        
        Args:
            base_path: 基础路径 E:\1\nieoasset\resource\fightResource
        """
        self.base_path = Path(base_path)
        
        # Skill相关路径
        self.skill_path = self.base_path / "skill"
        self.skill_source_1 = self.skill_path / "17903.swf"  # 用于1开头的数字
        self.skill_source_2 = self.skill_path / "20660.swf"  # 用于2开头的数字
        self.skill_skillset = self.skill_path / "skillset"
        self.skill_swf = self.skill_path / "swf"
        
        # Pet相关路径
        self.pet_path = self.base_path / "pet"
        self.pet_source = self.pet_path / "1020.swf"
        self.pet_swf = self.pet_path / "swf"
    
    def extract_number_from_skill_filename(self, filename: str) -> str:
        """
        从技能文件名中提取数字部分
        例如：从 "10001撞击.swf" 提取 "10001"
        
        Args:
            filename: 文件名（带或不带路径）
            
        Returns:
            提取的数字字符串，如果提取失败返回None
        """
        # 去掉路径和扩展名
        name = Path(filename).stem
        
        # 使用正则表达式提取开头的数字
        match = re.match(r'^(\d+)', name)
        if match:
            return match.group(1)
        return None
    
    def get_skill_target_files(self) -> List[Tuple[str, str, Path]]:
        """
        获取skill目录下需要替换的文件列表
        
        Returns:
            List[Tuple[源文件名, 目标文件名, 源文件路径]]，例如：[("10001撞击.swf", "10001.swf", Path("17903.swf"))]
        """
        targets = []
        
        if not self.skill_skillset.exists():
            print(f"[WARN] 警告：skillset文件夹不存在: {self.skill_skillset}")
            return targets
        
        # 扫描skillset文件夹
        for file_path in self.skill_skillset.glob("*.swf"):
            filename = file_path.name
            number = self.extract_number_from_skill_filename(filename)
            
            if number:
                target_name = f"{number}.swf"
                # 根据数字前缀选择源文件
                if number.startswith('1'):
                    source_file = self.skill_source_1
                elif number.startswith('2'):
                    source_file = self.skill_source_2
                else:
                    print(f"[WARN] 警告：数字 {number} 既不是1开头也不是2开头，跳过: {filename}")
                    continue
                
                targets.append((filename, target_name, source_file))
            else:
                print(f"[WARN] 警告：无法从文件名提取数字: {filename}")
        
        return targets
    
    def get_pet_target_files(self) -> List[Tuple[str, str]]:
        """
        获取pet/swf目录下需要替换的文件列表
        
        将fightResource/pet/swf/下所有.swf文件替换为1020.swf的内容
        
        Returns:
            List[Tuple[源文件名, 目标文件名]]，例如：[("010.swf", "010.swf")]
        """
        targets = []
        
        if not self.pet_swf.exists():
            print(f"[WARN] 警告：pet/swf文件夹不存在: {self.pet_swf}")
            return targets
        
        # 扫描pet/swf文件夹下的所有swf文件
        for file_path in self.pet_swf.glob("*.swf"):
            filename = file_path.name
            # 源文件统一使用1020.swf，目标为当前文件名
            targets.append((filename, filename))
        
        return targets
    
    def preview_replacements(self) -> dict:
        """
        预览将要执行的操作
        
        Returns:
            包含预览信息的字典
        """
        preview = {
            "skill": {
                "source_1_exists": self.skill_source_1.exists(),
                "source_1_path": str(self.skill_source_1),
                "source_2_exists": self.skill_source_2.exists(),
                "source_2_path": str(self.skill_source_2),
                "swf_dir_exists": self.skill_swf.exists(),
                "targets": []
            },
            "pet": {
                "source_exists": self.pet_source.exists(),
                "source_path": str(self.pet_source),
                "swf_dir_exists": self.pet_swf.exists(),
                "targets": []
            }
        }
        
        # 获取skill目标文件
        skill_targets = self.get_skill_target_files()
        for source_name, target_name, source_file in skill_targets:
            target_path = self.skill_swf / target_name
            preview["skill"]["targets"].append({
                "source_name": source_name,
                "target_name": target_name,
                "target_path": str(target_path),
                "target_exists": target_path.exists(),
                "source_file": source_file,
                "source_file_name": source_file.name
            })
        
        # 获取pet目标文件（fightResource/pet/swf下所有swf，统一用1020.swf覆盖）
        pet_targets = self.get_pet_target_files()
        for source_name, target_name in pet_targets:
            target_path = self.pet_swf / target_name
            preview["pet"]["targets"].append({
                "source_name": "1020.swf",
                "target_name": target_name,
                "target_path": str(target_path),
                "target_exists": target_path.exists()
            })
        
        return preview
    
    def print_preview(self, preview: dict):
        """
        打印预览信息
        """
        print("=" * 80)
        print("[预览] 替换操作预览")
        print("=" * 80)
        
        # Skill部分
        print("\n[Skill] Skill目录替换:")
        print(f"  源文件1 (1开头): {preview['skill']['source_1_path']}")
        print(f"  源文件1存在: {'[OK]' if preview['skill']['source_1_exists'] else '[X]'}")
        print(f"  源文件2 (2开头): {preview['skill']['source_2_path']}")
        print(f"  源文件2存在: {'[OK]' if preview['skill']['source_2_exists'] else '[X]'}")
        print(f"  swf目录存在: {'[OK]' if preview['skill']['swf_dir_exists'] else '[X]'}")
        print(f"  需要替换的文件数量: {len(preview['skill']['targets'])}")
        
        if preview['skill']['targets']:
            print("\n  目标文件列表:")
            for i, target in enumerate(preview['skill']['targets'], 1):
                status = "[OK]" if target['target_exists'] else "[WARN] (目标文件不存在)"
                source_status = "[OK]" if target['source_file'].exists() else "[X] (源文件不存在)"
                print(f"    {i}. {target['target_name']} <- {target['source_name']} (使用: {target['source_file_name']}) {status} {source_status}")
        
        # Pet部分
        print("\n[Pet] Pet目录替换:")
        print(f"  源文件: {preview['pet']['source_path']}")
        print(f"  源文件存在: {'[OK]' if preview['pet']['source_exists'] else '[X]'}")
        print(f"  swf目录存在: {'[OK]' if preview['pet']['swf_dir_exists'] else '[X]'}")
        print(f"  需要替换的文件数量: {len(preview['pet']['targets'])}")
        
        if preview['pet']['targets']:
            print("\n  目标文件列表:")
            for i, target in enumerate(preview['pet']['targets'], 1):
                status = "[OK]" if target['target_exists'] else "[WARN] (目标文件不存在)"
                print(f"    {i}. {target['target_name']} <- {target['source_name']} {status}")
        
        print("\n" + "=" * 80)
    
    def replace_skill_files(self, dry_run: bool = False) -> dict:
        """
        替换skill目录下的文件
        
        Args:
            dry_run: 如果为True，只预览不实际替换
            
        Returns:
            包含操作结果的字典
        """
        results = {
            "success": [],
            "failed": [],
            "skipped": []
        }
        
        if not self.skill_swf.exists():
            print(f"[ERROR] 错误：目标目录不存在: {self.skill_swf}")
            return results
        
        targets = self.get_skill_target_files()
        
        if not targets:
            print("[WARN] 警告：没有找到需要替换的文件")
            return results
        
        print(f"\n[开始] 开始替换skill文件 (共 {len(targets)} 个)...")
        
        for source_name, target_name, source_file in targets:
            target_path = self.skill_swf / target_name
            
            # 检查源文件是否存在
            if not source_file.exists():
                results["skipped"].append({
                    "target": target_name,
                    "reason": f"源文件不存在: {source_file.name}"
                })
                print(f"  [跳过] {target_name} (源文件不存在: {source_file.name})")
                continue
            
            # 检查目标文件是否存在
            if not target_path.exists():
                results["skipped"].append({
                    "target": target_name,
                    "reason": "目标文件不存在"
                })
                print(f"  [跳过] {target_name} (目标文件不存在)")
                continue
            
            if dry_run:
                print(f"  [预览] 将复制 {source_file.name} -> {target_path}")
                results["success"].append(target_name)
            else:
                try:
                    shutil.copy2(source_file, target_path)
                    print(f"  [成功] {target_name} (使用: {source_file.name})")
                    results["success"].append(target_name)
                except Exception as e:
                    print(f"  [失败] {target_name} - {str(e)}")
                    results["failed"].append({
                        "target": target_name,
                        "error": str(e)
                    })
        
        return results
    
    def replace_pet_files(self, dry_run: bool = False) -> dict:
        """
        替换pet目录下的文件
        
        Args:
            dry_run: 如果为True，只预览不实际替换
            
        Returns:
            包含操作结果的字典
        """
        results = {
            "success": [],
            "failed": [],
            "skipped": []
        }
        
        if not self.pet_source.exists():
            print(f"[ERROR] 错误：源文件不存在: {self.pet_source}")
            return results
        
        if not self.pet_swf.exists():
            print(f"[ERROR] 错误：目标目录不存在: {self.pet_swf}")
            return results
        
        targets = self.get_pet_target_files()
        
        if not targets:
            print("[WARN] 警告：没有找到需要替换的文件")
            return results
        
        print(f"\n[开始] 开始替换pet文件 (共 {len(targets)} 个)，全部使用1020.swf的内容...")
        
        for source_name, target_name in targets:
            target_path = self.pet_swf / target_name
            
            # 1020.swf自身无需复制
            if target_name == "1020.swf":
                results["skipped"].append({
                    "target": target_name,
                    "reason": "源文件自身，跳过"
                })
                continue
            
            if not target_path.exists():
                results["skipped"].append({
                    "target": target_name,
                    "reason": "目标文件不存在"
                })
                print(f"  [跳过] {target_name} (目标文件不存在)")
                continue
            
            if dry_run:
                print(f"  [预览] 将复制 {self.pet_source.name} -> {target_path}")
                results["success"].append(target_name)
            else:
                try:
                    shutil.copy2(self.pet_source, target_path)
                    print(f"  [成功] {target_name}")
                    results["success"].append(target_name)
                except Exception as e:
                    print(f"  [失败] {target_name} - {str(e)}")
                    results["failed"].append({
                        "target": target_name,
                        "error": str(e)
                    })
        
        return results
    
    def run(self, dry_run: bool = False):
        """
        执行替换操作
        
        Args:
            dry_run: 如果为True，只预览不实际替换
        """
        print("[工具] SWF文件批量替换工具")
        print("=" * 80)
        
        # 预览
        preview = self.preview_replacements()
        self.print_preview(preview)
        
        if dry_run:
            print("\n[预览模式] 不会实际执行替换操作")
            return
        
        # 确认
        print("\n[警告] 此操作将覆盖目标文件！")
        confirm = input("确认执行替换操作？(yes/no): ").strip().lower()
        
        if confirm not in ['yes', 'y']:
            print("[取消] 操作已取消")
            return
        
        # 执行替换
        print("\n" + "=" * 80)
        skill_results = self.replace_skill_files(dry_run=False)
        pet_results = self.replace_pet_files(dry_run=False)
        
        # 汇总结果
        print("\n" + "=" * 80)
        print("[汇总] 操作结果汇总")
        print("=" * 80)
        
        print(f"\n[Skill] Skill目录:")
        print(f"  成功: {len(skill_results['success'])}")
        print(f"  失败: {len(skill_results['failed'])}")
        print(f"  跳过: {len(skill_results['skipped'])}")
        
        print(f"\n[Pet] Pet目录:")
        print(f"  成功: {len(pet_results['success'])}")
        print(f"  失败: {len(pet_results['failed'])}")
        print(f"  跳过: {len(pet_results['skipped'])}")
        
        total_success = len(skill_results['success']) + len(pet_results['success'])
        total_failed = len(skill_results['failed']) + len(pet_results['failed'])
        total_skipped = len(skill_results['skipped']) + len(pet_results['skipped'])
        
        print(f"\n[总计] 总计:")
        print(f"  成功: {total_success}")
        print(f"  失败: {total_failed}")
        print(f"  跳过: {total_skipped}")
        
        if skill_results['failed'] or pet_results['failed']:
            print("\n[失败] 失败的文件:")
            for item in skill_results['failed']:
                print(f"  Skill: {item['target']} - {item['error']}")
            for item in pet_results['failed']:
                print(f"  Pet: {item['target']} - {item['error']}")
        
        print("\n" + "=" * 80)
        print("[完成] 操作完成！")


def main():
    """主函数"""
    import sys
    import io
    
    # 设置Windows控制台编码为UTF-8
    if sys.platform == 'win32':
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
        except:
            pass
    
    # 基础路径
    base_path = r"E:\1\nieoasset\resource\fightResource"
    
    # 检查路径是否存在
    if not os.path.exists(base_path):
        print(f"[ERROR] 错误：基础路径不存在: {base_path}")
        sys.exit(1)
    
    # 创建替换器
    replacer = SWFReplacer(base_path)
    
    # 检查是否有命令行参数
    dry_run = "--dry-run" in sys.argv or "-n" in sys.argv
    
    # 执行替换
    replacer.run(dry_run=dry_run)


if __name__ == "__main__":
    main()

