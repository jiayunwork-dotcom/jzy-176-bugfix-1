// 与后端 app/parser/model.py 逐字段对应的结构树类型。
// 前端不做任何 Markdown 解析，只渲染这棵树。

export interface Position {
  line: number;
  end_line: number | null;
}

export interface Recovery {
  code: string;
  message: string;
  line: number | null;
  end_line: number | null;
  detail: Record<string, unknown> | null;
  severity: "info" | "warning";
  page: number;
}

export type Inline =
  | { kind: "text"; value: string }
  | { kind: "escape"; value: string }
  | { kind: "emphasis"; children: Inline[] }
  | { kind: "strong"; children: Inline[] }
  | { kind: "code_span"; value: string }
  | { kind: "link"; url: string; title: string | null; children: Inline[] }
  | { kind: "image"; url: string; title: string | null; alt: string };

export interface Heading {
  kind: "heading";
  level: number;
  children: Inline[];
  position: Position;
  recoveries: Recovery[];
}
export interface Paragraph {
  kind: "paragraph";
  children: Inline[];
  position: Position;
  recoveries: Recovery[];
}
export interface CodeBlock {
  kind: "code_block";
  value: string;
  info: string;
  language: string | null;
  closed: boolean;
  position: Position;
  recoveries: Recovery[];
}
export interface Quote {
  kind: "quote";
  blocks: Block[];
  position: Position;
  recoveries: Recovery[];
}
export interface ListItem {
  kind: "list_item";
  blocks: Block[];
  level: number;
  position: Position;
  recoveries: Recovery[];
  loose: boolean;
  marker_column: number;
}
export interface BulletList {
  kind: "bullet_list";
  items: ListItem[];
  position: Position;
  recoveries: Recovery[];
}
export interface OrderedList {
  kind: "ordered_list";
  items: ListItem[];
  start: number;
  position: Position;
  recoveries: Recovery[];
}
export type Block =
  | Heading
  | Paragraph
  | CodeBlock
  | Quote
  | BulletList
  | OrderedList;

export interface Page {
  page_no: number;
  blocks: Block[];
  position: Position;
  recoveries: Recovery[];
}

export interface DocumentTree {
  pages: Page[];
  recoveries: Recovery[];
  kind: "document";
}

export interface ParseResponse {
  document: DocumentTree;
  page_count: number;
  recomputed_pages?: number[];
  reused_pages?: number[];
  new_content?: string;
}

export interface DocMeta {
  id: number;
  title: string;
  current_revision: number;
  created_at: number;
  updated_at: number;
}

export interface DocResponse extends DocMeta {
  revision: number;
  content: string;
  page_count: number;
  document: DocumentTree;
}

export interface ConflictResponse {
  error: "revision_conflict";
  message: string;
  current_revision: number;
  current_content: string;
  document_id: number;
}
