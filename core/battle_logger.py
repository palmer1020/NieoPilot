# core/battle_logger.py
"""
Per-battle CSV logger.

Each row = one battle.  File is append-only; thread-safe via a Lock.
"""
import csv
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Dict, Optional, Sequence


_CSV_COLUMNS = [
    "timestamp",
    "run_id",
    "mode",
    "battle_seq",
    "battle_duration_s",
    "enemy_pet_ids",
    "total_rounds",
    "result",
    "capsule_high",
    "capsule_super",
    "capsule_special",
    "capsule_invincible",
    # 胶囊循环策略信息（便于分析轮换勾选是否生效）
    "capsule_cycle_mode",   # default | mantis_legacy | special_only | super_only | high_only | custom
    "capsule_cycle_tiers",  # e.g. "super,special,super,super,special,super" or "special"
]

_CAPTURE_CSV_COLUMNS = [
    "timestamp",
    "run_id",
    "mode",
    "capture_seq",
    "battle_duration_s",
    "enemy_pet_ids",
    "total_rounds",
    "capsule_high",
    "capsule_super",
    "capsule_special",
    "capsule_invincible",
    "capsule_cycle_mode",
    "capsule_cycle_tiers",
]


class BattleLogger:
    """Append one CSV row per battle to ``log/battle_log.csv``.

    Captured battles are also appended to ``log/capture_log.csv``
    with a sequential ``capture_seq`` counter.
    """

    def __init__(self, log_dir: str = "log"):
        self._lock = threading.Lock()
        os.makedirs(log_dir, exist_ok=True)
        self._csv_path = os.path.join(log_dir, "battle_log.csv")
        self._capture_csv_path = os.path.join(log_dir, "capture_log.csv")

        self._ensure_csv_header(self._csv_path, _CSV_COLUMNS)
        self._ensure_csv_header(self._capture_csv_path, _CAPTURE_CSV_COLUMNS)

        self._run_id: str = ""
        self._mode: str = ""
        self._battle_seq: int = 0
        self._capture_seq: int = 0

    def _ensure_csv_header(self, path: str, expected_columns) -> None:
        """
        确保 CSV 存在且表头匹配。若已有旧表头，会原地迁移：补齐新列（旧行尾部填空）。
        """
        if not os.path.exists(path):
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(expected_columns)
            return

        try:
            with open(path, "r", newline="", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                rows = list(reader)
        except Exception:
            # 读失败就不动，避免破坏文件
            return

        if not rows:
            try:
                with open(path, "w", newline="", encoding="utf-8-sig") as f:
                    csv.writer(f).writerow(expected_columns)
            except Exception:
                pass
            return

        header = rows[0]
        if header == list(expected_columns):
            return

        # 迁移：对齐列数，补齐新列
        new_rows = [list(expected_columns)]
        target_len = len(expected_columns)
        for r in rows[1:]:
            rr = list(r)
            if len(rr) < target_len:
                rr.extend([""] * (target_len - len(rr)))
            elif len(rr) > target_len:
                rr = rr[:target_len]
            new_rows.append(rr)

        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerows(new_rows)
        except Exception:
            pass

    def new_run(self, mode_name: str) -> str:
        """Start a new logical run.  Returns the generated *run_id*."""
        with self._lock:
            self._run_id = uuid.uuid4().hex[:8]
            self._mode = mode_name
            self._battle_seq = 0
            self._capture_seq = 0
            return self._run_id

    def log_battle(
        self,
        *,
        enemy_pet_ids: Optional[Sequence[int]] = None,
        total_rounds: int = 0,
        result: str = "",
        battle_duration_s: float = 0.0,
        capsule_counts: Optional[Dict[str, int]] = None,
        capsule_cycle_mode: str = "",
        capsule_cycle_tiers: str = "",
    ) -> None:
        """Append one battle row. If result is 'captured', also append to capture_log."""
        counts = capsule_counts or {}
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        ids_str = ";".join(str(i) for i in (enemy_pet_ids or []))
        with self._lock:
            self._battle_seq += 1
            row = [
                ts,
                self._run_id,
                self._mode,
                self._battle_seq,
                f"{battle_duration_s:.1f}",
                ids_str,
                total_rounds,
                result,
                counts.get("high", 0),
                counts.get("super", 0),
                counts.get("special", 0),
                counts.get("invincible", 0),
                capsule_cycle_mode,
                capsule_cycle_tiers,
            ]
            try:
                with open(self._csv_path, "a", newline="", encoding="utf-8-sig") as f:
                    csv.writer(f).writerow(row)
            except Exception:
                pass

            if result == "captured":
                self._capture_seq += 1
                cap_row = [
                    ts,
                    self._run_id,
                    self._mode,
                    self._capture_seq,
                    f"{battle_duration_s:.1f}",
                    ids_str,
                    total_rounds,
                    counts.get("high", 0),
                    counts.get("super", 0),
                    counts.get("special", 0),
                    counts.get("invincible", 0),
                    capsule_cycle_mode,
                    capsule_cycle_tiers,
                ]
                try:
                    with open(self._capture_csv_path, "a", newline="", encoding="utf-8-sig") as f:
                        csv.writer(f).writerow(cap_row)
                except Exception:
                    pass
