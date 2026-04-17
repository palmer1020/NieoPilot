# core/human_verify_watcher.py
import os, json, time, hashlib
from typing import Optional, Tuple, List

from core.utils import window_manager

try:
    import pytesseract
except Exception:
    pytesseract = None


def _bbox(r) -> Tuple[float, float, float, float]:
    b = getattr(r, "outer_bbox", None)
    if callable(b):
        x1, y1, x2, y2 = b()
        return float(x1), float(y1), float(x2), float(y2)
    if b is not None:
        x1, y1, x2, y2 = b
        return float(x1), float(y1), float(x2), float(y2)
    raise ValueError("region has no outer_bbox")


def _ocr(img) -> str:
    if pytesseract is None or img is None:
        return ""
    try:
        g = img.convert("L")
        w, h = g.size
        g = g.resize((max(1, w * 3), max(1, h * 3)))
        txt = pytesseract.image_to_string(g, lang="chi_sim+eng", config="--psm 6")
        return " ".join((txt or "").split()).strip()
    except Exception:
        return ""


def _has_kw(txt: str) -> bool:
    if not txt:
        return False
    return ("正面" in txt) or ("侧面" in txt) or ("反面" in txt) or ("背面" in txt)


class HumanVerifyWatcher:
    KEY_PANEL = "人机验证.人机验证"
    KEY_INFO  = "人机验证.人机验证信息"
    KEY_OPTS  = [
        "人机验证.图像精灵一",
        "人机验证.图像精灵二",
        "人机验证.图像精灵三",
        "人机验证.图像精灵四",
    ]

    def __init__(self, bot, regions, save_root: str):
        self.bot = bot
        self.regions = regions
        self.save_root = save_root
        self.todo_dir = os.path.join(save_root, "未处理")
        os.makedirs(self.todo_dir, exist_ok=True)

    @staticmethod
    def contains_keywords(txt: str) -> bool:
        return _has_kw(txt)

    def _require(self, key: str):
        if hasattr(self.regions, "require"):
            return self.regions.require(key)
        r = self.regions.get(key) if hasattr(self.regions, "get") else None
        if not r:
            raise KeyError(f"missing region: {key}")
        return r

    def _grab(self, key: str):
        r = self._require(key)
        x1, y1, x2, y2 = _bbox(r)
        return window_manager.grab_game_bbox(x1, y1, x2, y2)

    def scan_info_text(self) -> str:
        """只扫文字，不打日志。"""
        img = self._grab(self.KEY_INFO)
        return _ocr(img)

    def capture_and_save_text(self, ocr_text: str) -> Optional[str]:
        """命中关键词时调用：保存 panel/info/4选项 + meta.json"""
        try:
            window_manager.maximize_window()
        except Exception:
            pass

        panel = self._grab(self.KEY_PANEL)
        info  = self._grab(self.KEY_INFO)

        opts = []
        for k in self.KEY_OPTS:
            try:
                opts.append(self._grab(k))
            except Exception:
                opts.append(None)

        stamp = time.strftime("%Y%m%d_%H%M%S")
        h = hashlib.md5((ocr_text or "").encode("utf-8", errors="ignore")).hexdigest()[:8]
        base = f"{stamp}_hv_{h}"

        out_panel = os.path.join(self.todo_dir, base + "_panel.png")
        out_info  = os.path.join(self.todo_dir, base + "_info.png")
        out_meta  = os.path.join(self.todo_dir, base + "_meta.json")

        if panel: panel.save(out_panel)
        if info:  info.save(out_info)

        opt_files = []
        for i, im in enumerate(opts, start=1):
            if im is None:
                continue
            fn = os.path.join(self.todo_dir, base + f"_opt{i}.png")
            im.save(fn)
            opt_files.append(os.path.basename(fn))

        meta = {
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ocr_text": ocr_text,
            "files": {
                "panel": os.path.basename(out_panel) if panel else "",
                "info":  os.path.basename(out_info) if info else "",
                "opts":  opt_files,
            },
        }
        with open(out_meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        return out_meta
