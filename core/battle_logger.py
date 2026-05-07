# core/battle_logger.py
"""
Per-battle CSV logger.

``log_battle_start`` inserts one row with record_phase=start; ``log_battle_end`` replaces
that same row in the CSV file (enemy_pet_ids merged, record_phase=single) instead of
appending a second line. Fallback append remains if no matching start row is found.

Thread-safe via a Lock. Reconnect-marker rows remain separate single rows.
"""
import csv
import os
import threading
import uuid
from datetime import datetime, timedelta
from typing import Dict, Optional, Sequence


_CSV_COLUMNS = [
    "timestamp",
    "run_id",
    "mode",
    "battle_seq",
    "battle_duration_s",
    "since_last_battle_end_s",
    "enemy_pet_ids",
    "total_rounds",
    "result",
    "reconnect_reason",
    "capsule_high",
    "capsule_super",
    "capsule_special",
    "capsule_invincible",
    # 胶囊循环策略信息（便于分析轮换勾选是否生效）
    "capsule_cycle_mode",   # default | mantis_legacy | special_only | super_only | high_only | custom
    "capsule_cycle_tiers",  # e.g. "super,special,super,super,special,super" or "special"
    "record_phase",  # single | battle_start=start（未收尾）| start/end 旧配对兼容
    "battle_instance_id",  # paired start row id；收尾后单行仍保留该 id 便于回溯
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


def _merge_enemy_id_csv_fields(start_field: str, end_ids: Optional[Sequence[int]]) -> str:
    """Merge start-row enemy_pet_ids string with end sequence; unique sorted."""
    def parse_field(s: str) -> set:
        out: set = set()
        if not (s or "").strip():
            return out
        for part in str(s).split(";"):
            p = part.strip()
            if p.isdigit():
                out.add(int(p))
        return out

    merged = parse_field(start_field or "")
    for i in end_ids or []:
        try:
            merged.add(int(i))
        except (TypeError, ValueError):
            pass
    if not merged:
        return ""
    return ";".join(str(x) for x in sorted(merged))


class BattleLogger:
    """Append one CSV row per battle to ``log/battle_log.csv``.

    ``log_battle_start`` + ``log_battle_end`` update the **same physical row**
    (in-place): the start line is rewritten with final stats instead of appending a second line.

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
        self._prev_battle_end_dt: Optional[datetime] = None
        self._reconnect_marker_logged: bool = False
        self._pending_instance_seq: Dict[str, int] = {}

    @staticmethod
    def _battle_col_index(name: str) -> int:
        return list(_CSV_COLUMNS).index(name)

    def _finalize_battle_csv_start_row(
        self,
        *,
        battle_instance_id: str,
        enemy_pet_ids: Optional[Sequence[int]],
        ts: str,
        seq: int,
        total_rounds: int,
        result: str,
        battle_duration_s: float,
        since_last_end_s: str,
        reconnect_reason: str,
        counts: Dict,
        capsule_cycle_mode: str,
        capsule_cycle_tiers: str,
    ) -> tuple[bool, str]:
        """
        将文件中 record_phase=start 且 battle_instance_id 匹配的行整行替换（record_phase=single）。
        返回 (是否成功改写, merged enemy_pet_ids CSV 字段)。
        """
        ii = BattleLogger._battle_col_index
        phase_i = ii("record_phase")
        bid_i = ii("battle_instance_id")
        eid_i = ii("enemy_pet_ids")
        tgt_len = len(_CSV_COLUMNS)
        try:
            with open(self._csv_path, "r", newline="", encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))
        except Exception:
            return False, ""
        if not rows:
            return False, ""
        found = -1
        start_ids_field = ""
        for i in range(1, len(rows)):
            r = list(rows[i])
            while len(r) < tgt_len:
                r.append("")
            if len(r) > bid_i and r[bid_i] == battle_instance_id and len(r) > phase_i and r[phase_i] == "start":
                found = i
                if len(r) > eid_i:
                    start_ids_field = r[eid_i] or ""
                break
        if found < 0:
            return False, ""
        merged_ids_final = _merge_enemy_id_csv_fields(start_ids_field, enemy_pet_ids)
        new_row = [
            ts,
            self._run_id,
            self._mode,
            seq,
            f"{battle_duration_s:.1f}",
            since_last_end_s,
            merged_ids_final,
            total_rounds,
            result,
            reconnect_reason,
            counts.get("high", 0),
            counts.get("super", 0),
            counts.get("special", 0),
            counts.get("invincible", 0),
            capsule_cycle_mode,
            capsule_cycle_tiers,
            "single",
            battle_instance_id,
        ]
        while len(new_row) < tgt_len:
            new_row.append("")
        if len(new_row) > tgt_len:
            new_row = new_row[:tgt_len]
        rows[found] = new_row
        try:
            with open(self._csv_path, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerows(rows)
            return True, merged_ids_final
        except Exception:
            return False, merged_ids_final

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
            self._prev_battle_end_dt = None
            self._reconnect_marker_logged = False
            self._pending_instance_seq.clear()
            return self._run_id

    def log_battle(
        self,
        *,
        enemy_pet_ids: Optional[Sequence[int]] = None,
        total_rounds: int = 0,
        result: str = "",
        battle_duration_s: float = 0.0,
        reconnect_reason: str = "",
        capsule_counts: Optional[Dict[str, int]] = None,
        capsule_cycle_mode: str = "",
        capsule_cycle_tiers: str = "",
    ) -> None:
        """Append one battle row（未配对时使用 record_phase=single）. If result is 'captured', also append to capture_log."""
        counts = capsule_counts or {}
        start_dt = datetime.now()
        ts = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
        ids_str = ";".join(str(i) for i in (enemy_pet_ids or []))
        with self._lock:
            since_last_end_s = ""
            if self._prev_battle_end_dt is not None:
                gap = (start_dt - self._prev_battle_end_dt).total_seconds()
                if gap < 0:
                    gap = 0.0
                since_last_end_s = f"{gap:.1f}"

            self._battle_seq += 1
            row = [
                ts,
                self._run_id,
                self._mode,
                self._battle_seq,
                f"{battle_duration_s:.1f}",
                since_last_end_s,
                ids_str,
                total_rounds,
                result,
                reconnect_reason,
                counts.get("high", 0),
                counts.get("super", 0),
                counts.get("special", 0),
                counts.get("invincible", 0),
                capsule_cycle_mode,
                capsule_cycle_tiers,
                "single",
                "",
            ]
            try:
                with open(self._csv_path, "a", newline="", encoding="utf-8-sig") as f:
                    csv.writer(f).writerow(row)
            except Exception:
                pass

            try:
                self._prev_battle_end_dt = start_dt + timedelta(seconds=float(battle_duration_s or 0.0))
            except Exception:
                self._prev_battle_end_dt = start_dt

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

    def log_battle_start(self, *, enemy_pet_ids: Optional[Sequence[int]] = None) -> str:
        """入战时先写一行（result=battle_start，不刷新 since_last_battle_end 基准）。"""
        bid = uuid.uuid4().hex[:12]
        start_dt = datetime.now()
        ts = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
        ids_str = ";".join(str(i) for i in (enemy_pet_ids or []))
        with self._lock:
            since_last_end_s = ""
            if self._prev_battle_end_dt is not None:
                gap = (start_dt - self._prev_battle_end_dt).total_seconds()
                if gap < 0:
                    gap = 0.0
                since_last_end_s = f"{gap:.1f}"
            self._battle_seq += 1
            seq = self._battle_seq
            self._pending_instance_seq[bid] = seq
            row = [
                ts,
                self._run_id,
                self._mode,
                seq,
                "0.0",
                since_last_end_s,
                ids_str,
                0,
                "battle_start",
                "",
                0,
                0,
                0,
                0,
                "",
                "",
                "start",
                bid,
            ]
            try:
                with open(self._csv_path, "a", newline="", encoding="utf-8-sig") as f:
                    csv.writer(f).writerow(row)
            except Exception:
                pass
        return bid

    def log_battle_end(
        self,
        battle_instance_id: str,
        *,
        enemy_pet_ids: Optional[Sequence[int]] = None,
        total_rounds: int = 0,
        result: str = "",
        battle_duration_s: float = 0.0,
        reconnect_reason: str = "",
        capsule_counts: Optional[Dict[str, int]] = None,
        capsule_cycle_mode: str = "",
        capsule_cycle_tiers: str = "",
    ) -> bool:
        """与 log_battle_start 配对：原地改写 start 行（record_phase→single），补全回合/结果/petid；失败则回退为追加一行（兼容）。"""
        counts = capsule_counts or {}
        start_dt = datetime.now()
        ts = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
        ok = False
        with self._lock:
            seq = self._pending_instance_seq.pop(battle_instance_id, None)
            if seq is None:
                return False
            since_last_end_s = ""
            if self._prev_battle_end_dt is not None:
                gap = (start_dt - self._prev_battle_end_dt).total_seconds()
                if gap < 0:
                    gap = 0.0
                since_last_end_s = f"{gap:.1f}"
            ok = True
            ids_str = ";".join(str(i) for i in (enemy_pet_ids or []))
            replaced, merged_ids = self._finalize_battle_csv_start_row(
                battle_instance_id=battle_instance_id,
                enemy_pet_ids=enemy_pet_ids,
                ts=ts,
                seq=seq,
                total_rounds=total_rounds,
                result=result,
                battle_duration_s=battle_duration_s,
                since_last_end_s=since_last_end_s,
                reconnect_reason=reconnect_reason,
                counts=counts,
                capsule_cycle_mode=capsule_cycle_mode,
                capsule_cycle_tiers=capsule_cycle_tiers,
            )
            if not replaced:
                row = [
                    ts,
                    self._run_id,
                    self._mode,
                    seq,
                    f"{battle_duration_s:.1f}",
                    since_last_end_s,
                    ids_str or merged_ids,
                    total_rounds,
                    result,
                    reconnect_reason,
                    counts.get("high", 0),
                    counts.get("super", 0),
                    counts.get("special", 0),
                    counts.get("invincible", 0),
                    capsule_cycle_mode,
                    capsule_cycle_tiers,
                    "end",
                    battle_instance_id,
                ]
                try:
                    with open(self._csv_path, "a", newline="", encoding="utf-8-sig") as f:
                        csv.writer(f).writerow(row)
                except Exception:
                    pass
                ids_str = ids_str or merged_ids

            try:
                self._prev_battle_end_dt = start_dt + timedelta(seconds=float(battle_duration_s or 0.0))
            except Exception:
                self._prev_battle_end_dt = start_dt

            cap_ids = merged_ids if replaced else (ids_str or merged_ids)
            if result == "captured":
                self._capture_seq += 1
                cap_row = [
                    ts,
                    self._run_id,
                    self._mode,
                    self._capture_seq,
                    f"{battle_duration_s:.1f}",
                    cap_ids,
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

        return ok

    def log_reconnect_marker_once(self, *, reason: str, flush_pending_battle_id: Optional[str] = None) -> None:
        """
        记录“本 run 因重连而结束”的标记行（写入 battle_log 最后）。
        同一 run 只记录一次，避免重试递归重复写入。
        若在战斗中先写了 log_battle_start，可传入 flush_pending_battle_id 先补一行 end（result=interrupt_before_reconnect）。
        """
        if flush_pending_battle_id:
            self.log_battle_end(
                flush_pending_battle_id,
                enemy_pet_ids=[],
                total_rounds=0,
                result="interrupt_before_reconnect",
                battle_duration_s=0.0,
                reconnect_reason=reason[:500] if reason else "",
                capsule_counts={},
                capsule_cycle_mode="",
                capsule_cycle_tiers="",
            )

        with self._lock:
            if self._reconnect_marker_logged:
                return
            self._reconnect_marker_logged = True

        self.log_battle(
            enemy_pet_ids=[],
            total_rounds=0,
            result="reconnect",
            battle_duration_s=0.0,
            reconnect_reason=reason,
            capsule_counts={},
            capsule_cycle_mode="",
            capsule_cycle_tiers="",
        )

