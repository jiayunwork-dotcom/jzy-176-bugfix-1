"""结构化 Markdown 讲稿解析器。

模块分工：
- lexer.py    词法切分：规范化、围栏状态机、翻页 token
- splitter.py 翻页切片
- inline.py   行内解析
- blocks.py   块级解析（含递归的列表项/引用）
- assembler.py 页组装与容错收尾
- engine.py   全量解析与增量重算
- model.py    文档树数据模型
"""
from .model import (
    Document, Page, Block, ListItem,
    Heading, Paragraph, BulletList, OrderedList, CodeBlock, Quote,
    Inline, Text, Escape, Emphasis, Strong, CodeSpan, Link, Image,
    Recovery, Position, to_plain,
)
from .engine import ParseEngine, Edit, IncrementalResult

__all__ = [
    "Document", "Page", "Block", "ListItem",
    "Heading", "Paragraph", "BulletList", "OrderedList", "CodeBlock", "Quote",
    "Inline", "Text", "Escape", "Emphasis", "Strong", "CodeSpan", "Link", "Image",
    "Recovery", "Position", "to_plain",
    "ParseEngine", "Edit", "IncrementalResult",
]
