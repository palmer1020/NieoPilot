import os

# =================配置区域=================
# 扫描的起始路径 ('.' 代表当前目录)
START_PATH = '.'

# 输出文件名
OUTPUT_FILE = 'project_context.txt'

# 需要包含的文件后缀
INCLUDE_EXTENSIONS1 = {'.py', '.json', '.md', '.txt'}
INCLUDE_EXTENSIONS = {'.py'}

# 需要忽略的文件夹 (非常重要，防止扫描 venv 和 git)
IGNORE_DIRS = {
    'venv', '__pycache__', '.git', '.idea', '.vscode', 
    'assets', 'images', 'build', 'dist', 'node_modules','fix_script','log','_restore_backup'
}

# 需要忽略的具体文件名
IGNORE_FILES = {
    'merge_project.py',  # 不合并自己
    'project_context.txt', # 不合并输出文件
    'package-lock.json',
    '.DS_Store'
}
# =========================================

def merge_files():
    # 确保输出文件是空的或者新建的
    try:
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as outfile:
            outfile.write(f"=== NIEO PROJECT CONTEXT ===\n")
            outfile.write(f"Generated at: {os.getcwd()}\n\n")

            total_files = 0

            # os.walk 递归遍历所有子目录
            for root, dirs, files in os.walk(START_PATH):
                # 1. 过滤掉忽略的目录 (修改 dirs 列表会影响 os.walk 的后续遍历)
                dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]

                for file in files:
                    # 获取文件后缀
                    _, ext = os.path.splitext(file)
                    
                    # 2. 检查是否是需要合并的文件类型
                    if ext.lower() in INCLUDE_EXTENSIONS and file not in IGNORE_FILES:
                        file_path = os.path.join(root, file)
                        rel_path = os.path.relpath(file_path, START_PATH) # 获取相对路径

                        print(f"正在合并: {rel_path}")

                        try:
                            # 写入分隔符和文件名
                            outfile.write(f"\n{'='*60}\n")
                            outfile.write(f"FILE: {rel_path}\n")
                            outfile.write(f"{'='*60}\n")

                            # 读取原文件内容并写入
                            with open(file_path, 'r', encoding='utf-8') as infile:
                                content = infile.read()
                                outfile.write(content)
                                outfile.write("\n") # 文件末尾加个换行
                            
                            total_files += 1

                        except Exception as e:
                            print(f"❌ 读取文件失败 {rel_path}: {e}")
                            outfile.write(f"\n[ERROR READING FILE: {e}]\n")

        print(f"\n✅ 合并完成！")
        print(f"共合并了 {total_files} 个文件。")
        print(f"结果已保存为: {os.path.abspath(OUTPUT_FILE)}")
        print("请将该文件发送给 AI。")

    except Exception as e:
        print(f"❌ 脚本运行出错: {e}")

if __name__ == '__main__':
    merge_files()

