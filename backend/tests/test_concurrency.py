"""同基准并发保存的测试。

约定（产品承诺）：拿同一个 base_revision 同时保存同一份讲稿，
无论并发多少个，必须——

- 恰好一个成功（返回新修订）；
- 其余全部 409（ConflictError），并带回服务端当前修订号与内容；
- 不允许 500（IntegrityError 漏出）或写一半的修订；
- 修订号连续、不跳不重（1, 2, 3, …），历史链完整。

覆盖两层：

1. 本文件：多个**各自独立的 SQLite 连接**（每线程一个 Storage 实例）
   共用同一个库文件、真实并发写。这与“多个服务进程共用一个库”是
   同一种锁语义——裁决必须由 SQLite 的写事务给出，进程内排队
   （一把 threading 锁）在这些连接之间根本不存在。
   另见 test_concurrency_multiproc.py 的跨 OS 进程对拍。
2. HTTP 层见 test_api.py：同步路由跑在线程池，同基准并发 PUT。

为了让竞态“稳定复现”而不靠调度运气，_WriteGate 让每个线程先用
**自己的连接**在事务之外核对基准（这正是旧实现核对/写入两步分离
的窗口），所有人核对通过后挡在写事务门口，由主线程统一放行、
同时进入真正的写事务：

- 旧实现（锁外核对 + 锁内写）：多个连接都已读到未前进的 current，
  放行后抢插同一修订号 → IntegrityError（HTTP 层即 500）；
- 新实现（核对并入 BEGIN IMMEDIATE）：写锁把事务串行化，先到者
  写 rev=N+1 并提交，后到者拿到锁后在事务内重读，current 已前进，
  一律 ConflictError。

用“条件变量计数 + 放行事件”而非 Barrier：Barrier 到齐即自动放行，
主线程插不进也观测不到那个瞬时；这里由主线程 wait_all() 后 open()。
"""
from __future__ import annotations

import os
import tempfile
import threading
import time

import pytest

from app.storage import ConflictError, Storage

CONCURRENCY = 16
ROUNDS = 5


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    seed = Storage(path)
    try:
        yield path
    finally:
        seed.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass


class _WriteGate:
    """见模块 docstring。按 Storage 实例（连接）打补丁，门内核对走
    各线程自己的连接，放行后调用真正的 save_revision。"""

    def __init__(self, n: int):
        self.n = n
        self._cond = threading.Condition()
        self._waiting = 0
        self._release = threading.Event()
        self._orig_save = Storage.save_revision

    def _arrive(self):
        with self._cond:
            self._waiting += 1
            self._cond.notify_all()
        assert self._release.wait(timeout=10), "门未在超时内放行"

    def wait_all(self):
        with self._cond:
            deadline = time.monotonic() + 5
            while self._waiting < self.n:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("并发线程未在写事务门口到齐")
                self._cond.wait(timeout=remaining)

    def open(self):
        self.wait_all()
        self._release.set()

    def install(self):
        gate = self
        orig_save = self._orig_save

        def save_gated(self, doc_id, content, base_revision, title=None):
            # —— 复刻旧实现：用“自己的连接”在事务之外核对基准 ——
            meta = self.get_meta(doc_id)
            if base_revision != meta.current_revision:
                current = self.get_revision(doc_id, meta.current_revision)
                raise ConflictError(
                    current_revision=meta.current_revision,
                    current_content=current.content,
                    document_id=doc_id,
                    base_revision=base_revision,
                )
            # —— 核对通过，挡在写事务门口，等主线程统一放行 ——
            gate._arrive()
            return orig_save(self, doc_id, content, base_revision, title)

        Storage.save_revision = save_gated

    def restore(self):
        Storage.save_revision = self._orig_save


def test_concurrent_same_base_across_connections_exactly_one_wins(db_path):
    seed = Storage(db_path)
    meta = seed.create_document("v1")
    base = 1  # 当前修订号；每轮唯一胜者让它 +1
    seed.close()

    for rnd in range(ROUNDS):
        # 每个并发线程一个独立 Storage = 独立连接（同库文件），
        # 不存在共享的进程锁，逼出数据库层面的真实裁决。
        workers = [Storage(db_path) for _ in range(CONCURRENCY)]
        gate = _WriteGate(CONCURRENCY)
        results: list[object] = [None] * CONCURRENCY
        start = threading.Barrier(CONCURRENCY)

        def worker(i):
            start.wait()
            try:
                results[i] = workers[i].save_revision(
                    meta.id, f"r{rnd}-c{i}", base_revision=base,
                )
            except Exception as exc:  # noqa: BLE001 - 在断言处分类
                results[i] = exc

        gate.install()
        try:
            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(CONCURRENCY)]
            for t in threads:
                t.start()
            gate.open()
            for t in threads:
                t.join(timeout=15)
                assert not t.is_alive(), "并发保存出现死锁/挂起"
        finally:
            gate.restore()
            for w in workers:
                w.close()

        oks = [r for r in results if not isinstance(r, Exception)]
        conflicts = [r for r in results if isinstance(r, ConflictError)]
        others = [r for r in results
                  if isinstance(r, Exception) and not isinstance(r, ConflictError)]
        assert not others, (
            f"第 {rnd} 轮出现 409 以外的异常（旧 bug 即 IntegrityError）: "
            f"{[(type(r).__name__, str(r)[:60]) for r in others]}"
        )
        assert len(oks) == 1, f"第 {rnd} 轮成功数应为 1，实际 {len(oks)}"
        assert len(conflicts) == CONCURRENCY - 1

        winner_rev = oks[0].revision
        assert winner_rev == base + 1
        for c in conflicts:
            assert c.current_revision == winner_rev
            assert c.base_revision == base
            assert c.document_id == meta.id

        # 用一个全新连接核验落盘事实：修订连续不跳不重、当前停在胜者版本
        checker = Storage(db_path)
        try:
            revs = [r.revision for r in checker.list_revisions(meta.id)]
            assert revs == list(range(1, winner_rev + 1))
            assert checker.get_meta(meta.id).current_revision == winner_rev
            assert checker.get_revision(meta.id).content == oks[0].content
        finally:
            checker.close()
        base = winner_rev  # 下一轮所有人基于本轮唯一胜者的新修订


def test_chain_of_stale_then_fresh_bases(db_path):
    """按顺序错开基准保存：rev 1→2→3… 连续；中途夹过期保存必 409。"""
    db = Storage(db_path)
    meta = db.create_document("v0")
    n = 12
    for rev in range(1, n + 1):
        r = db.save_revision(meta.id, f"v{rev}", base_revision=rev)
        assert r.revision == rev + 1
        if rev == n:
            continue
        # 每次保存后，旧基准立刻过期
        with pytest.raises(ConflictError) as ei:
            db.save_revision(meta.id, "过期", base_revision=rev)
        assert ei.value.current_revision == rev + 1
        assert ei.value.base_revision == rev
    revs = [r.revision for r in db.list_revisions(meta.id)]
    assert revs == list(range(1, n + 2))
    assert db.get_meta(meta.id).current_revision == n + 1
    db.close()
