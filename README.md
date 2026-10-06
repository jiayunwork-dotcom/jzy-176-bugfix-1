# 讲稿写作台（Markdown → 分页幻灯片）

浏览器里的写作台：左边敲 Markdown，右边即时看到**切好页**的幻灯片。
后端负责把文本切页、把每页解析成**结构化文档树**（不是 HTML），并提供
增量预览与带修订号的讲稿存储；前端只渲染后端给的树，不另做 Markdown
解析。

## 一键启动

需要 Docker（含 compose 插件）。

```bash
docker compose up --build
# 浏览器打开 http://localhost:8080
```

- 前端：http://localhost:8080
- 后端 API / 文档（FastAPI Swagger）：http://localhost:8000/docs
- SQLite 数据在命名卷 `deck-data`（容器内 `/data/decks.db`）。

## 本地开发

后端（Python 3.12；本地 3.11 也可跑测试）：

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m uvicorn app.api:app --reload --port 8000
python -m pytest -q
```

前端：

```bash
cd frontend
npm install
npm run dev          # 默认把 /api 代理到 http://localhost:8000
```

## 语法约定

- 单独一行、三个或更多连字符 `---` = 翻页符（允许首尾空白，可紧贴正文）。
- 块：ATX 标题、有序/无序列表（多层嵌套，tab 按 4 列折算）、围栏代码块
  （```` ``` ```` 与 `~~~`）、引用、段落。
- 行内：**粗体**、*斜体*、`行内代码`、[链接](url "title")、![图片](url "alt")、
  反斜杠转义。
- 围栏代码块里的 `---`、`#`、列表记号一律普通文字，**不会翻页**。
- 空文本得到零页；残缺输入（没关围栏、空标题、缩进混用、未闭合代码跨度）
  不崩不吞后文，每处补救都显示在右侧“� 容错标注”里。

容错与增量一致性的完整设计见 [`docs/error-recovery.md`](docs/error-recovery.md)。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/parse` | 整篇全量解析 |
| POST | `/api/parse/page/{page_no}` | 按页号取单页 |
| POST | `/api/parse/edits` | 提交偏移量编辑，做增量解析，返回重算/复用页号 |
| POST | `/api/documents` | 新建讲稿（首版修订 = 1） |
| GET | `/api/documents` | 讲稿列表 |
| GET | `/api/documents/{id}` | 读取当前修订（内容 + 结构树） |
| PUT | `/api/documents/{id}` | 带 `base_revision` 保存；过期返回 **409** + 当前修订/内容 |
| GET | `/api/documents/{id}/revisions` | 修订历史 |
| GET | `/api/documents/{id}/revisions/{rev}` | 读历史修订（内容 + 结构树） |

### 增量编辑

`POST /api/parse/edits`

```json
{
  "content": "编辑前文本",
  "edits": [{"start": 0, "end": 1, "replacement": "改"}]
}
```

返回 `document`（结构树）、`page_count`、`recomputed_pages`、
`reused_pages`、`new_content`。一批编辑针对同一基线、区间互不重叠。

### 保存冲突

`PUT` 携带 `base_revision`；服务器已更新时返回：

```json
{ "error": "revision_conflict", "current_revision": 3,
  "current_content": "……", "document_id": 1 }
```

前端弹出冲突框：采用服务器版本 / 保留我的版本另存 / 手动合并。

## 代码结构

```
backend/
  app/
    parser/
      lexer.py       词法切分、围栏状态机、强制翻页策略
      splitter.py    翻页切片（裁首尾空白行）
      inline.py      行内解析（分轨 + CommonMark 强调配对）
      blocks.py      块级解析（标题/段落/列表/围栏/引用，递归建树）
      assembler.py   页组装、容错文案定案
      engine.py      全量解析、编辑应用、增量重算（最长公共前后缀）
      model.py       结构树 dataclass
    storage.py       SQLite：文档/修订、乐观锁、按修订隔离的解析缓存
    api.py           FastAPI 路由
  tests/             解析/容错/增量对拍/存储/HTTP/示例基准
  tests/fixtures/sample_deck.md(.expected.json)   回归基准
frontend/
  src/
    api.ts           接口客户端（409 → RevisionConflict）
    types.ts         结构树类型（与后端逐字段对应）
    App.tsx          左右分栏、增量预览、自动保存、冲突处理
    components/      SlideView / MarkdownView / Recovery / ConflictDialog
docker-compose.yml   前后端两个容器
docs/error-recovery.md
```

## 测试覆盖（要求项逐条对应）

- 重复解析结果相等：`test_parser_basic.py::test_parse_is_deterministic`
- 代码块里的分隔线不翻页：`test_fences.py`
- 嵌套层级保留（二级不被压成一级）：`test_lists.py`
- 残缺输入不崩且容错点有标注：`test_recovery.py`
- 增量与全量逐字段一致：`test_incremental.py`（随机编辑序列对拍）
- 过期修订号保存被拒：`test_storage.py` / `test_api.py`
- 示例讲稿固定回归：`test_sample_fixture.py` +
  `tests/fixtures/sample_deck.expected.json`（≥3 页、多层列表、代码块）
