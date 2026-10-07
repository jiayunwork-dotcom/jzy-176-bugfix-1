"""多进程共用同一个 SQLite 库文件时的并发保存。

裁决必须由数据库给出：只在进程内排队（一把 threading 锁）在
多个服务进程 + 共享卷的部署形态下完全不存在。这里起多个 OS 进程、
各自持有独立的 Storage（独立连接），对同一库文件同基准并发保存，
要求与单进程同样严格：恰一个赢，其余 409，修订连续。

注意：用 spawn/fork 均可，这里默认 fork（Linux 测试环境），
把路径与 doc_id 通过参数传入。
"""
from __future__ import annotations

import os
import tempfile
from multiprocessing import Barrier, Process, Queue

import pytest

from app.storage import ConflictError, Storage

N_PROCS = 12


def _concurrent_saver(i: int, db_path: str, doc_id: int,
                      barrier, queue, ready_path: str):
    # fork 后子进程重新建自己的连接，确保不是共享父进程连接
    store = Storage(db_path)
    try:
        barrier.wait()
        try:
            store.save_revision(doc_id, f"proc-{i}", base_revision=1)
            queue.put(("ok", i, None))
        except ConflictError as exc:
            queue.put(("conflict", exc.current_revision, exc.base_revision))
        except Exception as exc:  # noqa: BLE001
            queue.put(("ERROR", type(exc).__name__, str(exc)[:120]))
    finally:
        store.close()
        # 写一个标记文件，帮助排查子进程是否真的都跑到了
        with open(ready_path + f".{i}", "w") as f:
            f.write("done")


@pytest.mark.parametrize("round_", range(3))
def test_multiprocess_same_db_one_winner(round_, tmp_path):
    db_path = str(tmp_path / "shared.db")
    seed = Storage(db_path)
    doc_id = seed.create_document("v1").id
    seed.close()

    queue: Queue = Queue()
    barrier = Barrier(N_PROCS)
    ready = str(tmp_path / "ready")
    procs = [
        Process(target=_concurrent_saver,
                args=(i, db_path, doc_id, barrier, queue, ready))
        for i in range(N_PROCS)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=20)
        assert p.exitcode == 0, f"子进程异常退出: {p.exitcode}"

    results = [queue.get(timeout=5) for _ in range(N_PROCS)]
    kinds = [r[0] for r in results]
    assert kinds.count("ok") == 1, results
    assert kinds.count("conflict") == N_PROCS - 1, results
    assert "ERROR" not in kinds, results
    for kind, cur, base in results:
        if kind == "conflict":
            assert cur == 2 and base == 1

    checker = Storage(db_path)
    try:
        revs = [r.revision for r in checker.list_revisions(doc_id)]
        assert revs == [1, 2]
        assert checker.get_meta(doc_id).current_revision == 2
        assert checker.get_revision(doc_id, 2).content.startswith("proc-")
        # 败者一方的原始内容并不在新修订里（没有静默覆盖），
        # 但它拿到的 409 里带回了服务端内容，可在前端合并后再存。
        assert checker.get_revision(doc_id, 1).content == "v1"
    finally:
        checker.close()
