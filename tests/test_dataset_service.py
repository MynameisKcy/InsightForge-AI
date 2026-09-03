"""DatasetService 单元测试（架构评审 R2 候选6）。

数据集生命周期事务（上传/删除/schema 探测）从 api/routes/datasets.py 的
110 行内联脚本收口为深模块 database/dataset_service.py；本测试直接构造
注入桩验证事务语义，路由级 HTTP 契约另见 tests/test_datasets_api.py。
"""
import json
import os

import pandas as pd
import pytest

from database.dataset_service import (
    DatasetService,
    DatasetServiceError,
)


class _FakeDuckDB:
    def __init__(self, load_result=None, probe_exc=None):
        self.load_result = load_result or {"success": True, "row_count": 2}
        self.probe_exc = probe_exc
        self.cols = [("city", "VARCHAR"), ("revenue", "DOUBLE")]
        self.sample = pd.DataFrame([{"city": "济南", "revenue": 100.0},
                                    {"city": "青岛", "revenue": 200.0}])
        self.dropped: list[str] = []

    def load_csv_dataset(self, fpath, table):
        return dict(self.load_result)

    def load_excel_dataset(self, fpath, table):
        return dict(self.load_result)

    def execute_fetchall(self, sql):
        # 对齐真实施主：execute+fetchall 已在连接锁内原子化，桩直接返回行
        if self.probe_exc:
            raise self.probe_exc
        if sql.startswith("DESCRIBE"):
            return self.cols
        if sql.startswith("SUMMARIZE"):
            return [("city", "VARCHAR", "济南", "青岛", None, None, 2, 0)]
        raise AssertionError(f"unexpected sql: {sql}")

    def query_df(self, sql):
        return self.sample

    def drop_table(self, table):
        self.dropped.append(table)


class _FakeMeta:
    """DatasourcesDB 桩：get/add/delete/list 的最小面。"""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.fail_add = False

    def get_dataset(self, name, owner_user_id=None):
        row = self.rows.get(name)
        if row and owner_user_id is not None and row["owner_user_id"] != owner_user_id:
            return None
        return row

    def add_dataset(self, **kw):
        if self.fail_add:
            return {"success": False, "error": "UNIQUE conflict"}
        self.rows[kw["name"]] = kw
        return {"success": True}

    def delete_dataset(self, name, owner_user_id=None):
        row = self.get_dataset(name, owner_user_id)
        if row:
            del self.rows[name]

    def list_datasets(self, owner_user_id=None):
        return [r for r in self.rows.values()
                if owner_user_id is None or r["owner_user_id"] == owner_user_id]


def _svc(tmp_path, duck=None, meta=None) -> tuple[DatasetService, _FakeDuckDB, _FakeMeta]:
    duck = duck or _FakeDuckDB()
    meta = meta or _FakeMeta()
    svc = DatasetService(
        duckdb_factory=lambda user_id=None: duck,
        meta_provider=lambda: meta,
        dir_provider=lambda: str(tmp_path),
    )
    return svc, duck, meta


CSV_BYTES = b"city,revenue\n100,1\n200,2\n"


class TestUpload:
    def test_success_writes_file_meta_and_payload(self, tmp_path):
        svc, duck, meta = _svc(tmp_path)
        payload = svc.upload(CSV_BYTES, "sales.csv", user_id="u1")
        assert payload["success"] is True
        assert payload["name"] == "sales"
        assert payload["row_count"] == 2
        assert payload["columns"] == ["city", "revenue"]
        assert payload["sample"][0]["city"] == "济南"
        assert os.path.isfile(str(tmp_path / "sales.csv"))
        assert meta.rows["sales"]["owner_user_id"] == "u1"

    def test_bad_extension_rejected_before_write(self, tmp_path):
        svc, _, _ = _svc(tmp_path)
        with pytest.raises(DatasetServiceError) as ei:
            svc.upload(b"x", "data.txt", user_id="u1")
        assert ei.value.status_code == 400
        assert list(tmp_path.iterdir()) == []

    def test_oversize_rejected_413(self, tmp_path):
        svc, _, _ = _svc(tmp_path)
        with pytest.raises(DatasetServiceError) as ei:
            svc.upload(b"x" * (svc.MAX_SIZE + 1), "big.csv", user_id="u1")
        assert ei.value.status_code == 413

    def test_load_failure_compensates_file(self, tmp_path):
        duck = _FakeDuckDB(load_result={"success": False, "error": "bad csv"})
        svc, _, _ = _svc(tmp_path, duck=duck)
        with pytest.raises(DatasetServiceError) as ei:
            svc.upload(CSV_BYTES, "broken.csv", user_id="u1")
        assert ei.value.status_code == 400
        assert ei.value.message == "bad csv"
        assert list(tmp_path.iterdir()) == []

    def test_probe_failure_compensates_file_500(self, tmp_path):
        duck = _FakeDuckDB(probe_exc=RuntimeError("duck exploded"))
        svc, _, _ = _svc(tmp_path, duck=duck)
        with pytest.raises(DatasetServiceError) as ei:
            svc.upload(CSV_BYTES, "boom.csv", user_id="u1")
        assert ei.value.status_code == 500
        assert list(tmp_path.iterdir()) == []

    def test_meta_failure_compensates_file(self, tmp_path):
        meta = _FakeMeta()
        meta.fail_add = True
        svc, _, _ = _svc(tmp_path, meta=meta)
        with pytest.raises(DatasetServiceError) as ei:
            svc.upload(CSV_BYTES, "dup.csv", user_id="u1")
        assert ei.value.status_code == 400
        assert "元数据写入失败" in ei.value.message
        assert list(tmp_path.iterdir()) == []

    def test_name_collision_appends_counter(self, tmp_path):
        svc, _, meta = _svc(tmp_path)
        meta.rows["sales"] = {"owner_user_id": "u1", "table_name": "sales"}
        payload = svc.upload(CSV_BYTES, "sales.csv", user_id="u1")
        assert payload["name"] == "sales_2"

    def test_chinese_filename_safe_table_preserves_display(self, tmp_path):
        svc, _, _ = _svc(tmp_path)
        payload = svc.upload(b"a,b\n1,2\n", "山东数据.csv", user_id="u1")
        assert payload["name"] == "ds_upload"
        assert payload["display_name"] == "山东数据"


class TestSampleSerialization:
    """f23b010 的另一半：upload 与 schema 共用同一序列化（Timestamp→ISO、NaN→null）。"""

    def test_timestamp_and_nan_are_json_safe(self, tmp_path):
        duck = _FakeDuckDB()
        duck.sample = pd.DataFrame([
            {"d": pd.Timestamp("2026-08-25"), "v": float("nan")},
        ])
        svc, _, _ = _svc(tmp_path, duck=duck)
        payload = svc.upload(CSV_BYTES, "sales.csv", user_id="u1")
        # 必须可 JSON 序列化（FastAPI allow_nan=False 的约束）且形态确定
        encoded = json.dumps(payload["sample"], ensure_ascii=False, allow_nan=False)
        assert "2026-08-25" in encoded
        assert payload["sample"][0]["v"] is None

    def test_schema_sample_uses_same_form_as_upload(self, tmp_path):
        duck = _FakeDuckDB()
        duck.sample = pd.DataFrame([{"d": pd.Timestamp("2026-08-25"), "v": 1.5}])
        svc, _, meta = _svc(tmp_path, duck=duck)
        meta.rows["sales"] = {
            "owner_user_id": "u1", "table_name": "sales",
            "source_type": "csv", "row_count": 1, "schema_json": "[]",
        }
        body = svc.schema("sales", user_id="u1")
        assert body["sample"][0]["d"] == "2026-08-25T00:00:00.000"
        assert body["sample"][0]["v"] == 1.5


class TestDelete:
    def test_delete_drops_table_file_and_meta(self, tmp_path):
        fpath = tmp_path / "mydata.csv"
        fpath.write_text("a,b\n1,2", encoding="utf-8")
        svc, duck, meta = _svc(tmp_path)
        meta.rows["mydata"] = {
            "owner_user_id": "u1", "table_name": "mydata", "file_path": str(fpath),
        }
        svc.delete("mydata", user_id="u1")
        assert duck.dropped == ["mydata"]
        assert not fpath.exists()
        assert "mydata" not in meta.rows

    def test_delete_missing_raises_404(self, tmp_path):
        svc, _, _ = _svc(tmp_path)
        with pytest.raises(DatasetServiceError) as ei:
            svc.delete("ghost", user_id="u1")
        assert ei.value.status_code == 404

    def test_delete_refuses_file_outside_dir(self, tmp_path):
        # 路径穿越防护：file_path 指向目录外文件时不删文件，其余照常
        outside = tmp_path.parent / "outside_secret.csv"
        outside.write_text("secret", encoding="utf-8")
        svc, _, meta = _svc(tmp_path)
        meta.rows["evil"] = {
            "owner_user_id": "u1", "table_name": "evil", "file_path": str(outside),
        }
        svc.delete("evil", user_id="u1")
        assert outside.exists()
        assert "evil" not in meta.rows


class TestSchema:
    def test_live_schema_with_stats(self, tmp_path):
        svc, _, meta = _svc(tmp_path)
        meta.rows["sales"] = {
            "owner_user_id": "u1", "table_name": "sales",
            "source_type": "csv", "row_count": 2, "schema_json": "[]",
        }
        body = svc.schema("sales", user_id="u1")
        assert body["columns"] == [{"name": "city", "type": "VARCHAR"},
                                   {"name": "revenue", "type": "DOUBLE"}]
        assert body["statistics"][0]["column"] == "city"

    def test_falls_back_to_cached_metadata(self, tmp_path):
        duck = _FakeDuckDB(probe_exc=RuntimeError("table not loaded"))
        svc, _, meta = _svc(tmp_path, duck=duck)
        meta.rows["cached"] = {
            "owner_user_id": "u1", "table_name": "cached",
            "source_type": "csv", "row_count": 5,
            "schema_json": '[{"name": "city", "type": "VARCHAR"}]',
        }
        body = svc.schema("cached", user_id="u1")
        assert body["columns"] == [{"name": "city", "type": "VARCHAR"}]
        assert "缓存 schema" in body["note"]

    def test_missing_raises_404(self, tmp_path):
        svc, _, _ = _svc(tmp_path)
        with pytest.raises(DatasetServiceError) as ei:
            svc.schema("ghost", user_id="u1")
        assert ei.value.status_code == 404
