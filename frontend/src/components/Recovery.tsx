import React from "react";
import type { Recovery } from "../types";

const LABELS: Record<string, string> = {
  unclosed_fence: "代码围栏未闭合",
  empty_heading: "空标题",
  mixed_indentation: "缩进混用",
  tab_indent_normalized: "制表符缩进",
  unclosed_code_span: "行内代码未闭合",
  bom_stripped: "已去除 BOM",
};

/**
 * 容错标注：每一处“猜”出来的地方都明确告诉写作者用了什么补救。
 * inline 时显示为标题行尾的小标记。
 */
export function RecoveryBadges({
  recoveries,
  inline = false,
}: {
  recoveries: Recovery[];
  inline?: boolean;
}) {
  if (!recoveries || recoveries.length === 0) return null;
  return (
    <span className={inline ? "rec-badges rec-badges-inline" : "rec-badges"}>
      {recoveries.map((r, i) => (
        <mark
          key={i}
          className={`rec-badge rec-${r.severity}`}
          title={`${r.message}${r.line ? `（第 ${r.line} 行）` : ""}`}
        >
          ⚑ {LABELS[r.code] ?? r.code}
        </mark>
      ))}
    </span>
  );
}
