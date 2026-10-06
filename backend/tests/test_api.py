"""HTTP 接口测试（FastAPI TestClient，临时 SQLite）。"""
import os
import tempfile

import pytest

# 在导入 api 前把数据库指到临时文件，保证各用例隔离
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DECK_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient                  # noqa: E402

from app.api import app, storage                            # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _clean_db():
    st = storage()
    yield
    st.close()
    try:
        os.unlink(_tmp.name)
    except OSError:
        pass


@pytest.fixture
def client():
    return TestClient(app)


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_full_parse_empty_is_zero_pages(client):
    r = client.post("/api/parse", json={"content": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["page_count"] == 0
    assert body["document"]["pages"] == []
    assert body["recomputed_pages"] == []


def test_full_parse_and_page_endpoint(client):
    r = client.post("/api/parse", json={"content": "甲\n---\n乙\n"})
    assert r.status_code == 200
    assert r.json()["page_count"] == 2

    r2 = client.post("/api/parse/page/2", json={"content": "甲\n---\n乙\n"})
    assert r2.status_code == 200
    assert r2.json()["page"]["page_no"] == 2

    missing = client.post("/api/parse/page/9", json={"content": "甲"})
    assert missing.status_code == 404


def test_edits_endpoint_reports_recomputed(client):
    r = client.post("/api/parse/edits", json={
        "content": "甲\n---\n乙\n",
        "edits": [{"start": 0, "end": 1, "replacement": "改"}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["recomputed_pages"] == [1]
    assert body["reused_pages"] == [2]
    assert body["new_content"] == "改\n---\n乙\n"


def test_document_lifecycle_and_conflict(client):
    # 新建
    r = client.post("/api/documents", json={"title": "t", "content": "v1"})
    assert r.status_code == 201
    doc_id = r.json()["id"]

    # 读取
    got = client.get(f"/api/documents/{doc_id}")
    assert got.status_code == 200
    assert got.json()["revision"] == 1
    assert "document" in got.json()

    # 基于 1 保存成功
    ok = client.put(f"/api/documents/{doc_id}",
                    json={"content": "v2", "base_revision": 1})
    assert ok.status_code == 200
    assert ok.json()["revision"] == 2

    # 再拿过期的 1 保存 -> 409，带当前修订与内容
    conflict = client.put(f"/api/documents/{doc_id}",
                          json={"content": "过期", "base_revision": 1})
    assert conflict.status_code == 409
    body = conflict.json()
    assert body["error"] == "revision_conflict"
    assert body["current_revision"] == 2
    assert body["current_content"] == "v2"

    # 历史列表与历史读取
    hist = client.get(f"/api/documents/{doc_id}/revisions")
    assert [x["revision"] for x in hist.json()["revisions"]] == [1, 2]
    old = client.get(f"/api/documents/{doc_id}/revisions/1")
    assert old.json()["content"] == "v1"
    assert old.json()["document"]["pages"]


def test_invalid_content_422(client):
    long_text = "x" * (2 * 1024 * 1024 + 1)
    r = client.post("/api/documents", json={"content": long_text})
    assert r.status_code == 422
    r2 = client.post("/api/documents", json={"content": "a\x00b"})
    assert r2.status_code == 422
