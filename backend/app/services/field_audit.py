"""字段审计台账：沉淀元数据服务与湖仓血缘对可追溯字段清单的核对结果。

台账按「表 + 字段版本 + 来源」幂等写入：重复核对只更新最后一次结果，不会堆出多份
互相矛盾的记录。元数据服务在注册、刷新、注销后写入；湖仓血缘在声明兼容目标并
引用已有字段清单后写入，两边用同一份字段指纹比对，结果保持一致。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from app.store import store

LEDGER = "field_audit_ledger"
SOURCE_METADATA = "元数据服务"
SOURCE_LINEAGE = "湖仓血缘"
RESULT_OK = "一致"
RESULT_MISMATCH = "不一致"
RESULT_PENDING = "待核对"
TARGET_DECLARED = "已声明兼容目标"


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rows() -> list[dict[str, Any]]:
    return store.aux.setdefault(LEDGER, [])


def _next_id() -> int:
    return max((int(row.get("id", 0)) for row in _rows()), default=0) + 1


def _next_code() -> str:
    return f"AUDT-{len(_rows()) + 1:04d}"


def declare_target(
    *,
    entry_id: int,
    table_no: str,
    table_name: str,
    version: str,
    fingerprint: str | None,
    source: str,
    actor: str = "system",
) -> dict[str, Any]:
    """先声明兼容目标：登记本次要以哪个版本、哪份可追溯字段清单为核对口径。"""
    with store.lock:
        row = _upsert(
            entry_id=entry_id,
            version=version,
            source=source,
            base={
                "表编号": table_no,
                "表名称": table_name,
                "兼容目标": "可追溯字段清单",
                "目标字段版本": version,
                "目标字段指纹": fingerprint,
                "声明状态": TARGET_DECLARED,
                "声明时间": now_text(),
                "操作人": actor,
            },
        )
        row["兼容目标"] = "可追溯字段清单"
        row["目标字段版本"] = version
        row["目标字段指纹"] = fingerprint
        row["声明状态"] = TARGET_DECLARED
        row["声明时间"] = now_text()
        return deepcopy(row)


def record_check(
    *,
    entry_id: int,
    table_no: str,
    table_name: str,
    version: str | None,
    fingerprint: str | None,
    fields: list[dict[str, Any]] | None,
    source: str,
    result: str,
    detail: str,
    deregistered: bool = False,
    current: bool = False,
    actor: str = "system",
) -> dict[str, Any]:
    """写入/更新字段审计台账中的一条核对结果。"""
    with store.lock:
        version = version or "未指定"
        row = _upsert(
            entry_id=entry_id,
            version=version,
            source=source,
            base={
                "表编号": table_no,
                "表名称": table_name,
                "数据来源": source,
                "兼容目标": "可追溯字段清单",
            },
        )
        row.update(
            {
                "表编号": table_no,
                "表名称": table_name,
                "字段版本": version,
                "字段指纹": fingerprint,
                "字段数": len(fields or []),
                "字段清单": deepcopy(fields or []),
                "字段摘要": "、".join(
                    str(field.get("name") or field.get("字段名") or "") for field in (fields or [])
                ),
                "数据来源": source,
                "兼容目标": "可追溯字段清单",
                "校验结果": result,
                "校验说明": detail,
                "注销状态": "已注销" if deregistered else "未注销",
                "当前版本": bool(current),
                "记录状态": RESULT_PENDING if result == RESULT_PENDING else "已核对",
                "更新时间": now_text(),
                "操作人": actor,
            }
        )
        row.setdefault("创建时间", now_text())
        return deepcopy(row)


def _upsert(*, entry_id: int, version: str, source: str, base: dict[str, Any]) -> dict[str, Any]:
    for row in _rows():
        if (
            int(row.get("entry_id", 0)) == int(entry_id)
            and row.get("字段版本") == version
            and row.get("数据来源") == source
        ):
            return row
    row = {
        "id": _next_id(),
        "台账编号": _next_code(),
        "entry_id": int(entry_id),
        **base,
        "字段版本": version,
        "数据来源": source,
        "校验结果": RESULT_PENDING,
        "校验说明": "",
        "注销状态": "未注销",
        "当前版本": False,
        "记录状态": RESULT_PENDING,
        "创建时间": now_text(),
        "更新时间": now_text(),
    }
    _rows().append(row)
    return row


def list_records(
    *,
    entry_id: int | None = None,
    version: str | None = None,
    source: str | None = None,
    result: str | None = None,
    table_name: str | None = None,
) -> list[dict[str, Any]]:
    with store.lock:
        rows = _rows()
        if entry_id is not None:
            rows = [row for row in rows if int(row.get("entry_id", 0)) == int(entry_id)]
        if version:
            rows = [row for row in rows if row.get("字段版本") == version]
        if source:
            rows = [row for row in rows if row.get("数据来源") == source]
        if result:
            rows = [row for row in rows if row.get("校验结果") == result]
        if table_name:
            rows = [row for row in rows if row.get("表名称") == table_name]
        return deepcopy(rows)


def consistency(entry_id: int, version: str | None = None) -> dict[str, Any]:
    """比对同一表同一版本在元数据与血缘两侧的核对结果。"""
    with store.lock:
        metadata_rows = list_records(entry_id=entry_id, source=SOURCE_METADATA)
        if not metadata_rows:
            return {
                "consistent": False,
                "result": RESULT_PENDING,
                "detail": "字段审计台账中缺少元数据服务侧的核对结果",
                "版本": version,
            }
        if version:
            latest_meta = next(
                (row for row in reversed(metadata_rows) if row.get("字段版本") == version),
                None,
            )
            if latest_meta is None:
                return {
                    "consistent": False,
                    "result": RESULT_PENDING,
                    "detail": f"字段审计台账中缺少版本 {version} 的元数据核对结果",
                    "版本": version,
                }
        else:
            # 默认以血缘实际引用的字段版本为口径：血缘可以合法引用历史版本，
            # 两侧核对的是同一份可追溯字段清单。失败的引用尝试会留下「待核对」
            # 记录，不作为默认口径。
            lineage_rows = list_records(entry_id=entry_id, source=SOURCE_LINEAGE)
            checked = [
                row for row in lineage_rows if row.get("校验结果") != RESULT_PENDING
            ]
            target = (checked or lineage_rows or [{}])[-1].get("字段版本")
            if target:
                latest_meta = next(
                    (row for row in reversed(metadata_rows) if row.get("字段版本") == target),
                    None,
                )
            else:
                latest_meta = next(
                    (row for row in reversed(metadata_rows) if row.get("当前版本")),
                    metadata_rows[-1],
                )
            if latest_meta is None:
                return {
                    "consistent": False,
                    "result": RESULT_PENDING,
                    "detail": "字段审计台账中的字段版本口径未对齐",
                    "版本": target,
                }
        target_version = latest_meta.get("字段版本")
        lineage_rows = list_records(
            entry_id=entry_id, version=str(target_version), source=SOURCE_LINEAGE
        )
        if not lineage_rows:
            return {
                "consistent": False,
                "result": RESULT_PENDING,
                "detail": "字段审计台账中缺少湖仓血缘侧的核对结果",
                "版本": target_version,
            }
        latest_lineage = lineage_rows[-1]
        same_fingerprint = latest_meta.get("字段指纹") == latest_lineage.get("字段指纹")
        same_result = latest_meta.get("校验结果") == latest_lineage.get("校验结果")
        consistent = bool(same_fingerprint and same_result and latest_meta.get("校验结果") == RESULT_OK)
        return {
            "consistent": consistent,
            "result": RESULT_OK if consistent else RESULT_MISMATCH,
            "detail": "元数据服务与湖仓血缘的可追溯字段清单口径一致"
            if consistent
            else "两侧字段清单或核对结果不一致",
            "版本": target_version,
            "元数据结果": latest_meta.get("校验结果"),
            "血缘结果": latest_lineage.get("校验结果"),
            "元数据字段指纹": latest_meta.get("字段指纹"),
            "血缘字段指纹": latest_lineage.get("字段指纹"),
            "注销状态": latest_lineage.get("注销状态"),
        }


def mark_deregistered(entry_id: int) -> None:
    """表注销后同步台账：保留记录与字段清单，只把注销状态改为已注销。"""
    with store.lock:
        for row in _rows():
            if int(row.get("entry_id", 0)) != int(entry_id):
                continue
            row["注销状态"] = "已注销"
            row["更新时间"] = now_text()


def clear() -> None:
    """测试辅助：清空台账。"""
    with store.lock:
        _rows().clear()
