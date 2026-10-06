"""容错测试：残缺输入不崩、不吞后文，且每处容错都有结构化标注。"""


def test_empty_heading_is_marked(engine):
    page = engine.parse_full("## \n正文\n").pages[0]
    assert page.blocks[0].kind == "heading"
    assert page.blocks[0].level == 2
    recs = [r for r in page.recoveries if r.code == "empty_heading"]
    assert len(recs) == 1
    assert recs[0].line is not None
    # 后文没有被吞
    assert page.blocks[1].kind == "paragraph"


def test_unclosed_code_span_is_marked_and_text_kept(engine):
    page = engine.parse_full("未闭合 `code 尾巴文字\n").pages[0]
    para = page.blocks[0]
    text = "".join(getattr(c, "value", "") for c in para.children)
    assert "`" in text and "未闭合" in text and "尾巴文字" in text
    codes = [r.code for r in page.recoveries]
    assert "unclosed_code_span" in codes


def test_recovery_has_position_and_message(engine):
    page = engine.parse_full("前文\n```\nxxx\n---\n后页\n").pages[0]
    rec = page.recoveries[0]
    assert rec.severity in ("warning", "info")
    assert isinstance(rec.message, str) and rec.message
    assert rec.line and rec.end_line and rec.end_line >= rec.line
    assert rec.detail and "resolution" in rec.detail


def test_recovery_annotations_survive_json_roundtrip(engine):
    from app.parser import to_plain
    src = "a\n```\nx\n---\nb\n"
    doc = to_plain(engine.parse_full(src))
    import json
    again = json.loads(json.dumps(doc, ensure_ascii=False))
    assert again == doc
    assert again["pages"][0]["recoveries"][0]["code"] == "unclosed_fence"


def test_dashed_lines_after_unclosed_fence_remain_normal(engine):
    # 截断点之后的新页里即便有 # 、--- 也正常解析，不被吞
    src = "```\n未关\n---\n# 新页标题\n- 新页条目\n"
    doc = engine.parse_full(src)
    assert len(doc.pages) == 2
    p2 = doc.pages[1]
    assert p2.blocks[0].kind == "heading"
    assert p2.blocks[1].kind == "bullet_list"
