"""存储测试：新建/读取/乐观锁/历史修订/存储与解析一致。"""
import pytest

from app.parser import ParseEngine, to_plain
from app.storage import (
    ConflictError, NotFoundError, ValidationError,
)


def test_create_and_read(store):
    meta = store.create_document("# 标题\n", "我的讲稿")
    assert meta.current_revision == 1
    got = store.get_revision(meta.id)
    assert got.content == "# 标题\n" and got.revision == 1


def test_save_increments_revision(store):
    meta = store.create_document("v1")
    r2 = store.save_revision(meta.id, "v2", base_revision=1)
    assert r2.revision == 2
    assert store.get_meta(meta.id).current_revision == 2
    # 历史修订仍可读且未被改写
    assert store.get_revision(meta.id, 1).content == "v1"


def test_stale_revision_save_rejected(store):
    meta = store.create_document("v1")
    store.save_revision(meta.id, "v2", base_revision=1)
    # 另一个标签页仍拿着 revision=1 来保存
    with pytest.raises(ConflictError) as ei:
        store.save_revision(meta.id, "来自过期标签页", base_revision=1)
    assert ei.value.current_revision == 2
    assert ei.value.current_content == "v2"
    # 服务端内容没有被覆盖
    assert store.get_revision(meta.id).content == "v2"


def test_get_nonexistent_raises(store):
    with pytest.raises(NotFoundError):
        store.get_revision(999)
    with pytest.raises(NotFoundError):
        store.get_meta(999)


def test_parse_revision_matches_full_parse(store, sample_text):
    meta = store.create_document(sample_text)
    stored_tree = to_plain(store.parse_revision(meta.id))
    full_tree = to_plain(ParseEngine().parse_full(sample_text))
    assert stored_tree == full_tree
    # 历史修订同样成立
    store.save_revision(meta.id, sample_text + "\n---\n追加页\n", 1)
    old_tree = to_plain(store.parse_revision(meta.id, 1))
    assert old_tree == to_plain(ParseEngine().parse_full(sample_text))


def test_incremental_cache_isolated_between_revisions(store, sample_text):
    meta = store.create_document(sample_text)
    from app.parser import Edit
    # 在 revision 1 上做增量（不落盘），不能污染别的修订缓存
    r1 = store.apply_edits_incremental(meta.id, 1, [Edit(0, 0, "草稿")])
    assert r1.document is not None
    # revision 2 落盘后是完全不同的文本
    store.save_revision(meta.id, "完全不同\n---\n内容", 1)
    tree = to_plain(store.parse_revision(meta.id, 2))
    assert tree == to_plain(ParseEngine().parse_full("完全不同\n---\n内容"))


def test_validation_rejects_non_text_and_oversize(store):
    with pytest.raises(ValidationError):
        store.create_document("含 NUL\x00 不像文本")
    with pytest.raises(ValidationError):
        # 直接调校验，避免构造超大对象
        from app.storage import validate_content, MAX_DOCUMENT_BYTES
        validate_content("x" * (MAX_DOCUMENT_BYTES + 1))
