"""解析器基础测试：零页、确定性、块类型与行内结构。"""
import pytest

from app.parser import ParseEngine, to_plain
from app.parser import model as M


def test_empty_text_is_zero_pages(engine):
    doc = engine.parse_full("")
    assert doc.pages == ()
    assert to_plain(doc)["pages"] == []


@pytest.mark.parametrize("text", ["   \n\t\n", "\n\n\n", " \n  \n "])
def test_blank_only_is_empty_pages(engine, text):
    # 有行但全空白：切成（一）个没有任何块的页，不报错
    doc = engine.parse_full(text)
    for p in doc.pages:
        assert p.blocks == ()


def test_parse_is_deterministic(engine, sample_text):
    """同一段文本解析多遍，结果逐字段相同。"""
    first = to_plain(engine.parse_full(sample_text))
    for _ in range(10):
        again = ParseEngine().parse_full(sample_text)
        assert to_plain(again) == first


def test_heading_levels(engine):
    doc = engine.parse_full("# 一\n\n###### 六\n\n####### 七个井号是普通文字\n")
    kinds = [b.level for b in doc.pages[0].blocks if b.kind == "heading"]
    assert kinds == [1, 6]
    assert "七个井号" in "".join(
        getattr(c, "value", "")
        for b in doc.pages[0].blocks if b.kind == "paragraph"
        for c in b.children
    )


def test_inline_structure(engine):
    doc = engine.parse_full("**粗** *斜* `代码` [链接](http://x) ![图](a.png)\n")
    para = doc.pages[0].blocks[0]
    seq = [c.kind for c in para.children]
    assert seq == ["strong", "text", "emphasis", "text", "code_span",
                   "text", "link", "text", "image"]


def test_emphasis_not_triggered_by_arithmetic(engine):
    doc = engine.parse_full("3 * 5 = 15 and a_b_c\n")
    para = doc.pages[0].blocks[0]
    assert all(c.kind == "text" for c in para.children)
    assert "3 * 5" in "".join(c.value for c in para.children)
