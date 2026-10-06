"""块级解析。

把一页的原始行解析成块树：标题、段落、有序/无序列表（多层嵌套）、
围栏代码块、引用。引用与列表项内部递归复用同一套解析器，所以列表里
可以放标题、引用、子列表，引用里可以放列表、代码块。

关键不变量（与 lexer 的咬合，详见 docs/error-recovery.md）：
- 词法层规定：只要一行独占匹配 ``---``+，它就是翻页符；即便出现在未
  闭合的围栏里，也会在该行之前隐式合上围栏。因此每一页的第一行围栏
  状态一定是"已关闭"，且页内任何原始行都不会匹配翻页正则；
- 于是块级解析器在页内自行扫描围栏，结果与词法层的全局状态严格一致，
  同时页与页完全独立 —— 这是增量重算正确性的地基。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from . import model as M
from .lexer import fence_match, PAGE_BREAK_RE
from .splitter import PageSlice

# ATX 标题：最多 3 空格前导，1-6 个 #
HEADING_RE = re.compile(r"^(?P<sp> {0,3})(?P<hashes>#{1,6})(?P<rest>.*)$")
# 列表项
BULLET_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-*+])(?P<after>.*)$")
ORDERED_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>\d{1,9}[.)])(?P<after>.*)$")
QUOTE_RE = re.compile(r"^(?P<up> {0,3})>(?P<body>.*)$")


@dataclass(frozen=True)
class VLine:
    """解析器输入行。

    递归解析（引用/列表项）时 raw 会被剥掉前缀，``col`` 记录 raw 第 0 列
    在页内对应的绝对列，使多缩进层的 marker 列计算不会重复折算。
    """
    line_no: int
    raw: str
    col: int = 0


@dataclass
class _PageParse:
    blocks: list
    recoveries: list


# ---------------------------------------------------------------- 工具

def indent_width(s: str) -> int:
    """Tab 展开到 4 列制表位后的列宽。"""
    w = 0
    for ch in s:
        w = w + (4 - w % 4) if ch == "\t" else w + 1
    return w


def strip_columns(raw: str, width: int) -> str:
    """吃掉前 ``width`` 列缩进，tab 跨列时用空格补齐剩余列。"""
    col, i = 0, 0
    while i < len(raw) and col < width:
        ch = raw[i]
        if ch == "\t":
            nxt = col + (4 - col % 4)
            if nxt <= width:
                col = nxt
                i += 1
            else:
                return " " * (nxt - width) + raw[i + 1:]
        else:
            col += 1
            i += 1
    return raw[i:]


def _is_blank(raw: str) -> bool:
    return raw.strip(" \t") == ""


def _marker(line: VLine):
    """若是列表项起始行，返回描述（列均为绝对列）；否则 None。"""
    m = BULLET_RE.match(line.raw)
    if m and m.group("after")[:1] in ("", " ", "\t"):
        ordered = False
        number = None
        marker = m.group("marker")
    else:
        m = ORDERED_RE.match(line.raw)
        if m and m.group("after")[:1] in ("", " ", "\t"):
            ordered = True
            marker = m.group("marker")
            number = int(marker[:-1])
        else:
            return None
    indent = m.group("indent")
    after = m.group("after")
    marker_col = line.col + indent_width(indent)
    # 内容列：marker 后紧跟的空白全部算缩进
    ws = 0
    k = 0
    while k < len(after) and after[k] in " \t":
        ws = ws + (4 - (marker_col + ws) % 4) if after[k] == "\t" else ws + 1
        k += 1
    content_col = marker_col + len(marker) + ws
    content = after[k:]
    return {
        "indent": indent, "marker_col": marker_col,
        "content_col": content_col, "ordered": ordered,
        "number": number, "content": content,
    }


def _heading(line: VLine):
    m = HEADING_RE.match(line.raw)
    if not m:
        return None
    rest = m.group("rest")
    if rest and rest[0] not in " \t":
        return None                    # #title 没空格 -> 普通段落
    level = len(m.group("hashes"))
    content = rest.strip()
    content = re.sub(r"\s+#+\s*$", "", content) if content else content
    return level, content


def _quote_line(line: VLine) -> Optional[VLine]:
    """是引用行则返回剥掉一层 ``>`` 后的内容行（列偏移 +2），否则 None。"""
    m = QUOTE_RE.match(line.raw)
    if not m:
        return None
    body = m.group("body")
    new_col = line.col + 1   # > 占一列
    # > 后允许吃掉一个空白
    if body[:1] in " \t":
        body = body[1:]
        new_col += 1
    return VLine(line.line_no, body, col=new_col)


# ---------------------------------------------------------------- 解析入口

def parse_slice(slice_: PageSlice) -> _PageParse:
    base = slice_.start_line - 1
    lines = [VLine(t.line_no - base, t.text) for t in slice_.tokens]
    return BlockParser(lines).parse()


def _abs_indent_of(line: VLine) -> int:
    lead = len(line.raw) - len(line.raw.lstrip(" \t"))
    return line.col + indent_width(line.raw[:lead])


def _subtree_end(flats, children, idxs) -> int:
    """一组同父 items 所覆盖的最大结束行（含其全部后代）。"""
    end = 0
    for idx in idxs:
        end = max(end, flats[idx]["end"])
        end = max(end, _subtree_end(flats, children, children.get(idx, [])))
    return end


class BlockParser:
    def __init__(self, lines: list[VLine]):
        self.lines = lines
        self.n = len(lines)
        self.recoveries: list[M.Recovery] = []

    def parse(self) -> _PageParse:
        blocks = []
        i = self._skip_blank(0)
        while i < self.n:
            line = self.lines[i]

            # 围栏代码块
            fm = fence_match(line.raw)
            if fm is not None:
                block, i = self._code_block(i, fm)
                blocks.append(block)
                i = self._skip_blank(i)
                continue

            # 标题
            h = _heading(line)
            if h is not None:
                level, content = h
                children, recs = self._inlines(content, line.line_no)
                rec = None
                if content.strip() == "":
                    rec = M.Recovery(
                        code=M.RECOVERY_EMPTY_HEADING, line=line.line_no,
                        severity="info",
                        message=f"第 {line.line_no} 行的 {level} 级标题井号后没有内容，保留为空标题",
                    )
                    self.recoveries.append(rec)
                blocks.append(M.Heading(
                    level=level, children=children,
                    position=M.Position(line.line_no),
                    recoveries=(rec,) if rec else (),
                ))
                i += 1
                i = self._skip_blank(i)
                continue

            # 引用
            if _quote_line(line) is not None:
                block, i = self._quote(i)
                blocks.append(block)
                i = self._skip_blank(i)
                continue

            # 列表
            if _marker(line) is not None:
                block, i = self._list(i)
                blocks.append(block)
                i = self._skip_blank(i)
                continue

            # 段落
            block, i = self._paragraph(i)
            blocks.append(block)
            i = self._skip_blank(i)

        return _PageParse(blocks, self.recoveries)

    # ------------------------------------------------------------ 各块

    def _code_block(self, i: int, opener: dict):
        open_w = indent_width(opener["indent"])
        begin_line = self.lines[i].line_no
        body: list[str] = []
        j = i + 1
        closed_line: Optional[int] = None
        while j < self.n:
            raw = self.lines[j].raw
            cm = fence_match(raw)
            if cm is not None and cm["char"] == opener["char"] \
                    and cm["len"] >= opener["len"] \
                    and indent_width(cm["indent"]) <= open_w \
                    and cm["info"].strip(" \t") == "":
                closed_line = self.lines[j].line_no
                j += 1
                break
            # 页内不存在翻页行：词法层要么把 --- 留在闭合围栏里（fence_line），
            # 要么在未闭合围栏处把它切成强制翻页点（下一页不含该行）。
            # 所以这里无条件把整行当代码正文，绝不能再按翻页提前退出。
            lead_w = indent_width(raw[:len(raw) - len(raw.lstrip(" \t"))])
            body.append(strip_columns(raw, min(open_w, lead_w)))
            j += 1

        info = opener["info"].strip()
        language = info.split()[0] if info else None
        closed = closed_line is not None
        rec = M.Recovery(
            code=M.RECOVERY_UNCLOSED_FENCE,
            line=begin_line, end_line=self.lines[j - 1].line_no,
            severity="warning",
            detail={"fence_char": opener["char"], "opener_col": open_w + 1,
                    "resolution": "__AT_BOUNDARY__"},
            message="__UNCLOSED_FENCE__",
        )
        block = M.CodeBlock(
            value="\n".join(body), info=info, language=language,
            closed=closed,
            position=M.Position(begin_line, closed_line or self.lines[j - 1].line_no),
            recoveries=() if closed else (rec,),
        )
        if not closed:
            self.recoveries.append(rec)
        return block, j

    def _quote(self, i: int) -> tuple[M.Quote, int]:
        inner: list[VLine] = []
        start = self.lines[i].line_no
        last_content_line = start
        j = i
        while j < self.n:
            q = _quote_line(self.lines[j])
            if q is not None:
                inner.append(q)
                if not _is_blank(q.raw):
                    last_content_line = q.line_no
                j += 1
                continue
            if _is_blank(self.lines[j].raw):
                # 仅当后面还跟引用行时，空行才属于引用块
                k = j + 1
                while k < self.n and _is_blank(self.lines[k].raw):
                    k += 1
                if k < self.n and _quote_line(self.lines[k]) is not None:
                    inner.append(VLine(self.lines[j].line_no, ""))
                    j += 1
                    continue
            break
        sub = BlockParser(inner)
        result = sub.parse()
        self.recoveries.extend(result.recoveries)
        return M.Quote(
            blocks=tuple(result.blocks),
            position=M.Position(start, last_content_line),
        ), j

    def _paragraph(self, i: int) -> tuple[M.Paragraph, int]:
        start = self.lines[i].line_no
        parts: list[str] = []
        last = start
        j = i
        while j < self.n:
            line = self.lines[j]
            if _is_blank(line.raw):
                break
            if fence_match(line.raw) is not None or _heading(line) is not None \
                    or _quote_line(line) is not None or _marker(line) is not None:
                break
            parts.append(line.raw.strip())
            last = line.line_no
            j += 1
        children, recs = self._inlines("\n".join(parts), start)
        return M.Paragraph(
            children=children, position=M.Position(start, last),
        ), j

    def _list(self, i: int) -> tuple:
        """消费一整个列表，返回 (List 节点, 下一位置)。

        两阶段：
        1. 线性扫描，把每个 marker 行拍平成 flat item 记录（带 marker 列、
           内容列、行号、容错），续行文本直接挂在所属 item 上 —— 靠
           “open item 内容列栈”决定归属，天然支持先深后浅、空格/tab 混用；
        2. 按 marker 列栈建树，递归解析每个 item 自己的续行（其中出现的
           marker 行会被子解析识别成嵌套列表）。
        """
        first = _marker(self.lines[i])
        first_col = first["marker_col"]
        ordered0 = first["ordered"]
        start_no = first["number"] or 1

        # ---- phase 1：拍平 ----
        flats: list[dict] = []
        # open_items: {col, content_col, rec}
        stack: list[dict] = []
        style_tabs: Optional[bool] = None
        j = i
        while j < self.n:
            line = self.lines[j]
            if fence_match(line.raw) is not None:
                break
            info = _marker(line)
            if info is not None:
                if info["marker_col"] < first_col:
                    break
                if info["marker_col"] == first_col and info["ordered"] != ordered0:
                    break  # 同列切换列表类型 -> 另起一个列表
                uses_tab = "\t" in info["indent"]
                item_recs: list[M.Recovery] = []
                if "\t" in info["indent"]:
                    item_recs.append(M.Recovery(
                        code=M.RECOVERY_INDENT_TAB, line=line.line_no,
                        severity="info",
                        detail={"prefix_repr": repr(info["indent"])},
                        message=f"第 {line.line_no} 行列表缩进含制表符，已按 4 列制表位折算",
                    ))
                if style_tabs is None:
                    style_tabs = uses_tab
                elif line.col == 0 and info["indent"] and uses_tab != style_tabs:
                    item_recs.append(M.Recovery(
                        code=M.RECOVERY_MIXED_INDENT, line=line.line_no,
                        severity="warning",
                        detail={"prefix_repr": repr(info["indent"]),
                                "column": info["marker_col"]},
                        message=(
                            f"第 {line.line_no} 行列表缩进混用了制表符与空格，"
                            "层级按折算后的列计算，请统一缩进写法"
                        ),
                    ))
                self.recoveries.extend(item_recs)
                rec = {
                    "col": info["marker_col"],
                    "content_col": info["content_col"],
                    "ordered": info["ordered"],
                    "number": info["number"],
                    "lines": [VLine(line.line_no, info["content"],
                                    col=info["content_col"])] if info["content"] else [],
                    "start": line.line_no, "end": line.line_no,
                    "recoveries": tuple(item_recs), "loose": False,
                }
                flats.append(rec)
                while stack and stack[-1]["col"] >= info["marker_col"]:
                    stack.pop()
                stack.append(rec)
                j += 1
                continue

            # 非 marker 行
            if _is_blank(line.raw):
                # 空行：先暂存，遇非缩进行结束列表时丢弃；属于某 item 则标 loose
                k = j + 1
                while k < self.n and _is_blank(self.lines[k].raw):
                    k += 1
                if k >= self.n:
                    j = k
                    break
                nxt = self.lines[k]
                if fence_match(nxt.raw) is not None:
                    break
                nmark = _marker(nxt)
                nxt_w = _abs_indent_of(nxt)
                if nmark is not None:
                    if nmark["marker_col"] < first_col:
                        j = k
                        break
                    # 空行后的 marker：新 item，空行挂给当前最深 item
                    if stack:
                        stack[-1]["loose"] = True
                    j = k
                    continue
                if nxt_w > first_col and stack:
                    stack[-1]["loose"] = True
                    j = k
                    continue
                # 空行后顶格普通内容：列表结束（空行丢弃）
                break

            w = _abs_indent_of(line)
            if w > first_col and stack:
                owner = stack[-1]
                # 续行必须达到当前最深 item 的内容列；达不到就是懒续行
                if w >= owner["content_col"]:
                    owner["lines"].append(VLine(
                        line.line_no,
                        strip_columns(line.raw, owner["content_col"]),
                        col=owner["content_col"],
                    ))
                else:
                    if _heading(line) is not None or _quote_line(line) is not None:
                        break
                    owner["lines"].append(VLine(line.line_no, line.raw.strip(), col=w))
                owner["end"] = line.line_no
                j += 1
                continue
            # 顶格普通行：作为最后一个顶层 item 的懒续行
            if w <= first_col and flats:
                if _heading(line) is not None or _quote_line(line) is not None:
                    break
                last_top = None
                for f in flats:
                    if f["col"] == first_col:
                        last_top = f
                # 若最近的 marker 开着更深的 item，顶格行意味着列表结束
                if stack and stack[-1]["col"] > first_col:
                    break
                if last_top is not None:
                    last_top["lines"].append(VLine(line.line_no, line.raw.strip(), col=w))
                    last_top["end"] = line.line_no
                    j += 1
                    continue
            break

        # 去尾部空行
        for rec in flats:
            while rec["lines"] and rec["lines"][-1].raw == "":
                rec["lines"].pop()
                rec["loose"] = True

        # ---- phase 2：栈定父项 ----
        # 每个 flat 的父项 = 源顺序中最近的、marker 列严格更小的项。
        parent: list[Optional[int]] = [None] * len(flats)
        pstack: list[int] = []
        for idx, rec in enumerate(flats):
            while pstack and flats[pstack[-1]]["col"] >= rec["col"]:
                pstack.pop()
            parent[idx] = pstack[-1] if pstack else None
            pstack.append(idx)

        children: dict[int, list[int]] = {}
        roots: list[int] = []
        for idx, p in enumerate(parent):
            if p is None:
                roots.append(idx)
            else:
                children.setdefault(p, []).append(idx)

        root_items = self._build_group(flats, children, roots, 0)
        end = max((r["end"] for r in flats), default=self.lines[i].line_no)
        common = dict(items=tuple(root_items),
                      position=M.Position(self.lines[i].line_no, end))
        if ordered0:
            return M.OrderedList(start=start_no, **common), j
        return M.BulletList(**common), j

    def _build_group(self, flats, children, idxs, depth) -> list[M.ListItem]:
        """构造一组同父 items（idxs 按源序），子项按 (marker 列, 有序性)
        分成连续的列表节点 —— 列不同意味着写作时换了缩进档位，
        视为两个独立子列表（层级仍然照实保留，不被压平）。
        """
        out: list[M.ListItem] = []
        for idx in idxs:
            rec = flats[idx]
            sub = BlockParser(rec["lines"])
            result = sub.parse()
            self.recoveries.extend(result.recoveries)
            blocks = list(result.blocks)

            groups: list[list[int]] = []
            for ch in children.get(idx, []):
                if (groups and flats[ch]["col"] == flats[groups[-1][-1]]["col"]
                        and flats[ch]["ordered"] == flats[groups[-1][-1]]["ordered"]):
                    groups[-1].append(ch)
                else:
                    groups.append([ch])
            for g in groups:
                items = self._build_group(flats, children, g, depth + 1)
                start = flats[g[0]]["start"]
                end_line = _subtree_end(flats, children, g)
                if flats[g[0]]["ordered"]:
                    blocks.append(M.OrderedList(
                        items=tuple(items),
                        start=flats[g[0]]["number"] or 1,
                        position=M.Position(start, end_line),
                    ))
                else:
                    blocks.append(M.BulletList(
                        items=tuple(items),
                        position=M.Position(start, end_line),
                    ))
            out.append(M.ListItem(
                blocks=tuple(blocks), level=depth,
                position=M.Position(rec["start"], rec["end"]),
                recoveries=rec["recoveries"], loose=rec["loose"],
                marker_column=rec["col"],
            ))
        return out

    # ------------------------------------------------------------ 杂项

    def _inlines(self, text: str, line_no: int):
        children, recs = inline_mod.parse_inlines(text)
        fixed = []
        for r in recs:
            fixed.append(M.Recovery(
                code=r.code, message=r.message, line=line_no,
                end_line=line_no, detail=r.detail, severity=r.severity,
            ))
        self.recoveries.extend(fixed)
        return children, fixed

    def _skip_blank(self, i: int) -> int:
        while i < self.n and _is_blank(self.lines[i].raw) \
                and fence_match(self.lines[i].raw) is None:
            i += 1
        return i


# 延迟导入避免循环感
from . import inline as inline_mod  # noqa: E402
