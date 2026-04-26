# -*- coding: utf-8 -*-
"""
游戏内核 stdout 行匹配：兼容旧版「斜杠路径」与新版结构化日志，例如：
  time=... level=INFO msg=从主目录提供文件 path=login\\Login.swf
  time=... path=resource\\map\\500001.swf
  time=... msg=从主目录提供文件 path=resource\\item\\cloth\\swf\\....swf
  time=... msg=本地未找到文件，从远程服务器获取 path=resource\\pet\\swf\\144.swf

匹配时对「整行」以及所有 path= 片段分别探测，避免仅斜杠路径导致漏检。
path= 片段有时不含 resource\\ 前缀（例如 item\\petItem\\icon\\、fightResource\\pet\\swf\\），
故在对应 RE 中增加以段首为锚的变体（与 pet\\swf\\、pet\\sound\\ 一致）。

newNpc：偶见 path= 或独立片段为 wNpc\\multi\\0.swf（截断/换行导致缺 ne），RE_NEWNPC_MULTI 含容错分支。
"""
from __future__ import annotations

import re
from typing import Iterable, Iterator, List, Match, Optional, Pattern, Tuple, Union

# 从一行中提取 path= 后的路径片段（可多次）
_PATH_EQ_RE = re.compile(r"(?i)(?:^|[\s;])path=([^\s]+)")

# --- 资源路径（POSIX 或反斜杠）---
RE_MAP_SWF_ID = re.compile(
    r"(?:(?:/resource/map/|resource[\\/]map[\\/])|^map[\\/])(\d+)\.swf",
    re.IGNORECASE,
)
# 仅有 map 路径前缀（未带 .swf id 时）
RE_MAP_PATH_LOOSE = re.compile(
    r"(?:/resource/map\b|resource[\\/]map(?:[\\/]|\b))",
    re.IGNORECASE,
)
RE_NEWNPC_MULTI = re.compile(
    r"(?:/resource/newNpc/multi/0\.swf|resource[\\/]newNpc[\\/]multi[\\/]0\.swf"
    r"|^newNpc[\\/]multi[\\/]0\.swf|^wNpc[\\/]multi[\\/]0\.swf)",
    re.IGNORECASE,
)
RE_PETITEM = re.compile(
    r"(?:/resource/item/petItem/icon/|resource[\\/]item[\\/]petItem[\\/]icon[\\/]"
    r"|^item[\\/]petItem[\\/]icon[\\/])",
    re.IGNORECASE,
)
RE_FIGHT_PET = re.compile(
    r"(?:/resource/fightResource/pet/swf/|resource[\\/]fightResource[\\/]pet[\\/]swf[\\/]"
    r"|^fightResource[\\/]pet[\\/]swf[\\/])",
    re.IGNORECASE,
)
RE_FIGHT_SKILL_SWF = re.compile(
    r"(?:/resource/fightResource/skill/swf/|resource[\\/]fightResource[\\/]skill[\\/]swf[\\/]"
    r"|^fightResource[\\/]skill[\\/]swf[\\/])",
    re.IGNORECASE,
)
RE_FIGHT_SKILL_DIR = re.compile(
    r"(?:/resource/fightResource/skill/|resource[\\/]fightResource[\\/]skill[\\/])",
    re.IGNORECASE,
)
# 校准用 pet/swf：新版 path 片段可能仅为 pet\swf\254.swf（无 resource 前缀）。分段匹配时 ^pet 不会误匹配 fightResource\pet\swf 整段。
RE_PET_SWF = re.compile(
    r"(?:/resource/pet/swf/|resource[\\/]pet[\\/]swf[\\/]|^pet[\\/]swf[\\/])",
    re.IGNORECASE,
)
RE_PET_SOUND = re.compile(
    r"(?:/resource/pet/sound/|resource[\\/]pet[\\/]sound[\\/]|^pet[\\/]sound[\\/])",
    re.IGNORECASE,
)
# 背包/仓库等界面根节点 PetStorage.swf（path= 片段常为 PetStorage.swf 或 resource\PetStorage.swf）
RE_PETSTORAGE_SWF = re.compile(
    r"(?:/resource/)?PetStorage\.swf|resource[\\/]PetStorage\.swf|\bPetStorage\.swf\b",
    re.IGNORECASE,
)
RE_IP_TXT = re.compile(
    r"(?:/ip\.txt|\\\\ip\.txt|\bip\.txt|path=\S*ip\.txt)",
    re.IGNORECASE,
)

RE_LOGIN_SWF_LINE = re.compile(
    r"/login/Login\.swf|login[\\/]Login\.swf|path=\S*Login\.swf",
    re.IGNORECASE,
)

# 带捕获数字 id（finditer）
RE_FIGHT_PET_ID = re.compile(
    r"(?:(?:/resource/fightResource/pet/swf/|resource[\\/]fightResource[\\/]pet[\\/]swf[\\/])"
    r"|^fightResource[\\/]pet[\\/]swf[\\/])(\d+)\.swf",
    re.IGNORECASE,
)
RE_PET_ID_CALIB = re.compile(
    r"(?:/resource/pet/swf/|resource[\\/]pet[\\/]swf[\\/]|^pet[\\/]swf[\\/])(\d+)\.swf",
    re.IGNORECASE,
)
RE_PET_SOUND_ID = re.compile(
    r"(?:/resource/pet/sound/|resource[\\/]pet[\\/]sound[\\/]|^pet[\\/]sound[\\/])(\d+)\.mp3",
    re.IGNORECASE,
)


def _target_mp3_id_set(mp3_id_or_ids: Union[int, Tuple[int, ...], List[int]]) -> set[int]:
    if isinstance(mp3_id_or_ids, (tuple, list)):
        return {int(x) for x in mp3_id_or_ids}
    return {int(mp3_id_or_ids)}


def iter_pet_sound_mp3_ids_in_line(line: str) -> Iterator[int]:
    """从一行（含 path= 片段）中解析 pet sound 的 mp3 数字 id。"""
    for m in finditer_kernel_line(RE_PET_SOUND_ID, line):
        try:
            yield int(m.group(1))
        except ValueError:
            continue


def line_has_target_pet_sound_id(line: str, mp3_id_or_ids: Union[int, Tuple[int, ...], List[int]]) -> bool:
    want = _target_mp3_id_set(mp3_id_or_ids)
    for pid in iter_pet_sound_mp3_ids_in_line(line):
        if pid in want:
            return True
    return False


def lines_have_target_pet_sound_id(
    lines: Iterable[str], mp3_id_or_ids: Union[int, Tuple[int, ...], List[int]]
) -> bool:
    """是否与 map/swf 一致：整行 + path= 分段用 RE_PET_SOUND_ID 匹配，避免只认 /{id}.mp3 漏掉反斜杠路径。"""
    for line in lines:
        if line_has_target_pet_sound_id(line, mp3_id_or_ids):
            return True
    return False


def first_matching_pet_sound_id_in_lines(
    lines: Iterable[str], mp3_id_or_ids: Union[int, Tuple[int, ...], List[int]]
) -> Optional[int]:
    want = _target_mp3_id_set(mp3_id_or_ids)
    for line in lines:
        for pid in iter_pet_sound_mp3_ids_in_line(line):
            if pid in want:
                return pid
    return None


def kernel_line_search_segments(line: str) -> List[str]:
    """整行 + 每个 path= 片段，用于子串或正则梭巡。"""
    s = str(line).strip()
    out: List[str] = [s]
    for m in _PATH_EQ_RE.finditer(s):
        out.append(m.group(1))
    return out


def line_matches(needle: Union[str, Pattern], line: str) -> bool:
    """
    needle 为子串或已编译正则；在整行及 path= 片段上或运算。
    """
    segs = kernel_line_search_segments(line)
    if hasattr(needle, "search"):
        pat: Pattern = needle  # type: ignore[assignment]
        return any(pat.search(seg) for seg in segs)
    n = str(needle)
    return any(n in seg for seg in segs)


def finditer_kernel_line(pattern: Pattern, line: str) -> Iterator[Match]:
    """对整行与各 path= 片段分别 finditer。"""
    for seg in kernel_line_search_segments(line):
        yield from pattern.finditer(seg)


def first_map_id_in_line(line: str) -> Optional[int]:
    for seg in kernel_line_search_segments(line):
        m = RE_MAP_SWF_ID.search(seg)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def re_map_swf_exact_id(map_id: int) -> Pattern:
    """匹配指定地图 id（含 path= 片段仅为 map\\id.swf 的情况）。"""
    mid = int(map_id)
    return re.compile(
        rf"(?:(?:/resource/map/|resource[\\/]map[\\/])|^map[\\/]){mid}\.swf",
        re.IGNORECASE,
    )


def first_pet_swf_id_in_line(line: str) -> Optional[int]:
    for seg in kernel_line_search_segments(line):
        m = RE_PET_ID_CALIB.search(seg)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def line_has_login_swf(line: str) -> bool:
    s = str(line)
    if "/login/Login.swf" in s:
        return True
    if line_matches(RE_LOGIN_SWF_LINE, s):
        return True
    if "从主目录提供文件" in s and "Login.swf" in s:
        return True
    return False


def iter_lines_map_ids(lines: Iterable[str]) -> List[int]:
    ids: List[int] = []
    for line in lines:
        for seg in kernel_line_search_segments(line):
            m = RE_MAP_SWF_ID.search(seg)
            if m:
                try:
                    ids.append(int(m.group(1)))
                except ValueError:
                    pass
    return ids
