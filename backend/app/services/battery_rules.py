"""蓄电池组唯一判定口径。

容量/内阻结论、许可范围校验、状态流转方向全部收在这里，
服务层、路由层、存量重判都只能引用这一处，不允许各写一份判法。
"""
from __future__ import annotations

from typing import Any

# 更换状态只能沿这个序列单向往下走，不许倒回。
STATUS_ORDER = ["容量合格", "容量下降", "需更换", "已更换"]

# 每个当前状态允许执行的唯一动作（正好推进到下一状态）。
NEXT_ACTION = {
    "容量合格": "记录下降",
    "容量下降": "安排更换",
    "需更换": "完成更换",
}
ACTION_RULES = {
    "记录下降": "容量下降",
    "安排更换": "需更换",
    "完成更换": "已更换",
}
FINAL_STATUS = "已更换"

REQUIRED_FIELDS = ["电池组编号", "电池类型", "额定容量", "内阻值"]

# 许可范围：超范围一律不许存。数值允许带单位（Ah / mΩ）。
RATED_CAPACITY_MIN_AH = 10.0
RATED_CAPACITY_MAX_AH = 3000.0
RESISTANCE_MIN_MOHM = 0.0
RESISTANCE_MAX_MOHM = 100.0

# 结论阈值：内阻值单位 mΩ；容量比 = 实测容量 / 额定容量。
RESISTANCE_GOOD_MAX = 12.0
RESISTANCE_DROP_MAX = 25.0
CAPACITY_RATIO_GOOD = 0.8
CAPACITY_RATIO_DROP = 0.6


def parse_number(value: Any) -> float | None:
    """把带单位或空白的输入解析成数字；空值或无法解析时返回 None，绝不猜成 0。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    # 去掉常见单位尾巴与千分位；只取数字本体，避免「abc」被当成 0。
    for unit in ("mΩ", "mOhm", "毫欧", "Ah", "ah", "aH"):
        if text.endswith(unit):
            text = text[: -len(unit)].strip()
            break
    text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _index(status: str | None) -> int:
    return STATUS_ORDER.index(status) if status in STATUS_ORDER else -1


def resistance_conclusion(resistance_mohm: float) -> str:
    """按内阻值给出容量结论。"""
    if resistance_mohm <= RESISTANCE_GOOD_MAX:
        return "容量合格"
    if resistance_mohm <= RESISTANCE_DROP_MAX:
        return "容量下降"
    return "需更换"


def ratio_conclusion(capacity_ratio: float) -> str:
    """按实测容量占额定容量的比例给出容量结论。"""
    if capacity_ratio >= CAPACITY_RATIO_GOOD:
        return "容量合格"
    if capacity_ratio >= CAPACITY_RATIO_DROP:
        return "容量下降"
    return "需更换"


def assess(resistance_mohm: float | None, capacity_ratio: float | None = None) -> str:
    """根据最近一次实测推导更换结论；两项都在时取更差的一档。"""
    candidates: list[str] = []
    if resistance_mohm is not None:
        candidates.append(resistance_conclusion(resistance_mohm))
    if capacity_ratio is not None:
        candidates.append(ratio_conclusion(capacity_ratio))
    if not candidates:
        # 实测内阻缺失不许再记成合格，按最差结论处理并要求补测。
        return "需更换"
    return STATUS_ORDER[max(_index(item) for item in candidates)]


def validate_admission(values: dict[str, Any]) -> list[str]:
    """入库许可校验：必填、可解析、许可范围，逐项点名，不合规不许存。"""
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        if not str(values.get(field) or "").strip():
            errors.append(f"缺少必填字段：{field}")

    raw_rated = str(values.get("额定容量") or "").strip()
    if raw_rated:
        rated = parse_number(raw_rated)
        if rated is None:
            errors.append("额定容量不是有效数值，无法判定许可范围")
        elif not (
            RATED_CAPACITY_MIN_AH <= rated <= RATED_CAPACITY_MAX_AH
        ):
            errors.append(
                f"额定容量 {rated:g}Ah 超出许可范围"
                f"（{RATED_CAPACITY_MIN_AH:g}~{RATED_CAPACITY_MAX_AH:g}Ah），不予登记"
            )

    raw_resistance = str(values.get("内阻值") or "").strip()
    if raw_resistance:
        resistance = parse_number(raw_resistance)
        if resistance is None:
            errors.append("内阻值不是有效数值，无法判定许可范围")
        elif not (RESISTANCE_MIN_MOHM < resistance <= RESISTANCE_MAX_MOHM):
            errors.append(
                f"内阻值 {resistance:g}mΩ 超出许可范围"
                f"（{RESISTANCE_MIN_MOHM:g}~{RESISTANCE_MAX_MOHM:g}mΩ），不予登记"
            )

    ratio = values.get("容量比")
    if str(ratio or "").strip():
        parsed_ratio = parse_number(ratio)
        if parsed_ratio is None:
            errors.append("容量比不是有效数值")
        elif not 0 < parsed_ratio <= 1:
            errors.append("容量比超出许可范围（0~1 之间），不予登记")

    return errors


def validate_measurement(values: dict[str, Any]) -> list[str]:
    """登记现场实测时的许可校验：内阻值必填、可解析、在许可范围内。"""
    errors: list[str] = []
    raw_resistance = str(values.get("内阻值") or "").strip()
    if not raw_resistance:
        errors.append("缺少必填字段：内阻值")
        return errors
    resistance = parse_number(raw_resistance)
    if resistance is None:
        errors.append("内阻值不是有效数值，无法判定许可范围")
    elif not (RESISTANCE_MIN_MOHM < resistance <= RESISTANCE_MAX_MOHM):
        errors.append(
            f"内阻值 {resistance:g}mΩ 超出许可范围"
            f"（{RESISTANCE_MIN_MOHM:g}~{RESISTANCE_MAX_MOHM:g}mΩ），不予登记"
        )

    raw_ratio = str(values.get("容量比") or "").strip()
    if raw_ratio:
        parsed_ratio = parse_number(raw_ratio)
        if parsed_ratio is None:
            errors.append("容量比不是有效数值")
        elif not 0 < parsed_ratio <= 1:
            errors.append("容量比超出许可范围（0~1 之间），不予登记")
    return errors


def allowed_action(status: str | None) -> str | None:
    """当前状态唯一可执行的动作；已更换或状态非法时不允许任何动作。"""
    return NEXT_ACTION.get(status or "")


def advance_only(current: str | None, target: str) -> tuple[bool, str]:
    """状态只能沿序列往后走、且一次只能推进一格；倒回/跳级都拦下。"""
    cur_idx, target_idx = _index(current), _index(target)
    if cur_idx < 0:
        return False, f"当前状态「{current}」不在允许的状态序列里"
    if target_idx < 0:
        return False, f"目标状态「{target}」不在允许的状态序列里"
    if target_idx <= cur_idx:
        return False, f"更换状态只能从{STATUS_ORDER[0]}单向推进到{FINAL_STATUS}，不允许倒回"
    if target_idx != cur_idx + 1:
        return False, "一次只能推进到下一状态，不允许跳级"
    return True, ""
