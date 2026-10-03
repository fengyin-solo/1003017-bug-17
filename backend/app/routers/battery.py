"""蓄电池组接口：维护蓄电池组，覆盖实测登记、记录下降、安排更换、完成更换等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.battery import BatteryService

router = APIRouter(prefix="/api/battery", tags=["蓄电池组"])

service = BatteryService()

LIST_FIELDS = ["电池组编号", "电池类型", "额定容量", "所属站点", "放电时长", "内阻值", "投用日期", "电池状态"]
STATUSES = ["容量合格", "容量下降", "需更换", "已更换"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按电池组编号检索"),
    status: str | None = Query(default=None, description="容量合格、容量下降、需更换、已更换"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按电池组编号与状态过滤蓄电池组列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出蓄电池组清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "battery", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条蓄电池组明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"蓄电池组 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条蓄电池组；内阻或额定容量超出许可范围一律拒收，并点名超的是哪一项。"""
    entry, errors = service.create_entry(payload.values)
    if errors:
        return ActionResult(ok=False, message="；".join(errors))
    return ActionResult(ok=True, message="蓄电池组已登记", entry=entry)


@router.post("/{entry_id}/measurements", response_model=ActionResult)
def add_measurement(entry_id: int, payload: EntryPayload) -> ActionResult:
    """登记最近一次实测：内阻值留底；同一组结论冲突时以这次实测为准（状态不倒回）。"""
    entry, errors = service.add_measurement(entry_id, payload.values)
    if errors:
        return ActionResult(ok=False, message="；".join(errors))
    return ActionResult(ok=True, message="实测已登记，更换结论已按最近一次实测更新", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) ->ActionResult:
    """对单条蓄电池组执行动作；只能沿 容量合格→容量下降→需更换→已更换 单向推进。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
