"""湖仓血缘追踪业务规则：状态流转、字段引用与筛选口径都收在这里。

血缘记录引用元数据表的可追溯字段清单时严格按两步走：
1. 先在字段审计台账中声明「可追溯字段清单」的兼容目标（表 + 字段版本）；
2. 声明成功后才读取已有字段清单，把字段版本、字段快照与注销状态固化到血缘记录上。

引用失败时只重试还没完成的清单项，血缘记录继续沿用上一稳定口径，不会被空清单覆盖。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services import field_audit
from app.services.field_registry import MODULE as METADATA_MODULE
from app.services.field_registry import field_registry, fields_fingerprint, normalize_fields
from app.store import store

MODULE = "lineage"
REQUIRED_FIELDS = ["记录编号", "上游表名", "下游表名"]
STATUS_ORDER = ["已记录", "已关联", "已过期", "已清理"]
ACTION_RULE = "关联血缘"
STABLE_KEYS = (
    "字段版本",
    "引用的字段版本",
    "字段清单",
    "引用字段清单",
    "字段指纹",
    "引用字段指纹",
    "注销状态",
    "引用注销状态",
    "引用字段摘要",
)


class LineageService:
    # ------------------------------------------------------------------ 初始化
    def ensure_init(self) -> None:
        """首次访问时给尚未引用的示例血缘记录补齐字段引用。"""
        field_registry.ensure_init()
        with store.lock:
            if store.aux.get("lineage_refs_ready"):
                return
            store.aux["lineage_refs_ready"] = True
            for entry in store.rows(MODULE):
                if entry.get("引用的字段版本"):
                    continue
                requested = (
                    str(entry.get("字段版本") or entry.get("引用建议版本") or "").strip() or None
                )
                self._reference_fields(entry, requested_version=requested, retry=False)

    # ------------------------------------------------------------------ 查询
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        self.ensure_init()
        self._sync_deregistered()
        with store.lock:
            rows = store.rows(MODULE)
            if keyword:
                rows = [
                    row
                    for row in rows
                    if keyword in str(row.get("记录编号", ""))
                    or keyword in str(row.get("上游表名", ""))
                    or keyword in str(row.get("下游表名", ""))
                ]
            if status:
                rows = [row for row in rows if row.get("status") == status]
            total = len(rows)
            start = max(page - 1, 0) * size
            return [deepcopy(row) for row in rows[start:start + size]], total

    def _sync_deregistered(self) -> None:
        """注销状态以元数据表为准，列表读取时同步到血缘引用。"""
        with store.lock:
            for metadata_entry in store.rows(METADATA_MODULE):
                if metadata_entry.get("status") == "已注销":
                    field_registry.sync_deregistered_references(metadata_entry)

    def get_entry(
        self,
        entry_id: int,
        *,
        version: str | None = None,
    ) -> dict[str, Any] | None:
        self.ensure_init()
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None
            payload = deepcopy(entry)
            self._sync_deregistered()
            resolved = self._resolve_source(entry)
            metadata_entry = resolved["entry"]
            if metadata_entry is not None:
                field_registry.ensure_entry(metadata_entry)
                field_registry.sync_deregistered_references(metadata_entry)
                versions = field_registry.versions(int(metadata_entry.get("id", 0)))
                payload["可回查字段版本"] = [
                    {
                        "version": item.get("version"),
                        "状态": item.get("status"),
                        "注销状态": item.get("注销状态"),
                        "当前版本": bool(item.get("current")),
                        "最后有效": bool(item.get("last_valid")),
                    }
                    for item in versions
                ]
                referenced = version or entry.get("字段版本") or entry.get("引用的字段版本")
                selected = field_registry.get_version(
                    int(metadata_entry.get("id", 0)), str(referenced) if referenced else None
                )
                if selected is None:
                    selected = field_registry.last_valid_version(int(metadata_entry.get("id", 0)))
                if selected is not None:
                    payload["回查字段版本"] = selected.get("version")
                    payload["回查字段清单"] = deepcopy(selected.get("字段清单", []))
                    payload["回查字段指纹"] = selected.get("字段指纹")
                    payload["回查注销状态"] = selected.get("注销状态")
                last_valid = field_registry.last_valid_version(int(metadata_entry.get("id", 0)))
                if last_valid is not None:
                    payload["最后有效字段版本"] = last_valid.get("version")
                    payload["最后有效字段清单"] = deepcopy(last_valid.get("字段清单", []))
                    payload["最后有效字段指纹"] = last_valid.get("字段指纹")
            return payload

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            rows = store.rows(MODULE)
            entry: dict[str, Any] = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "记录编号": str(values.get("记录编号") or "").strip(),
                "上游表名": str(values.get("上游表名") or "").strip(),
                "下游表名": str(values.get("下游表名") or "").strip(),
                "转换类型": values.get("转换类型") or "",
                "字段映射": values.get("字段映射") or "",
                "操作时间": values.get("操作时间") or "",
                "操作批次": values.get("操作批次") or "",
                "记录状态": STATUS_ORDER[0],
                "status": STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
                "引用状态": "待引用",
                "兼容目标": "可追溯字段清单",
            }
            rows.append(entry)
            # 登记即尝试一次引用；失败不阻断登记，只把未完成项留给重试。
            self._reference_fields(entry, retry=False)
            return deepcopy(entry), []

    # ------------------------------------------------------------------ 动作
    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        self.ensure_init()
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"血缘记录 {entry_id} 不存在或已归档"
            if action == ACTION_RULE:
                return self._link(entry, values)
            if action == "标记过期":
                return self._advance(entry, "已过期", "血缘记录已标记过期")
            if action == "清理记录":
                if entry.get("status") == "已清理" or entry.get("记录状态") == "已清理":
                    return deepcopy(entry), "血缘记录已清理"
                return self._advance(entry, "已清理", "血缘记录已清理")
            if action == "重试引用":
                return self._retry(entry)
            return None, f"动作「{action}」不属于湖仓血缘追踪可执行范围"

    def _link(
        self, entry: dict[str, Any], values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        requested = str(values.get("字段版本") or values.get("version") or "").strip() or None
        ok, message = self._reference_fields(entry, requested_version=requested, retry=False)
        if not ok:
            # 关联失败时不改状态，保留上一稳定口径，未完成项等重试。
            return None, message
        entry["记录状态"] = "已关联"
        entry["status"] = "已关联"
        entry["pending"] = False
        entry["abnormal"] = False
        return deepcopy(entry), message

    def _retry(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        ok, message = self._reference_fields(entry, retry=True)
        if not ok:
            return None, message
        entry["pending"] = False
        entry["abnormal"] = False
        if entry.get("status") == "已记录":
            entry["status"] = "已关联"
            entry["记录状态"] = "已关联"
        return deepcopy(entry), message

    def _advance(
        self, entry: dict[str, Any], target: str, message: str
    ) -> tuple[dict[str, Any] | None, str]:
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["记录状态"] = target
        entry["status"] = target
        entry["pending"] = target not in {"已过期", "已清理"}
        entry["abnormal"] = False
        # 标记过期/清理不影响已固化的字段版本引用，血缘仍可回查。
        return deepcopy(entry), message

    # ------------------------------------------------------------------ 引用核心
    def _resolve_source(self, entry: dict[str, Any]) -> dict[str, Any]:
        """按上游表名找到对应的元数据表，取当前或指定的字段版本。"""
        table_name = str(entry.get("上游表名") or "").strip()
        for metadata_entry in store.rows(METADATA_MODULE):
            if str(metadata_entry.get("表名称") or "") == table_name or str(
                metadata_entry.get("表编号") or ""
            ) == table_name:
                field_registry.ensure_entry(metadata_entry)
                return {"entry": metadata_entry}
        return {"entry": None}

    def _reference_fields(
        self,
        entry: dict[str, Any],
        *,
        requested_version: str | None = None,
        retry: bool = False,
    ) -> tuple[bool, str]:
        """先声明兼容目标，再引用已有字段清单；失败时沿用上一稳定口径。"""
        resolved = self._resolve_source(entry)
        metadata_entry = resolved["entry"]
        if metadata_entry is None:
            return self._reference_failure(
                entry,
                retry=retry,
                reason=f"上游表 {entry.get('上游表名')} 尚未在元数据服务登记，可追溯字段清单未完成",
            )

        entry_id = int(metadata_entry.get("id", 0))
        table_no = str(metadata_entry.get("表编号") or "")
        table_name = str(metadata_entry.get("表名称") or "")

        # 第一步：先确定并声明兼容目标，再引用数据。
        target_version: dict[str, Any] | None = None
        if requested_version:
            target_version = field_registry.get_version(entry_id, requested_version)
            if target_version is None:
                # 版本不存在时兼容目标无法声明，不往台账里写一条无依据的待核对记录。
                return self._reference_failure(
                    entry,
                    retry=retry,
                    reason=f"字段版本 {requested_version} 不存在，可追溯字段清单未完成",
                )
        else:
            target_version = field_registry.current_version(entry_id) or field_registry.last_valid_version(
                entry_id
            )
        if target_version is None:
            return self._reference_failure(
                entry,
                retry=retry,
                reason="元数据表没有可用的字段版本，可追溯字段清单未完成",
            )

        declared = field_audit.declare_target(
            entry_id=entry_id,
            table_no=table_no,
            table_name=table_name,
            version=str(target_version.get("version")),
            fingerprint=target_version.get("字段指纹"),
            source=field_audit.SOURCE_LINEAGE,
        )

        # 第二步：声明成功后才读取已有字段清单。
        fields = normalize_fields(target_version.get("字段清单"))
        if not fields:
            field_audit.record_check(
                entry_id=entry_id,
                table_no=table_no,
                table_name=table_name,
                version=str(target_version.get("version")),
                fingerprint=None,
                fields=[],
                source=field_audit.SOURCE_LINEAGE,
                result=field_audit.RESULT_PENDING,
                detail="兼容目标已声明，但字段清单为空，保留上一稳定口径",
                deregistered=bool(target_version.get("deregistered")),
                current=bool(target_version.get("current")),
            )
            return self._reference_failure(
                entry,
                retry=retry,
                reason="字段清单为空，可追溯字段清单未完成",
            )

        fingerprint = fields_fingerprint(fields)
        expected = target_version.get("字段指纹")
        consistent = fingerprint == expected
        record = field_audit.record_check(
            entry_id=entry_id,
            table_no=table_no,
            table_name=table_name,
            version=str(target_version.get("version")),
            fingerprint=fingerprint,
            fields=fields,
            source=field_audit.SOURCE_LINEAGE,
            result=field_audit.RESULT_OK if consistent else field_audit.RESULT_MISMATCH,
            detail=(
                "已声明兼容目标并引用可追溯字段清单"
                if consistent
                else "字段清单与声明的兼容目标不一致，保留上一稳定口径"
            ),
            deregistered=bool(target_version.get("deregistered")),
            current=bool(target_version.get("current")),
        )
        entry["字段版本"] = target_version.get("version")
        entry["引用的字段版本"] = target_version.get("version")
        entry["字段清单"] = deepcopy(fields)
        entry["引用字段清单"] = deepcopy(fields)
        entry["字段指纹"] = fingerprint
        entry["引用字段指纹"] = fingerprint
        entry["引用字段摘要"] = "、".join(str(field.get("name")) for field in fields)
        entry["注销状态"] = "已注销" if target_version.get("deregistered") else "未注销"
        entry["引用注销状态"] = entry["注销状态"]
        entry["引用来源"] = table_name
        entry["兼容目标"] = "可追溯字段清单"
        entry["目标声明状态"] = declared.get("声明状态")
        entry["引用状态"] = "已引用"
        entry["引用说明"] = record.get("校验说明")
        entry["引用时间"] = record.get("更新时间")
        entry["校验结果"] = record.get("校验结果")
        entry["abnormal"] = not consistent
        # 引用成功后清单项已闭环，清掉失败时留下的未完成项。
        entry.pop("未完成项", None)
        if retry:
            entry["重试次数"] = int(entry.get("重试次数", 0)) + 1
            entry["重试范围"] = "仅重试可追溯字段清单未完成项"
        message = (
            f"已引用 {table_name} 字段版本 {target_version.get('version')}"
            if consistent
            else f"{table_name} 字段版本 {target_version.get('version')} 校验不一致，沿用上一稳定口径"
        )
        return consistent, message

    def _reference_failure(
        self,
        entry: dict[str, Any],
        *,
        retry: bool,
        reason: str,
    ) -> tuple[bool, str]:
        """引用失败：保留上一稳定口径，只标记未完成项。"""
        entry["引用状态"] = "待重试"
        entry["兼容目标"] = "可追溯字段清单"
        entry["未完成项"] = ["可追溯字段清单"]
        entry["引用说明"] = reason
        entry["pending"] = True
        entry["abnormal"] = True
        if "字段版本" not in entry and "引用的字段版本" not in entry:
            entry["字段版本"] = ""
            entry["引用字段清单"] = []
            entry["注销状态"] = entry.get("注销状态") or "未注销"
        else:
            # 已有稳定口径时，失败不覆盖任何已引用字段。
            for key in STABLE_KEYS:
                entry.setdefault(key, None)
        if retry:
            entry["重试次数"] = int(entry.get("重试次数", 0)) + 1
            entry["重试范围"] = "仅重试可追溯字段清单未完成项"
        return False, reason
