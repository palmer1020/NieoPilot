import csv
import os
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, Optional

try:
    from config import BASE_PATH
except Exception:
    BASE_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


SIGNIN_RECORD_FIELDS = (
    "time",
    "business_date",
    "business_month",
    "signin_count",
    "counted",
    "source",
)


BEE_AWARD_RECORD_FIELDS = (
    "time",
    "business_month",
    "business_date",
    "source",
)


def beijing_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def business_date_6am(value: Optional[datetime] = None) -> date:
    now = value or beijing_now()
    bj_tz = timezone(timedelta(hours=8))
    if now.tzinfo is None:
        bj = now.replace(tzinfo=bj_tz)
    else:
        bj = now.astimezone(bj_tz)
    return (bj - timedelta(hours=6)).date()


def business_day_number_6am(value: Optional[datetime] = None) -> int:
    return int(business_date_6am(value).day)


def signin_record_path() -> str:
    return os.path.join(BASE_PATH, "data", "records", "签到.xls")


def bee_award_record_path() -> str:
    return os.path.join(BASE_PATH, "data", "records", "小蜜蜂.xls")


def _parse_int(raw: object, default: int = 0) -> int:
    try:
        return int(str(raw or "").strip())
    except Exception:
        return default


def _iter_rows(path: str) -> Iterable[Dict[str, str]]:
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        return ()
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return tuple(csv.DictReader(f, delimiter="\t"))


def append_signin_record(source: str = "", now: Optional[datetime] = None) -> Dict[str, object]:
    recorded_at = now or beijing_now()
    biz_date = business_date_6am(recorded_at)
    biz_date_text = biz_date.isoformat()
    month_key = biz_date.strftime("%Y-%m")
    path = signin_record_path()

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rows = list(_iter_rows(path))
        month_rows = [
            row for row in rows
            if str(row.get("business_month") or "").strip() == month_key
        ]
        same_day_rows = [
            row for row in month_rows
            if str(row.get("business_date") or "").strip() == biz_date_text
        ]
        month_max_count = max(
            (_parse_int(row.get("signin_count"), 0) for row in month_rows),
            default=0,
        )
        if same_day_rows:
            counted = False
            signin_count = max(
                (_parse_int(row.get("signin_count"), 0) for row in same_day_rows),
                default=month_max_count,
            )
        else:
            counted = True
            signin_count = (month_max_count + 1) if month_max_count > 0 else int(biz_date.day)

        write_header = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=SIGNIN_RECORD_FIELDS, delimiter="\t")
            if write_header:
                writer.writeheader()
            writer.writerow(
                {
                    "time": recorded_at.astimezone(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
                    "business_date": biz_date_text,
                    "business_month": month_key,
                    "signin_count": int(signin_count),
                    "counted": "1" if counted else "0",
                    "source": str(source or "").strip(),
                }
            )
        return {
            "ok": True,
            "path": path,
            "time": recorded_at.isoformat(timespec="seconds"),
            "business_date": biz_date_text,
            "business_month": month_key,
            "signin_count": int(signin_count),
            "counted": counted,
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "path": path,
            "time": recorded_at.isoformat(timespec="seconds"),
            "business_date": biz_date_text,
            "business_month": month_key,
            "signin_count": int(biz_date.day),
            "counted": False,
            "error": str(exc),
        }


def has_monthly_bee_award(now: Optional[datetime] = None) -> bool:
    biz_date = business_date_6am(now)
    month_key = biz_date.strftime("%Y-%m")
    path = bee_award_record_path()
    try:
        return any(
            str(row.get("business_month") or "").strip() == month_key
            for row in _iter_rows(path)
        )
    except Exception:
        return False


def append_monthly_bee_award(source: str = "", now: Optional[datetime] = None) -> Dict[str, object]:
    recorded_at = now or beijing_now()
    biz_date = business_date_6am(recorded_at)
    month_key = biz_date.strftime("%Y-%m")
    path = bee_award_record_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if has_monthly_bee_award(recorded_at):
            return {
                "ok": True,
                "path": path,
                "business_month": month_key,
                "business_date": biz_date.isoformat(),
                "recorded": False,
                "error": "",
            }
        write_header = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=BEE_AWARD_RECORD_FIELDS, delimiter="\t")
            if write_header:
                writer.writeheader()
            writer.writerow(
                {
                    "time": recorded_at.astimezone(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
                    "business_month": month_key,
                    "business_date": biz_date.isoformat(),
                    "source": str(source or "").strip(),
                }
            )
        return {
            "ok": True,
            "path": path,
            "business_month": month_key,
            "business_date": biz_date.isoformat(),
            "recorded": True,
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "path": path,
            "business_month": month_key,
            "business_date": biz_date.isoformat(),
            "recorded": False,
            "error": str(exc),
        }
