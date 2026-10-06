import type {
  ConflictResponse,
  DocMeta,
  DocResponse,
  ParseResponse,
} from "./types";

export class RevisionConflict extends Error {
  constructor(public readonly body: ConflictResponse) {
    super(body.message);
  }
}

async function jsonOrThrow(res: Response): Promise<any> {
  if (res.status === 409) {
    throw new RevisionConflict((await res.json()) as ConflictResponse);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

export interface EditIn {
  start: number;
  end: number;
  replacement: string;
}

export const api = {
  parseEdits(content: string, edits: EditIn[]): Promise<ParseResponse> {
    return fetch("/api/parse/edits", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, edits }),
    }).then(jsonOrThrow);
  },

  parseFull(content: string): Promise<ParseResponse> {
    return fetch("/api/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    }).then(jsonOrThrow);
  },

  listDocuments(): Promise<{ documents: DocMeta[] }> {
    return fetch("/api/documents").then(jsonOrThrow);
  },

  createDocument(title: string, content: string): Promise<DocMeta> {
    return fetch("/api/documents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, content }),
    }).then(jsonOrThrow);
  },

  getDocument(id: number, revision?: number): Promise<DocResponse> {
    const q = revision ? `?revision=${revision}` : "";
    return fetch(`/api/documents/${id}${q}`).then(jsonOrThrow);
  },

  saveDocument(
    id: number,
    content: string,
    baseRevision: number,
    title?: string,
  ): Promise<DocResponse> {
    return fetch(`/api/documents/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        content,
        base_revision: baseRevision,
        title,
      }),
    }).then(jsonOrThrow);
  },

  listRevisions(
    id: number,
  ): Promise<{ document_id: number; revisions: { revision: number; created_at: number }[] }> {
    return fetch(`/api/documents/${id}/revisions`).then(jsonOrThrow);
  },

  getRevision(id: number, revision: number): Promise<DocResponse> {
    return fetch(`/api/documents/${id}/revisions/${revision}`).then(jsonOrThrow);
  },
};
