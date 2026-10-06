"""讲稿存储（SQLite）。

每份讲稿是不可变的修订链：每次保存产生一个新 revision（递增），
历史修订永不改写、永不删除。revision=0 表示“尚未保存”，新建/首次
保存从 1 开始。

并发正确性（乐观锁）：保存必须携带它所依据的 base_revision；
服务端当前修订已更新则拒绝（ConflictError），返回当前修订与文本，
绝不悄悄覆盖。

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
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        # 解析缓存：(doc_id, revision) -> _CachedParse，绝不跨修订共用
        self._parse_cache: dict[tuple[int, int], _CachedParse] = {}

    @contextmanager
    def _tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _init_schema(self) -> None:
        with self._tx() as conn:
            conn.executescript(
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
        with self._tx() as conn:
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
        """以 base_revision 为依据保存新修订；过期则抛 ConflictError。"""
        validate_content(content)
        meta = self.get_meta(doc_id)
        if base_revision != meta.current_revision:
            current = self.get_revision(doc_id, meta.current_revision)
            raise ConflictError(
                current_revision=meta.current_revision,
                current_content=current.content,
                document_id=doc_id,
            )
        new_rev = meta.current_revision + 1
        now = time.time()
        with self._tx() as conn:
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
