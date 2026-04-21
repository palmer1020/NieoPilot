# core/logger.py
import os
import time
import logging
import threading
from collections import deque
from typing import Callable, Optional, Tuple, List, Union

logger = logging.getLogger("NieoPilot")

# ===============================
# Kernel log callbacks + buffer
# ===============================
_kernel_callbacks: List[Callable[[str], None]] = []
_kernel_lock = threading.Lock()
_kernel_cv = threading.Condition(_kernel_lock)

# Buffer stores: (seq, ts, line)
_kernel_buf: "deque[Tuple[int, float, str]]" = deque(maxlen=8000)
_kernel_seq = 0


def init_logger(log_dir: str = "log", filename: str = "nieopilot.log"):
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, filename)

    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info(f"📄 日志系统已启动，保存路径: {path}")
    return path


def add_kernel_log_callback(cb: Callable[[str], None]):
    """Dashboard/独立窗口用：把高级日志行推送出去（线程安全）"""
    with _kernel_lock:
        _kernel_callbacks.append(cb)


def remove_kernel_log_callback(cb: Callable[[str], None]):
    with _kernel_lock:
        if cb in _kernel_callbacks:
            _kernel_callbacks.remove(cb)


def emit_kernel_log(line: str):
    """WindowManager 读到游戏 stdout 后调用这里"""
    global _kernel_seq

    line = (line or "").strip()
    if not line:
        return

    # 先缓存
    with _kernel_cv:
        _kernel_seq += 1
        _kernel_buf.append((_kernel_seq, time.time(), line))
        _kernel_cv.notify_all()

    # 再广播（不要拿锁调用回调，避免死锁）
    with _kernel_lock:
        callbacks = list(_kernel_callbacks)

    for cb in callbacks:
        try:
            cb(line)
        except Exception:
            pass


def kernel_cursor() -> int:
    with _kernel_lock:
        return _kernel_seq


def fetch_kernel_since(cursor: int, *, return_rows: bool = False) -> Union[List[str], List[Tuple[int, float, str]]]:
    """
    从 cursor(序号) 之后取 kernel buffer。
    - return_rows=False -> List[str]
    - return_rows=True  -> List[(seq, ts, line)]
    """
    with _kernel_lock:
        rows = [r for r in _kernel_buf if r[0] > int(cursor)]

    if return_rows:
        return rows
    return [r[2] for r in rows]


def wait_kernel_contains(substr: str, *args, **kwargs) -> Union[bool, Tuple[bool, int, Optional[str]]]:
    """
    兼容两种调用方式：

    A) 旧版（三元组返回）:
        found, new_cursor, matched_line = wait_kernel_contains(substr, cursor, timeout)

    B) 新版（布尔返回）:
        ok = wait_kernel_contains(substr, timeout_s=60, poll=0.1, cursor=None)

    参数：
    - poll: Condition wait 的最大等待步长（默认0.2）
    - timeout_s/timeout: 总超时秒数（默认30）
    - cursor: 起始 cursor；不传则从当前 cursor 开始（只看“之后”）
    """
    # --- Style A: old positional (cursor, timeout) ---
    if len(args) >= 2 and isinstance(args[0], int) and isinstance(args[1], (int, float)):
        cursor = int(args[0])
        timeout = float(args[1])
        poll = float(kwargs.get("poll", 0.2))
        return _wait_kernel_contains_by_cursor(substr, cursor, timeout, poll)

    # --- Style B: new keyword style -> return bool ---
    cursor = kwargs.get("cursor", None)
    if cursor is None:
        cursor = kernel_cursor()
    else:
        cursor = int(cursor)

    timeout = kwargs.get("timeout_s", None)
    if timeout is None:
        timeout = kwargs.get("timeout", 30.0)
    timeout = float(timeout)

    poll = float(kwargs.get("poll", 0.2))

    found, _new_cursor, _matched = _wait_kernel_contains_by_cursor(substr, cursor, timeout, poll)
    return found


def _wait_kernel_contains_by_cursor(substr: str, cursor: int, timeout: float, poll: float) -> Tuple[bool, int, Optional[str]]:
    """substr 可为「子串」或已编译正则；与 path= 新格式兼容见 core.kernel_log_match.line_matches。"""
    from core.kernel_log_match import line_matches

    deadline = time.time() + float(timeout)
    matched_line: Optional[str] = None

    while True:
        rows = fetch_kernel_since(cursor, return_rows=True)
        for seq, _ts, line in rows:
            if seq > cursor:
                cursor = seq
            if line_matches(substr, line):
                matched_line = line
                return True, cursor, matched_line

        remaining = deadline - time.time()
        if remaining <= 0:
            return False, cursor, None

        with _kernel_cv:
            _kernel_cv.wait(timeout=min(max(float(poll), 0.01), remaining))
