"""可追溯字段清单：元数据表的字段版本仓库。

一条元数据表在任意时刻只允许有一个「当前字段版本」；刷新会生成新版本并把旧版本
降为历史版本，旧版本不会被删除，供血缘记录继续引用。注销表也只是把当前版本标记
为注销状态并保留为「最后有效字段清单」，不会丢失字段定义。

这里的所有读写都必须在 ``store.lock`` 下完成：并发刷新、并发切换版本、注销与
血缘核对会同时访问同一张表的版本列表，靠这把锁保证「只有一个当前版本」。
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime
from typing import Any, Iterable

from app.seed import SEED_FIELD_VERSIONS
from app.services import field_audit
from app.store import store

MODULE = "metadata"
VERSION_ID_RE = re.compile(r"^(?P<prefix>META-(?P<day>\d{8}))-(?P<seq>\d+)$")
CURRENT = "当前"
HISTORY = "历史"
DEREGISTERED = "已注销"


class VersionConflict(RuntimeError):
    """刷新携带的基线版本已经不是当前版本。"""


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_code() -> str:
    return datetime.now().strftime("%Y%m%d")


def _field_item(raw: dict[str, Any], fallback_name: str = "") -> dict[str, Any] | None:
    name = (
        raw.get("name")
        or raw.get("字段名")
        or raw.get("字段")
        or raw.get("column")
        or raw.get("column_name")
        or fallback_name
    )
    if name is None or not str(name).strip():
        return None
    name = str(name).strip()
    data_type = (
        raw.get("type")
        or raw.get("类型")
        or raw.get("data_type")
        or raw.get("数据类型")
        or raw.get("dtype")
        or "string"
    )
    data_type = str(data_type).strip() or "string"
    nullable = raw.get("nullable", raw.get("可空", True))
    if isinstance(nullable, str):
        nullable = nullable.strip().lower() not in {"false", "0", "否", "no", "n"}
    comment = raw.get("comment") or raw.get("说明") or raw.get("描述") or raw.get("备注") or ""
    return {
        "name": name,
        "type": data_type,
        "nullable": bool(nullable),
        "comment": str(comment),
        "字段名": name,
        "类型": data_type,
        "可空": bool(nullable),
        "说明": str(comment),
    }


def _split_top_level(text: str) -> list[str]:
    """按逗号/分号/换行拆分，但括号内的逗号（如 decimal(18,2)）不拆。"""
    tokens: list[str] = []
    buffer: list[str] = []
    depth = 0
    for char in text:
        if char in "([{（【":
            depth += 1
        elif char in ")]}）】":
            depth = max(depth - 1, 0)
        if depth == 0 and char in ",，;；\n":
            token = "".join(buffer).strip()
            if token:
                tokens.append(token)
            buffer = []
            continue
        buffer.append(char)
    token = "".join(buffer).strip()
    if token:
        tokens.append(token)
    return tokens


def _from_text(text: str) -> list[dict[str, Any]]:
    """把字段清单文本解析成字段明细。

    支持 JSON、``name:type:nullable:comment``、``name:type`` 以及逗号/分号/
    换行分隔的字段名列表。示例数据里的普通文本也能被读取，不会静默返回空清单。
    """
    text = text.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    else:
        if isinstance(parsed, (list, dict)):
            return normalize_fields(parsed)

    fields: list[dict[str, Any]] = []
    for token in _split_top_level(text):
        parts = [part.strip() for part in re.split(r"[:：]", token)]
        raw = {"name": parts[0], "type": parts[1] if len(parts) > 1 else "string"}
        if len(parts) > 2 and parts[2]:
            raw["nullable"] = parts[2]
        if len(parts) > 3 and parts[3]:
            raw["comment"] = parts[3]
        item = _field_item(raw)
        if item:
            fields.append(item)
    return fields


def normalize_fields(raw: Any) -> list[dict[str, Any]]:
    """把多种字段清单写法统一成 ``[{name,type,nullable,comment}]``。"""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        return _from_text(raw)
    if isinstance(raw, dict):
        if any(key in raw for key in ("name", "字段名", "字段", "column", "column_name")):
            item = _field_item(raw)
            return [item] if item else []
        return [item for key, value in raw.items() if (item := _field_item({"name": key, "type": value}))]
    if isinstance(raw, Iterable):
        fields: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, str):
                fields.extend(_from_text(item))
            elif isinstance(item, dict):
                normalized = _field_item(item)
                if normalized:
                    fields.append(normalized)
        # 字段名重复时只保留第一次出现的定义，避免同一版本出现两份字段定义。
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for field in fields:
            if field["name"] in seen:
                continue
            seen.add(field["name"])
            unique.append(field)
        return unique
    return []


def field_names(fields: list[dict[str, Any]]) -> list[str]:
    return [str(field.get("name") or field.get("字段名") or "") for field in fields]


def fields_fingerprint(fields: list[dict[str, Any]]) -> str:
    canonical = [
        {
            "name": str(field.get("name") or field.get("字段名") or ""),
            "type": str(field.get("type") or field.get("类型") or ""),
            "nullable": bool(field.get("nullable", field.get("可空", True))),
        }
        for field in fields
    ]
    payload = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def field_summary(fields: list[dict[str, Any]]) -> str:
    return "、".join(field_names(fields))


class FieldRegistry:
    """字段版本仓库，所有版本数据都放在内存仓库的 aux 区。"""

    def _versions_map(self) -> dict[int, list[dict[str, Any]]]:
        return store.aux.setdefault("field_versions", {})

    def _request_map(self) -> dict[str, Any]:
        return store.aux.setdefault("field_version_requests", {})

    def versions(self, entry_id: int) -> list[dict[str, Any]]:
        return self._versions_map().setdefault(int(entry_id), [])

    def get_version(self, entry_id: int, version_id: str | None = None) -> dict[str, Any] | None:
        versions = self.versions(entry_id)
        if not versions:
            return None
        if version_id:
            for version in versions:
                if version.get("version") == version_id:
                    return version
            return None
        return self.current_version(entry_id)

    def current_version(self, entry_id: int) -> dict[str, Any] | None:
        versions = self.versions(entry_id)
        for version in versions:
            if version.get("current"):
                return version
        # 历史数据兜底：只有一个最新版本时视作当前版本，但不会凭空造第二个当前版本。
        return versions[-1] if len(versions) == 1 else None

    def last_valid_version(self, entry_id: int) -> dict[str, Any] | None:
        versions = self.versions(entry_id)
        for version in versions:
            if version.get("last_valid"):
                return version
        current = self.current_version(entry_id)
        if current:
            return current
        return versions[-1] if versions else None

    def ensure_init(self) -> None:
        """首次访问时把示例元数据表补齐为版本化字段清单。"""
        with store.lock:
            if store.aux.get("field_versions_ready"):
                return
            for row in store.rows(MODULE):
                self._ensure_entry_locked(row)
            store.aux["field_versions_ready"] = True

    def ensure_entry(self, entry: dict[str, Any]) -> None:
        with store.lock:
            self._ensure_entry_locked(entry)

    def _ensure_entry_locked(self, entry: dict[str, Any]) -> None:
        entry_id = int(entry.get("id", 0))
        versions = self.versions(entry_id)
        if versions:
            self._sync_entry_summary(entry)
            return
        table_no = str(entry.get("表编号") or f"META-{entry_id:04d}")
        table_name = str(entry.get("表名称") or table_no)

        # 示例版本从种子数据读取；业务逻辑里不硬编码具体表。
        seeded = SEED_FIELD_VERSIONS.get(table_name) or SEED_FIELD_VERSIONS.get(table_no)
        if seeded:
            previous_id: str | None = None
            for item in seeded:
                versions.append(
                    self._make_version(
                        entry_id,
                        table_no,
                        table_name,
                        str(item["version"]),
                        item["fields"],
                        current=bool(item.get("current")),
                        status=CURRENT if item.get("current") else HISTORY,
                        source=str(item.get("source") or "register"),
                        supersedes=previous_id,
                        created_at=str(item.get("created_at") or now_text()),
                        revision=int(item.get("revision", 1)),
                    )
                )
                previous_id = str(item["version"])
            self._sync_entry_summary(entry)
            return

        # 优先使用登记时保留的结构化字段定义；表上的「字段清单」可能已经
        # 是摘要文本，不能反解析成字段，否则会丢掉字段名。
        fields = normalize_fields(entry.get("字段定义"))
        if not fields:
            fields = normalize_fields(entry.get("字段列表"))
        if not fields:
            fields = normalize_fields(entry.get("字段清单"))
        if not fields:
            fields = [{"name": "id", "type": "bigint", "nullable": False, "comment": "主键"}]
        version_id = self._allocate_version_id(entry_id, table_no)
        versions.append(
            self._make_version(
                entry_id,
                table_no,
                table_name,
                version_id,
                fields,
                current=True,
                status=CURRENT,
                source="register",
                supersedes=None,
                created_at=str(entry.get("最后更新") or now_text()),
                revision=1,
            )
        )
        self._sync_entry_summary(entry)

    def _make_version(
        self,
        entry_id: int,
        table_no: str,
        table_name: str,
        version_id: str,
        fields: list[dict[str, Any]],
        *,
        current: bool,
        status: str,
        source: str,
        supersedes: str | None,
        created_at: str,
        revision: int,
        deregistered: bool = False,
        last_valid: bool = False,
    ) -> dict[str, Any]:
        normalized = normalize_fields(fields)
        return {
            "version": version_id,
            "版本号": version_id,
            "entry_id": entry_id,
            "表编号": table_no,
            "表名称": table_name,
            "status": status,
            "状态": status,
            "current": current,
            "is_current": current,
            "deregistered": deregistered,
            "注销状态": DEREGISTERED if deregistered else "未注销",
            "last_valid": last_valid,
            "最后有效": last_valid,
            "fields": normalized,
            "字段清单": deepcopy(normalized),
            "字段摘要": field_summary(normalized),
            "字段数": len(normalized),
            "字段指纹": fields_fingerprint(normalized),
            "source": source,
            "来源": source,
            "supersedes": supersedes,
            "replaces": supersedes,
            "created_at": created_at,
            "创建时间": created_at,
            "revision": revision,
        }

    def _allocate_version_id(self, entry_id: int, table_no: str, requested: str | None = None) -> str:
        versions = self.versions(entry_id)
        if requested:
            requested = requested.strip()
            if requested:
                if any(version.get("version") == requested for version in versions):
                    raise VersionConflict(f"字段版本 {requested} 已存在")
                return requested
        prefix = f"META-{today_code()}"
        for version in reversed(versions):
            match = VERSION_ID_RE.match(str(version.get("version") or ""))
            if match:
                prefix = match.group("prefix")
                break
        used = []
        for version in versions:
            match = VERSION_ID_RE.match(str(version.get("version") or ""))
            if match and match.group("prefix") == prefix:
                used.append(int(match.group("seq")))
        return f"{prefix}-{max(used, default=0) + 1:02d}"

    def create_version(
        self,
        entry: dict[str, Any],
        fields: list[dict[str, Any]],
        *,
        source: str = "refresh",
        request_id: str | None = None,
        base_version: str | None = None,
        requested_version: str | None = None,
    ) -> tuple[dict[str, Any], bool, str]:
        """生成或复用字段版本，返回 ``(版本, 是否新建, 说明)``。"""
        with store.lock:
            self._ensure_entry_locked(entry)
            entry_id = int(entry.get("id", 0))
            versions = self.versions(entry_id)
            current = self.current_version(entry_id)
            if not fields:
                raise ValueError("字段清单不能为空")

            request_key = f"{entry_id}:{request_id}" if request_id else ""
            if request_key:
                recorded = self._request_map().get(request_key)
                if recorded:
                    existing = self.get_version(entry_id, str(recorded.get("version")))
                    if existing:
                        return existing, False, "重复刷新请求已复用同一字段版本"

            if base_version and current and base_version != current.get("version"):
                raise VersionConflict(
                    f"基线版本 {base_version} 已被 {current.get('version')} 取代，请基于当前版本重试"
                )

            fingerprint = fields_fingerprint(fields)
            if current and current.get("字段指纹") == fingerprint and not current.get("deregistered"):
                result = (current, False, "刷新结果与当前版本一致，未生成重复版本")
            else:
                version_id = self._allocate_version_id(entry_id, str(entry.get("表编号") or ""), requested_version)
                previous = current
                for version in versions:
                    if version.get("current"):
                        version["current"] = False
                        version["is_current"] = False
                        if not version.get("deregistered"):
                            version["status"] = HISTORY
                            version["状态"] = HISTORY
                new_version = self._make_version(
                    entry_id,
                    str(entry.get("表编号") or f"META-{entry_id:04d}"),
                    str(entry.get("表名称") or ""),
                    version_id,
                    fields,
                    current=True,
                    status=CURRENT,
                    source=source,
                    supersedes=previous.get("version") if previous else None,
                    created_at=now_text(),
                    revision=int(previous.get("revision", 0)) + 1 if previous else 1,
                )
                versions.append(new_version)
                self._sync_entry_summary(entry)
                result = (new_version, True, "已生成新的字段版本，旧版本保留供血缘引用")

            if request_key:
                self._request_map()[request_key] = {"version": result[0].get("version"), "created": result[1]}
            self._sync_entry_summary(entry)
            return result

    def activate_version(self, entry: dict[str, Any], version_id: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            self._ensure_entry_locked(entry)
            entry_id = int(entry.get("id", 0))
            target = self.get_version(entry_id, version_id)
            if target is None:
                return None, f"字段版本 {version_id} 不存在"
            if entry.get("status") == "已注销":
                return None, "已注销的元数据表只能回查历史字段版本，不能切换当前版本"
            if target.get("deregistered"):
                return None, f"字段版本 {version_id} 已随表注销，不能再切换为当前版本"
            if target.get("current"):
                return target, f"字段版本 {version_id} 已是当前版本"
            for version in self.versions(entry_id):
                is_target = version is target
                version["current"] = is_target
                version["is_current"] = is_target
                if not version.get("deregistered"):
                    version["status"] = CURRENT if is_target else HISTORY
                    version["状态"] = version["status"]
            self._sync_entry_summary(entry)
            return target, f"当前字段版本已切换为 {version_id}"

    def deregister(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """注销表：保留最后有效字段清单，供血缘回查。"""
        with store.lock:
            self._ensure_entry_locked(entry)
            entry_id = int(entry.get("id", 0))
            versions = self.versions(entry_id)
            if not versions:
                return None, "该元数据表没有字段版本，无法注销"
            current = self.current_version(entry_id) or versions[-1]
            for version in versions:
                version["deregistered"] = True
                version["注销状态"] = DEREGISTERED
                version["last_valid"] = version is current
                version["最后有效"] = version is current
                if version.get("current"):
                    version["status"] = DEREGISTERED
                    version["状态"] = DEREGISTERED
                elif version.get("status") != HISTORY:
                    version["status"] = HISTORY
                    version["状态"] = HISTORY
            entry["status"] = "已注销"
            entry["注销状态"] = DEREGISTERED
            entry["注销前字段版本"] = current.get("version")
            entry["最后有效字段版本"] = current.get("version")
            entry["pending"] = False
            self._sync_entry_summary(entry)
            self._mark_references_deregistered(entry, current.get("version"))
            return current, f"元数据表已注销，最后有效字段清单 {current.get('version')} 已保留"

    def _mark_references_deregistered(self, entry: dict[str, Any], last_version: str | None) -> None:
        """表注销后，引用它的血缘记录与审计台账同步注销状态，但保留字段清单。"""
        table_name = str(entry.get("表名称") or "")
        table_no = str(entry.get("表编号") or "")
        for line in store.rows("lineage"):
            upstream = str(line.get("上游表名") or "")
            if upstream not in {table_name, table_no}:
                continue
            line["引用注销状态"] = DEREGISTERED
            line["注销状态"] = DEREGISTERED
            line["最后有效字段版本"] = last_version
            # 已引用的字段清单一律保留；回查时按最后有效版本取。
        field_audit.mark_deregistered(int(entry.get("id", 0)))

    def sync_deregistered_references(self, entry: dict[str, Any]) -> None:
        """读取时兜底：注销状态以元数据表为准同步到血缘引用。"""
        if entry.get("status") != "已注销":
            return
        current = self.current_version(int(entry.get("id", 0)))
        last_valid = self.last_valid_version(int(entry.get("id", 0)))
        canonical = current or last_valid
        self._mark_references_deregistered(
            entry, canonical.get("version") if canonical else None
        )

    def register(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            self._ensure_entry_locked(entry)
            entry_id = int(entry.get("id", 0))
            versions = self.versions(entry_id)
            if entry.get("status") == "已注销":
                return None, "已注销的元数据表不能直接重新注册，请登记新的元数据表"
            if not versions:
                return None, "该元数据表没有可用的字段清单"
            current = self.current_version(entry_id) or versions[-1]
            for version in versions:
                is_current = version is current
                version["current"] = is_current
                version["is_current"] = is_current
                if not version.get("deregistered"):
                    version["status"] = CURRENT if is_current else HISTORY
                    version["状态"] = version["status"]
            self._sync_entry_summary(entry)
            return current, "元数据表已注册"

    def _sync_entry_summary(self, entry: dict[str, Any]) -> None:
        entry_id = int(entry.get("id", 0))
        versions = self.versions(entry_id)
        current = self.current_version(entry_id)
        last_valid = self.last_valid_version(entry_id)
        canonical = current or last_valid
        if canonical:
            entry["字段清单"] = canonical.get("字段摘要", "")
            entry["字段定义"] = deepcopy(canonical.get("字段清单", []))
            entry["字段列表"] = deepcopy(canonical.get("字段清单", []))
            entry["可追溯字段清单"] = deepcopy(canonical.get("字段清单", []))
            entry["字段指纹"] = canonical.get("字段指纹")
        entry["当前字段版本"] = current.get("version") if current else None
        entry["current_version"] = current.get("version") if current else None
        entry["最后有效字段版本"] = (
            (last_valid or current).get("version") if (last_valid or current) else None
        )
        entry["last_valid_version"] = entry["最后有效字段版本"]
        entry["字段版本数"] = len(versions)
        deregistered = any(version.get("deregistered") for version in versions) or entry.get("status") == "已注销"
        entry["注销状态"] = DEREGISTERED if deregistered else "未注销"
        entry["deregistered"] = bool(deregistered)
        entry["字段版本摘要"] = [
            {
                "version": version.get("version"),
                "状态": version.get("status"),
                "current": bool(version.get("current")),
                "字段数": version.get("字段数"),
                "创建时间": version.get("created_at"),
                "注销状态": version.get("注销状态"),
            }
            for version in versions
        ]

    def attach_versions(self, entry: dict[str, Any], *, include_fields: bool = True) -> dict[str, Any]:
        """给接口响应补上版本清单与可追溯字段清单，不改变仓库里的原始记录。"""
        with store.lock:
            self._ensure_entry_locked(entry)
            payload = dict(entry)
            versions = deepcopy(self.versions(int(entry.get("id", 0))))
            if not include_fields:
                versions = [
                    {key: value for key, value in version.items() if key not in {"fields", "字段清单"}}
                    for version in versions
                ]
            payload["field_versions"] = versions
            payload["字段版本清单"] = deepcopy(versions)
            payload["字段版本摘要"] = deepcopy(entry.get("字段版本摘要", []))
            return payload


field_registry = FieldRegistry()
