# -*- coding: utf-8 -*-
"""若不存在 config.py，则从 config.template.py 复制一份（供首次克隆或新环境使用）。"""
from __future__ import annotations

import os
import shutil
import sys

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def ensure_config_py(project_root: str | None = None) -> None:
    root = project_root or _PROJECT_ROOT
    cfg = os.path.join(root, "config.py")
    tpl = os.path.join(root, "config.template.py")
    if os.path.isfile(cfg):
        return
    if not os.path.isfile(tpl):
        print(
            "[NieoPilot] 缺少 config.py，且未找到 config.template.py。",
            file=sys.stderr,
        )
        sys.exit(1)
    shutil.copyfile(tpl, cfg)
    print(
        "[NieoPilot] 已从 config.template.py 生成 config.py，请按本机路径修改后如有需要再运行。",
        file=sys.stderr,
    )
