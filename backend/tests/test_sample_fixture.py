"""示例讲稿回归基准：结构树固定，任何解析改动导致差异都会在这里暴露。"""
from app.parser import to_plain


def test_sample_deck_matches_golden_snapshot(engine, sample_text, sample_expected):
    doc = engine.parse_full(sample_text)
    assert to_plain(doc) == sample_expected


def test_sample_deck_has_at_least_three_pages(engine, sample_text):
    doc = engine.parse_full(sample_text)
    assert len(doc.pages) >= 3


def test_sample_deck_contains_multilevel_lists(engine, sample_text):
    doc = engine.parse_full(sample_text)
    levels = set()
    for page in doc.pages:
        def scan(blocks):
            for b in blocks:
                if b.kind in ("bullet_list", "ordered_list"):
                    for it in b.items:
                        levels.add(it.level)
                        scan(it.blocks)
        scan(page.blocks)
    assert levels >= {0, 1, 2, 3}


def test_sample_deck_contains_code_block_with_plain_dashes(engine, sample_text):
    doc = engine.parse_full(sample_text)
    code_blocks = [
        b for page in doc.pages for b in page.blocks
        if b.kind == "code_block"
    ]
    assert code_blocks, "示例至少要有一个代码块"
    # 其中一个代码块内含 --- 与 # ，且整个示例页数不变（它们没翻页）
    joined = "\n".join(b.value for b in code_blocks)
    assert "---" in joined and "#" in joined
    assert len(doc.pages) == 3
