#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将工程模板同步到微端 NieoData/resource（等价于 Dashboard 四个 SWF 按钮一次执行）：

  PetStorage / pet 254 / fight pet / fight skill

项目根目录：  python swf/sync_project_templates.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> int:
    if sys.platform == "win32":
        try:
            import io

            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
        except Exception:
            pass

    from core.swf_resource_ops import sync_all_four

    ok, msg = sync_all_four()
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
