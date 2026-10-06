"""结构化文档树的数据模型。

解析结果是一棵不可变的 dataclass 树，而不是拼好的 HTML：

Document
 └─ Page[]                       每页对应一个翻页符切出的片段
     ├─ blocks: Block[]
     └─ recoveries: Recovery[]   本页的容错标注
Block: Heading | Paragraph | BulletList | OrderedList | CodeBlock | Quote
ListItem
 ├─ blocks（段落、子列表……）
 └─ recovery?                    缩进混用等挂在具体项上
Inline: Text | Emphasis | Strong | CodeSpan | Link | Image | Escape

每个节点带 page-relative 的 position（合成列表体行号为 None）。
树以 dataclasses.asdict 序列化成 JSON 友好的结构。
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field, asdict
from typing import Any, Optional, Union


@dataclass(frozen=True)
class Position:
    """页内位置（1 基行号）。合成展开行（列表项内容重缩进）没有原始行号。"""
    line: int
    end_line: Optional[int] = None


# ---------------- 行内 ----------------

class _Inline:
    """行内节点基类；子类各自声明字段，kind 在构造时固定。"""
    kind: str = "inline"


@dataclass(frozen=True)
class Text(_Inline):
    value: str
    kind: str = "text"


@dataclass(frozen=True)
class Escape(_Inline):
    """反斜杠转义，如 \\*，value 存转义后的字面字符。"""
    value: str
    kind: str = "escape"


@dataclass(frozen=True)
class Emphasis(_Inline):
    children: tuple["Inline", ...]
    kind: str = "emphasis"


@dataclass(frozen=True)
class Strong(_Inline):
    children: tuple["Inline", ...]
    kind: str = "strong"


@dataclass(frozen=True)
class CodeSpan(_Inline):
    value: str
    kind: str = "code_span"


@dataclass(frozen=True)
class Link(_Inline):
    url: str
    title: Optional[str]
    children: tuple["Inline", ...]
    kind: str = "link"


@dataclass(frozen=True)
class Image(_Inline):
    url: str
    title: Optional[str]
    alt: str
    kind: str = "image"


Inline = _Inline


# ---------------- 块级 ----------------

@dataclass(frozen=True)
class Heading:
    level: int                      # 1..6
    children: tuple[Inline, ...]
    position: Position
    kind: str = field(default="heading")
    recoveries: tuple["Recovery", ...] = ()


@dataclass(frozen=True)
class Paragraph:
    children: tuple[Inline, ...]
    position: Position
    kind: str = field(default="paragraph")
    recoveries: tuple["Recovery", ...] = ()


@dataclass(frozen=True)
class ListItem:
    blocks: tuple["Block", ...]
    level: int                      # 从 0 开始的嵌套深度
    position: Position
    kind: str = field(default="list_item")
    recoveries: tuple["Recovery", ...] = ()
    loose: bool = False
    marker_column: int = 0         # marker 所在列（tab 按 4 列折算），用于重算层级


@dataclass(frozen=True)
class BulletList:
    items: tuple[ListItem, ...]
    position: Position
    kind: str = field(default="bullet_list")
    recoveries: tuple["Recovery", ...] = ()


@dataclass(frozen=True)
class OrderedList:
    items: tuple[ListItem, ...]
    start: int
    position: Position
    kind: str = field(default="ordered_list")
    recoveries: tuple["Recovery", ...] = ()


@dataclass(frozen=True)
class CodeBlock:
    """围栏代码块。info 是开围栏后的原始尾巴，language 是其中第一段。"""
    value: str
    info: str
    language: Optional[str]
    closed: bool
    position: Position
    kind: str = field(default="code_block")
    recoveries: tuple["Recovery", ...] = ()


@dataclass(frozen=True)
class Quote:
    blocks: tuple["Block", ...]
    position: Position
    kind: str = field(default="quote")
    recoveries: tuple["Recovery", ...] = ()


Block = Union[Heading, Paragraph, BulletList, OrderedList, CodeBlock, Quote]


@dataclass(frozen=True)
class Recovery:
    """一处容错。

    code: 容错类型，见 RECOVERY_*；
    line / end_line: 页内 1 基行号（文档级标注为 None）；
    detail: 机器可读的补充信息；
    message: 给写作者看的中文说明；
    severity: info / warning。
    """
    code: str
    message: str
    line: Optional[int] = None
    end_line: Optional[int] = None
    detail: Optional[dict[str, Any]] = None
    severity: str = "warning"
    page: int = 0                    # 总装时统一打页号（1 基）


# ---------------- 页 / 文档 ----------------

@dataclass(frozen=True)
class Page:
    page_no: int                    # 1 基，总装时分配
    blocks: tuple[Block, ...]
    position: Position
    recoveries: tuple[Recovery, ...] = ()


@dataclass(frozen=True)
class Document:
    pages: tuple[Page, ...]
    recoveries: tuple[Recovery, ...] = ()   # 文档级标注（如 BOM）
    kind: str = field(default="document")


# 容错类型常量
RECOVERY_UNCLOSED_FENCE = "unclosed_fence"
RECOVERY_EMPTY_HEADING = "empty_heading"
RECOVERY_MIXED_INDENT = "mixed_indentation"
RECOVERY_INDENT_TAB = "tab_indent_normalized"
RECOVERY_UNCLOSED_CODESPAN = "unclosed_code_span"
RECOVERY_BOM = "bom_stripped"


def to_plain(obj: Any) -> Any:
    """递归转成 dict/list/str 原语（JSON 可直接序列化，tuple 统一成 list）。"""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_plain(v) for k, v in asdict(obj).items()}
    if isinstance(obj, tuple):
        return [to_plain(v) for v in obj]
    if isinstance(obj, list):
        return [to_plain(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    return obj
