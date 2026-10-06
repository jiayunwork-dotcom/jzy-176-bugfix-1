import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, EditIn, RevisionConflict } from "./api";
import type { ConflictResponse, DocumentTree, ParseResponse } from "./types";
import { SlideView } from "./components/SlideView";
import { ConflictDialog } from "./components/ConflictDialog";

const SAMPLE = `# 欢迎使用讲稿写作台

左边写 Markdown，右边即时按**页**预览。

- 单独一行 \`---\` 就是翻页
- 支持 **粗体**、*斜体*、\`行内代码\`
  - 以及多层列表
  - 第二级不会被压扁
- 还有[链接](https://example.com)和图片

> 围栏代码块里的 \`---\` 不会翻页。

\`\`\`python
def hello():
    # 这里的 --- 和 # 都是普通文字
    return "hi"
\`\`\`
---
# 第二页

1. 有序项一
2. 有序项二
   1. 嵌套有序
   2. 再来一个

---
# 第三页

试试删掉上面某个 \`---\`，或忘记关掉一个代码围栏，
右边会标出每一处“猜”出来的地方。
`;

type SaveState =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "saved"; revision: number; at: number }
  | { kind: "error"; message: string };

export default function App() {
  const [content, setContent] = useState<string>(SAMPLE);
  const [tree, setTree] = useState<DocumentTree | null>(null);
  const [pageCount, setPageCount] = useState(0);
  const [pageNo, setPageNo] = useState(1);
  const [recomputed, setRecomputed] = useState<number[]>([]);
  const [reused, setReused] = useState<number[]>([]);

  const [docId, setDocId] = useState<number | null>(null);
  const [baseRevision, setBaseRevision] = useState(0);
  const [serverRevision, setServerRevision] = useState(0);
  const [saveState, setSaveState] = useState<SaveState>({ kind: "idle" });
  const [conflict, setConflict] = useState<ConflictResponse | null>(null);

  // 增量解析：以 ref 记录上次已解析文本，敲字时只发一批偏移量编辑。
  const lastParsed = useRef<string>("");
  const parseTimer = useRef<number | null>(null);
  const saveTimer = useRef<number | null>(null);

  const scheduleParse = useCallback((next: string) => {
    if (parseTimer.current) window.clearTimeout(parseTimer.current);
    parseTimer.current = window.setTimeout(async () => {
      const prev = lastParsed.current;
      try {
        let resp: ParseResponse;
        if (prev === "" || prev === next) {
          resp = await api.parseFull(next);
        } else {
          const edits = diffToEdits(prev, next);
          if (edits.length === 0) {
            lastParsed.current = next;
            return;
          }
          try {
            resp = await api.parseEdits(prev, edits);
          } catch {
            // 增量接口异常时退回全量，界面正确性优先
            resp = await api.parseFull(next);
          }
        }
        lastParsed.current = next;
        setTree(resp.document);
        setPageCount(resp.page_count);
        setRecomputed(resp.recomputed_pages ?? []);
        setReused(resp.reused_pages ?? []);
        setPageNo((n) => Math.min(Math.max(1, n), Math.max(1, resp.page_count)));
      } catch (e) {
        console.error("解析失败", e);
      }
    }, 220);
  }, []);

  // 初次加载先全量解析一次
  useEffect(() => {
    lastParsed.current = SAMPLE;
    scheduleParse(SAMPLE);
  }, [scheduleParse]);

  const onEdit = (next: string) => {
    setContent(next);
    scheduleParse(next);
  };

  // ---------------- 自动保存 ----------------
  const save = useCallback(
    async (text: string, rev: number) => {
      if (docId == null) return;
      setSaveState({ kind: "saving" });
      try {
        const resp = await api.saveDocument(docId, text, rev);
        setBaseRevision(resp.revision);
        setServerRevision(resp.revision);
        setSaveState({ kind: "saved", revision: resp.revision, at: Date.now() });
      } catch (e) {
        if (e instanceof RevisionConflict) {
          setConflict(e.body);
          setSaveState({ kind: "idle" });
        } else {
          setSaveState({ kind: "error", message: (e as Error).message });
        }
      }
    },
    [docId],
  );

  // 停止输入 800ms 后自动保存
  useEffect(() => {
    if (docId == null) return;
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      if (content !== lastSavedRef.current) save(content, baseRevision);
    }, 800);
    return () => {
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
    };
  }, [content, docId, baseRevision, save]);

  const lastSavedRef = useRef<string>("");
  useEffect(() => {
    if (saveState.kind === "saved") lastSavedRef.current = content;
  }, [saveState, content]);

  const createNew = async () => {
    const meta = await api.createDocument("无标题讲稿", content);
    setDocId(meta.id);
    setBaseRevision(meta.current_revision);
    setServerRevision(meta.current_revision);
    lastSavedRef.current = content;
    setSaveState({ kind: "saved", revision: meta.current_revision, at: Date.now() });
  };

  // ---------------- 冲突处理 ----------------
  const chooseServer = () => {
    if (!conflict) return;
    setContent(conflict.current_content);
    setBaseRevision(conflict.current_revision);
    setServerRevision(conflict.current_revision);
    lastSavedRef.current = conflict.current_content;
    setConflict(null);
    scheduleParse(conflict.current_content);
  };

  const forceMine = async (merged: string) => {
    if (!conflict) return;
    setConflict(null);
    await save(merged, conflict.current_revision);
    if (merged !== content) {
      setContent(merged);
      scheduleParse(merged);
    }
  };

  const currentPage = useMemo(
    () => tree?.pages.find((p) => p.page_no === pageNo) ?? tree?.pages[0] ?? null,
    [tree, pageNo],
  );

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">📝 讲稿写作台</div>
        <div className="topbar-meta">
          {docId == null ? (
            <button className="btn btn-primary" onClick={createNew}>
              新建讲稿并保存
            </button>
          ) : (
            <>
              <span className="rev">
                修订 #{baseRevision}
                {serverRevision !== baseRevision && ` (服务器 #${serverRevision})`}
              </span>
              <SaveBadge state={saveState} />
            </>
          )}
        </div>
      </header>

      <main className="panes">
        <section className="pane pane-editor">
          <textarea
            className="editor"
            value={content}
            spellCheck={false}
            onChange={(e) => onEdit(e.target.value)}
            placeholder="在这里写 Markdown，单独一行 --- 翻页"
          />
        </section>

        <section className="pane pane-preview">
          <div className="preview-toolbar">
            <button
              className="btn btn-small"
              disabled={pageNo <= 1}
              onClick={() => setPageNo((n) => n - 1)}
            >
              上一页
            </button>
            <span className="page-indicator">
              {pageCount === 0 ? "0 / 0" : `${pageNo} / ${pageCount}`}
            </span>
            <button
              className="btn btn-small"
              disabled={pageNo >= pageCount}
              onClick={() => setPageNo((n) => n + 1)}
            >
              下一页
            </button>
            <span className="recompute-hint" title="本次增量预览重算的页">
              {recomputed.length > 0 &&
                `重算第 ${recomputed.join(", ")} 页 · 复用 ${reused.length} 页`}
            </span>
          </div>
          <div className="slides">
            {currentPage ? (
              <SlideView page={currentPage} />
            ) : (
              <div className="no-page">空文本得到零页，从左边开始写吧。</div>
            )}
          </div>
        </section>
      </main>

      {conflict && (
        <ConflictDialog
          conflict={conflict}
          localContent={content}
          onChooseServer={chooseServer}
          onForceMine={forceMine}
          onMerge={() => undefined}
        />
      )}
    </div>
  );
}

function SaveBadge({ state }: { state: SaveState }) {
  if (state.kind === "saving") return <span className="save saving">保存中…</span>;
  if (state.kind === "saved")
    return <span className="save saved">已保存（修订 #{state.revision}）</span>;
  if (state.kind === "error")
    return <span className="save error">保存失败：{state.message}</span>;
  return <span className="save">未保存</span>;
}

/**
 * 计算从上一文本到新文本的最小编辑（单区间）。
 * 编辑器每次改动通常只在一个位置增删，单区间足以覆盖；
 * 若结构变化复杂，增量接口失败也会在调用处退回全量。
 */
function diffToEdits(prev: string, next: string): EditIn[] {
  let lo = 0;
  while (lo < prev.length && lo < next.length && prev[lo] === next[lo]) lo++;
  let hiP = prev.length;
  let hiN = next.length;
  while (hiP > lo && hiN > lo && prev[hiP - 1] === next[hiN - 1]) {
    hiP--;
    hiN--;
  }
  if (lo === hiP && lo === hiN) return [];
  return [{ start: lo, end: hiP, replacement: next.slice(lo, hiN) }];
}
