"""讲稿存储（SQLite）。

每份讲稿是不可变的修订链：每次保存产生一个新 revision（递增），
历史修订永不改写、永不删除。revision=0 表示“尚未保存”，新建/首次
保存从 1 开始。

并发正确性（乐观锁）：保存必须携带它所依据的 base_revision；
服务端当前修订已更新则拒绝（ConflictError），返回当前修订与文本，
绝不悄悄覆盖。

“核对基准修订”与“写入新修订”必须是不可分割的一步，这一点不能只靠
本进程内的锁——多个服务进程可以共用同一个库文件。保存走一条
*写事务*（BEGIN IMMEDIATE）：先拿到 SQLite 的库级写锁，再在同一事务
里核对 current_revision 并插入新修订行。拿不到锁的进程在 busy_timeout
内等待；等拿到锁时，先提交者已经把 current_revision 推进，过期方在
事务内看到新值并得到 ConflictError，于是同基准并发保存的结果恒为
“恰好一个成功、其余 409”，且失败路径在提交前回滚，不留半截修订。
完整论证与代价见 docs/concurrency.md。

存储与解析的咬合：每个 revision 行存原始文本；结构树不入库，按需由
ParseEngine 解析。每个修订有自己独立的引擎/缓存（_ParseStore 以
(document_id, revision) 为键），增量缓存绝不跨修订串用。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from .parser import ParseEngine
from .parser.engine import Edit

BUSY_TIMEOUT_MS = 5000


class ConflictError(Exception):
    """base_revision 已过期：保存被拒。携带服务端当前修订与文本。"""

    def __init__(self, current_revision: int, current_content: str,
                 document_id: int):
        super().__init__(f"修订 {current_revision} 已更新，你的保存基于过期版本")
        self.current_revision = current_revision
        self.current_content = current_content
        self.document_id = document_id


class NotFoundError(Exception):
    pass


class ValidationError(Exception):
    pass


MAX_DOCUMENT_BYTES = 2 * 1024 * 1024     # 单份讲稿 2 MiB 上限


@dataclass(frozen=True)
class Revision:
    document_id: int
    revision: int
    content: str
    created_at: float


@dataclass(frozen=True)
class DocumentMeta:
    id: int
    title: str
    current_revision: int
    created_at: float
    updated_at: float


def validate_content(content: str) -> None:
    if not isinstance(content, str):
        raise ValidationError("内容必须是文本（字符串）")
    try:
        nbytes = len(content.encode("utf-8"))
    except (UnicodeEncodeError, AttributeError):
        raise ValidationError("内容不是合法的 Unicode 文本")
    if nbytes > MAX_DOCUMENT_BYTES:
        raise ValidationError(
            f"讲稿过长：{nbytes} 字节，超过 {MAX_DOCUMENT_BYTES} 字节上限"
        )
    # 拒绝明显的二进制特征：NUL 字符在正常讲稿里不该出现
    if "\x00" in content:
        raise ValidationError("内容含 NUL 字符，不像文本，已拒绝")


class _CachedParse:
    """某一修订的解析状态：引擎 + 最近一次增量的旧文本。"""
    __slots__ = ("engine",)

    def __init__(self) -> None:
        self.engine = ParseEngine()


class Storage:
    def __init__(self, path: str = ":memory:"):
        # 进程内锁只保护解析缓存；落盘的正确性由 SQLite 写锁裁决。
        self._lock = threading.RLock()
        # isolation_level=None：事务边界全部由我们自己显式控制，
        # 避免 Python sqlite3 隐式 BEGIN 与 BEGIN IMMEDIATE 互相打架。
        self._conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        # 解析缓存：(doc_id, revision) -> _CachedParse，绝不跨修订共用
        self._parse_cache: dict[tuple[int, int], _CachedParse] = {}

    @contextmanager
    def _tx(self):
        """只读事务：BEGIN（延迟）+ COMMIT/ROLLBACK。

        同进程内多个线程共用一个连接，事务边界（BEGIN..COMMIT）必须在
        进程锁内串行，否则两个线程会在同一连接上交错开关事务。
        跨进程的裁决不依赖这把锁，而由 SQLite 写锁完成（见 _write_tx）。
        """
        conn = self._conn
        with self._lock:
            conn.execute("BEGIN")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @contextmanager
    def _write_tx(self):
        """写事务：BEGIN IMMEDIATE 立即取得库级保留写锁。

        拿到锁之后再读 current_revision，读到的一定是已提交的最新值；
        核对与插入在同一事务内完成，中间不可能有别的进程推进修订号。
        进程锁只负责让同进程线程不在同一连接上交错事务；进程间互斥由
        BEGIN IMMEDIATE 的写锁 + busy_timeout 保证，因此多进程共用同一
        库文件时乐观锁语义依然成立。
        """
        conn = self._conn
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _init_schema(self) -> None:
        # executescript 会先提交当前事务，且 DDL 仅在启动时执行一次，
        # 不放进显式事务；各 CREATE 语句自带 IF NOT EXISTS。
        self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    current_revision INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    document_id INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (document_id, revision),
                    FOREIGN KEY (document_id) REFERENCES documents(id)
                );
                CREATE INDEX IF NOT EXISTS idx_rev_doc
                    ON revisions(document_id, revision);
                """
            )

    # ------------------------------------------------------------ 文档

    def create_document(self, content: str = "", title: str = "无标题讲稿") -> DocumentMeta:
        validate_content(content)
        now = time.time()
        with self._write_tx() as conn:
            cur = conn.execute(
                "INSERT INTO documents(title, current_revision, created_at, updated_at)"
                " VALUES (?, 1, ?, ?)",
                (title, now, now),
            )
            doc_id = cur.lastrowid
            conn.execute(
                "INSERT INTO revisions(document_id, revision, content, created_at)"
                " VALUES (?, 1, ?, ?)",
                (doc_id, content, now),
            )
        self._parse_cache[(doc_id, 1)] = _CachedParse()
        return self.get_meta(doc_id)

    def get_meta(self, doc_id: int) -> DocumentMeta:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE id=?", (doc_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"讲稿 {doc_id} 不存在")
        return DocumentMeta(
            id=row["id"], title=row["title"],
            current_revision=row["current_revision"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def list_documents(self) -> list[DocumentMeta]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM documents ORDER BY updated_at DESC, id"
            ).fetchall()
        return [DocumentMeta(
            id=r["id"], title=r["title"], current_revision=r["current_revision"],
            created_at=r["created_at"], updated_at=r["updated_at"],
        ) for r in rows]

    # ------------------------------------------------------------ 修订

    def get_revision(self, doc_id: int, revision: Optional[int] = None) -> Revision:
        meta = self.get_meta(doc_id)
        rev = meta.current_revision if revision is None else revision
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM revisions WHERE document_id=? AND revision=?",
                (doc_id, rev),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"讲稿 {doc_id} 修订 {rev} 不存在")
        return Revision(
            document_id=doc_id, revision=rev,
            content=row["content"], created_at=row["created_at"],
        )

    def list_revisions(self, doc_id: int) -> list[Revision]:
        self.get_meta(doc_id)
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM revisions WHERE document_id=? ORDER BY revision",
                (doc_id,),
            ).fetchall()
        return [Revision(
            document_id=doc_id, revision=r["revision"],
            content=r["content"], created_at=r["created_at"],
        ) for r in rows]

    def save_revision(
        self,
        doc_id: int,
        content: str,
        base_revision: int,
        title: Optional[str] = None,
    ) -> Revision:
        """以 base_revision 为依据保存新修订；过期则抛 ConflictError。

        核对基准与写入新修订在同一条 BEGIN IMMEDIATE 写事务里完成，
        对多进程共用同一库文件同样成立：拿到写锁的事务看到的
        current_revision 一定是最新已提交值，随后的插入与推进原子可见。
        """
        validate_content(content)
        try:
            with self._write_tx() as conn:
                row = conn.execute(
                    "SELECT current_revision FROM documents WHERE id=?",
                    (doc_id,),
                ).fetchone()
                if row is None:
                    # NotFound 在事务内抛出后由上下文管理器回滚（此处无写入）
                    raise NotFoundError(f"讲稿 {doc_id} 不存在")
                current = row["current_revision"]
                if base_revision != current:
                    cur_row = conn.execute(
                        "SELECT content FROM revisions"
                        " WHERE document_id=? AND revision=?",
                        (doc_id, current),
                    ).fetchone()
                    raise ConflictError(
                        current_revision=current,
                        current_content=cur_row["content"] if cur_row else "",
                        document_id=doc_id,
                    )
                new_rev = current + 1
                now = time.time()
                conn.execute(
                    "INSERT INTO revisions(document_id, revision, content, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (doc_id, new_rev, content, now),
                )
                if title is not None:
                    conn.execute(
                        "UPDATE documents SET current_revision=?, updated_at=?, title=?"
                        " WHERE id=?",
                        (new_rev, now, title, doc_id),
                    )
                else:
                    conn.execute(
                        "UPDATE documents SET current_revision=?, updated_at=? WHERE id=?",
                        (new_rev, now, doc_id),
                    )
        except sqlite3.IntegrityError as exc:
            # 兜底：即便底层锁策略将来被改动，唯一键冲突也必须表现为 409
            # 语义而不是 500。重新取一次当前状态，转成冲突。
            meta = self.get_meta(doc_id)
            current = self.get_revision(doc_id, meta.current_revision)
            raise ConflictError(
                current_revision=meta.current_revision,
                current_content=current.content,
                document_id=doc_id,
            ) from exc
        self._parse_cache[(doc_id, new_rev)] = _CachedParse()
        return self.get_revision(doc_id, new_rev)

    # ------------------------------------------------------------ 解析

    def _cache(self, doc_id: int, revision: int) -> _CachedParse:
        key = (doc_id, revision)
        cp = self._parse_cache.get(key)
        if cp is None:
            # 历史修订首次访问：惰性建立独立缓存，不与任何别的修订共享
            cp = _CachedParse()
            self._parse_cache[key] = cp
        return cp

    def parse_revision(self, doc_id: int, revision: Optional[int] = None):
        """全量解析指定修订（默认当前）。返回 Document 树。"""
        rev = self.get_revision(doc_id, revision)
        cp = self._cache(doc_id, rev.revision)
        with self._lock:
            return cp.engine.parse_full(rev.content)

    def parse_page(self, doc_id: int, page_no: int,
                   revision: Optional[int] = None):
        rev = self.get_revision(doc_id, revision)
        cp = self._cache(doc_id, rev.revision)
        with self._lock:
            return cp.engine.get_page(rev.content, page_no)

    def apply_edits_incremental(
        self, doc_id: int, base_revision: int, edits: list[Edit],
    ):
        """在指定修订的文本上做增量解析（不落盘）。

        只针对该修订自己的引擎缓存，返回 IncrementalResult 与新文本。
        持久化走 save_revision（会建立新修订的独立缓存）。
        """
        rev = self.get_revision(doc_id, base_revision)
        cp = self._cache(doc_id, base_revision)
        with self._lock:
            return cp.engine.apply_incremental(rev.content, edits)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
            self._parse_cache.clear()
