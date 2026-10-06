import React, { useState } from "react";
import type { ConflictResponse } from "../types";

/**
 * 保存冲突对话框：明确告知当前修订号，让写作者选择
 *  - 保留服务器版本（放弃本地改动）
 *  - 保留我的版本（以最新修订号为依据重新保存）
 *  - 手动合并（左右对照，自己改完再存）
 * 绝不会悄悄覆盖任何一方。
 */
export function ConflictDialog({
  conflict,
  localContent,
  onChooseServer,
  onForceMine,
  onMerge,
}: {
  conflict: ConflictResponse;
  localContent: string;
  onChooseServer: () => void;
  onForceMine: (merged: string) => void;
  onMerge: () => void;
}) {
  const [mode, setMode] = useState<"choose" | "merge">("choose");
  const [merged, setMerged] = useState(localContent);

  return (
    <div className="modal-backdrop">
      <div className="modal" role="dialog" aria-modal="true">
        <h2>保存冲突：这份讲稿已被别处修改</h2>
        <p className="modal-desc">
          你基于第 <b>？</b> 修订在编辑，而服务器当前已是第{" "}
          <b>{conflict.current_revision}</b> 修订。为避免覆盖别人的改动，本次保存已被拒绝。
        </p>

        {mode === "choose" ? (
          <div className="conflict-actions">
            <button className="btn" onClick={onChooseServer}>
              采用服务器版本（放弃我的改动）
            </button>
            <button
              className="btn btn-primary"
              onClick={() => onForceMine(localContent)}
            >
              保留我的版本（另存为最新修订）
            </button>
            <button className="btn" onClick={() => setMode("merge")}>
              手动合并
            </button>
            <details className="conflict-preview">
              <summary>查看服务器版本</summary>
              <pre>{conflict.current_content}</pre>
            </details>
          </div>
        ) : (
          <div className="merge-view">
            <div className="merge-cols">
              <div>
                <h3>我的版本</h3>
                <pre>{localContent}</pre>
              </div>
              <div>
                <h3>服务器版本（第 {conflict.current_revision} 修订）</h3>
                <pre>{conflict.current_content}</pre>
              </div>
            </div>
            <h3>合并结果（可编辑）</h3>
            <textarea
              className="merge-editor"
              value={merged}
              onChange={(e) => setMerged(e.target.value)}
            />
            <div className="conflict-actions">
              <button className="btn" onClick={() => setMode("choose")}>
                返回
              </button>
              <button className="btn btn-primary" onClick={() => onForceMine(merged)}>
                用合并结果保存
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
