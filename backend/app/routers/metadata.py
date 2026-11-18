"""元数据服务接口：维护元数据表，覆盖注册表、刷新元数据、注销表等动作。

列表返回当前字段版本摘要；明细可以按 ``?version=`` 回查历史字段版本，注销后的表
也能读到「最后有效字段清单」。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services import field_audit
from app.services.metadata import MetadataService

router = APIRouter(prefix="/api/metadata", tags=["元数据服务"])

service = MetadataService()

LIST_FIELDS = ["表编号", "表名称", "字段清单", "分区信息", "统计信息", "最后更新", "数据大小", "表状态"]
STATUSES = ["草稿", "已注册", "已过期", "已注销"]


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出元数据服务清单：返回当前过滤条件下的全量数据。"""
    items = service.export_entries()
    return {"module": "metadata", "total": len(items), "items": items}


@router.get("/audit")
def list_audit(
    entry_id: int | None = Query(default=None, description="按元数据表 ID 过滤"),
    version: str | None = Query(default=None, description="按字段版本过滤"),
    source: str | None = Query(default=None, description="元数据服务、湖仓血缘"),
    result: str | None = Query(default=None, description="一致、不一致、待核对"),
    table_name: str | None = Query(default=None, description="按表名称过滤"),
) -> dict[str, Any]:
    """字段审计台账：查看元数据服务与湖仓血缘对可追溯字段清单的核对结果。"""
    records = field_audit.list_records(
        entry_id=entry_id,
        version=version,
        source=source,
        result=result,
        table_name=table_name,
    )
    return {"items": records, "total": len(records)}


@router.get("/audit/consistency/{entry_id}")
def audit_consistency(
    entry_id: int,
    version: str | None = Query(default=None, description="只比对指定字段版本"),
) -> dict[str, Any]:
    """核对同一表同一字段版本在元数据侧与血缘侧的口径是否一致。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"元数据表 {entry_id} 不存在或已归档")
    return field_audit.consistency(entry_id, version)


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按表编号或表名称检索"),
    status: str | None = Query(default=None, description="草稿、已注册、已过期、已注销"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按表编号与状态过滤元数据服务列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}/versions")
def list_versions(entry_id: int) -> dict[str, Any]:
    """字段版本清单：当前版本 + 历史版本，历史版本保留给血缘引用。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"元数据表 {entry_id} 不存在或已归档")
    return {
        "entry_id": entry_id,
        "当前字段版本": entry.get("当前字段版本"),
        "最后有效字段版本": entry.get("最后有效字段版本"),
        "注销状态": entry.get("注销状态"),
        "items": entry.get("field_versions", []),
        "total": len(entry.get("field_versions", [])),
    }


@router.get("/{entry_id}/versions/{version_id}")
def get_version(entry_id: int, version_id: str) -> dict[str, Any]:
    """读取指定字段版本的完整字段清单；版本不存在时给出可读说明。"""
    entry = service.get_entry(entry_id, version_id=version_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"元数据表 {entry_id} 不存在或已归档")
    if entry.get("查看字段版本") != version_id:
        raise HTTPException(status_code=404, detail=f"字段版本 {version_id} 不存在")
    return entry


@router.get("/{entry_id}/audit")
def entry_audit(entry_id: int) -> dict[str, Any]:
    """单表字段审计台账，包含两侧核对与一致性结论。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"元数据表 {entry_id} 不存在或已归档")
    records = field_audit.list_records(entry_id=entry_id)
    return {
        "entry_id": entry_id,
        "当前字段版本": entry.get("当前字段版本"),
        "最后有效字段版本": entry.get("最后有效字段版本"),
        "注销状态": entry.get("注销状态"),
        "items": records,
        "total": len(records),
        "一致性": field_audit.consistency(entry_id, entry.get("当前字段版本") or entry.get("最后有效字段版本")),
    }


@router.get("/{entry_id}", response_model=dict)
def get_entry(
    entry_id: int,
    version: str | None = Query(default=None, description="回查指定字段版本"),
) -> dict:
    """读取单条元数据表明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id, version_id=version)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"元数据表 {entry_id} 不存在或已归档")
    if version and entry.get("查看字段版本") != version:
        raise HTTPException(status_code=404, detail=f"字段版本 {version} 不存在")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条元数据表，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="元数据表已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条元数据表执行注册表、刷新元数据、注销表、切换字段版本。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
