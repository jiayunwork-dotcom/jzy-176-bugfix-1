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
  | { kind: "dirty" }
  | { kind: "conflict" }
  | { kind: "error"; message: string };

const AUTOSAVE_DELAY = 800;

export default function App() {
  const [content, setContent] = useState<string>(SAMPLE);
  const [tree, setTree] = useState<DocumentTree | null>(null);
  const [pageCount, setPageCount] = useState(0);
  const [pageNo, setPageNo] = useState(1);
  const [recomputed, setRecomputed] = useState<number[]>([]);
  const [reused, setReused] = useState<number[]>([]);

  const [docId, setDocId] = useState<number | null>(null);
  const [baseRevision, setBaseRevision] = useState(0);
  const [saveState, setSaveState] = useState<SaveState>({ kind: "idle" });
  const [conflict, setConflict] = useState<ConflictResponse | null>(null);

  // 增量解析：以 ref 记录上次已解析文本，敲字时只发一批偏移量编辑。
  const lastParsed = useRef<string>("");
  const parseTimer = useRef<number | null>(null);

  // ---- 自动保存所需的“最新值”镜像（避免异步闭包读到旧 state）----
  // contentRef：编辑器此刻的全部文本（onChange 第一时间同步）。
  const contentRef = useRef(content);
  // baseRevRef：本页下一次保存应依据的修订号（见下“单飞+合并”说明）。
  const baseRevRef = useRef(0);
  // lastSavedRef：与服务端一致的文本 + 修订号；它是“有没有未保存改动”
  // 的唯一判据，只在保存成功 / 新建 / 采用服务端版本时更新。
  const lastSavedRef = useRef<{ text: string; revision: number }>({
    text: "",
    revision: 0,
  });
  const conflictRef = useRef<ConflictResponse | null>(null);
  const saveTimer = useRef<number | null>(null);
  const saveRunning = useRef(false);
  const pumpRef = useRef<() => Promise<void>>(async () => {});

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
    contentRef.current = next;
    scheduleParse(next);
    scheduleSave();
  };

  // ---------------- 自动保存 ----------------
  // 设计（与 docs/concurrency.md 对应）：
  //   1. 每次改动停 AUTOSAVE_DELAY 后尝试保存；
  //   2. 同一时刻只允许一个 PUT 在飞（单飞）。保存期间又敲了字，
  //      不并发发第二个，而是等这个返回后“合并”再存一次；
  //   3. 下一次保存依据哪个修订号？——依据“已确认保存的那一版”：
  //      保存成功就前移到返回的新修订号；保存期间继续敲的字是在
  //      该新修订之上的新改动，用新修订号再存，绝不会用旧号覆盖；
  //   4. 待存文本与 lastSaved 相同则不发请求——没有改动不产生新修订，
  //      也不会把刚存好的内容当成新改动再提交一遍。
  const scheduleSave = useCallback(() => {
    if (docId == null || conflictRef.current) return;
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      void pumpRef.current();
    }, AUTOSAVE_DELAY);
  }, [docId]);

  // 卸载时清掉待发的保存定时器，避免卸载后再 setState
  useEffect(() => {
    return () => {
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
    };
  }, []);

  // 保存泵：串行消费“编辑器相对服务端的净改动”。
  useEffect(() => {
    pumpRef.current = async () => {
      const id = docId;
      if (id == null || saveRunning.current || conflictRef.current) return;
      const text = contentRef.current;
      // 与服务端一致：没有净改动，什么都不发，状态如实显示已保存。
      if (
        text === lastSavedRef.current.text &&
        baseRevRef.current === lastSavedRef.current.revision
      ) {
        setSaveState({
          kind: "saved",
          revision: lastSavedRef.current.revision,
          at: Date.now(),
        });
        return;
      }
      saveRunning.current = true;
      setSaveState({ kind: "saving" });
      try {
        const resp = await api.saveDocument(id, text, baseRevRef.current);
        baseRevRef.current = resp.revision;
        setBaseRevision(resp.revision);
        lastSavedRef.current = { text, revision: resp.revision };
        // 保存往返期间人又敲了字？contentRef 已比刚存的新——
        // 状态标脏并安排一次“基于新修订号”的后续保存，这段字不丢。
        if (contentRef.current !== text) {
          setSaveState({ kind: "dirty" });
          scheduleSave();
        } else {
          setSaveState({ kind: "saved", revision: resp.revision, at: Date.now() });
        }
      } catch (e) {
        if (e instanceof RevisionConflict) {
          // 冲突期间锁定自动保存，等人决定；本地内容原样保留。
          conflictRef.current = e.body;
          setConflict(e.body);
          setSaveState({ kind: "conflict" });
          if (saveTimer.current) window.clearTimeout(saveTimer.current);
        } else {
          setSaveState({ kind: "error", message: (e as Error).message });
          // 网络/5xx 不是冲突：稍后重试同一份未保存内容。
          saveTimer.current = window.setTimeout(
            () => void pumpRef.current(),
            1500,
          );
        }
      } finally {
        saveRunning.current = false;
      }
    };
  }, [docId, scheduleSave]);

  const createNew = async () => {
    const meta = await api.createDocument("无标题讲稿", contentRef.current);
    setDocId(meta.id);
    setBaseRevision(meta.current_revision);
    baseRevRef.current = meta.current_revision;
    lastSavedRef.current = {
      text: contentRef.current,
      revision: meta.current_revision,
    };
    setSaveState({
      kind: "saved",
      revision: meta.current_revision,
      at: Date.now(),
    });
  };

  // ---------------- 冲突处理 ----------------
  // 冲突框打开期间忽略编辑器自动保存（onEdit 里也被 conflictRef 挡住），
  // 三个选择都在这里显式完成。
  const chooseServer = () => {
    if (!conflict) return;
    const c = conflict.current_content;
    setContent(c);
    contentRef.current = c;
    setBaseRevision(conflict.current_revision);
    baseRevRef.current = conflict.current_revision;
    lastSavedRef.current = { text: c, revision: conflict.current_revision };
    conflictRef.current = null;
    setConflict(null);
    setSaveState({
      kind: "saved",
      revision: conflict.current_revision,
      at: Date.now(),
    });
    scheduleParse(c);
  };

  // “保留我的”与“合并后保存”走同一条路：把要保留的文本，
  // 以冲突返回的服务端当前修订号为基准另存为最新修订。
  const forceMine = async (merged: string) => {
    if (!conflict) return;
    const currentRev = conflict.current_revision;
    const id = docId as number;
    conflictRef.current = null;
    setConflict(null);
    setSaveState({ kind: "saving" });
    try {
      const resp = await api.saveDocument(id, merged, currentRev);
      baseRevRef.current = resp.revision;
      setBaseRevision(resp.revision);
      lastSavedRef.current = { text: merged, revision: resp.revision };
      if (contentRef.current !== merged) {
        setContent(merged);
        contentRef.current = merged;
        scheduleParse(merged);
      }
      setSaveState({ kind: "saved", revision: resp.revision, at: Date.now() });
    } catch (e) {
      // 极罕见：做决定期间又有第三端写入。重新拿冲突，本地文本不丢。
      if (e instanceof RevisionConflict) {
        conflictRef.current = e.body;
        setConflict(e.body);
        setSaveState({ kind: "conflict" });
      } else {
        setSaveState({ kind: "error", message: (e as Error).message });
      }
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
              <span className="rev">修订 #{baseRevision}</span>
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
            readOnly={conflict != null}
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
        />
      )}
    </div>
  );
}

function SaveBadge({ state }: { state: SaveState }) {
  if (state.kind === "saving") return <span className="save saving">保存中…</span>;
  if (state.kind === "saved")
    return <span className="save saved">已保存（修订 #{state.revision}）</span>;
  if (state.kind === "dirty")
    return <span className="save dirty">有未保存的改动…</span>;
  if (state.kind === "conflict")
    return <span className="save error">保存冲突，等待处理</span>;
  if (state.kind === "error")
    return <span className="save error">保存失败：{state.message}（将重试）</span>;
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
