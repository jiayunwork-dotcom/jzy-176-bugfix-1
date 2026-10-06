import React from "react";
import type { Page } from "../types";
import { BlockView } from "./MarkdownView";
import { RecoveryBadges } from "./Recovery";

export function SlideView({ page }: { page: Page }) {
  return (
    <article className="slide">
      <header className="slide-head">
        <span className="slide-no">第 {page.page_no} 页</span>
        <RecoveryBadges recoveries={page.recoveries} />
      </header>
      <div className="slide-body">
        {page.blocks.length === 0 ? (
          <p className="slide-empty">（空白页）</p>
        ) : (
          page.blocks.map((b, i) => <BlockView key={i} node={b} />)
        )}
      </div>
    </article>
  );
}
