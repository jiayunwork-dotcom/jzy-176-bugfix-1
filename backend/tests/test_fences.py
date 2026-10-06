"""代码围栏测试：围栏内的翻页符/井号一律普通文字；未闭合容错。"""
from app.parser import to_plain


def test_dashes_inside_closed_fence_do_not_break_pages(engine):
    src = "```\n---\n# 标题\n- 不是列表\n```\n后面的文字\n"
    doc = engine.parse_full(src)
    assert len(doc.pages) == 1
    code = doc.pages[0].blocks[0]
    assert code.kind == "code_block"
    assert code.closed is True
    assert "---" in code.value and "# 标题" in code.value
    assert code.recoveries == ()
    assert doc.pages[0].blocks[1].kind == "paragraph"


def test_tilde_fence_ignores_backticks(engine):
    src = "~~~\n```\n# x\n~~~\n文字\n"
    doc = engine.parse_full(src)
    code = doc.pages[0].blocks[0]
    assert code.kind == "code_block" and code.closed is True
    assert "```" in code.value and "# x" in code.value


def test_unclosed_fence_swallowed_to_eof(engine):
    src = "# 标题\n\n```python\nprint(1)\n没关"
    doc = engine.parse_full(src)
    page = doc.pages[0]
    code = [b for b in page.blocks if b.kind == "code_block"][0]
    assert code.closed is False
    assert code.language == "python"
    rec = page.recoveries[0]
    assert rec.code == "unclosed_fence"
    assert rec.detail["resolution"] == "swallowed_to_eof"
    assert rec.line is not None


def test_unclosed_fence_cut_at_page_break(engine):
    src = "前文\n```\n代码行\n---\n新页\n"
    doc = engine.parse_full(src)
    assert len(doc.pages) == 2
    p1, p2 = doc.pages
    code = [b for b in p1.blocks if b.kind == "code_block"][0]
    assert code.closed is False
    assert "代码行" in code.value
    assert p1.recoveries[0].detail["resolution"] == "cut_at_page_break"
    # 新页不受影响：没有代码块、没有容错
    assert all(b.kind != "code_block" for b in p2.blocks)
    assert p2.recoveries == ()
    assert any("新页" in getattr(c, "value", "")
               for b in p2.blocks for c in getattr(b, "children", ()))


def test_dash_break_still_works_outside_fence(engine):
    doc = engine.parse_full("甲\n---\n乙\n---\n丙\n")
    assert len(doc.pages) == 3


def test_fence_with_glued_break_line(engine):
    # 分隔线紧贴正文，没有空行，仍然翻页
    doc = engine.parse_full("正文\n---\n第二页\n")
    assert len(doc.pages) == 2
