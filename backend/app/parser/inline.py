"""行内解析（文本/定界符分轨 + CommonMark 配对）。

输入一行纯文本（围栏代码块的内容不会经过这里），输出 Inline 序列。
支持：反斜杠转义、行内代码、加粗/斜体（``*`` 与 ``_``）、链接、图片。

实现分两轨：
- 第一趟按出现顺序产生“文本片”（Text/CodeSpan/Link/Image/Escape）和
  “强调定界符”（DelimRun），定界符记录它左右两侧的文本片边界；
- 第二趟只在定界符序列上做配对（CommonMark 的 flanking + 奇偶规则），
  配对成功就把两个定界符之间的文本片包进 Emphasis/Strong。

这样定界符不与 Text 节点混在一个列表里挪来挪去，结构清晰且确定 ——
同一段文本解析任意遍，结果逐字段相同。未闭合行内代码会标
unclosed_code_span；未配对的强调定界符按普通字符输出（这是 flanking
规则的正常结论，不算“猜”，不标注）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

from . import model as M

ESCAPABLE = set(r"""!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~""")


@dataclass
class _Seg:
    """一段已解析好的行内内容（不含强调）。"""
    node: M.Inline
    # 该片段在原始文本中的 [lo, hi)，用于判定定界符两侧字符。
    lo: int
    hi: int


@dataclass
class _Run:
    marker: str
    length: int
    pos: int            # 在原始文本中的起始下标
    can_open: bool
    can_close: bool
    seg_after: int      # 右侧第一个文本片在 segs 中的下标
    active: bool = True


def _ws(ch: str) -> bool:
    return ch in " \t\n"


def _punct(ch: str) -> bool:
    return ch != "" and not ch.isalnum() and not _ws(ch)


def _run_of(s: str, i: int, ch: str) -> int:
    j = i
    while j < len(s) and s[j] == ch:
        j += 1
    return j


def parse_inlines(text: str) -> tuple[tuple[M.Inline, ...], tuple[M.Recovery, ...]]:
    recs: list[M.Recovery] = []
    segs: list[_Seg] = []
    runs: list[_Run] = []

    n = len(text)
    i = 0
    while i < n:
        c = text[i]

        # 转义
        if c == "\\" and i + 1 < n and text[i + 1] in ESCAPABLE:
            segs.append(_Seg(M.Escape(text[i + 1]), i, i + 2))
            i += 2
            continue

        # 行内代码：等长反引号串配对，内部不做任何解析
        if c == "`":
            j = _run_of(text, i, "`")
            run_len = j - i
            k, close = j, -1
            while k < n:
                if text[k] == "`":
                    k2 = _run_of(text, k, "`")
                    if k2 - k == run_len:
                        close = k2
                        break
                    k = k2
                else:
                    k += 1
            if close >= 0:
                inner = text[j:close - run_len]
                if len(inner) >= 2 and inner[0] == " " and inner[-1] == " " and inner.strip():
                    inner = inner[1:-1]
                segs.append(_Seg(M.CodeSpan(inner.replace("\n", " ")), i, close))
                i = close
                continue
            recs.append(M.Recovery(
                code=M.RECOVERY_UNCLOSED_CODESPAN,
                line=None, severity="warning",
                detail={"opener_col": i + 1, "ticks": run_len},
                message=f"行内代码缺少配对的 {run_len} 个反引号，开记号按普通文字处理",
            ))
            segs.append(_Seg(M.Text("`" * run_len), i, j))
            i = j
            continue

        # 图片
        if c == "!" and i + 1 < n and text[i + 1] == "[":
            node, ni = _link(text, i + 1, n, image=True)
            if node is not None:
                segs.append(_Seg(node, i, ni))
                i = ni
                continue

        # 链接（文本内部不再嵌链接/图片，确定且够用）
        if c == "[":
            node, ni = _link(text, i, n, image=False)
            if node is not None:
                segs.append(_Seg(node, i, ni))
                i = ni
                continue

        # 强调定界符 run（整串一个 run，配对时再按 1/2 消耗）
        if c in "*_":
            j = _run_of(text, i, c)
            prev_ch = text[i - 1] if i > 0 else ""
            next_ch = text[j] if j < n else ""
            lf, rf = _flanking(prev_ch, next_ch)
            if c == "_":
                can_open = lf and not (lf and rf and prev_ch and not _ws(prev_ch))
                can_close = rf and not (lf and rf and next_ch and not _ws(next_ch))
            else:
                can_open, can_close = lf, rf
            runs.append(_Run(c, j - i, i, can_open, can_close, len(segs)))
            i = j
            continue

        # 普通字符：累积成文本片（到下一个特殊结构为止）
        lo = i
        i += 1
        while i < n and text[i] != "\\" and text[i] != "`" and text[i] != "[" \
                and text[i] not in "*_!":
            i += 1
        segs.append(_Seg(M.Text(text[lo:i]), lo, i))

    # ---------------- 第二趟：强调配对 ----------------
    # 用“定界符是否仍存活 + 配对栈”直接在 runs 上操作；
    # 最终输出时按 pos 顺序把存活定界符的字面量插回文本。
    _pair_emphasis(text, runs, segs)

    nodes = _assemble(text, segs, runs)
    return tuple(nodes), tuple(recs)


def _flanking(prev_ch: str, next_ch: str) -> tuple[bool, bool]:
    left = next_ch != "" and not _ws(next_ch) and (
        not _punct(next_ch) or _punct(prev_ch) or prev_ch == ""
    )
    right = prev_ch != "" and not _ws(prev_ch) and (
        not _punct(prev_ch) or _punct(next_ch) or next_ch == ""
    )
    return left, right


def _pair_emphasis(text: str, runs: list[_Run], segs: list[_Seg]) -> list:
    """CommonMark 定界符配对，返回强调区间列表。

    每个区间记录精确字符边界 [open_pos, close_end) 与 take（1/2）：
    - opener 从其“可用区”右端消耗（``***a***`` 里 strong 用内侧两个、
      emphasis 用最外一个）；
    - closer 从左端顺序消耗；
    - closer 用剩且能打开的字符重新压栈（``*a**b*`` 这类的关键）。
    """
    intervals: list[dict] = []
    for r in runs:
        r.used_close = 0      # 作为 closer 已消耗的前导字符数
        r.used_open = 0       # 作为 opener 已消耗的尾部字符数

    def _remain(r: "_Run") -> int:
        return r.length - r.used_close - r.used_open

    stack: list[int] = []
    for ci, cur in enumerate(runs):
        if not cur.can_close:
            if cur.can_open:
                stack.append(ci)
            continue

        while _remain(cur) > 0:
            oi = _nearest_opener(runs, stack, cur.marker, _remain)
            if oi is None:
                break
            opener = runs[oi]
            rem_open = _remain(opener)
            rem_close = _remain(cur)
            if rem_open >= 2 and rem_close >= 2:
                take = 2
            elif rem_open % 2 == 1:
                take = 1
            else:
                _deactivate(stack, oi)
                continue

            # opener 消耗可用区右端 take 个
            open_off = opener.length - opener.used_open - take
            opener.used_open += take
            # closer 消耗左端 take 个
            close_off = cur.used_close
            cur.used_close += take

            intervals.append({
                "open_pos": opener.pos + open_off,
                "close_end": cur.pos + close_off + take,
                "take": take,
                "open_seq": len(intervals),
            })
            if _remain(opener) == 0:
                try:
                    stack.remove(oi)
                except ValueError:
                    pass

        if _remain(cur) > 0 and cur.can_open:
            stack.append(ci)

    return intervals


def _nearest_opener(runs, stack, marker, remain_fn) -> Optional[int]:
    for k in range(len(stack) - 1, -1, -1):
        idx = stack[k]
        r = runs[idx]
        if r.marker == marker and remain_fn(r) > 0:
            return idx
    return None


def _deactivate(stack: list[int], idx: int) -> None:
    try:
        stack.remove(idx)
    except ValueError:
        pass


def _assemble(text: str, segs: list[_Seg], runs: list[_Run]) -> list[M.Inline]:
    """按精确区间把文本片包成嵌套 Emphasis/Strong，其余定界符当字面量。"""
    intervals = _pair_emphasis(text, runs, segs)

    # 每个 run 被消耗的字符区间（offset 相对 run 起点），余下为字面量。
    consumed: dict[int, list[tuple[int, int]]] = {i: [] for i in range(len(runs))}
    # 重新推导消耗范围：closer 前导 used_close 个、opener 尾部 used_open 个
    for ri, r in enumerate(runs):
        if r.used_close:
            consumed[ri].append((0, r.used_close))
        if r.used_open:
            consumed[ri].append((r.length - r.used_open, r.length))

    # 事件流：open/close 区间 + seg 叶子 + 字面量叶子
    # 同位置先闭后开：嵌套区间的开/闭位置必然不同（opener 从右端消耗、
    # closer 从左端消耗），同位置只可能是相邻区间的接缝。
    events: list[tuple[int, int, object]] = []
    for idx, iv in enumerate(intervals):
        events.append((iv["open_pos"], 1, ("open", idx)))
        events.append((iv["close_end"], 0, ("close", idx)))
    for si, sg in enumerate(segs):
        events.append((sg.lo, 2, ("seg", si)))
    for ri, r in enumerate(runs):
        ranges = _merge_ranges(consumed[ri])
        cursor = 0
        for a, b in ranges:
            if a > cursor:
                events.append((r.pos + cursor, 2,
                               ("lit", r.marker * (a - cursor))))
            cursor = b
        if cursor < r.length:
            events.append((r.pos + cursor, 2,
                           ("lit", r.marker * (r.length - cursor))))

    # 同位置：先开后闭再叶子；开事件中外层（close_end 更晚）在前，
    # 闭事件中内层（open_pos 更晚）在前。
    iv_list = intervals
    def sort_key(ev):
        pos, order, payload = ev
        if payload[0] == "open":
            iv = iv_list[payload[1]]
            return (pos, 1, -iv["close_end"])
        if payload[0] == "close":
            iv = iv_list[payload[1]]
            return (pos, 0, iv["open_pos"])
        return (pos, 2, 0)

    events.sort(key=sort_key)

    root: list = []
    boxes: list = []
    def container():
        return boxes[-1] if boxes else root

    for ev in events:
        payload = ev[2]
        if payload[0] == "open":
            box: list = []
            container().append(("wrap", payload[1], box))
            boxes.append(box)
        elif payload[0] == "close":
            # 配对是栈算法，区间必然良构，直接关闭当前盒子
            boxes.pop()
        elif payload[0] == "seg":
            container().append(("node", segs[payload[1]].node))
        else:
            container().append(("node", M.Text(payload[1])))

    return _materialize(root, {i: iv_list[i]["take"] for i in range(len(iv_list))})


def _merge_ranges(ranges):
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _materialize(items: list, pair_take: dict[int, int]) -> list[M.Inline]:
    out: list[M.Inline] = []
    for item in items:
        if item[0] == "node":
            node = item[1]
            # 合并相邻 Text
            if isinstance(node, M.Text) and out and isinstance(out[-1], M.Text):
                out[-1] = M.Text(out[-1].value + node.value)
            else:
                out.append(node)
        else:
            _, pid, box = item
            children = tuple(_materialize(box, pair_take))
            cls = M.Strong if pair_take[pid] == 2 else M.Emphasis
            out.append(cls(children))
    return out


def _link(s: str, bracket: int, hi: int, image: bool) -> tuple[Optional[M.Inline], int]:
    """解析 ``[text](url "title")``；失败返回 (None, bracket)。"""
    depth, j = 1, bracket + 1
    while j < hi:
        if s[j] == "\\" and j + 1 < hi:
            j += 2
            continue
        if s[j] == "[":
            depth += 1
        elif s[j] == "]":
            depth -= 1
            if depth == 0:
                break
        j += 1
    if depth != 0 or j >= hi or s[j] != "]" or j + 1 >= hi or s[j + 1] != "(":
        return None, bracket

    k = j + 2
    url: list[str] = []
    while k < hi and s[k] not in ')" \t':
        if s[k] == "\\" and k + 1 < hi:
            url.append(s[k + 1])
            k += 2
            continue
        url.append(s[k])
        k += 1
    title: Optional[str] = None
    while k < hi and s[k] in " \t":
        k += 1
    if k < hi and s[k] in '"\'':
        q, t0 = s[k], k + 1
        k = t0
        while k < hi and s[k] != q:
            k += 1
        if k >= hi:
            return None, bracket
        title = s[t0:k]
        k += 1
        while k < hi and s[k] in " \t":
            k += 1
    if k >= hi or s[k] != ")":
        return None, bracket

    label = s[bracket + 1:j]
    if image:
        return M.Image(url="".join(url), title=title, alt=label), k + 1
    children, _ = parse_inlines(label)
    return M.Link(url="".join(url), title=title, children=children), k + 1
