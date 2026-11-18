"""湖仓血缘追踪接口：维护血缘记录，覆盖关联血缘、标记过期、清理记录等动作。

血缘记录只引用一份完整的字段定义：先声明可追溯字段清单的兼容目标，再读取已有
字段清单，并把字段版本、字段快照与注销状态固化到记录上。记录明细支持按
``?version=`` 回查注销前最后一次有效字段清单。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services import field_audit
from app.services.lineage import LineageService

router = APIRouter(prefix="/api/lineage", tags=["湖仓血缘追踪"])

service = LineageService()

LIST_FIELDS = ["记录编号", "上游表名", "下游表名", "转换类型", "字段映射", "操作时间", "操作批次", "记录状态"]
STATUSES = ["已记录", "已关联", "已过期", "已清理"]


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出湖仓血缘追踪清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "lineage", "total": total, "items": items}


@router.get("/audit")
def list_audit(
    entry_id: int | None = Query(default=None, description="按元数据表 ID 过滤"),
    version: str | None = Query(default=None, description="按字段版本过滤"),
    result: str | None = Query(default=None, description="一致、不一致、待核对"),
    table_name: str | None = Query(default=None, description="按表名称过滤"),
) -> dict[str, Any]:
    """字段审计台账：查看湖仓血缘侧的可追溯字段清单核对结果。"""
    records = field_audit.list_records(
        entry_id=entry_id,
        version=version,
        source=field_audit.SOURCE_LINEAGE,
        result=result,
        table_name=table_name,
    )
    return {"items": records, "total": len(records)}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按记录编号、上游表名或下游表名检索"),
    status: str | None = Query(default=None, description="已记录、已关联、已过期、已清理"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按记录编号与状态过滤湖仓血缘追踪列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(
    entry_id: int,
    version: str | None = Query(default=None, description="回查指定字段版本，含注销前最后有效版本"),
) -> dict:
    """读取单条血缘记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id, version=version)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"血缘记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条血缘记录，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="血缘记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条血缘记录执行关联血缘、标记过期、清理记录、重试引用。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
