"""HTTP 接口层（FastAPI）。

路由总览：
解析（不涉落盘，直接对请求体文本工作）
  POST /api/parse                 整篇全量解析
  POST /api/parse/page/{page_no}  按页号取单页
  POST /api/parse/edits           提交一批编辑做增量解析

讲稿
  POST /api/documents              新建
  GET  /api/documents              列表
  GET  /api/documents/{id}         读取当前修订（含结构树）
  PUT  /api/documents/{id}         带 base_revision 保存（过期返回 409）
  GET  /api/documents/{id}/revisions                修订历史
  GET  /api/documents/{id}/revisions/{rev}          读历史修订（含结构树）

页面渲染所用结构树一律来自后端；前端不再做任何 Markdown 解析。
"""
from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .parser import ParseEngine, Edit, to_plain
from .parser.model import Document
from .storage import (
    ConflictError, NotFoundError, Storage, ValidationError,
)

DB_PATH = os.environ.get("DECK_DB_PATH", "/data/decks.db")

app = FastAPI(title="Markdown 讲稿写作台", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_storage: Optional[Storage] = None


def storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage(DB_PATH)
    return _storage


# --------------------------------------------------------------- 模型

class ParseRequest(BaseModel):
    content: str = Field(default="", description="整篇 Markdown 文本")


class PageParseRequest(BaseModel):
    content: str = ""


class EditIn(BaseModel):
    start: int = Field(ge=0, description="起始字符偏移（含）")
    end: int = Field(ge=0, description="结束字符偏移（不含）")
    replacement: str = ""


class EditsRequest(BaseModel):
    content: str = Field(description="编辑前的基线文本")
    edits: list[EditIn] = Field(default_factory=list)


class CreateDocumentRequest(BaseModel):
    title: str = "无标题讲稿"
    content: str = ""


class SaveDocumentRequest(BaseModel):
    content: str
    base_revision: int = Field(ge=0, description="本次保存所依据的修订号")
    title: Optional[str] = None


# --------------------------------------------------------------- 序列化

def _doc_payload(document: Document, *, page_count: int) -> dict[str, Any]:
    return {
        "document": to_plain(document),
        "page_count": page_count,
    }


def _meta(m) -> dict[str, Any]:
    return {
        "id": m.id, "title": m.title,
        "current_revision": m.current_revision,
        "created_at": m.created_at, "updated_at": m.updated_at,
    }


# --------------------------------------------------------------- 解析路由

@app.post("/api/parse")
def parse_full(req: ParseRequest):
    engine = ParseEngine()
    doc = engine.parse_full(req.content)
    payload = _doc_payload(doc, page_count=engine.page_count)
    payload["recomputed_pages"] = list(range(1, engine.page_count + 1))
    payload["reused_pages"] = []
    return payload


@app.post("/api/parse/page/{page_no}")
def parse_page(page_no: int, req: PageParseRequest):
    engine = ParseEngine()
    try:
        page = engine.get_page(req.content, page_no)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"页 {page_no} 不存在")
    return {"page": to_plain(page), "page_count": engine.page_count}


@app.post("/api/parse/edits")
def parse_edits(req: EditsRequest):
    edits = []
    for e in req.edits:
        if e.end < e.start:
            raise HTTPException(status_code=422, detail="编辑区间 end 不能小于 start")
        edits.append(Edit(e.start, e.end, e.replacement))
    engine = ParseEngine()
    engine.parse_full(req.content)
    try:
        result = engine.apply_incremental(req.content, edits)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "document": to_plain(result.document),
        "page_count": len(result.document.pages),
        "recomputed_pages": list(result.recomputed_pages),
        "reused_pages": list(result.reused_pages),
        "new_content": engine._text,
    }


# --------------------------------------------------------------- 讲稿路由

@app.post("/api/documents", status_code=201)
def create_document(req: CreateDocumentRequest):
    try:
        meta = storage().create_document(req.content, req.title)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _meta(meta)


@app.get("/api/documents")
def list_documents():
    return {"documents": [_meta(m) for m in storage().list_documents()]}


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: int, revision: Optional[int] = None,
                 include_tree: bool = True):
    try:
        meta = storage().get_meta(doc_id)
        rev = storage().get_revision(doc_id, revision)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    payload = _meta(meta)
    payload["revision"] = rev.revision
    payload["content"] = rev.content
    if include_tree:
        doc = storage().parse_revision(doc_id, rev.revision)
        payload["page_count"] = len(doc.pages)
        payload["document"] = to_plain(doc)
    return payload


@app.put("/api/documents/{doc_id}")
def save_document(doc_id: int, req: SaveDocumentRequest):
    try:
        rev = storage().save_revision(
            doc_id, req.content, req.base_revision, req.title,
        )
        meta = storage().get_meta(doc_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ConflictError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": "revision_conflict",
                "message": str(exc),
                "current_revision": exc.current_revision,
                "current_content": exc.current_content,
                "document_id": exc.document_id,
            },
        )
    doc = storage().parse_revision(doc_id, rev.revision)
    payload = _meta(meta)
    payload["revision"] = rev.revision
    payload["content"] = rev.content
    payload["page_count"] = len(doc.pages)
    payload["document"] = to_plain(doc)
    return payload


@app.get("/api/documents/{doc_id}/revisions")
def list_revisions(doc_id: int):
    try:
        revs = storage().list_revisions(doc_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {
        "document_id": doc_id,
        "revisions": [
            {"revision": r.revision, "created_at": r.created_at}
            for r in revs
        ],
    }


@app.get("/api/documents/{doc_id}/revisions/{revision}")
def get_revision(doc_id: int, revision: int):
    try:
        meta = storage().get_meta(doc_id)
        rev = storage().get_revision(doc_id, revision)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    doc = storage().parse_revision(doc_id, revision)
    return {
        "id": meta.id, "title": meta.title,
        "current_revision": meta.current_revision,
        "revision": rev.revision, "content": rev.content,
        "page_count": len(doc.pages),
        "document": to_plain(doc),
    }


@app.get("/api/health")
def health():
    return {"ok": True}
