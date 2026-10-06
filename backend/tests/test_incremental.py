"""增量解析测试：任意编辑序列后与全量解析逐字段一致。"""
import random

import pytest

from app.parser import Edit, ParseEngine, to_plain

CHARS = list("#-*`>\n\t .0123)[]()!_~abc你好'\"\\") + [
    "---", "\n\n", "  ", "    ", "\t", "```", "~~~", "1. ", "- ", "## ",
]


def _rand_text(rng, nlines=10):
    return "\n".join(
        "".join(rng.choice(CHARS) for _ in range(rng.randint(0, 20)))
        for _ in range(rng.randint(0, nlines))
    )


def _non_overlapping_edits(rng, text, k):
    pts = sorted(rng.sample(range(len(text) + 1), min(2 * k, len(text) + 1)))
    edits = []
    repls = ["", "x", "\n", "---\n", "```\n", "## \n", "- ", "\t- ",
             "~~~\n", "字", "  ", "1. "]
    for i in range(0, len(pts) - 1, 2):
        edits.append(Edit(pts[i], pts[i + 1], rng.choice(repls)))
    return edits


def _apply(text, edits):
    out = text
    for e in sorted(edits, key=lambda x: x.start, reverse=True):
        out = out[:e.start] + e.replacement + out[e.end:]
    return out


@pytest.mark.parametrize("seed", range(40))
def test_incremental_matches_full_random(seed):
    rng = random.Random(1000 + seed)
    cur = _rand_text(rng)
    engine = ParseEngine()
    engine.parse_full(cur)
    for _ in range(rng.randint(1, 6)):
        edits = _non_overlapping_edits(rng, cur, rng.randint(1, 3))
        result = engine.apply_incremental(cur, edits)
        new = _apply(cur, edits)
        full = ParseEngine().parse_full(new)
        assert to_plain(result.document) == to_plain(full), new
        # 重算页号集合合法且覆盖真实差异
        assert set(result.recomputed_pages) | set(result.reused_pages) == \
            set(range(1, len(full.pages) + 1))
        cur = new


def test_edit_inside_one_page_only_recomputes_that_page():
    src = "甲页\n---\n乙页\n---\n丙页\n"
    engine = ParseEngine()
    engine.parse_full(src)
    result = engine.apply_incremental(src, [Edit(0, 1, "改")])
    assert result.recomputed_pages == (1,)
    assert result.reused_pages == (2, 3)


def test_insert_page_break_repages_tail():
    src = "同一页第一行\n第二行\n---\n尾页\n"
    engine = ParseEngine()
    engine.parse_full(src)
    result = engine.apply_incremental(src, [Edit(0, 0, "新页\n---\n")])
    new = "新页\n---\n" + src
    full = ParseEngine().parse_full(new)
    assert to_plain(result.document) == to_plain(full)
    assert len(full.pages) == 3


def test_closing_fence_via_edit_recomputes_tail():
    # 在未闭合围栏中间补上关围栏，后续页结构变化必须重算且与全量一致
    src = "前文\n```\n代码\n---\n后页\n- a\n"
    engine = ParseEngine()
    engine.parse_full(src)
    edits = [Edit(src.index("代码") + 2, src.index("代码") + 2, "\n```")]
    result = engine.apply_incremental(src, edits)
    new = _apply(src, edits)
    full = ParseEngine().parse_full(new)
    assert to_plain(result.document) == to_plain(full)


def test_no_edits_reuses_all_pages():
    src = "甲\n---\n乙\n"
    engine = ParseEngine()
    engine.parse_full(src)
    result = engine.apply_incremental(src, [])
    assert result.recomputed_pages == ()
    assert result.reused_pages == (1, 2)


def test_empty_text_editing():
    engine = ParseEngine()
    engine.parse_full("")
    result = engine.apply_incremental("", [Edit(0, 0, "新内容")])
    full = ParseEngine().parse_full("新内容")
    assert to_plain(result.document) == to_plain(full)
