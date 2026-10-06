import React from "react";
import type { Block, CodeBlock, Inline, ListItem, OrderedList, BulletList, Quote, Heading } from "../types";
import { RecoveryBadges } from "./Recovery";

export function InlineView({ node }: { node: Inline }): JSX.Element {
  switch (node.kind) {
    case "text":
    case "escape":
      return <>{node.value}</>;
    case "emphasis":
      return (
        <em>
          {node.children.map((c, i) => (
            <InlineView key={i} node={c} />
          ))}
        </em>
      );
    case "strong":
      return (
        <strong>
          {node.children.map((c, i) => (
            <InlineView key={i} node={c} />
          ))}
        </strong>
      );
    case "code_span":
      return <code className="md-code-span">{node.value}</code>;
    case "link":
      return (
        <a href={node.url} title={node.title ?? undefined} target="_blank" rel="noreferrer">
          {node.children.map((c, i) => (
            <InlineView key={i} node={c} />
          ))}
        </a>
      );
    case "image":
      return (
        <img
          className="md-image"
          src={node.url}
          alt={node.alt}
          title={node.title ?? undefined}
        />
      );
  }
}

function HeadingView({ node }: { node: Heading }) {
  const Tag = (`h${node.level}` as unknown) as keyof JSX.IntrinsicElements;
  return (
    <Tag className={`md-h md-h${node.level}`}>
      {node.children.map((c, i) => (
        <InlineView key={i} node={c} />
      ))}
      <RecoveryBadges recoveries={node.recoveries} inline />
    </Tag>
  );
}

function CodeBlockView({ node }: { node: CodeBlock }) {
  return (
    <div className="md-code-block-wrap">
      {node.language && <span className="md-code-lang">{node.language}</span>}
      <pre className={`md-code-block${node.closed ? "" : " is-unclosed"}`}>
        <code>{node.value}</code>
      </pre>
      <RecoveryBadges recoveries={node.recoveries} />
    </div>
  );
}

function ListItems({
  items,
  ordered,
  start,
}: {
  items: ListItem[];
  ordered: boolean;
  start?: number;
}) {
  const Tag = ordered ? "ol" : "ul";
  return (
    <Tag
      className="md-list"
      start={ordered ? start ?? 1 : undefined}
      style={ordered ? { ["--list-start" as any]: start ?? 1 } : undefined}
    >
      {items.map((item, i) => (
        <li key={i} className={item.loose ? "md-li md-li-loose" : "md-li"}>
          {item.blocks.map((b, j) => (
            <BlockView key={j} node={b} />
          ))}
          <RecoveryBadges recoveries={item.recoveries} />
        </li>
      ))}
    </Tag>
  );
}

function ListView({ node }: { node: OrderedList | BulletList }) {
  return (
    <>
      <ListItems
        items={node.items}
        ordered={node.kind === "ordered_list"}
        start={node.kind === "ordered_list" ? node.start : undefined}
      />
      <RecoveryBadges recoveries={node.recoveries} />
    </>
  );
}

function QuoteView({ node }: { node: Quote }) {
  return (
    <blockquote className="md-quote">
      {node.blocks.map((b, i) => (
        <BlockView key={i} node={b} />
      ))}
      <RecoveryBadges recoveries={node.recoveries} />
    </blockquote>
  );
}

export function BlockView({ node }: { node: Block }): JSX.Element {
  switch (node.kind) {
    case "heading":
      return <HeadingView node={node} />;
    case "paragraph":
      return (
        <p className="md-p">
          {node.children.map((c, i) => (
            <InlineView key={i} node={c} />
          ))}
        </p>
      );
    case "code_block":
      return <CodeBlockView node={node} />;
    case "quote":
      return <QuoteView node={node} />;
    case "bullet_list":
    case "ordered_list":
      return <ListView node={node} />;
  }
}
