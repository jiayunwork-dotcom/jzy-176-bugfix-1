"""页组装：token 切片 -> Page 结构树。

页号在这里统一分配（1 基）；块级解析产出的位置都是页内行号，
页号不能泄露进块里 —— 这样同一页源文本解析结果才可缓存复用。

容错收尾：未闭合围栏的 resolution 在这一层定案。围栏只可能以两种方式
结束：后面还有翻页符（隐式合上）或一路吞到文末，两者正好由
“这是不是最后一页”决定。
"""
from __future__ import annotations

from . import model as M
from .blocks import parse_slice
from .splitter import PageSlice

AT_BOUNDARY = "__AT_BOUNDARY__"
UNCLOSED_MSG = "__UNCLOSED_FENCE__"

RES_CUT_AT_BREAK = "cut_at_page_break"
RES_EOF = "swallowed_to_eof"


def build_page(slice_: PageSlice, page_no: int, is_last: bool) -> M.Page:
    parsed = parse_slice(slice_)
    blocks = tuple(_stamp_code_blocks(parsed.blocks, is_last))
    recs = tuple(_stamp_recoveries(parsed.recoveries, page_no, is_last))
    # 位置全部使用“页内相对行号”（1 基）：这是页结果可缓存复用的前提。
    end = slice_.tokens[-1].line_no - slice_.start_line + 1 if slice_.tokens else None
    return M.Page(
        page_no=page_no, blocks=blocks,
        position=M.Position(1, end),
        recoveries=recs,
    )


def _stamp_recoveries(recs, page_no: int, is_last: bool):
    out = []
    for r in recs:
        if r.code == M.RECOVERY_UNCLOSED_FENCE:
            resolution = RES_EOF if is_last else RES_CUT_AT_BREAK
            msg = (
                "代码围栏没有关闭：已一路当作代码吞到本页末尾。"
                if is_last else
                "代码围栏没有关闭：已在下一个翻页符处隐式合上，"
                "翻页符之后的内容不受影响（若它本该属于代码，请补上关闭围栏）。"
            )
            detail = dict(r.detail or {})
            detail["resolution"] = resolution
            out.append(M.Recovery(
                code=r.code, message=msg, line=r.line, end_line=r.end_line,
                detail=detail, severity=r.severity, page=page_no,
            ))
        else:
            out.append(M.Recovery(
                code=r.code, message=r.message, line=r.line, end_line=r.end_line,
                detail=r.detail, severity=r.severity, page=page_no,
            ))
    return out


def _stamp_code_blocks(blocks, is_last: bool):
    """给树内所有 CodeBlock 的未闭合围栏标注定案（文案与页级标注一致）。"""
    out = []
    for b in blocks:
        if isinstance(b, M.CodeBlock):
            recs = tuple(_finish_fence_rec(b.recoveries, is_last))
            b = M.CodeBlock(
                value=b.value, info=b.info, language=b.language,
                closed=b.closed, position=b.position, kind=b.kind,
                recoveries=recs,
            )
        elif isinstance(b, M.Quote):
            b = M.Quote(
                blocks=tuple(_stamp_code_blocks(b.blocks, is_last)),
                position=b.position, kind=b.kind, recoveries=b.recoveries,
            )
        elif isinstance(b, (M.BulletList, M.OrderedList)):
            b = type(b)(
                items=tuple(_stamp_items(b.items, is_last)),
                **({"start": b.start} if isinstance(b, M.OrderedList) else {}),
                position=b.position, kind=b.kind, recoveries=b.recoveries,
            )
        out.append(b)
    return out


def _finish_fence_rec(recs, is_last: bool):
    out = []
    for r in recs:
        if r.code == M.RECOVERY_UNCLOSED_FENCE:
            resolution = RES_EOF if is_last else RES_CUT_AT_BREAK
            msg = (
                "代码围栏没有关闭：已一路当作代码吞到本页末尾。"
                if is_last else
                "代码围栏没有关闭：已在下一个翻页符处隐式合上，"
                "翻页符之后的内容不受影响（若它本该属于代码，请补上关闭围栏）。"
            )
            detail = dict(r.detail or {})
            detail["resolution"] = resolution
            out.append(M.Recovery(
                code=r.code, message=msg, line=r.line, end_line=r.end_line,
                detail=detail, severity=r.severity, page=r.page,
            ))
        else:
            out.append(r)
    return out


def _stamp_items(items, is_last: bool):
    out = []
    for it in items:
        out.append(M.ListItem(
            blocks=tuple(_stamp_code_blocks(it.blocks, is_last)),
            level=it.level, position=it.position, kind=it.kind,
            recoveries=it.recoveries, loose=it.loose,
            marker_column=it.marker_column,
        ))
    return out
