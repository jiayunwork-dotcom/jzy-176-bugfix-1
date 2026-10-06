"""解析引擎：全量解析、按页取页、编辑应用与增量重算。

增量策略（正确性论证见 docs/error-recovery.md 的“增量一致性”一节）：

1. 页的解析结果只依赖「该页源文本」+「是否最后一页」。最后一页这一维
   只影响未闭合围栏收尾文案，块结构完全相同；
2. 一次编辑是若干 (start, end, replacement) 偏移量编辑，先应用到文本；
3. 用翻页行把旧/新文本切成页源（这一步只是按行分组，与 lexer 的
   page_break 判定同源），从两端对齐，找最长公共前后缀；
4. 未对齐的中间页重算，公共前缀/后缀直接复用旧的 Page 节点（页号在
   返回前重新分配）；
5. 因此：公共页因为「页源相同 ⇒ 解析结果相同」而被复用；受影响页全量
   重算。最终结果 = 对新文本一次全量解析，逐字段一致。

返回里始终带 recomputed_pages（1 基新页号），让调用方/界面知道
“这次实际重算了哪几页”。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import model as M
from .assembler import build_page
from .lexer import tokenize, normalize
from .splitter import split_pages

_PAGE_BREAK_LINE_RE = re.compile(r"^[ \t]*-{3,}[ \t]*$")


# --------------------------------------------------------------- 页源切分

def _page_sources(text: str) -> list[str]:
    """与解析器严格同源的页源切分：tokenize -> split_pages。

    不能在这里朴素地按 ``---`` 行切，因为词法层对"未闭合围栏后的
    ``---``"有强制翻页策略（见 lexer 策略 B），朴素切分会与之不一致，
    增量复用就可能对错页。
    """
    tokens, _ = tokenize(text)
    slices = split_pages(tokens)
    return ["\n".join(t.text for t in sl.tokens) for sl in slices]


# --------------------------------------------------------------- 编辑

@dataclass(frozen=True)
class Edit:
    """对旧文本的半开区间替换：[start, end) -> replacement。偏移按字符计。"""
    start: int
    end: int
    replacement: str

    def __post_init__(self):
        if self.start < 0 or self.end < self.start:
            raise ValueError("非法编辑区间")


def apply_edits(text: str, edits: list[Edit]) -> str:
    """应用一批**针对同一基线文本**的编辑。

    多个区间互不重叠（允许首尾相接）；从后往前应用，前面编辑的偏移
    不受后面替换影响。区间合法性统一按原始 text 校验。
    """
    ordered = sorted(edits, key=lambda x: (x.start, x.end))
    for a, b in zip(ordered, ordered[1:]):
        if a.end > b.start:
            raise ValueError("一批编辑的区间不能重叠")
    out = text
    for e in sorted(edits, key=lambda x: (x.start, x.end), reverse=True):
        if e.end > len(text):
            raise ValueError("编辑区间超出文本长度")
        out = out[:e.start] + e.replacement + out[e.end:]
    return out


# --------------------------------------------------------------- 引擎

@dataclass
class IncrementalResult:
    document: M.Document
    recomputed_pages: tuple[int, ...]   # 新文档的 1 基页号
    reused_pages: tuple[int, ...]


class ParseEngine:
    """带页级缓存的引擎。缓存按 (页源, is_last) 保存解析好的 Page。

    存储层为每个修订保存独立引擎实例（或调用 reset），缓存绝不跨修订。
    """

    def __init__(self) -> None:
        self._text: str = ""
        self._pages: list[M.Page] = []
        self._doc_recoveries: list[M.Recovery] = []

    # ---- 全量 ----
    def parse_full(self, text: str) -> M.Document:
        self._text = text
        norm, had_bom = normalize(text)
        tokens, _ = tokenize(text)
        slices = split_pages(tokens)
        total = len(slices)
        self._pages = [
            build_page(sl, idx + 1, idx == total - 1)
            for idx, sl in enumerate(slices)
        ]
        self._doc_recoveries = self._doc_level(had_bom, norm == "" and text != "")
        return self._document()

    def _document(self) -> M.Document:
        return M.Document(
            pages=tuple(self._pages),
            recoveries=tuple(self._doc_recoveries),
        )

    def _doc_level(self, had_bom: bool, stripped_to_empty: bool) -> list[M.Recovery]:
        recs = []
        if had_bom:
            recs.append(M.Recovery(
                code=M.RECOVERY_BOM, line=None, severity="info",
                detail=None,
                message="文档开头的 UTF-8 BOM 已移除",
            ))
        return recs

    # ---- 取页 ----
    def get_page(self, text: str, page_no: int) -> M.Page:
        if not self._pages or self._text != text:
            self.parse_full(text)
        if page_no < 1 or page_no > len(self._pages):
            raise KeyError(page_no)
        return self._pages[page_no - 1]

    @property
    def page_count(self) -> int:
        return len(self._pages)

    # ---- 增量 ----
    def apply_incremental(self, old_text: str, edits: list[Edit]) -> IncrementalResult:
        if old_text != self._text:
            # 引擎状态与调用方声明的旧文本不一致（不该发生）：退回全量
            new_text = apply_edits(old_text, edits)
            self.parse_full(new_text)
            return IncrementalResult(
                self._document(),
                tuple(range(1, len(self._pages) + 1)), (),
            )
        new_text = apply_edits(old_text, edits)
        if not edits:
            return IncrementalResult(
                self._document(), (), tuple(range(1, len(self._pages) + 1)),
            )

        # 页源对齐只用于找公共前缀/后缀（按整页文本相等判定）。
        old_src = _page_sources(old_text)
        new_src = _page_sources(new_text)

        # 最长公共前缀 / 后缀
        pre = 0
        lim = min(len(old_src), len(new_src))
        while pre < lim and old_src[pre] == new_src[pre]:
            pre += 1
        suf = 0
        while (suf < len(old_src) - pre and suf < len(new_src) - pre
               and old_src[len(old_src) - 1 - suf] == new_src[len(new_src) - 1 - suf]):
            suf += 1

        # 中间段必须从“整篇新文本”的 token 切片重建 —— 不能单独 tokenize
        # 页源字符串：未闭合围栏的强制翻页判定依赖它在整篇中的位置。
        new_tokens, _ = tokenize(new_text)
        new_slices = split_pages(new_tokens)

        reused_old = self._pages  # 页号尚未重排
        pages: list[M.Page] = []
        recomputed: list[int] = []

        # 前缀复用
        for idx in range(pre):
            pages.append(reused_old[idx])
        # 中间段重算
        mid_end_new = len(new_src) - suf
        total_new = len(new_src)
        for new_idx in range(pre, mid_end_new):
            sl = new_slices[new_idx]
            is_last = new_idx == total_new - 1
            pages.append(build_page(sl, new_idx + 1, is_last))
            recomputed.append(new_idx + 1)
        # 后缀复用
        for k in range(suf):
            old_idx = len(old_src) - suf + k
            new_idx = len(new_src) - suf + k
            is_last = new_idx == total_new - 1
            page = reused_old[old_idx]
            if is_last != (old_idx == len(old_src) - 1):
                # 最后一页身份变化：结构不变，只需重打围栏收尾标注
                page = _restamp_last(page, is_last)
            pages.append(page)

        # 重新分配页号（节点里的位置都是页内行号，不动）
        pages = [_renumber(p, idx + 1) for idx, p in enumerate(pages)]
        reused_pages = [
            i + 1 for i in range(total_new)
            if (i < pre or i >= mid_end_new)
        ]

        self._text = new_text
        self._pages = pages
        return IncrementalResult(self._document(), tuple(recomputed),
                                 tuple(reused_pages))


def _renumber(page: M.Page, page_no: int) -> M.Page:
    if page.page_no == page_no:
        return page
    recs = tuple(M.Recovery(
        code=r.code, message=r.message, line=r.line, end_line=r.end_line,
        detail=r.detail, severity=r.severity, page=page_no,
    ) for r in page.recoveries)
    return M.Page(
        page_no=page_no, blocks=page.blocks,
        position=page.position, recoveries=recs,
    )


def _restamp_last(page: M.Page, is_last: bool) -> M.Page:
    """最后一页身份变化时，只重打未闭合围栏的收尾文案。"""
    from .assembler import _stamp_code_blocks
    # 先把页码归零再用 build 路径打标：直接复用 assembler 的内部函数
    blocks = tuple(_stamp_code_blocks(page.blocks, is_last))
    recs = []
    from .assembler import _stamp_recoveries
    for r in page.recoveries:
        stamped = _stamp_recoveries([r], r.page, is_last)[0]
        recs.append(stamped)
    return M.Page(
        page_no=page.page_no, blocks=blocks,
        position=page.position, recoveries=tuple(recs),
    )
