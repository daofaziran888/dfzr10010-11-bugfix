"""元数据服务业务规则：状态流转、字段版本与筛选口径都收在这里。

关键约束：
- 注册成功后状态必须是「已注册」，不能在草稿上原地打转；
- 刷新元数据只生成新的字段版本，旧版本保留，已过期/已注销的表不会被刷新成已注册；
- 注销表保留最后有效字段清单，血缘仍可回查；
- 任意时刻同一张表只有一个当前字段版本。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services import field_audit
from app.services.field_registry import (
    DEREGISTERED,
    MODULE,
    field_names,
    field_registry,
    fields_fingerprint,
    normalize_fields,
)
from app.store import store

REQUIRED_FIELDS = ["表编号", "表名称", "字段清单"]
STATUS_ORDER = ["草稿", "已注册", "已过期", "已注销"]
STATUS_DRAFT, STATUS_REGISTERED, STATUS_EXPIRED, STATUS_DEREGISTERED = STATUS_ORDER

ACTION_REGISTER = "注册表"
ACTION_REFRESH = "刷新元数据"
ACTION_DEREGISTER = "注销表"
ACTION_ACTIVATE_VERSION = "切换字段版本"
ACTIONS = [ACTION_REGISTER, ACTION_REFRESH, ACTION_DEREGISTER, ACTION_ACTIVATE_VERSION]
FIELD_VERSION_KEYS = ("字段列表", "字段清单", "fields", "field_list", "columns")
BASE_VERSION_KEYS = ("基线版本", "base_version", "当前字段版本", "基于版本")


class MetadataService:
    # ------------------------------------------------------------------ 初始化
    def ensure_ready(self) -> None:
        """首次访问时补齐字段版本，并把初始核对结果写入字段审计台账。"""
        field_registry.ensure_init()
        with store.lock:
            if store.aux.get("metadata_audit_ready"):
                return
            store.aux["metadata_audit_ready"] = True
            for entry in store.rows(MODULE):
                field_registry.ensure_entry(entry)
                self._record_metadata_audit(entry, action="登记")

    # ------------------------------------------------------------------ 查询
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        self.ensure_ready()
        with store.lock:
            rows = store.rows(MODULE)
            if keyword:
                rows = [
                    row
                    for row in rows
                    if keyword in str(row.get("表编号", "")) or keyword in str(row.get("表名称", ""))
                ]
            if status:
                rows = [row for row in rows if row.get("status") == status]
            total = len(rows)
            start = max(page - 1, 0) * size
            selected = rows[start:start + size]
            items = [field_registry.attach_versions(row, include_fields=False) for row in selected]
            return items, total

    def get_entry(self, entry_id: int, version_id: str | None = None) -> dict[str, Any] | None:
        self.ensure_ready()
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None
            return self._detail(entry, version_id)

    def export_entries(self) -> list[dict[str, Any]]:
        items, _ = self.list_entries(page=1, size=10_000)
        return items

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        fields = self._extract_fields(values)
        missing = [
            field
            for field in REQUIRED_FIELDS
            if field != "字段清单" and not str(values.get(field) or "").strip()
        ]
        if not fields:
            missing.append("字段清单")
        if missing:
            return None, missing
        with store.lock:
            rows = store.rows(MODULE)
            table_no = str(values.get("表编号") or "").strip()
            if any(str(row.get("表编号")) == table_no for row in rows):
                return None, [f"表编号 {table_no} 已存在"]
            entry = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "表编号": table_no,
                "表名称": str(values.get("表名称") or "").strip(),
                "分区信息": values.get("分区信息") or "",
                "统计信息": values.get("统计信息") or "",
                "最后更新": values.get("最后更新") or "",
                "数据大小": values.get("数据大小") or "",
                "表状态": STATUS_DRAFT,
                "status": STATUS_DRAFT,
                "pending": True,
                "abnormal": False,
                "注销状态": "未注销",
            }
            entry["字段清单"] = "、".join(field_names(fields))
            entry["字段定义"] = deepcopy(fields)
            rows.append(entry)
            field_registry.ensure_entry(entry)
            self._record_metadata_audit(entry, action="登记")
            return self._detail(entry), []

    # ------------------------------------------------------------------ 动作
    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        self.ensure_ready()
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"元数据表 {entry_id} 不存在或已归档"
            field_registry.ensure_entry(entry)
            if action == ACTION_REGISTER:
                return self._register(entry)
            if action == ACTION_REFRESH:
                return self._refresh(entry, values)
            if action == ACTION_DEREGISTER:
                return self._deregister(entry)
            if action == ACTION_ACTIVATE_VERSION:
                return self._activate_version(entry, values)
            return None, f"动作「{action}」不属于元数据服务可执行范围"

    def _register(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        current_status = str(entry.get("status") or STATUS_DRAFT)
        if current_status == STATUS_DEREGISTERED:
            return None, "已注销的元数据表不能重新注册，请登记新的元数据表"
        if current_status == STATUS_REGISTERED and entry.get("当前字段版本"):
            self._record_metadata_audit(entry, action=ACTION_REGISTER)
            return self._detail(entry), "元数据表已注册，当前字段版本未变化"
        version, message = field_registry.register(entry)
        if version is None:
            return None, message
        entry["status"] = STATUS_REGISTERED
        entry["表状态"] = STATUS_REGISTERED
        entry["pending"] = False
        entry["abnormal"] = False
        entry["注册时间"] = entry.get("注册时间") or version.get("created_at")
        self._record_metadata_audit(entry, action=ACTION_REGISTER)
        return self._detail(entry), f"元数据表已注册，当前字段版本 {version.get('version')}"

    def _refresh(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        current_status = str(entry.get("status") or STATUS_DRAFT)
        if current_status == STATUS_DEREGISTERED:
            return (
                None,
                "已注销的元数据表不能刷新字段清单，可回查注销前最后一次有效字段清单"
                f"（{entry.get('最后有效字段版本')}）",
            )

        provided = self._extract_fields(values)
        if not provided:
            # 未显式提交字段清单时，按当前登记的字段定义重算，用于确认口径是否已一致。
            provided = normalize_fields(entry.get("字段定义"))
        if not provided:
            return None, "刷新元数据需要提供非空的字段清单"

        base_version = self._first_text(values, BASE_VERSION_KEYS) or None
        request_id = self._first_text(values, ("request_id", "请求编号", "幂等键")) or None
        requested_version = self._first_text(values, ("版本号", "version", "目标版本")) or None

        try:
            version, _created, message = field_registry.create_version(
                entry,
                provided,
                source="refresh",
                request_id=request_id,
                base_version=base_version,
                requested_version=requested_version,
            )
        except ValueError as error:
            return None, str(error)
        except Exception as error:  # VersionConflict 在服务层转成可读提示
            if error.__class__.__name__ == "VersionConflict":
                return None, f"刷新未生效：{error}"
            raise

        # 刷新只更新字段版本，不把已过期/草稿状态强行改成已注册。
        if current_status == STATUS_EXPIRED:
            entry["status"] = STATUS_EXPIRED
            entry["表状态"] = STATUS_EXPIRED
        elif current_status == STATUS_DRAFT:
            entry["status"] = STATUS_DRAFT
            entry["表状态"] = STATUS_DRAFT
        else:
            entry["status"] = STATUS_REGISTERED
            entry["表状态"] = STATUS_REGISTERED
        entry["最后更新"] = version.get("created_at")
        entry["pending"] = entry["status"] in {STATUS_DRAFT, STATUS_EXPIRED}
        entry["abnormal"] = False
        self._record_metadata_audit(entry, action=ACTION_REFRESH)
        return self._detail(entry), f"{message}（{version.get('version')}）"

    def _deregister(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if entry.get("status") == STATUS_DEREGISTERED:
            self._record_metadata_audit(entry, action=ACTION_DEREGISTER)
            return self._detail(entry), "元数据表已注销，最后有效字段清单已保留"
        last_valid, message = field_registry.deregister(entry)
        if last_valid is None:
            return None, message
        self._record_metadata_audit(entry, action=ACTION_DEREGISTER)
        return self._detail(entry), message

    def _activate_version(
        self, entry: dict[str, Any], values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        version_id = self._first_text(values, ("字段版本", "version", "版本号"))
        if not version_id:
            return None, "切换字段版本需要提供字段版本号"
        version, message = field_registry.activate_version(entry, version_id)
        if version is None:
            return None, message
        self._record_metadata_audit(entry, action=ACTION_ACTIVATE_VERSION)
        return self._detail(entry), message

    # ------------------------------------------------------------------ 台账
    def record_audit(
        self,
        entry_id: int,
        *,
        actor: str = "元数据服务",
    ) -> dict[str, Any] | None:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None
            field_registry.ensure_entry(entry)
            self._record_metadata_audit(entry, action="核对")
            return self._detail(entry)

    def _record_metadata_audit(self, entry: dict[str, Any], *, action: str) -> None:
        entry_id = int(entry.get("id", 0))
        table_no = str(entry.get("表编号") or "")
        table_name = str(entry.get("表名称") or "")
        versions = field_registry.versions(entry_id)
        canonical = field_registry.current_version(entry_id) or field_registry.last_valid_version(entry_id)
        for version in versions:
            stored_fields = version.get("字段清单") or []
            fingerprint = fields_fingerprint(stored_fields)
            expected = version.get("字段指纹")
            fields_consistent = bool(stored_fields) and fingerprint == expected
            # 表上的字段清单摘要必须与当前（或最后有效）版本一致，否则视为状态错位。
            summary_consistent = True
            if version is canonical:
                summary_consistent = entry.get("字段清单") in (None, "", version.get("字段摘要"))
            ok = fields_consistent and summary_consistent
            detail = (
                f"{action}后字段清单与版本记录一致"
                if ok
                else "字段清单与版本记录不一致，已保留原版本依据"
            )
            if version.get("deregistered"):
                detail += "；注销后保留最后有效字段清单"
            elif not version.get("current"):
                detail += "；历史版本继续供血缘引用"
            field_audit.record_check(
                entry_id=entry_id,
                table_no=table_no,
                table_name=table_name,
                version=str(version.get("version")),
                fingerprint=fingerprint if ok else expected,
                fields=stored_fields,
                source=field_audit.SOURCE_METADATA,
                result=field_audit.RESULT_OK if ok else field_audit.RESULT_MISMATCH,
                detail=detail,
                deregistered=bool(version.get("deregistered")),
                current=bool(version.get("current")),
                actor="元数据服务",
            )

    # ------------------------------------------------------------------ 辅助
    def _detail(self, entry: dict[str, Any], version_id: str | None = None) -> dict[str, Any]:
        payload = field_registry.attach_versions(entry)
        selected = field_registry.get_version(int(entry.get("id", 0)), version_id) or field_registry.current_version(
            int(entry.get("id", 0))
        )
        if selected is None:
            selected = field_registry.last_valid_version(int(entry.get("id", 0)))
        if selected is not None:
            payload["查看字段版本"] = selected.get("version")
            payload["查看字段版本状态"] = selected.get("status")
            payload["查看字段清单"] = deepcopy(selected.get("字段清单", []))
            payload["查看字段指纹"] = selected.get("字段指纹")
            payload["可追溯字段清单"] = deepcopy(selected.get("字段清单", []))
            payload["字段清单"] = selected.get("字段摘要", payload.get("字段清单"))
            payload["字段指纹"] = selected.get("字段指纹")
        payload["字段审计台账"] = field_audit.list_records(entry_id=int(entry.get("id", 0)))
        return payload

    @staticmethod
    def _extract_fields(values: dict[str, Any]) -> list[dict[str, Any]]:
        for key in FIELD_VERSION_KEYS:
            if key in values and values[key] not in (None, "", [], {}):
                fields = normalize_fields(values[key])
                if fields:
                    return fields
        return []

    @staticmethod
    def _first_text(values: dict[str, Any], keys: tuple[str, ...]) -> str:
        for key in keys:
            text = str(values.get(key) or "").strip()
            if text:
                return text
        return ""

    # ------------------------------------------------------------------ 兼容旧调用
    def field_versions(self, entry_id: int) -> list[dict[str, Any]] | None:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None
            field_registry.ensure_entry(entry)
            return field_registry.versions(entry_id)
