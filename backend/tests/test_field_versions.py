"""元数据字段版本与血缘引用的回归测试（标准库 unittest，无需额外依赖）。

运行：cd backend && python -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.seed import SEED_ROWS
from app.services import field_audit
from app.services.field_registry import field_registry
from app.services.lineage import LineageService
from app.services.metadata import MetadataService
from app.store import store


def reset_store() -> None:
    """每个用例从干净示例数据开始，避免共享内存仓库互相影响。"""
    with store.lock:
        store._tables = {name: copy.deepcopy(rows) for name, rows in SEED_ROWS.items()}
        store.aux.clear()


class FieldVersionFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.metadata = MetadataService()
        self.lineage = LineageService()
        self.metadata.ensure_ready()

    def _create(self, table_no: str, table_name: str, fields: str) -> dict:
        entry, missing = self.metadata.create_entry(
            {"表编号": table_no, "表名称": table_name, "字段清单": fields}
        )
        self.assertEqual(missing, [])
        return entry

    def test_register_moves_draft_to_registered(self) -> None:
        entry = self._create("META-T-001", "t_register", "a:bigint:false:主键")
        self.assertEqual(entry["status"], "草稿")
        registered, message = self.metadata.run_action(entry["id"], "注册表", {})
        self.assertEqual(registered["status"], "已注册")
        self.assertIn("已注册", message)
        self.assertTrue(registered["当前字段版本"])

    def test_refresh_expired_stays_expired_and_keeps_history(self) -> None:
        entry = self._create("META-T-002", "t_expired", "a:string:true")
        self.metadata.run_action(entry["id"], "注册表", {})
        self.metadata.run_action(entry["id"], "注销表", {})
        refreshed, message = self.metadata.run_action(
            entry["id"], "刷新元数据", {"字段列表": "a:string:true;b:int:true"}
        )
        self.assertIsNone(refreshed)
        self.assertIn("已注销", message)

    def test_expired_table_refresh_does_not_become_registered(self) -> None:
        expired = next(row for row in store.rows("metadata") if row.get("status") == "已过期")
        refreshed, _ = self.metadata.run_action(
            expired["id"], "刷新元数据", {"字段列表": "x:bigint:true"}
        )
        self.assertEqual(refreshed["status"], "已过期")
        self.assertEqual(sum(1 for item in refreshed["字段版本清单"] if item["current"]), 1)

    def test_only_one_current_version_under_concurrent_refresh(self) -> None:
        entry = self._create("META-T-003", "t_concurrent", "a:string:true")
        self.metadata.run_action(entry["id"], "注册表", {})
        base = entry["当前字段版本"]

        def refresh(index: int):
            return self.metadata.run_action(
                entry["id"],
                "刷新元数据",
                {
                    "基线版本": base,
                    "request_id": f"req-{index}",
                    "字段列表": "a:string:true;b:bigint:true",
                },
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(refresh, range(6)))

        succeeded = [item for item in results if item[0] is not None]
        self.assertTrue(succeeded)
        detail = self.metadata.get_entry(entry["id"])
        self.assertEqual(sum(1 for item in detail["字段版本清单"] if item["current"]), 1)
        # 同字段清单的并发刷新只落一个版本，其余请求复用同一版本。
        fingerprints = {item["字段指纹"] for item in detail["字段版本清单"]}
        self.assertEqual(len(fingerprints), len(detail["字段版本清单"]))

    def test_same_fields_refresh_does_not_create_duplicate_version(self) -> None:
        entry = self._create("META-T-004", "t_same", "a:string:true")
        self.metadata.run_action(entry["id"], "注册表", {})
        first, _ = self.metadata.run_action(
            entry["id"], "刷新元数据", {"字段列表": "a:bigint:true"}
        )
        second, message = self.metadata.run_action(
            entry["id"], "刷新元数据", {"字段列表": "a:bigint:true"}
        )
        self.assertEqual(first["当前字段版本"], second["当前字段版本"])
        self.assertIn("未生成重复版本", message)

    def test_deregister_keeps_last_valid_fields_and_blocks_refresh(self) -> None:
        entry = self._create("META-T-005", "t_deregister", "a:bigint:false;b:string:true")
        self.metadata.run_action(entry["id"], "注册表", {})
        deregistered, message = self.metadata.run_action(entry["id"], "注销表", {})
        self.assertEqual(deregistered["status"], "已注销")
        self.assertIn("最后有效字段清单", message)
        self.assertEqual(
            [field["name"] for field in deregistered["可追溯字段清单"]], ["a", "b"]
        )
        blocked, reason = self.metadata.run_action(
            entry["id"], "刷新元数据", {"字段列表": "z:int:true"}
        )
        self.assertIsNone(blocked)
        self.assertIn("已注销", reason)

    def test_switch_current_version_keeps_single_current(self) -> None:
        entry = self._create("META-T-006", "t_switch", "a:string:true")
        self.metadata.run_action(entry["id"], "注册表", {})
        old = entry["当前字段版本"]
        refreshed, _ = self.metadata.run_action(
            entry["id"], "刷新元数据", {"字段列表": "a:string:true;b:int:true"}
        )
        new = refreshed["当前字段版本"]
        switched, _ = self.metadata.run_action(
            entry["id"], "切换字段版本", {"字段版本": old}
        )
        self.assertEqual(switched["当前字段版本"], old)
        self.assertEqual(sum(1 for item in switched["字段版本清单"] if item["current"]), 1)
        self.assertTrue(any(item["version"] == new for item in switched["字段版本清单"]))

    def test_lake_orders_versions_and_lineage_reference(self) -> None:
        lake = next(
            row for row in store.rows("metadata") if row.get("表名称") == "lake.orders"
        )
        detail = self.metadata.get_entry(lake["id"])
        names = {item["version"]: item["字段数"] for item in detail["字段版本清单"]}
        self.assertIn("META-20261115-03", names)
        self.assertIn("META-20261115-04", names)
        self.assertEqual(detail["当前字段版本"], "META-20261115-04")

        line, missing = self.lineage.create_entry(
            {"记录编号": "LINE-T-01", "上游表名": "lake.orders", "下游表名": "out"}
        )
        self.assertEqual(missing, [])
        linked, _ = self.lineage.run_action(
            line["id"], "关联血缘", {"字段版本": "META-20261115-03"}
        )
        self.assertEqual(linked["引用的字段版本"], "META-20261115-03")
        self.assertEqual(
            [field["name"] for field in linked["引用字段清单"]],
            ["order_id", "dt", "amount", "customer_id"],
        )
        self.assertEqual(linked["引用注销状态"], "未注销")

    def test_failed_reference_keeps_stable_baseline_and_retries(self) -> None:
        entry = self._create("META-T-007", "t_retry", "a:string:true")
        self.metadata.run_action(entry["id"], "注册表", {})
        line, _ = self.lineage.create_entry(
            {"记录编号": "LINE-T-02", "上游表名": "t_retry", "下游表名": "out"}
        )
        baseline = copy.deepcopy(line["引用字段清单"])
        failed, reason = self.lineage.run_action(
            line["id"], "关联血缘", {"字段版本": "META-19990101-01"}
        )
        self.assertIsNone(failed)
        self.assertIn("不存在", reason)
        preserved = self.lineage.get_entry(line["id"])
        self.assertEqual(preserved["引用状态"], "待重试")
        self.assertEqual(preserved["未完成项"], ["可追溯字段清单"])
        self.assertEqual(preserved["引用字段清单"], baseline)

        retried, _ = self.lineage.run_action(line["id"], "重试引用", {})
        self.assertNotIn("未完成项", retried)
        self.assertEqual(retried["引用状态"], "已引用")

    def test_audit_ledger_matches_between_metadata_and_lineage(self) -> None:
        entry = self._create("META-T-008", "t_audit", "a:string:true")
        self.metadata.run_action(entry["id"], "注册表", {})
        line, _ = self.lineage.create_entry(
            {"记录编号": "LINE-T-03", "上游表名": "t_audit", "下游表名": "out"}
        )
        self.lineage.run_action(line["id"], "关联血缘", {})
        records = field_audit.list_records(entry_id=entry["id"])
        sources = {record["数据来源"] for record in records}
        self.assertEqual(sources, {"元数据服务", "湖仓血缘"})
        result = field_audit.consistency(entry["id"])
        self.assertTrue(result["consistent"], result)

    def test_list_and_detail_agree_after_reentry(self) -> None:
        entry = self._create("META-T-009", "t_agree", "a:string:true")
        self.metadata.run_action(entry["id"], "注册表", {})
        self.metadata.run_action(entry["id"], "刷新元数据", {"字段列表": "a:int:true"})
        listed = next(
            row for row in self.metadata.list_entries(page=1, size=200)[0]
            if row["id"] == entry["id"]
        )
        detail = self.metadata.get_entry(entry["id"])
        self.assertEqual(listed["当前字段版本"], detail["当前字段版本"])
        self.assertEqual(listed["字段清单"], detail["字段清单"])


if __name__ == "__main__":
    unittest.main()
