"""蓄电池组业务规则收口：入库许可、实测留底、状态流转、存量重判。

判定口径只允许来自 battery_rules，本模块负责落到内存仓库，
列表、详情、动作接口共用同一份同步逻辑，杜绝两处各算各的。
"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.battery_rules import (
    ACTION_RULES,
    FINAL_STATUS,
    STATUS_ORDER,
    advance_only,
    allowed_action,
    assess,
    parse_number,
    validate_admission,
    validate_measurement,
)
from app.store import store

MODULE = "battery"
RULE_VERSION = 2

DISPLAY_FIELDS = [
    "电池组编号", "电池类型", "额定容量", "所属站点",
    "放电时长", "内阻值", "投用日期",
]


def _today() -> str:
    return datetime.date.today().isoformat()


def latest_measurement(entry: dict[str, Any]) -> dict[str, Any] | None:
    """取最近一次实测留底（列表按时间追加，末条即最新）。"""
    records = entry.get("measurements") or []
    return records[-1] if records else None


def sync_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """把统一口径刷到列表/详情共用字段上：结论一处算，两处必然一致。

    结论以最近一次实测为准（实测发现更差档位时立即采纳）；
    同时工作流状态只许沿序列推进不许倒回，因此取「当前工作流档位」与
    「最新实测档位」中更靠后的一个，已更换保持终态。
    """
    measured = latest_measurement(entry)
    current = entry.get("status") if entry.get("status") in STATUS_ORDER else STATUS_ORDER[0]

    if current == FINAL_STATUS:
        status = FINAL_STATUS
    elif measured is not None and measured.get("conclusion") in STATUS_ORDER:
        status = STATUS_ORDER[max(
            STATUS_ORDER.index(current),
            STATUS_ORDER.index(measured["conclusion"]),
        )]
    else:
        status = current

    entry["status"] = status
    # 列表读「电池状态」列，详情读 status，两处指向同一个值，不会再打架。
    entry["电池状态"] = status
    entry["pending"] = status != FINAL_STATUS
    entry["abnormal"] = status in ("容量下降", "需更换")

    if measured is not None:
        resistance = measured.get("resistance")
        if isinstance(resistance, (int, float)):
            entry["内阻值"] = f"{resistance:g}mΩ"
            entry["最近实测日期"] = measured.get("measured_at", "")
        else:
            raw = str(entry.get("内阻值") or "").strip()
            entry["内阻值"] = f"{raw or '未记录'}（缺有效实测，待补测）"
            entry["最近实测日期"] = measured.get("measured_at", "")
    return entry


class BatteryService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [sync_entry(dict(row)) for row in store.rows(MODULE)]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("电池组编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return sync_entry(dict(entry)) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        # 内阻和额定容量过了许可范围一律不许存，并点名超的是哪一项。
        errors = validate_admission(values)
        if errors:
            return None, errors

        rows = store.rows(MODULE)
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        for field in DISPLAY_FIELDS:
            entry[field] = str(values.get(field) or "").strip()

        resistance = parse_number(values.get("内阻值"))
        ratio = parse_number(values.get("容量比"))
        conclusion = assess(resistance, ratio)
        entry["measurements"] = [{
            "measured_at": str(values.get("实测日期") or _today()).strip(),
            "resistance": resistance,
            "rated_capacity": parse_number(values.get("额定容量")),
            "capacity_ratio": ratio,
            "conclusion": conclusion,
            "source": "登记实测",
        }]
        entry["rule_version"] = RULE_VERSION
        # 新组没有走过任何工作流动作，结论完全以首次实测为准。
        entry["status"] = conclusion
        rows.append(entry)
        return sync_entry(dict(entry)), []

    def add_measurement(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """登记最近一次现场实测：内阻留底，冲突结论以这次实测为准（不许倒回）。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, [f"蓄电池组 {entry_id} 不存在或已归档"]
        sync_entry(entry)
        if entry["status"] == FINAL_STATUS:
            return None, ["该蓄电池组已完成更换，不再接收新的实测结论"]

        # 实测同样走许可范围：内阻缺失或越界一律不收，并点名超的是哪一项。
        errors = validate_measurement(values)
        if errors:
            return None, errors

        resistance = parse_number(values.get("内阻值"))
        ratio = parse_number(values.get("容量比"))
        conclusion = assess(resistance, ratio)
        entry.setdefault("measurements", []).append({
            "measured_at": str(values.get("实测日期") or _today()).strip(),
            "resistance": resistance,
            "rated_capacity": parse_number(entry.get("额定容量")),
            "capacity_ratio": ratio,
            "conclusion": conclusion,
            "source": str(values.get("来源") or "现场实测"),
        })
        entry["rule_version"] = RULE_VERSION
        return sync_entry(dict(entry)), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"蓄电池组 {entry_id} 不存在或已归档"
        sync_entry(entry)
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于蓄电池组可执行范围"

        expected = allowed_action(entry["status"])
        if expected is None:
            return None, "该蓄电池组已更换，流程已终结，不允许再操作"
        if action != expected:
            return None, (
                f"当前状态为「{entry['status']}」，只允许执行「{expected}」；"
                "更换状态只能单向推进，不允许倒回或跳级"
            )

        ok, message = advance_only(entry["status"], ACTION_RULES[action])
        if not ok:
            return None, message
        entry["status"] = ACTION_RULES[action]
        entry["rule_version"] = RULE_VERSION
        return sync_entry(dict(entry)), f"蓄电池组已{action}"

    def rejudge_all(self) -> dict[str, int]:
        """存量数据按新口径重判一遍，并把当初实测的内阻留底。

        - 已经是「已更换」的：只修列表字段和待处理标记，保持终态，不再回待更换名单；
        - 内阻值能解析且在许可范围内：归档为一条实测留底，按新口径给结论；
        - 内阻缺失/无法解析/超范围的：不许再记成合格，置「需更换」并标异常、附说明，待补测。
        """
        migrated = kept = rejected = 0
        for entry in store.rows(MODULE):
            if entry.get("rule_version") == RULE_VERSION:
                continue
            raw_resistance = str(entry.get("内阻值") or "").strip()
            resistance = parse_number(raw_resistance)
            rated = parse_number(entry.get("额定容量"))
            note = None
            record: dict[str, Any]

            if entry.get("status") == FINAL_STATUS:
                # 终态不再重判结论，但当初的内阻照样留底。
                record = {
                    "measured_at": str(entry.get("投用日期") or _today()),
                    "resistance": resistance,
                    "rated_capacity": rated,
                    "capacity_ratio": None,
                    "conclusion": FINAL_STATUS,
                    "source": "存量重判",
                    "raw": raw_resistance,
                }
                entry.setdefault("measurements", []).append(record)
                kept += 1
            elif resistance is None or not (0 < resistance <= 100):
                # 旧系统把空内阻/非法内阻也记成了合格，新口径一律拦下，不许存成合格。
                record = {
                    "measured_at": _today(),
                    "resistance": None,
                    "rated_capacity": rated,
                    "capacity_ratio": None,
                    "conclusion": "需更换",
                    "source": "存量重判",
                    "raw": raw_resistance,
                }
                entry.setdefault("measurements", []).append(record)
                entry["status"] = "需更换"
                note = "存量内阻记录缺失或超出许可范围，已按新口径置为需更换，请补测内阻值"
                rejected += 1
            else:
                record = {
                    "measured_at": str(entry.get("投用日期") or _today()),
                    "resistance": resistance,
                    "rated_capacity": rated,
                    "capacity_ratio": None,
                    "conclusion": assess(resistance),
                    "source": "存量重判",
                    "raw": raw_resistance,
                }
                entry.setdefault("measurements", []).append(record)
                migrated += 1

            if note:
                entry["重判说明"] = note
            entry["rule_version"] = RULE_VERSION
            sync_entry(entry)
        return {"migrated": migrated, "kept": kept, "rejected": rejected}
