"""蓄电池组业务规则：许可范围校验、更换结论判定、状态流转、存量重判全部收口在这一处。

历史上列表、详情、动作接口各写了一套判法，结论互相打架；现在任何地方拿到的
结论都只能来自 :meth:`BatteryService._sync` —— 判据只有这一套：

1. 额定容量、内阻值超出许可范围一律不许入库，并在错误信息里点名是哪一项；
2. 更换结论取「当前流转状态」与「最近一次实测结论」中更靠后的一档
   （同一组电池列表与详情因此永远一致，且最近一次实测可以把结论向前顶）；
3. 状态只能沿 容量合格→容量下降→需更换→已更换 顺向流转，不许倒回、跨级；
4. 每次实测的内阻值都进 ``实测留底``，不覆盖历史。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.store import store

MODULE = "battery"
REQUIRED_FIELDS = ["电池组编号", "电池类型", "额定容量", "内阻值"]
OPTIONAL_FIELDS = ["所属站点", "放电时长", "投用日期"]

# 状态只能沿该序列顺向流转。
STATUS_ORDER = ["容量合格", "容量下降", "需更换", "已更换"]
ACTION_RULES = {"记录下降": "容量下降", "安排更换": "需更换", "完成更换": "已更换"}
REPLACE_WAITING = "需更换"

# 入库许可范围：额定容量单位 Ah，内阻值单位 mΩ。
RATED_CAPACITY_MIN = 1.0
RATED_CAPACITY_MAX = 5000.0
RESISTANCE_MIN = 0.0      # 内阻必须为正数，空着或零值都不许记合格
RESISTANCE_MAX = 50.0     # 超过 50mΩ 一律不许入库

# 最近一次实测的判定阈值（容量、内阻共用同一套判据，取更差的一档）：
RESISTANCE_DROP_AT = 10.0   # 内阻 ≥ 10mΩ：容量下降
RESISTANCE_FAIL_AT = 20.0   # 内阻 ≥ 20mΩ：需更换
CAPACITY_DROP_RATIO = 0.8   # 实测容量/额定容量 < 80%：容量下降
CAPACITY_FAIL_RATIO = 0.6   # 实测容量/额定容量 < 60%：需更换

READING_ARCHIVE = "实测留底"


def _as_float(value: Any) -> float | None:
    """把录入值解析成数值；空串、非数值一律返回 None，交由调用方点名驳回。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class BatteryService:
    # ---------- 读取：列表与详情走同一个序列化口径 ----------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        synced = [self._sync(entry) for entry in rows]
        if keyword:
            synced = [row for row in synced if keyword in str(row.get("电池组编号", ""))]
        if status:
            # 业务口径里常说的「待更换」即「需更换」
            wanted = REPLACE_WAITING if status == "待更换" else status
            synced = [row for row in synced if row["status"] == wanted]
        total = len(synced)
        start = max(page - 1, 0) * size
        return synced[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return self._sync(entry)

    # ---------- 登记：许可范围校验，超哪一项点哪一项 ----------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, [f"缺少必填字段：{name}" for name in missing]

        rated, resistance, measured_capacity, errors = self._validate_reading(values)
        if errors:
            return None, errors

        rows = store.rows(MODULE)
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        for field in [*REQUIRED_FIELDS, *OPTIONAL_FIELDS]:
            if str(values.get(field) or "").strip():
                entry[field] = values.get(field)
        entry["额定容量"] = rated
        entry["内阻值"] = resistance
        if measured_capacity is not None:
            entry["实测容量"] = measured_capacity
        entry[READING_ARCHIVE] = [{
            "实测日期": self._reading_date(values.get("投用日期")),
            "内阻值": resistance,
            "实测容量": measured_capacity,
        }]
        # 初始结论完全由首次实测决定
        entry["status"] = self._conclude(resistance, rated, measured_capacity)
        rows.append(entry)
        return self._sync(entry), []

    # ---------- 实测录入：以最近一次实测为准，历史内阻留底 ----------

    def record_measurement(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"蓄电池组 {entry_id} 不存在或已归档"

        reading = {
            "额定容量": entry.get("额定容量"),
            "内阻值": values.get("内阻值"),
            "实测容量": values.get("实测容量"),
        }
        rated, resistance, measured_capacity, errors = self._validate_reading(reading)
        if errors:
            return None, "；".join(errors)

        entry["内阻值"] = resistance
        if measured_capacity is not None:
            entry["实测容量"] = measured_capacity
        entry.setdefault(READING_ARCHIVE, []).append({
            "实测日期": self._reading_date(values.get("实测日期")),
            "内阻值": resistance,
            "实测容量": measured_capacity,
        })
        self._sync(entry)
        return entry, f"实测内阻 {resistance:g}mΩ 已留底，最新结论：{entry['status']}"

    # ---------- 状态流转：只能顺向往下一步走 ----------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"蓄电池组 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于蓄电池组可执行范围"

        self._sync(entry)
        current_idx = STATUS_ORDER.index(entry["status"])
        target_idx = STATUS_ORDER.index(ACTION_RULES[action])
        chain = "→".join(STATUS_ORDER)
        if target_idx <= current_idx:
            return None, f"状态只能沿「{chain}」顺向流转，当前为「{entry['status']}」，不能{action}倒回"
        if target_idx > current_idx + 1:
            return None, f"状态不能跨级流转，请先执行到中间状态（{chain}）"

        entry["status"] = STATUS_ORDER[target_idx]
        self._sync(entry)
        return entry, f"蓄电池组已{action}"

    # ---------- 存量重判：新口径上线后把旧数据整体过一遍 ----------

    def rejudge_existing(self) -> int:
        """按统一判据重判全部存量记录，并把当初实测的内阻留进实测档案。"""
        count = 0
        for entry in store.rows(MODULE):
            if READING_ARCHIVE not in entry:
                resistance = _as_float(entry.get("内阻值"))
                if resistance is not None and RESISTANCE_MIN < resistance <= RESISTANCE_MAX:
                    entry[READING_ARCHIVE] = [{
                        "实测日期": self._reading_date(entry.get("投用日期")),
                        "内阻值": resistance,
                        "实测容量": _as_float(entry.get("实测容量")),
                    }]
                # 内阻为空/非数值/越界的情况不伪造留底，由 _sync 点名。
            self._sync(entry)
            count += 1
        return count

    # ---------- 统一判据：所有结论只允许从这里产出 ----------

    def _validate_reading(
        self, values: dict[str, Any]
    ) -> tuple[float | None, float | None, float | None, list[str]]:
        """校验额定容量/内阻值/实测容量，返回 (额定容量, 内阻, 实测容量, 错误清单)。"""
        errors: list[str] = []

        rated = _as_float(values.get("额定容量"))
        if rated is None:
            errors.append(f"额定容量不是有效数值：{values.get('额定容量')!r}")
        elif not RATED_CAPACITY_MIN <= rated <= RATED_CAPACITY_MAX:
            errors.append(
                f"额定容量超出许可范围"
                f"（{RATED_CAPACITY_MIN:g}~{RATED_CAPACITY_MAX:g} Ah）：{rated:g}"
            )

        raw_resistance = values.get("内阻值")
        resistance = _as_float(raw_resistance)
        if resistance is None:
            shown = "为空" if str(raw_resistance or "").strip() == "" else f"为 {raw_resistance!r}"
            errors.append(
                f"内阻值{shown}，须填写大于 {RESISTANCE_MIN:g} 且不超过 "
                f"{RESISTANCE_MAX:g} mΩ 的有效数值"
            )
        elif not RESISTANCE_MIN < resistance <= RESISTANCE_MAX:
            errors.append(
                f"内阻值超出许可范围（大于 {RESISTANCE_MIN:g} 且不超过 "
                f"{RESISTANCE_MAX:g} mΩ）：{resistance:g}"
            )

        measured_capacity: float | None = None
        raw_capacity = values.get("实测容量")
        if str(raw_capacity or "").strip():
            measured_capacity = _as_float(raw_capacity)
            if measured_capacity is None or measured_capacity <= 0:
                errors.append(f"实测容量不是有效正数：{raw_capacity!r}")

        return rated, resistance, measured_capacity, errors

    @staticmethod
    def _conclude(resistance: float, rated: float | None, measured_capacity: float | None) -> str:
        """按最近一次实测推出 容量合格/容量下降/需更换（已更换不在此处理）。"""
        level = 0
        if resistance >= RESISTANCE_FAIL_AT:
            level = 2
        elif resistance >= RESISTANCE_DROP_AT:
            level = 1
        if measured_capacity is not None and rated:
            ratio = measured_capacity / rated
            if ratio < CAPACITY_FAIL_RATIO:
                level = max(level, 2)
            elif ratio < CAPACITY_DROP_RATIO:
                level = max(level, 1)
        return STATUS_ORDER[level]

    def _measured_level(self, entry: dict[str, Any]) -> tuple[int, list[str]]:
        """最近一次实测推出的结论档位；数据不合规时点名并保底放到容量下降。"""
        archive = entry.get(READING_ARCHIVE) or []
        latest = archive[-1] if archive else {"内阻值": entry.get("内阻值"), "实测容量": entry.get("实测容量")}
        resistance = _as_float(latest.get("内阻值"))
        rated = _as_float(entry.get("额定容量"))
        measured_capacity = _as_float(latest.get("实测容量"))

        notes: list[str] = []
        if rated is None or not RATED_CAPACITY_MIN <= rated <= RATED_CAPACITY_MAX:
            notes.append(f"额定容量超出许可范围或不是有效数值：{entry.get('额定容量')!r}")
        if resistance is None:
            notes.append("内阻值缺失或不是有效数值，不能记为容量合格，待补测")
            return 1, notes
        if not RESISTANCE_MIN < resistance <= RESISTANCE_MAX:
            notes.append(
                f"内阻值超出许可范围（大于 {RESISTANCE_MIN:g} 且不超过 "
                f"{RESISTANCE_MAX:g} mΩ）：{resistance:g}"
            )
            # 越界值不许参与正常判档，直接挂到需更换
            return 2, notes

        level = STATUS_ORDER.index(self._conclude(resistance, rated, measured_capacity))
        return level, notes

    def _sync(self, entry: dict[str, Any]) -> dict[str, Any]:
        """把 entry 的结论统一重算并对齐所有派生字段（列表、详情共用）。"""
        measured_level, notes = self._measured_level(entry)
        current = entry.get("status")
        current_level = STATUS_ORDER.index(current) if current in STATUS_ORDER else 0
        # 最近一次实测可把结论向前顶，但流转状态不许倒回（已更换永不掉档）。
        level = max(current_level, measured_level)

        conclusion = STATUS_ORDER[level]
        entry["status"] = conclusion
        # 列表历史上读的是自由文本「电池状态」字段，直接对齐，杜绝两处挂两套结论。
        entry["电池状态"] = conclusion
        entry["pending"] = conclusion != STATUS_ORDER[-1]
        entry["abnormal"] = conclusion in ("容量下降", "需更换") or bool(notes)
        if notes:
            entry["重判说明"] = "；".join(notes)
        else:
            entry.pop("重判说明", None)
        return entry

    @staticmethod
    def _reading_date(value: Any) -> str:
        text = str(value or "").strip()
        if text:
            try:
                date.fromisoformat(text)
                return text
            except ValueError:
                pass
        return date.today().isoformat()
