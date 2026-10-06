"""词法切分（lexer）。

职责：
1. 输入规范化：去掉 UTF-8 BOM、统一 CR/CRLF 为 LF，按物理行切开；
2. 在扫描过程中跟踪代码围栏状态，识别：
   - fence_begin / fence_end / fence_line
   - page_break（独占一行的 ``---``+，仅在围栏外生效）
   - 其它普通行 plain

翻页符与围栏的咬合（两遍扫描，见 docs/error-recovery.md 的策略 B）：
- 第一遍严格扫描：围栏内只有语法正确的关围栏能合上；围栏里的 ``---``、
  ``# 标题`` 一律 fence_line。于是**正常闭合**的代码块里出现 ``---``
  绝不会翻页，这是硬规则；
- 第一遍结束后若仍有开着的围栏，在它之后找最早的独占 ``---`` 行，
  把该行标记为“强制翻页点”，重扫一遍。于是**忘了关**的围栏只吞到
  那一行，不连累后面的幻灯片；该行只发 page_break（不发合成 fence_end，
  块级解析器会把未闭合块结束在页末并标注容错）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# 独占一行、三个或更多连字符（允许首尾水平空白）。
PAGE_BREAK_RE = re.compile(r"^[ \t]*-{3,}[ \t]*$")

# 围栏：任意前导空白 + ``` 或 ~~~（3 个起步，多了也行）。
# 反引号围栏的 info 里不允许出现反引号；波浪线围栏 info 任意。
FENCE_BACKTICK_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,})(?P<info>[^`]*)$")
FENCE_TILDE_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>~{3,})(?P<info>.*)$")


@dataclass(frozen=True)
class Token:
    kind: str               # page_break | fence_begin | fence_end | fence_line | plain
    line_no: int            # 1 基，文档原始物理行号
    text: str               # 行原文（未做缩进变换）
    indent: str = ""        # 围栏行的前导空白
    fence_char: str = ""    # "`" 或 "~"
    fence_len: int = 0
    info: str = ""


def normalize(text: str) -> tuple[str, bool]:
    """统一换行；返回 (新文本, 是否去掉了 BOM)。"""
    had_bom = False
    if text and text[0] == "\ufeff":
        text = text[1:]
        had_bom = True
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, had_bom


def _match_fence(text: str) -> Optional[re.Match[str]]:
    m = FENCE_BACKTICK_RE.match(text)
    if m:
        return m
    return FENCE_TILDE_RE.match(text)


def fence_match(raw: str) -> Optional[dict]:
    """块级解析器用的围栏探测：返回统一 dict，非围栏行返回 None。

    块级解析器在**页内**调用 —— 词法层已保证页首围栏状态为关闭、
    页内不会出现翻页行，所以这里只看本行语法即可，与全局扫描一致。
    """
    m = _match_fence(raw)
    if m is None:
        return None
    fence = m.group("fence")
    return {
        "char": fence[0],
        "len": len(fence),
        "indent": m.group("indent"),
        "info": m.group("info"),
    }


def _indent_width(s: str) -> int:
    w = 0
    for ch in s:
        w = w + (4 - w % 4) if ch == "\t" else w + 1
    return w


def _scan(lines: list[str], force_breaks: frozenset[int]) -> tuple[
        list[Token], Optional[int]]:
    """单遍扫描。返回 (tokens, 仍开着的围栏的开行行号；已关则 None)。

    force_breaks 中的行号强制按翻页处理（即使它在某个围栏内部），
    且该行只发 page_break，不发合成 fence_end。
    """
    tokens: list[Token] = []
    open_char: Optional[str] = None
    open_len = 0
    open_indent_w = 0
    open_line: Optional[int] = None

    for idx, line in enumerate(lines, start=1):
        if open_char is not None and idx in force_breaks:
            tokens.append(Token("page_break", idx, line))
            open_char = None
            open_len = 0
            open_indent_w = 0
            open_line = None
            continue

        if open_char is not None:
            m = _match_fence(line)
            closes = (
                m is not None
                and m.group("fence")[0] == open_char
                and len(m.group("fence")) >= open_len
                and _indent_width(m.group("indent")) <= open_indent_w
                and m.group("info").strip(" \t") == ""
            )
            if closes:
                tokens.append(Token(
                    "fence_end", idx, line,
                    indent=m.group("indent"),
                    fence_char=open_char, fence_len=len(m.group("fence")),
                ))
                open_char = None
                open_len = 0
                open_indent_w = 0
                open_line = None
            else:
                tokens.append(Token("fence_line", idx, line))
            continue

        if PAGE_BREAK_RE.match(line):
            tokens.append(Token("page_break", idx, line))
            continue

        m = _match_fence(line)
        if m:
            open_char = m.group("fence")[0]
            open_len = len(m.group("fence"))
            open_indent_w = _indent_width(m.group("indent"))
            open_line = idx
            tokens.append(Token(
                "fence_begin", idx, line,
                indent=m.group("indent"),
                fence_char=open_char, fence_len=open_len,
                info=m.group("info"),
            ))
            continue

        tokens.append(Token("plain", idx, line))

    return tokens, open_line


def tokenize(text: str) -> tuple[list[Token], bool]:
    """把整篇文本切成 token 线性列表。返回 (tokens, had_bom)。"""
    text, had_bom = normalize(text)
    if text == "":
        return [], had_bom
    lines = text.split("\n")

    # 第一遍：严格扫描，找最后一个未闭合围栏的开行行号。
    tokens, open_line = _scan(lines, frozenset())

    # 有未闭合围栏：找它之后最早的独占 --- 行作为强制翻页点，然后重扫。
    # 重扫后可能仍有开着的围栏（截断点之后又开了新围栏且没关），
    # 迭代到收敛（每次最多新增一个截断点，且行号严格增大）。
    force: set[int] = set()
    while open_line is not None:
        cut: Optional[int] = None
        for idx in range(open_line + 1, len(lines) + 1):
            if idx in force:
                break  # 再往后已属于下一页，本围栏到此为止
            if PAGE_BREAK_RE.match(lines[idx - 1]):
                cut = idx
                break
        if cut is None:
            break  # 一路吞到文末：不再截断，未闭合留给块级层标注
        force.add(cut)
        tokens, open_line = _scan(lines, frozenset(force))

    return tokens, had_bom
