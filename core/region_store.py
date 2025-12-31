# core/region_store.py
from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.logger import logger

REGION_ROOT_NAME = os.path.join("assets", "regions")


def _norm_key(key: str) -> str:
    key = (key or "").strip()
    key = key.replace("\\", ".").replace("/", ".")
    while ".." in key:
        key = key.replace("..", ".")
    return key.strip(".")


@dataclass(frozen=True)
class Region:
    key: str
    points: List[Tuple[float, float]]
    click: Dict

    def outer_bbox(self) -> Tuple[float, float, float, float]:
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)

        # 关键：bbox 至少 2x2（你很多是 2px 方块，不要让 grab 返回 None）
        if (x2 - x1) < 2:
            x2 = x1 + 2
        if (y2 - y1) < 2:
            y2 = y1 + 2
        return x1, y1, x2, y2

    def inner_bbox(self) -> Tuple[float, float, float, float]:
        # 你要求：内接长方形千万不要消减
        return self.outer_bbox()

    def sample_click_point(self) -> Tuple[float, float]:
        x1, y1, x2, y2 = self.inner_bbox()

        # random 点击：默认 true/false 都兼容
        random_on = bool(self.click.get("random", True))
        if not random_on:
            return (x1 + x2) / 2, (y1 + y2) / 2

        w = max(1.0, x2 - x1)
        h = max(1.0, y2 - y1)

        # 非常小的区域：直接点中心最稳
        if w <= 2.0 and h <= 2.0:
            return (x1 + x2) / 2, (y1 + y2) / 2

        # 否则在 bbox 内均匀随机
        gx = random.uniform(x1, x2)
        gy = random.uniform(y1, y2)
        return gx, gy


class RegionStore:
    """
    读取 assets/regions 下所有 json
    - 自动把路径映射成 key：对战/逃跑/切换逃跑面板.json -> 对战.逃跑.切换逃跑面板
    - 同时兼容 json 内写了 key 的情况
    """

    def __init__(self, project_root: Optional[str] = None, region_root: Optional[str] = None):
        if region_root:
            self.root = os.path.abspath(region_root)
        else:
            if not project_root:
                raise ValueError("RegionStore 需要 project_root 或 region_root")
            self.root = os.path.abspath(os.path.join(project_root, REGION_ROOT_NAME))

        self._regions: Dict[str, Region] = {}
        self._loaded = False
        self.load_all()

    # -------------------------
    # public api
    # -------------------------
    def load_all(self) -> None:
        self._regions.clear()
        root = Path(self.root)
        if not root.exists():
            logger.warning(f"[RegionStore] 区域根目录不存在: {self.root}")
            self._loaded = True
            return

        files = list(root.rglob("*.json"))
        for fp in files:
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[RegionStore] 读取失败: {fp} ({e})")
                continue

            points = data.get("points") or []
            if not isinstance(points, list) or len(points) == 0:
                continue

            pts: List[Tuple[float, float]] = []
            for p in points:
                try:
                    pts.append((float(p[0]), float(p[1])))
                except Exception:
                    pass
            if not pts:
                continue

            click = data.get("click") or {"random": True}

            # 1) 以路径构造 key
            rel = fp.relative_to(root)
            parts = list(rel.parts)
            name = Path(parts[-1]).stem
            folders = parts[:-1]
            path_key = _norm_key(".".join([*folders, name]))  # 对战.逃跑.切换逃跑面板

            # 2) json 内部 key（如果有）
            json_key = _norm_key(str(data.get("key", ""))) if data.get("key") else ""

            # 建 Region
            region = Region(key=path_key, points=pts, click=click)

            # 存主 key
            self._regions[path_key] = region

            # 存 json_key alias
            if json_key:
                self._regions.setdefault(json_key, region)

            # 额外 alias：如果 folders>=1，给 “一级分类.文件名”
            if len(folders) >= 1:
                alias = _norm_key(f"{folders[0]}.{name}")
                self._regions.setdefault(alias, region)

        self._loaded = True
        logger.info(f"[RegionStore] ✅ 已加载区域: {len(self._regions)} keys | root={self.root}")

    def reload(self) -> None:
        self.load_all()

    def keys(self) -> List[str]:
        return sorted(self._regions.keys())

    def get(self, key: str) -> Optional[Region]:
        key = _norm_key(key)
        if not key:
            return None
        return self._regions.get(key)

    def get_region(self, key: str) -> Optional[Region]:
        return self.get(key)

    def load(self, category: str, name: str) -> Optional[Region]:
        # 兼容 category/name 里带斜杠的情况
        key = _norm_key(f"{category}.{name}")
        r = self.get(key)
        if r:
            return r

        # 再试：category 可能是 对战/逃跑 这种
        key2 = _norm_key(f"{category}.{name}")
        return self.get(key2)

    def suggest(self, query: str, limit: int = 8) -> List[str]:
        q = _norm_key(query)
        if not q:
            return []
        hits = [k for k in self._regions.keys() if q in k]
        return hits[:limit]

    def require(self, key: str) -> Region:
        r = self.get(key)
        if r:
            return r
        sug = self.suggest(key)
        raise KeyError(f"找不到区域: {key} | suggest={sug} | root={self.root}")


