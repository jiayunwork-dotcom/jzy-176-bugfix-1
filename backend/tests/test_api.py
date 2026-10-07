"""HTTP 接口测试（FastAPI TestClient，临时 SQLite）。"""
import os
import tempfile
import threading
import time

import pytest

# 在导入 api 前把数据库指到临时文件，保证各用例隔离
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DECK_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient                  # noqa: E402

from app.api import app, storage                            # noqa: E402
from app.storage import Storage                             # noqa: E402


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


class _HTTPCheckGate:
    """把所有并发请求挡在“事务外核对之后、写事务之前”，由主线程放行。

    与 test_concurrency.py 的 _WriteGate 同理：显式复刻旧实现的
    核对顺序（锁外读 current → 通过 → 挡住 → 统一放行 → 进写事务）。
    旧代码在此必然多个请求抢插同号修订 → IntegrityError → 500；
    新代码在 BEGIN IMMEDIATE 事务内重读 current，先写者赢、其余 409。
    """

    def __init__(self, n):
        self.n = n
        self._cond = threading.Condition()
        self._waiting = 0
        self._release = threading.Event()
        self._orig_save = Storage.save_revision

    def _arrive(self):
        with self._cond:
            self._waiting += 1
            self._cond.notify_all()
        self._release.wait(timeout=10)

    def wait_all(self):
        with self._cond:
            deadline = time.monotonic() + 5
            while self._waiting < self.n:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("并发请求未在写事务门口到齐")
                self._cond.wait(timeout=remaining)

    def open(self):
        self.wait_all()
        self._release.set()

    def install(self):
        gate = self
        orig_save = self._orig_save
        db_path = os.environ["DECK_DB_PATH"]

        def save_gated(self, doc_id, content, base_revision, title=None):
            # 每个并发请求用自己的独立连接，复刻真实的跨连接竞态
            # （共享 Storage 的单连接 + 进程锁会掩盖它）。
            own = Storage(db_path)
            try:
                # —— 复刻旧实现：事务之外用自己的连接核对基准 ——
                meta = own.get_meta(doc_id)
                if base_revision != meta.current_revision:
                    current = own.get_revision(doc_id, meta.current_revision)
                    from app.storage import ConflictError
                    raise ConflictError(
                        current_revision=meta.current_revision,
                        current_content=current.content,
                        document_id=doc_id,
                        base_revision=base_revision,
                    )
                # —— 核对通过，挡在写事务门口，等主线程统一放行 ——
                gate._arrive()
                return orig_save(
                    own, doc_id, content, base_revision, title,
                )
            finally:
                own.close()

        Storage.save_revision = save_gated

    def restore(self):
        Storage.save_revision = self._orig_save


def test_concurrent_same_base_put_exactly_one_200_rest_409(client):
    """同基准并发 PUT：恰一个 200，其余 409，绝不 500；修订连续。"""
    n = 16
    for rnd in range(5):
        created = client.post("/api/documents",
                              json={"title": "race", "content": "v1"})
        assert created.status_code == 201
        doc_id = created.json()["id"]

        gate = _HTTPCheckGate(n)
        gate.install()
        statuses: list[int | None] = [None] * n
        bodies: list[dict] = [None] * n
        start = threading.Barrier(n)

        def worker(i):
            start.wait()
            resp = client.put(
                f"/api/documents/{doc_id}",
                json={"content": f"r{rnd}-c{i}", "base_revision": 1},
            )
            statuses[i] = resp.status_code
            bodies[i] = resp.json()

        try:
            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(n)]
            for t in threads:
                t.start()
            gate.open()
            for t in threads:
                t.join(timeout=10)
                assert not t.is_alive(), "并发 PUT 挂起"
        finally:
            gate.restore()

        assert statuses.count(200) == 1, statuses
        assert statuses.count(409) == n - 1, statuses
        assert 500 not in statuses, "同基准并发不应产生 500"

        winner = next(b for s, b in zip(statuses, bodies) if s == 200)
        assert winner["revision"] == 2
        for s, b in zip(statuses, bodies):
            if s == 409:
                assert b["error"] == "revision_conflict"
                assert b["base_revision"] == 1
                assert b["current_revision"] == 2
                assert b["current_content"].startswith("r")
                assert b["document_id"] == doc_id

        hist = client.get(f"/api/documents/{doc_id}/revisions").json()
        assert [x["revision"] for x in hist["revisions"]] == [1, 2]
        current = client.get(f"/api/documents/{doc_id}").json()
        assert current["current_revision"] == 2
        assert current["content"] == winner["content"]


def test_ungated_concurrent_same_base_never_500(client):
    """不设卡、原始时序的高并发：允许调度运气，但任何一轮都不许 5xx。

    多次重复（每轮新建文档）正是线上“连着试多轮都能见到”的形态。
    """
    n = 12
    for rnd in range(10):
        created = client.post("/api/documents",
                              json={"title": "plain", "content": "v1"})
        doc_id = created.json()["id"]
        start = threading.Barrier(n)
        statuses: list[int] = []

        def worker():
            start.wait()
            resp = client.put(
                f"/api/documents/{doc_id}",
                json={"content": "same", "base_revision": 1},
            )
            statuses.append(resp.status_code)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(statuses) == n
        assert all(s in (200, 409) for s in statuses), statuses
        assert statuses.count(200) == 1
        assert statuses.count(409) == n - 1
        hist = client.get(f"/api/documents/{doc_id}/revisions").json()
        assert [x["revision"] for x in hist["revisions"]] == [1, 2]
