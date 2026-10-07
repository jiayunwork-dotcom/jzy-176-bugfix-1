"""同基准并发保存的回归测试。

约定：携带相同 base_revision 的并发保存，无论来多少个、来自多少个
线程/进程，结果必须是——恰好一个写出新修订，其余全部 409（带回服务端
当前修订号与内容），不出现 5xx，修订链连续不跳不重，没有写了一半的
行。

覆盖三层：
1. Storage 内存库（同进程多线程，屏障同时放行）；
2. HTTP TestClient（真实走路由/线程池，断言状态码与响应体）；
3. 多个独立进程共用同一个库文件（验证裁决在数据库而非进程锁）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.storage import ConflictError, Storage

BACKEND = Path(__file__).resolve().parent.parent
N = 20
ROUNDS = 8


# --------------------------------------------------------------- Storage

def test_concurrent_same_base_storage_only_one_wins():
    for round_ in range(ROUNDS):
        store = Storage(":memory:")
        meta = store.create_document("v1")
        barrier = threading.Barrier(N)
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker(i: int) -> None:
            barrier.wait()  # 所有线程同时冲进 save_revision
            try:
                store.save_revision(meta.id, f"x{i}", base_revision=1)
                out = "ok"
            except ConflictError:
                out = "conflict"
            except Exception as exc:  # 任何别的异常都算违约（曾经的 500）
                out = f"error:{type(exc).__name__}"
            with lock:
                outcomes.append(out)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert outcomes.count("ok") == 1, (round_, outcomes)
        assert outcomes.count("conflict") == N - 1, (round_, outcomes)
        assert not [o for o in outcomes if o.startswith("error")], (round_, outcomes)

        revs = store.list_revisions(meta.id)
        assert [r.revision for r in revs] == [1, 2]
        assert store.get_meta(meta.id).current_revision == 2
        # 只有一份内容落盘，文档指针与修订行一致（无“半截修订”）
        assert store.get_revision(meta.id, 2).content.startswith("x")
        store.close()


def test_concurrent_bursts_chained_revisions_stay_contiguous():
    """两批同基准并发（先抢 rev2，再抢 rev3）：修订号连续不跳不重。"""
    store = Storage(":memory:")
    meta = store.create_document("v1")

    def burst(base: int) -> None:
        barrier = threading.Barrier(N)
        winners = []
        wlock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            try:
                store.save_revision(meta.id, f"content-base-{base}",
                                    base_revision=base)
                with wlock:
                    winners.append(1)
            except ConflictError:
                pass

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(winners) == 1

    burst(base=1)
    burst(base=2)
    assert [r.revision for r in store.list_revisions(meta.id)] == [1, 2, 3]
    assert store.get_meta(meta.id).current_revision == 3
    store.close()


# ------------------------------------------------------------------ HTTP

@pytest.fixture
def api_client(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setenv("DECK_DB_PATH", str(db_path))
    # 延迟导入并替换全局 Storage，保证本用例拿到独立文件库
    from app import api as api_module
    from fastapi.testclient import TestClient

    monkeypatch.setattr(api_module, "_storage",
                        api_module.Storage(str(db_path)))
    yield TestClient(api_module.app)
    api_module.storage().close()


def test_concurrent_same_base_http_exactly_one_200_rest_409(api_client):
    doc_id = api_client.post(
        "/api/documents", json={"content": "v1"}
    ).json()["id"]

    barrier = threading.Barrier(N)
    codes: list[int] = []
    bodies: list[dict] = []
    rlock = threading.Lock()

    def worker(i: int) -> None:
        # 每个线程自己的 TestClient/portal，屏障后同时发请求
        from fastapi.testclient import TestClient
        client = TestClient(api_client.app)
        barrier.wait()
        resp = client.put(
            f"/api/documents/{doc_id}",
            json={"content": f"from-{i}", "base_revision": 1},
        )
        with rlock:
            codes.append(resp.status_code)
            if resp.status_code == 409:
                bodies.append(resp.json())

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert codes.count(200) == 1, sorted(codes)
    assert codes.count(409) == N - 1, sorted(codes)
    assert not [c for c in codes if c >= 500], sorted(codes)
    # 每个 409 都按约定带回当前修订号、当前内容与文档 id
    assert bodies, "至少应有一个 409 响应体"
    for body in bodies:
        assert body["error"] == "revision_conflict"
        assert body["current_revision"] == 2
        assert body["current_content"].startswith("from-")
        assert body["document_id"] == doc_id

    hist = api_client.get(f"/api/documents/{doc_id}/revisions").json()
    assert [r["revision"] for r in hist["revisions"]] == [1, 2]
    current = api_client.get(f"/api/documents/{doc_id}").json()
    assert current["revision"] == 2


# ------------------------------------------------------------ 多进程

WORKER = (
    """
import os, sys, time
sys.path.insert(0, __BACKEND__)
from app.storage import Storage, ConflictError

db_path, doc_id, enter_dir, go_file = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
# 进场登记后等待统一放行，最大化“同时 BEGIN IMMEDIATE”的碰撞
open(os.path.join(enter_dir, str(os.getpid())), "w").close()
for _ in range(1000):
    if os.path.exists(go_file):
        break
    time.sleep(0.005)
store = Storage(db_path)
try:
    store.save_revision(doc_id, "content-from-pid-%d" % os.getpid(), base_revision=1)
    print("ok")
except ConflictError as exc:
    print("409:%d" % exc.current_revision)
except Exception as exc:
    print("500:%s:%s" % (type(exc).__name__, exc))
finally:
    store.close()
""".replace("__BACKEND__", repr(str(BACKEND)))
)


def test_concurrent_same_base_across_processes(tmp_path):
    """多个独立进程（各自连接、各自锁）共用同一个库文件。

    进程内锁在此完全帮不上忙；该用例证明“核对+写入不可分割”由
    SQLite 写事务裁决，横向扩成多进程时承诺依然成立。
    """
    db_path = tmp_path / "shared.db"
    enter_dir = tmp_path / "entered"
    enter_dir.mkdir()
    go_file = tmp_path / "go"

    store = Storage(str(db_path))
    meta = store.create_document("v1")
    store.close()

    nproc = 10
    procs = []
    for _ in range(nproc):
        procs.append(subprocess.Popen(
            [sys.executable, "-c", WORKER,
             str(db_path), str(meta.id), str(enter_dir), str(go_file)],
            stdout=subprocess.PIPE, text=True,
        ))
    # 等所有子进程进场后再放行
    deadline = 10
    waited = 0.0
    while len(list(enter_dir.iterdir())) < nproc and waited < deadline:
        time.sleep(0.02)
        waited += 0.02
    assert len(list(enter_dir.iterdir())) == nproc
    go_file.write_text("go")

    outs = sorted(p.communicate(timeout=30)[0].strip() for p in procs)
    assert outs.count("ok") == 1, outs
    assert outs.count("409:2") == nproc - 1, outs
    assert not [o for o in outs if o.startswith("500")], outs

    store = Storage(str(db_path))
    assert [r.revision for r in store.list_revisions(meta.id)] == [1, 2]
    assert store.get_meta(meta.id).current_revision == 2
    store.close()
