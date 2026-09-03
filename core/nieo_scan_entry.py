# core/nieo_scan_entry.py
"""内置 / 自定义尼奥共用的变化点扫描与入战点击（含校准探针）。"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, List, Optional, Tuple, Union

from core.kernel_log_match import (
    RE_FIGHT_PET,
    RE_FIGHT_PET_ID,
    RE_FIGHT_SKILL_SWF,
    RE_PETITEM,
    finditer_kernel_line,
    line_matches,
)
from core.logger import fetch_kernel_since, kernel_cursor
from core.region_store import Region
from core.utils import window_manager

if TYPE_CHECKING:
    from core.dar_route_runner import DarRouteRunner

ScanOutcome = Union[Tuple[str, Region], str]  # success tuple | "reconnect"


@dataclass
class NieoEntryResult:
    kind: str  # success | reconnect | failed
    tx: float = 0.0
    ty: float = 0.0
    pet_ids: Optional[set] = None
    next_map_id: Optional[int] = None


def resolve_scan_reference(
    runner: "DarRouteRunner",
    current_map_id: int,
    current_prefix: str,
    reg_z: Region,
    *,
    pem: bool = False,
    profile: Any = None,
    previous_map_id: Optional[int] = None,
) -> Tuple[Optional[Region], Tuple[float, float]]:
    if profile is not None:
        if getattr(profile, "stay_on_b_map", False):
            reg = reg_z
        else:
            from core.nieo_mode_registry import nieo_scan_reference_keys

            ref_key, legacy = nieo_scan_reference_keys(
                profile, current_map_id, previous_map_id
            )
            reg = runner.regions.get(ref_key) or runner.regions.get(legacy)
            if reg is None:
                runner._emit(
                    f"⚠️ 未找到 ABC 参考传送点 {ref_key!r}/{legacy!r}（上一轮 map={previous_map_id}），"
                    f"临时回退 Z",
                    "WARN",
                )
                reg = reg_z
    elif pem:
        if current_map_id == 26:
            reg = runner.regions.get("纯净能量二.to一")
        else:
            reg = runner.regions.get("纯净能量一.to二")
        reg = reg or reg_z
    elif current_map_id == 10:
        reg = runner.regions.get(f"{current_prefix}.to二") or reg_z
    else:
        reg = runner.regions.get(f"{current_prefix}.to一") or reg_z
    return reg, runner._region_center(reg)


def _collect_route_mutations(
    runner: "DarRouteRunner",
    route_points_sorted: List[Tuple[str, Region]],
    *,
    pem: bool,
) -> List[Tuple[str, Region, float]]:
    """单轮扫描：收集所有达到阈值的突变点。"""
    found: List[Tuple[str, Region, float]] = []
    for key, reg in route_points_sorted:
        base = runner._baseline.get(key)
        if base is None:
            continue
        sig = runner._grab_sig(reg, downsample=(8, 8))
        diff = runner._sig_diff(sig, base)
        th = runner._threshold.get(key, 9999.0)
        if diff >= th:
            found.append((key, reg, diff))
    return found


def scan_select_change_point(
    runner: "DarRouteRunner",
    route_points_sorted: List[Tuple[str, Region]],
    reference_center: Tuple[float, float],
    stop_event,
    *,
    pem: bool = False,
    max_scan_duration: float = 10.0,
    use_foreground: bool = True,
    timeout_action: str = "reconnect",
    single_map_mode: bool = False,
    single_map_scope: str = "",
    selection_mode: str = "nearest",
) -> ScanOutcome:
    """
    首变点触发后 **再扫一整轮** 所有刷新点，再按 selection_mode 选择最近或最远突变点。

    timeout_action：扫描在 max_scan_duration 内无突变时的处理方式：
      - "reconnect"（默认）：触发强制重连，返回 "reconnect"
      - "giveup"：返回 "giveup"，由调用方决定（如露西之核：B 图不刷精灵则放弃本轮切回 A）
    """
    single_map_state = None
    if single_map_mode:
        states = getattr(runner, "_single_map_scan_states", None)
        if not isinstance(states, dict):
            states = {}
            runner._single_map_scan_states = states
        single_map_state = states.setdefault(single_map_scope or "default", {})

    first_change_key = None
    selected_key = None
    selected_reg = None
    change_found = False
    scan_start_time = time.time()

    while not change_found:
        if stop_event.is_set() or getattr(runner.bot, "stop_current", False):
            break

        if (time.time() - scan_start_time) > max_scan_duration:
            if timeout_action == "giveup":
                runner._emit(
                    f"🔕 扫描超时（{max_scan_duration}s）未找到变化点，放弃本轮（不重连）",
                    "WARN",
                )
                return "giveup"
            runner._emit(
                f"⏱️ 扫描超时（{max_scan_duration}s），未找到变化点，触发强制重连",
                "WARN",
            )
            if pem:
                if runner._check_pure_energy_reconnect_condition(
                    use_foreground, stop_event, force_reconnect=True
                ):
                    return "reconnect"
            elif runner._check_nieo_reconnect_condition(
                use_foreground, stop_event, force_reconnect=True
            ):
                return "reconnect"
            return "reconnect"

        scan_points = random.sample(route_points_sorted, len(route_points_sorted))
        if single_map_state is not None and len(scan_points) > 1:
            last_start_key = single_map_state.get("last_scan_start_key")
            if scan_points[0][0] == last_start_key:
                for swap_idx in range(1, len(scan_points)):
                    if scan_points[swap_idx][0] != last_start_key:
                        scan_points[0], scan_points[swap_idx] = (
                            scan_points[swap_idx],
                            scan_points[0],
                        )
                        break
            single_map_state["last_scan_start_key"] = scan_points[0][0]
        for idx, (key, reg) in enumerate(scan_points):
            if stop_event.is_set() or getattr(runner.bot, "stop_current", False):
                break
            base = runner._baseline.get(key)
            if base is None:
                continue

            sig = runner._grab_sig(reg, downsample=(8, 8))
            diff = runner._sig_diff(sig, base)
            th = runner._threshold.get(key, 9999.0)

            if diff >= th:
                first_change_key = key
                runner._emit(
                    f"🎯 检测到第一个变化点：{key}（diff={diff:.2f}，索引{idx}），"
                    f"再扫一整轮所有点…",
                    "SUCCESS",
                )
                all_mutations = _collect_route_mutations(
                    runner, route_points_sorted, pem=pem
                )
                if not all_mutations:
                    all_mutations = [(key, reg, diff)]

                selection_mode = str(selection_mode or "nearest").strip().lower()
                choose_farthest = selection_mode == "farthest"
                candidates = all_mutations
                previous_selected_key = (
                    single_map_state.get("last_selected_key")
                    if single_map_state is not None
                    else None
                )
                alternatives = [
                    item for item in all_mutations if item[0] != previous_selected_key
                ]
                if (not choose_farthest) and previous_selected_key and alternatives:
                    candidates = alternatives
                    runner._emit(
                        f"[单图] 上轮已选 {previous_selected_key}，本轮避开该点",
                        "INFO",
                    )
                elif (not choose_farthest) and previous_selected_key:
                    runner._emit(
                        f"[单图] 仅 {previous_selected_key} 为有效突变点，保留该点",
                        "INFO",
                    )

                best_key = candidates[0][0]
                best_reg = candidates[0][1]
                best_diff = candidates[0][2]
                best_dist = runner._route_distance_for_scan(
                    best_key, best_reg, reference_center
                )
                for mk, mr, md in candidates:
                    dist = runner._route_distance_for_scan(mk, mr, reference_center)
                    if (choose_farthest and dist > best_dist) or (
                        (not choose_farthest) and dist < best_dist
                    ):
                        best_key, best_reg, best_diff, best_dist = mk, mr, md, dist

                selected_key, selected_reg = best_key, best_reg
                distance_label = (
                    "链路距Z"
                    if best_key in getattr(runner, "_active_route_chain_distances", {})
                    else "距参考点"
                )
                if single_map_state is not None:
                    single_map_state["last_selected_key"] = best_key
                if best_key != first_change_key:
                    runner._emit(
                        f"🎯 第二轮全点扫描：选{'最远' if choose_farthest else '最近'}突变 {best_key}（diff={best_diff:.2f}，"
                        f"{distance_label}≈{best_dist:.0f}）",
                        "SUCCESS",
                    )
                else:
                    runner._emit(
                        f"🎯 第二轮全点扫描：仍选{'最远突变 ' if choose_farthest else ' '}{best_key}（diff={best_diff:.2f}，"
                        f"{distance_label}≈{best_dist:.0f}）",
                        "INFO",
                    )
                change_found = True
                break

        if not change_found:
            time.sleep(0.1)

    if not change_found or selected_key is None or selected_reg is None:
        return None  # type: ignore[return-value]

    farthest_mode = str(selection_mode).strip().lower() == "farthest"
    final_choice = (
        "全部突变中的最远点"
        if farthest_mode
        else ("第一个变化点" if selected_key == first_change_key else "第二轮最近点")
    )
    runner._emit(
        f"✅ 最终选择变化点：{selected_key}（{final_choice}），准备触发对战",
        "SUCCESS",
    )
    return selected_key, selected_reg


def entry_click_loop(
    runner: "DarRouteRunner",
    tx: float,
    ty: float,
    selected_key: str,
    use_foreground: bool,
    stop_event,
    *,
    pem: bool = False,
    current_map_id: int,
    current_prefix: str,
) -> NieoEntryResult:
    """持续点击变化点直至入战；遇校准则清探针后继续点击，超时则放弃。"""
    if not runner._unified_framework:
        runner._emit("❌ 统一框架未初始化，无法执行入战校准逻辑", "ERROR")
        return NieoEntryResult(kind="failed")

    runner._nieo_calibration_records.append(
        {
            "point_key": selected_key,
            "point_xy": (tx, ty),
            "calibration_success": False,
            "entry_result": None,
        }
    )

    timeout_s = runner.NIEO_ENTRY_CLICK_TIMEOUT_SEC
    click_interval = 0.5
    last_click_time = 0.0
    entry_deadline: Optional[float] = None
    current_cursor = kernel_cursor()

    runner._emit(
        f"🖱️ 持续点击变化点，等待 fightResource/pet/swf/ 或 PetItem"
        f"（首次点击起 {timeout_s:.0f}s 内须入战，遇校准继续点击）…",
        "INFO",
    )

    result: Optional[Tuple[float, float, Optional[set]]] = None
    collected_pet_ids: Optional[set] = None
    petitem_detected = False

    while result is None:
        if stop_event.is_set() or getattr(runner.bot, "stop_current", False):
            runner._emit("⛔ 点击过程中被停止", "WARN")
            break

        now = time.time()
        if entry_deadline is not None and now >= entry_deadline:
            break

        if now - last_click_time >= click_interval:
            if use_foreground:
                window_manager.click(tx, ty)
            else:
                window_manager.click_background(tx, ty)
            last_click_time = now
            if entry_deadline is None:
                entry_deadline = now + timeout_s
                runner._emit(
                    f"⏱️ 入战倒计时 {timeout_s:.0f}s（从首次点击变化点开始）",
                    "DEBUG",
                )
            time.sleep(0.05)

        calibration_guard_cursor = current_cursor
        try:
            lines = fetch_kernel_since(current_cursor)
            if isinstance(lines, list):
                for idx, line in enumerate(lines):
                    line_str = str(line)
                    if line_matches(RE_PETITEM, line_str):
                        petitem_detected = True
                        if runner._nieo_swf_to_petitem_swf_time is not None:
                            petitem_time = time.time()
                            current_delta = petitem_time - runner._nieo_swf_to_petitem_swf_time
                            runner._nieo_swf_to_petitem_current_time = current_delta
                            if (
                                runner._nieo_swf_to_petitem_min_time is None
                                or current_delta < runner._nieo_swf_to_petitem_min_time
                            ):
                                runner._nieo_swf_to_petitem_min_time = current_delta
                            runner._emit(
                                f"📊 [时间测量] fightpetswf到PetItem: {current_delta:.3f}s "
                                f"(最小值: {runner._nieo_swf_to_petitem_min_time:.3f}s)",
                                "INFO",
                            )
                        runner._emit("✅ 检测到PetItem信号（已入战），停止点击", "SUCCESS")
                        collected_pet_ids = runner._collect_fight_pet_ids_immediate(
                            stop_event, current_lines=lines, start_index=idx
                        )
                        result = (tx, ty, collected_pet_ids)
                        if runner._nieo_calibration_records:
                            runner._nieo_calibration_records[-1]["entry_result"] = "success"
                        runner._nieo_consecutive_entry_failures = 0
                        runner._petswf_to_petitem_consecutive_over_threshold = 0
                        break

                    if line_matches(RE_FIGHT_PET, line_str) and result is None:
                        runner._nieo_swf_to_petitem_swf_time = time.time()
                        runner._emit(
                            f"✅ 检测到fightResource/pet/swf/信号（已入战），停止点击，开始收集所有pet IDs",
                            "INFO",
                        )
                        initial_pet_ids = runner._collect_fight_pet_ids_immediate(
                            stop_event, current_lines=lines, start_index=idx
                        )
                        pet_ids = set(initial_pet_ids) if initial_pet_ids else set()
                        collect_start_time = time.time()
                        collect_timeout = 3.0
                        found_skill = False
                        found_petitem = False
                        collect_cursor = kernel_cursor()

                        while (time.time() - collect_start_time) < collect_timeout:
                            if stop_event.is_set() or getattr(runner.bot, "stop_current", False):
                                break
                            collect_lines = fetch_kernel_since(collect_cursor)
                            if isinstance(collect_lines, list):
                                for collect_line in collect_lines:
                                    collect_line_str = str(collect_line)
                                    if line_matches(RE_PETITEM, collect_line_str):
                                        found_petitem = True
                                        break
                                    if line_matches(RE_FIGHT_SKILL_SWF, collect_line_str):
                                        found_skill = True
                                        break
                                    for m in finditer_kernel_line(RE_FIGHT_PET_ID, collect_line_str):
                                        try:
                                            pet_id = int(m.group(1))
                                            pet_ids.add(pet_id)
                                        except (ValueError, AttributeError):
                                            pass
                                collect_cursor = kernel_cursor()
                            if found_skill or found_petitem:
                                break
                            time.sleep(0.05)

                        collected_pet_ids = pet_ids if pet_ids else None
                        result = (tx, ty, collected_pet_ids)
                        if runner._nieo_calibration_records:
                            runner._nieo_calibration_records[-1]["entry_result"] = "success"
                        runner._nieo_consecutive_entry_failures = 0
                        runner._petswf_to_petitem_consecutive_over_threshold = 0
                        break

                if result is not None or petitem_detected:
                    break
            current_cursor = kernel_cursor()
        except Exception as e:
            runner._emit(f"⚠️ 检查内核日志异常: {e}", "WARN")

        if result is not None:
            break

        # 校准探针是屏幕状态，可能和内核日志到达存在几十到几百毫秒竞争。
        # 在执行校准前再扫一次内核：只要已经看到入战信号，就不能再校准。
        try:
            fresh_lines = fetch_kernel_since(calibration_guard_cursor)
            if isinstance(fresh_lines, list):
                for idx, line in enumerate(fresh_lines):
                    line_str = str(line)
                    if line_matches(RE_PETITEM, line_str):
                        petitem_detected = True
                        runner._emit("✅ 校准前检测到PetItem信号（已入战），跳过校准", "SUCCESS")
                        collected_pet_ids = runner._collect_fight_pet_ids_immediate(
                            stop_event, current_lines=fresh_lines, start_index=idx
                        )
                        result = (tx, ty, collected_pet_ids)
                        if runner._nieo_calibration_records:
                            runner._nieo_calibration_records[-1]["entry_result"] = "success"
                        runner._nieo_consecutive_entry_failures = 0
                        runner._petswf_to_petitem_consecutive_over_threshold = 0
                        break

                    if line_matches(RE_FIGHT_PET, line_str):
                        runner._nieo_swf_to_petitem_swf_time = time.time()
                        runner._emit(
                            "✅ 校准前检测到fightResource/pet/swf/信号（已入战），跳过校准并收集pet IDs",
                            "SUCCESS",
                        )
                        initial_pet_ids = runner._collect_fight_pet_ids_immediate(
                            stop_event, current_lines=fresh_lines, start_index=idx
                        )
                        pet_ids = set(initial_pet_ids) if initial_pet_ids else set()
                        collect_start_time = time.time()
                        collect_timeout = 3.0
                        found_skill = False
                        found_petitem = False
                        collect_cursor = kernel_cursor()

                        while (time.time() - collect_start_time) < collect_timeout:
                            if stop_event.is_set() or getattr(runner.bot, "stop_current", False):
                                break
                            collect_lines = fetch_kernel_since(collect_cursor)
                            if isinstance(collect_lines, list):
                                for collect_line in collect_lines:
                                    collect_line_str = str(collect_line)
                                    if line_matches(RE_PETITEM, collect_line_str):
                                        found_petitem = True
                                        break
                                    if line_matches(RE_FIGHT_SKILL_SWF, collect_line_str):
                                        found_skill = True
                                        break
                                    for m in finditer_kernel_line(RE_FIGHT_PET_ID, collect_line_str):
                                        try:
                                            pet_id = int(m.group(1))
                                            pet_ids.add(pet_id)
                                        except (ValueError, AttributeError):
                                            pass
                                collect_cursor = kernel_cursor()
                            if found_skill or found_petitem:
                                break
                            time.sleep(0.05)

                        collected_pet_ids = pet_ids if pet_ids else None
                        result = (tx, ty, collected_pet_ids)
                        if runner._nieo_calibration_records:
                            runner._nieo_calibration_records[-1]["entry_result"] = "success"
                        runner._nieo_consecutive_entry_failures = 0
                        runner._petswf_to_petitem_consecutive_over_threshold = 0
                        break
                current_cursor = kernel_cursor()
            if result is not None or petitem_detected:
                break
        except Exception as e:
            runner._emit(f"⚠️ 校准前检查内核日志异常: {e}", "WARN")

        if runner._unified_framework._check_calibration_probes():
            runner._emit("🧭 检测到校准探针，执行校准", "WARN")
            if runner._nieo_calibration_records:
                calib_record = runner._nieo_calibration_records[-1]
            else:
                calib_record = {
                    "point_key": selected_key,
                    "point_xy": (tx, ty),
                    "calibration_success": False,
                    "entry_result": None,
                }
                runner._nieo_calibration_records.append(calib_record)

            x_values, _regions_dict = runner._unified_framework._calculate_x_values()
            distribution, target_idx = runner._unified_framework._analyze_distribution(x_values)
            if target_idx is not None:
                try:
                    fresh_lines = fetch_kernel_since(calibration_guard_cursor)
                    if isinstance(fresh_lines, list) and any(
                        line_matches(RE_PETITEM, str(line)) or line_matches(RE_FIGHT_PET, str(line))
                        for line in fresh_lines
                    ):
                        runner._emit("✅ 校准点击前检测到入战信号，取消校准并回退cursor交给入战处理", "SUCCESS")
                        current_cursor = calibration_guard_cursor
                        continue
                except Exception as e:
                    runner._emit(f"⚠️ 校准点击前检查内核日志异常: {e}", "WARN")
                runner._unified_framework._calibrate_click_group(target_idx, use_foreground)
                time.sleep(0.3)
                calib_record["calibration_success"] = True
                runner._emit(
                    f"📝 [校准记录] 点击点：{selected_key} ({tx:.0f},{ty:.0f})，校准成功",
                    "INFO",
                )
                entry_deadline = time.time() + timeout_s
                last_click_time = 0.0
                runner._emit(
                    f"✅ 校准成功，继续点击变化点（入战倒计时重置为 {timeout_s:.0f}s）",
                    "INFO",
                )
                continue

            previous_fallback_idx = int(
                getattr(runner, "_nieo_calibration_fallback_group", 0) or 0
            )
            fallback_idx = previous_fallback_idx % 4 + 1
            runner._nieo_calibration_fallback_group = fallback_idx
            runner._emit(
                f"📝 [校准记录] 点击点：{selected_key} ({tx:.0f},{ty:.0f})，"
                f"无法确定目标组，按轮次点击校准组{fallback_idx}",
                "WARN",
            )
            clicked = runner._unified_framework._calibrate_click_group(
                fallback_idx, use_foreground
            )
            calib_record["calibration_success"] = bool(clicked)
            if not clicked:
                runner._emit("⚠️ 随机校准组点击失败，继续等待下一次校准探针", "WARN")
                time.sleep(0.1)
                continue
            time.sleep(0.3)
            entry_deadline = time.time() + timeout_s
            last_click_time = 0.0
            runner._emit(
                f"✅ 随机校准组{fallback_idx}已点击，继续点击变化点（倒计时重置 {timeout_s:.0f}s）",
                "INFO",
            )
            continue

        time.sleep(0.02)

    if result is not None:
        rtx, rty, rids = result
        runner._current_pos = (rtx, rty)
        return NieoEntryResult(kind="success", tx=rtx, ty=rty, pet_ids=rids)

    entry_result = None
    if runner._nieo_calibration_records:
        entry_result = runner._nieo_calibration_records[-1].get("entry_result")
        if entry_result is None:
            runner._nieo_calibration_records[-1]["entry_result"] = "timeout"
            entry_result = "timeout"  # 同步更新本地变量，否则下方 == "timeout" 永远 False
            runner._emit(
                f"⏱️ 点击超时（首次点击后 {timeout_s:.0f}s 未检测到 fightPet/PetItem）",
                "WARN",
            )

    if entry_result == "timeout":
        runner._nieo_consecutive_entry_failures += 1
        runner._emit(
            f"📊 [入战失败计数] 连续入战失败次数：{runner._nieo_consecutive_entry_failures}",
            "INFO",
        )
        if runner._nieo_consecutive_entry_failures >= 3:
            if stop_event.is_set() or getattr(runner.bot, "stop_current", False):
                return NieoEntryResult(kind="failed")
            runner._emit(
                f"⚠️ 连续{runner._nieo_consecutive_entry_failures}次入战失败，执行重连",
                "WARN",
            )
            if pem:
                if runner._check_pure_energy_reconnect_condition(
                    use_foreground, stop_event, force_reconnect=True
                ):
                    return NieoEntryResult(kind="reconnect")
            elif runner._check_nieo_reconnect_condition(
                use_foreground, stop_event, force_reconnect=True
            ):
                return NieoEntryResult(kind="reconnect")
            return NieoEntryResult(kind="reconnect")

    if runner._should_abort_main_loop_for_reconnect(stop_event):
        return NieoEntryResult(kind="reconnect")

    configured = getattr(runner, "_configured_nieo_profile", None) is not None
    sw_to = runner._neighbor_switch_twomap_after_fail(
        pem=pem or configured,
        current_map_id=current_map_id,
        current_prefix=current_prefix,
        use_foreground=use_foreground,
        stop_event=stop_event,
    )
    return NieoEntryResult(kind="failed", next_map_id=sw_to)
