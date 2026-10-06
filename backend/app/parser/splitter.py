"""翻页切分（page splitter）。

消费 lexer 的 token 流，按 page_break 切成 PageSlice。
切页与围栏状态完全解耦：lexer 已保证围栏内不会出现 page_break token，
所以这里的切片逻辑天然不会把代码块拦腰切断。

每页首尾的空白行会被裁掉（页内空行保留）：它们不产生任何块，却会让
“全空白页”和“空页”结构不同，破坏页源相等判定与增量复用。裁掉之后，
页内相对行号一律从第一个有效行算起，结果只取决于实际内容。

容错（未闭合围栏）的代价也在这一层可见：切片只是线性分组，未闭合的
fence_begin 会留在所在页，块级解析在页末把它标成 unclosed_fence。
"""
from __future__ import annotations

from dataclasses import dataclass

from .lexer import Token


@dataclass(frozen=True)
class PageSlice:
    page_no: int            # 占位，块级解析后由 assembler 重排
    tokens: tuple[Token, ...]
    start_line: int         # 该页第一个有效 token 的文档行号（空页取分隔符行号+1）


def _is_blank_token(tok: Token) -> bool:
    return tok.kind == "plain" and tok.text.strip(" \t") == ""


def _trim(tokens: list[Token]) -> list[Token]:
    lo, hi = 0, len(tokens)
    while lo < hi and _is_blank_token(tokens[lo]):
        lo += 1
    while hi > lo and _is_blank_token(tokens[hi - 1]):
        hi -= 1
    return tokens[lo:hi]


def split_pages(tokens: list[Token]) -> list[PageSlice]:
    """按 page_break 切。空文本（无 token）-> []，得到零页。

    连续分隔符 / 首尾分隔符会产生空页，空页保留（一页空白也是一页）。
    """
    raw_pages: list[list[Token]] = [[]]
    for tok in tokens:
        if tok.kind == "page_break":
            raw_pages.append([])
        else:
            raw_pages[-1].append(tok)

    pages: list[PageSlice] = []
    # 文本完全为空（连一行都没有）时为零页。
    if not tokens:
        return pages

    for idx, raw in enumerate(raw_pages):
        kept = _trim(raw)
        if kept:
            start = kept[0].line_no
        else:
            # 空页起始行：上一个分隔符行号 + 1。第一页前面无分隔符时为 1。
            start = 1
        pages.append(PageSlice(len(pages) + 1, tuple(kept), start))
    return pages
