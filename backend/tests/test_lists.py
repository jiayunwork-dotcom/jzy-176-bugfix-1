"""列表测试：有序/无序、多层嵌套、空格与制表符混用层级不被压平。"""


def _levels(list_block):
    out = []

    def walk(items, depth=0):
        for it in items:
            out.append((depth, it.marker_column))
            for b in it.blocks:
                if b.kind in ("bullet_list", "ordered_list"):
                    walk(b.items, depth + 1)
    walk(list_block.items)
    return out


def test_bullet_nesting_spaces(engine):
    src = "- a\n  - b\n    - c\n      - d\n- e\n"
    lst = engine.parse_full(src).pages[0].blocks[0]
    assert lst.kind == "bullet_list"
    assert _levels(lst) == [(0, 0), (1, 2), (2, 4), (3, 6), (0, 0)]


def test_ordered_nesting_with_start(engine):
    src = "1. one\n2. two\n   1. two-one\n   2. two-two\n3. three\n"
    lst = engine.parse_full(src).pages[0].blocks[0]
    assert lst.kind == "ordered_list" and lst.start == 1
    levels = _levels(lst)
    assert levels == [(0, 0), (0, 0), (1, 3), (1, 3), (0, 0)]
    sub = [b for b in lst.items[1].blocks if b.kind == "ordered_list"][0]
    assert sub.start == 1


def test_deep_nested_not_flattened(engine):
    """二级项绝不能被压成一级（硬性要求）。"""
    src = "\n".join([
        "- 一级",
        "  - 二级",
        "    - 三级",
        "      - 四级",
        "  - 二级兄弟",
    ]) + "\n"
    lst = engine.parse_full(src).pages[0].blocks[0]
    top = lst.items
    assert len(top) == 1
    first = top[0]
    sub = [b for b in first.blocks if b.kind == "bullet_list"][0]
    assert [it.level for it in sub.items] == [1, 1]
    deep = [b for b in sub.items[0].blocks if b.kind == "bullet_list"][0]
    assert deep.items[0].level == 2


def test_mixed_space_and_tab_indentation(engine):
    # 一个子项用 tab，另一个用 4 空格，折算后都必须挂到二级
    src = "- a\n\t- b(tab)\n    - c(spaces)\n- d\n"
    page = engine.parse_full(src).pages[0]
    lst = page.blocks[0]
    sub = [b for b in lst.items[0].blocks if b.kind == "bullet_list"][0]
    assert {it.level for it in sub.items} == {1}
    codes = {r.code for r in page.recoveries}
    assert "tab_indent_normalized" in codes
    assert "mixed_indentation" in codes


def test_ordered_and_bullet_switch(engine):
    src = "- a\n- b\n1. c\n2. d\n"
    page = engine.parse_full(src).pages[0]
    kinds = [b.kind for b in page.blocks]
    assert kinds == ["bullet_list", "ordered_list"]


def test_paragraph_inside_item_with_inline(engine):
    src = "- 普通 **粗** 文字\n- 第二\n"
    item = engine.parse_full(src).pages[0].blocks[0].items[0]
    para = item.blocks[0]
    assert para.kind == "paragraph"
    assert [c.kind for c in para.children] == ["text", "strong", "text"]
