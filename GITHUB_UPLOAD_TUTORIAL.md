# GitHub 上传教程 - NieoPilot

本教程将帮助你将 NieoPilot 项目上传到 GitHub，实现多平台代码同步。

## 📋 前置准备

### 1. 安装 Git（如果还没有）
- 下载地址：https://git-scm.com/download/win
- 安装时选择默认选项即可

### 2. 创建 GitHub 账号
- 访问：https://github.com
- 注册一个新账号（如果还没有）

### 3. 配置 Git（首次使用需要）
打开 PowerShell 或 CMD，执行以下命令：

```bash
git config --global user.name "你的GitHub用户名"
git config --global user.email "你的GitHub邮箱"
```

## 🚀 上传步骤

### 步骤 1：在 GitHub 上创建新仓库

1. 登录 GitHub
2. 点击右上角的 **"+"** → **"New repository"**
3. 填写仓库信息：
   - **Repository name**: `NieoPilot`（或你喜欢的名字）
   - **Description**: `Nieo 游戏自动化脚本`
   - **Visibility**: 选择 **Private**（私有）或 **Public**（公开）
   - **不要**勾选 "Initialize this repository with a README"（我们已经有了代码）
4. 点击 **"Create repository"**

### 步骤 2：在本地初始化 Git 仓库

在项目根目录（`C:\Users\dayuz\OneDrive\Desktop\nieo\NieoPilot`）打开 PowerShell，执行：

```powershell
# 初始化 Git 仓库
git init

# 添加所有文件到暂存区
git add .

# 创建第一次提交
git commit -m "Initial commit: NieoPilot project"
```

### 步骤 3：连接到 GitHub 远程仓库

在 GitHub 上创建仓库后，你会看到一个页面，上面有仓库的 URL，类似：
- HTTPS: `https://github.com/你的用户名/NieoPilot.git`
- SSH: `git@github.com:你的用户名/NieoPilot.git`

**推荐使用 HTTPS**（更简单），执行：

```powershell
# 添加远程仓库（将 YOUR_USERNAME 替换为你的 GitHub 用户名）
git remote add origin https://github.com/YOUR_USERNAME/NieoPilot.git

# 查看远程仓库（确认添加成功）
git remote -v
```

### 步骤 4：推送代码到 GitHub

```powershell
# 推送代码到 GitHub（main 分支）
git branch -M main
git push -u origin main
```

**注意**：第一次推送时，GitHub 会要求你输入用户名和密码：
- **用户名**：你的 GitHub 用户名
- **密码**：需要使用 **Personal Access Token**（不是 GitHub 密码）

### 步骤 5：创建 Personal Access Token（如果需要）

如果 GitHub 要求使用 Token：

1. 访问：https://github.com/settings/tokens
2. 点击 **"Generate new token"** → **"Generate new token (classic)"**
3. 填写信息：
   - **Note**: `NieoPilot Upload`
   - **Expiration**: 选择过期时间（建议 90 天或 No expiration）
   - **Scopes**: 勾选 `repo`（完整仓库访问权限）
4. 点击 **"Generate token"**
5. **复制生成的 token**（只显示一次，务必保存）
6. 在推送时，密码处粘贴这个 token

## 📝 日常更新代码流程

### 在新电脑上克隆仓库

```powershell
# 克隆仓库到本地
git clone https://github.com/YOUR_USERNAME/NieoPilot.git
cd NieoPilot

# 创建虚拟环境并安装依赖
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
pip install Pillow pytesseract
```

### 修改代码后上传

```powershell
# 查看修改的文件
git status

# 添加修改的文件
git add .

# 提交修改（写清楚修改内容）
git commit -m "修复 next_move_at 未初始化问题"

# 推送到 GitHub
git push
```

### 从 GitHub 拉取最新代码

```powershell
# 拉取最新代码
git pull
```

## 🔄 多平台同步工作流程

### 场景 1：在电脑 A 修改代码

```powershell
# 1. 修改代码
# 2. 提交并推送
git add .
git commit -m "描述你的修改"
git push
```

### 场景 2：在电脑 B 获取最新代码

```powershell
# 1. 拉取最新代码
git pull

# 2. 如果有冲突，解决冲突后：
git add .
git commit -m "解决冲突"
git push
```

## ⚠️ 注意事项

1. **不要上传敏感信息**：
   - `config.py` 中的游戏路径是个人配置，可以考虑添加到 `.gitignore`
   - 如果包含敏感信息，使用环境变量或配置文件模板

2. **提交前检查**：
   ```powershell
   git status  # 查看要提交的文件
   git diff    # 查看具体修改内容
   ```

3. **提交信息要清晰**：
   - 好的提交信息：`"修复尼奥模式校准abort逻辑"`
   - 不好的提交信息：`"修改"` 或 `"更新"`

4. **定期推送**：
   - 建议每次完成一个功能或修复后立即推送
   - 避免长时间不推送导致代码丢失

## 🛠️ 常用 Git 命令

```powershell
# 查看状态
git status

# 查看修改内容
git diff

# 查看提交历史
git log

# 撤销未提交的修改
git checkout -- 文件名

# 创建新分支
git checkout -b 分支名

# 切换分支
git checkout 分支名

# 查看所有分支
git branch
```

## 📚 更多资源

- Git 官方文档：https://git-scm.com/doc
- GitHub 帮助：https://docs.github.com
- Git 可视化学习：https://learngitbranching.js.org

---

**完成！** 现在你的代码已经上传到 GitHub，可以在任何地方通过 `git clone` 和 `git pull` 来同步代码了。

