import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import React from "react";
import App from "../App";

/**
 * 页面侧行为测试（jsdom + 假定时器 + fetch 桩）。
 *
 * 验证的是“保存这条线”的状态机，不是后端：
 *  - 新建后继续敲字，停顿后一定发出保存（旧版永远不发）；
 *  - 顶栏如实在 保存中 / 已保存 / 有未保存的改动 之间切换；
 *  - 无改动不产生多余 PUT（不制造空修订）；连续敲字去抖成一次保存；
 *  - 上一保存还没返回时继续敲字：不重复提交已存内容，返回后以新修订号
 *    只把“后来敲的”再存一次；
 *  - 409 时弹窗写明自己基于第几修订（不是问号），本地文字不丢；
 *    保留我的 / 采用服务器 / 合并后保存 三条路径各自正确；
 *  - 网络错误进入失败态并自动重试。
 *
 * 说明：不用 runAllTimersAsync（会与测试库的轮询定时器互相续命造成死循环），
 * 一律按有界时间片推进，每片之间消化微任务。
 */

interface PutCall {
  url: string;
  body: { content: string; base_revision: number };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const EMPTY_TREE = {
  document: { kind: "document", pages: [], recoveries: [] },
  page_count: 0,
  recomputed_pages: [],
  reused_pages: [],
};

function renderApp() {
  const utils = render(<App />);
  const editor = utils.container.querySelector("textarea.editor") as HTMLTextAreaElement;
  return { ...utils, editor };
}

/** 按时间片推进假定时器，直到 check 通过或超时 */
async function advanceUntil(check: () => void, totalMs = 3000, step = 10) {
  let elapsed = 0;
  // eslint-disable-next-line no-constant-condition
  while (true) {
    try {
      check();
      return;
    } catch {
      if (elapsed >= totalMs) throw new Error("条件未在限定时间内满足");
    }
    await vi.advanceTimersByTimeAsync(step);
    elapsed += step;
  }
}

async function advanceMs(ms: number) {
  await vi.advanceTimersByTimeAsync(ms);
}

async function clickCreate(fetchMock: ReturnType<typeof vi.fn>) {
  fireEvent.click(screen.getByRole("button", { name: "新建讲稿并保存" }));
  await advanceUntil(() =>
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/documents",
      expect.objectContaining({ method: "POST" }),
    ),
  );
}

function typeInto(editor: HTMLTextAreaElement, next: string) {
  fireEvent.change(editor, { target: { value: next } });
}

describe("自动保存", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let puts: PutCall[];

  beforeEach(() => {
    vi.useFakeTimers();
    puts = [];
    fetchMock = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      const u = String(url);
      const method = init?.method ?? "GET";
      if (method === "POST" && u === "/api/documents") {
        return jsonResponse(201, {
          id: 1, title: "t", current_revision: 1,
          created_at: 1, updated_at: 1,
        });
      }
      if (method === "PUT") {
        const body = JSON.parse(String(init?.body)) as PutCall["body"];
        puts.push({ url: u, body });
        return jsonResponse(200, {
          id: 1, title: "t", current_revision: body.base_revision + 1,
          revision: body.base_revision + 1, content: body.content,
          page_count: 0, document: EMPTY_TREE.document,
          created_at: 1, updated_at: 2,
        });
      }
      if (u.startsWith("/api/parse")) {
        return jsonResponse(200, EMPTY_TREE);
      }
      return jsonResponse(404, { detail: `unexpected ${method} ${u}` });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("新建后继续敲字，停顿后必定保存；无改动不再多发；顶栏状态如实切换", async () => {
    const { editor } = renderApp();
    // 尚未新建：顶栏只有“新建讲稿并保存”按钮，没有保存状态徽标
    expect(screen.queryByTestId("save-badge")).toBeNull();

    await clickCreate(fetchMock);
    await advanceUntil(() =>
      expect(screen.getByTestId("save-badge")).toHaveTextContent("已保存（修订 #1）"),
    );

    // 敲一个字：立刻应显示“有未保存的改动”，而不是继续挂着“已保存”
    const draft = editor.value + "新内容";
    typeInto(editor, draft);
    expect(screen.getByTestId("save-badge")).toHaveTextContent("有未保存的改动");

    // 停顿未满 800ms：还没发请求
    await advanceMs(300);
    expect(puts).toHaveLength(0);

    // 停顿满后：保存中 → 已保存
    await advanceMs(600);
    await advanceUntil(() => expect(puts).toHaveLength(1));
    expect(puts[0].body.base_revision).toBe(1);
    expect(puts[0].body.content).toBe(draft);
    await advanceUntil(() =>
      expect(screen.getByTestId("save-badge")).toHaveTextContent("已保存（修订 #2）"),
    );

    // 再等很久也不应冒出多余的保存请求（旧版的静默丢字就是没有任何请求）
    const before = puts.length;
    await advanceMs(3000);
    expect(puts).toHaveLength(before);
  });

  it("连续快速敲字去抖为一次保存，发出的是最新全文", async () => {
    const { editor } = renderApp();
    await clickCreate(fetchMock);

    let v = editor.value;
    for (const ch of ["甲", "乙", "丙"]) {
      v += ch;
      typeInto(editor, v);
    }
    await advanceMs(300);
    expect(puts).toHaveLength(0);
    await advanceMs(600);
    await advanceUntil(() => expect(puts).toHaveLength(1));
    expect(puts[0].body.content.endsWith("甲乙丙")).toBe(true);
  });

  it("上一保存未返回时继续敲字：不重复提交旧文，返回后只以新修订号补存新改动", async () => {
    // 让第一次 PUT 挂住，直到测试手动放行
    let resolvePut1: (r: Response) => void = () => {};
    fetchMock.mockImplementation(async (url: string | URL | Request, init?: RequestInit) => {
      const u = String(url);
      const method = init?.method ?? "GET";
      if (method === "POST" && u === "/api/documents") {
        return jsonResponse(201, {
          id: 1, title: "t", current_revision: 1, created_at: 1, updated_at: 1,
        });
      }
      if (method === "PUT" && puts.length === 0) {
        const body = JSON.parse(String(init?.body)) as PutCall["body"];
        puts.push({ url: u, body });
        return new Promise<Response>((res) => {
          resolvePut1 = res;
        });
      }
      if (method === "PUT") {
        const body = JSON.parse(String(init?.body)) as PutCall["body"];
        puts.push({ url: u, body });
        return jsonResponse(200, {
          id: 1, title: "t", current_revision: 3,
          revision: 3, content: body.content, page_count: 0,
          document: EMPTY_TREE.document, created_at: 1, updated_at: 3,
        });
      }
      if (u.startsWith("/api/parse")) return jsonResponse(200, EMPTY_TREE);
      return jsonResponse(404, { detail: `unexpected ${method} ${u}` });
    });

    const { editor } = renderApp();
    await clickCreate(fetchMock);

    const withX = editor.value + "X";
    typeInto(editor, withX);
    await advanceMs(800);
    await advanceUntil(() => expect(puts).toHaveLength(1));
    expect(puts[0].body).toMatchObject({ base_revision: 1 });
    expect(puts[0].body.content.endsWith("X")).toBe(true);
    expect(screen.getByTestId("save-badge")).toHaveTextContent("保存中");

    // 在飞期间又敲字
    const withXY = withX + "Y";
    typeInto(editor, withXY);
    expect(screen.getByTestId("save-badge")).toHaveTextContent("保存中");

    // 第一次保存返回 rev2
    resolvePut1(jsonResponse(200, {
      id: 1, title: "t", current_revision: 2, revision: 2,
      content: puts[0].body.content, page_count: 0,
      document: EMPTY_TREE.document, created_at: 1, updated_at: 2,
    }));

    // 紧接着的第二次保存：基线必须是 2，正文同时含 X 与 Y；
    // 且第一次的正文（只有 X）没有被原样再提交一遍
    await advanceUntil(() => expect(puts).toHaveLength(2));
    expect(puts[1].body.base_revision).toBe(2);
    expect(puts[1].body.content.endsWith("XY")).toBe(true);
    expect(puts[1].body.content).not.toBe(puts[0].body.content);
    await advanceUntil(() =>
      expect(screen.getByTestId("save-badge")).toHaveTextContent("已保存（修订 #3）"),
    );
  });

  it("保存请求 500 时显示失败并自动重试，重试成功后回到已保存", async () => {
    let failed = false;
    fetchMock.mockImplementation(async (url: string | URL | Request, init?: RequestInit) => {
      const u = String(url);
      const method = init?.method ?? "GET";
      if (method === "POST" && u === "/api/documents") {
        return jsonResponse(201, {
          id: 1, title: "t", current_revision: 1, created_at: 1, updated_at: 1,
        });
      }
      if (method === "PUT") {
        const body = JSON.parse(String(init?.body)) as PutCall["body"];
        if (!failed) {
          failed = true;
          puts.push({ url: u, body });
          return jsonResponse(500, { detail: "boom" });
        }
        puts.push({ url: u, body });
        return jsonResponse(200, {
          id: 1, title: "t", current_revision: 2, revision: 2,
          content: body.content, page_count: 0, document: EMPTY_TREE.document,
          created_at: 1, updated_at: 2,
        });
      }
      if (u.startsWith("/api/parse")) return jsonResponse(200, EMPTY_TREE);
      return jsonResponse(404, { detail: `unexpected ${method} ${u}` });
    });

    const { editor } = renderApp();
    await clickCreate(fetchMock);
    typeInto(editor, editor.value + "改");
    await advanceMs(800);
    await advanceUntil(() =>
      expect(screen.getByTestId("save-badge")).toHaveTextContent("保存失败"),
    );
    // 1500ms 后自动重试成功（同一份未保存文本被重试，而不是被当成已存）
    await advanceMs(1500);
    await advanceUntil(() =>
      expect(screen.getByTestId("save-badge")).toHaveTextContent("已保存（修订 #2）"),
    );
    expect(puts).toHaveLength(2);
    expect(puts[0].body.content).toBe(puts[1].body.content);
  });

  it("409：弹窗写明自己基于第 1 修订、服务器第 3 修订，本地文字不丢，保留我的另存成功", async () => {
    fetchMock.mockImplementation(async (url: string | URL | Request, init?: RequestInit) => {
      const u = String(url);
      const method = init?.method ?? "GET";
      if (method === "POST" && u === "/api/documents") {
        return jsonResponse(201, {
          id: 1, title: "t", current_revision: 1, created_at: 1, updated_at: 1,
        });
      }
      if (method === "PUT") {
        const body = JSON.parse(String(init?.body)) as PutCall["body"];
        puts.push({ url: u, body });
        if (body.base_revision === 1) {
          return jsonResponse(409, {
            error: "revision_conflict",
            message: "已更新",
            current_revision: 3,
            current_content: "来自另一个标签页的修订三",
            document_id: 1,
          });
        }
        return jsonResponse(200, {
          id: 1, title: "t", current_revision: 4, revision: 4,
          content: body.content, page_count: 0, document: EMPTY_TREE.document,
          created_at: 1, updated_at: 4,
        });
      }
      if (u.startsWith("/api/parse")) return jsonResponse(200, EMPTY_TREE);
      return jsonResponse(404, { detail: `unexpected ${method} ${u}` });
    });

    const { editor } = renderApp();
    await clickCreate(fetchMock);
    const mine = editor.value + "甲页的补充";
    typeInto(editor, mine);
    await advanceMs(800);

    // 弹窗出现，且明确写着双方修订号（旧版这里是问号）
    await advanceUntil(() => expect(screen.getByRole("dialog")).toBeTruthy());
    expect(screen.getByTestId("conflict-base-rev")).toHaveTextContent("1");
    expect(screen.getByTestId("conflict-current-rev")).toHaveTextContent("3");
    expect(screen.getByRole("dialog")).not.toHaveTextContent("？");
    // 本地刚敲的字仍在编辑框里，没有丢
    expect(editor.value).toBe(mine);
    // 顶栏不再谎称“已保存”
    expect(screen.getByTestId("save-badge")).not.toHaveTextContent("已保存");

    // 选“保留我的版本”：必须以服务器当前修订 3 为基线另存
    fireEvent.click(
      screen.getByRole("button", { name: "保留我的版本（另存为最新修订）" }),
    );
    await advanceUntil(() => {
      const keepMine = puts.find((p) => p.body.base_revision === 3);
      expect(keepMine).toBeTruthy();
      expect(keepMine!.body.content).toBe(mine);
    });
    await advanceUntil(() =>
      expect(screen.getByTestId("save-badge")).toHaveTextContent("已保存（修订 #4）"),
    );
    // 前端只拿过期基线 1 强推过一次；另存用的是 3，对方 rev3 不会被覆盖
    expect(puts.filter((p) => p.body.base_revision === 1)).toHaveLength(1);
  });

  it("409：采用服务器版本后编辑器与基线切到 rev3，再敲字以 rev3 为基线保存", async () => {
    fetchMock.mockImplementation(async (url: string | URL | Request, init?: RequestInit) => {
      const u = String(url);
      const method = init?.method ?? "GET";
      if (method === "POST" && u === "/api/documents") {
        return jsonResponse(201, {
          id: 1, title: "t", current_revision: 1, created_at: 1, updated_at: 1,
        });
      }
      if (method === "PUT") {
        const body = JSON.parse(String(init?.body)) as PutCall["body"];
        puts.push({ url: u, body });
        return jsonResponse(409, {
          error: "revision_conflict",
          message: "已更新",
          current_revision: 3,
          current_content: "服务器版全文",
          document_id: 1,
        });
      }
      if (u.startsWith("/api/parse")) return jsonResponse(200, EMPTY_TREE);
      return jsonResponse(404, { detail: `unexpected ${method} ${u}` });
    });

    const { editor } = renderApp();
    await clickCreate(fetchMock);
    typeInto(editor, editor.value + "本地改动");
    await advanceMs(800);
    await advanceUntil(() => expect(screen.getByRole("dialog")).toBeTruthy());

    fireEvent.click(
      screen.getByRole("button", { name: "采用服务器版本（放弃我的改动）" }),
    );
    expect(editor.value).toBe("服务器版全文");
    expect(screen.getByTestId("rev-label")).toHaveTextContent("修订 #3");
    expect(screen.getByTestId("save-badge")).toHaveTextContent("已保存（修订 #3）");

    // 再敲字：请求必须带着新基线 3 发出（桩再回 409 也无所谓，验证的是基线）
    typeInto(editor, "服务器版全文-继续写");
    await advanceMs(800);
    await advanceUntil(() => expect(puts[puts.length - 1].body.base_revision).toBe(3));
  });

  it("409：手动合并后的文本以服务器当前修订为基线保存", async () => {
    fetchMock.mockImplementation(async (url: string | URL | Request, init?: RequestInit) => {
      const u = String(url);
      const method = init?.method ?? "GET";
      if (method === "POST" && u === "/api/documents") {
        return jsonResponse(201, {
          id: 1, title: "t", current_revision: 1, created_at: 1, updated_at: 1,
        });
      }
      if (method === "PUT") {
        const body = JSON.parse(String(init?.body)) as PutCall["body"];
        puts.push({ url: u, body });
        if (body.base_revision === 1) {
          return jsonResponse(409, {
            error: "revision_conflict",
            message: "已更新",
            current_revision: 2,
            current_content: "服务器第二版",
            document_id: 1,
          });
        }
        return jsonResponse(200, {
          id: 1, title: "t", current_revision: 3, revision: 3,
          content: body.content, page_count: 0, document: EMPTY_TREE.document,
          created_at: 1, updated_at: 3,
        });
      }
      if (u.startsWith("/api/parse")) return jsonResponse(200, EMPTY_TREE);
      return jsonResponse(404, { detail: `unexpected ${method} ${u}` });
    });

    const { editor } = renderApp();
    await clickCreate(fetchMock);
    typeInto(editor, editor.value + "本地");
    await advanceMs(800);
    await advanceUntil(() => expect(screen.getByRole("dialog")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "手动合并" }));
    const mergeEditor = document.querySelector(
      "textarea.merge-editor",
    ) as HTMLTextAreaElement;
    expect(mergeEditor).toBeTruthy();
    fireEvent.change(mergeEditor, { target: { value: "手动合并后的两全文本" } });
    fireEvent.click(screen.getByRole("button", { name: "用合并结果保存" }));

    await advanceUntil(() => {
      const merged = puts.find(
        (p) => p.body.base_revision === 2 && p.body.content === "手动合并后的两全文本",
      );
      expect(merged).toBeTruthy();
    });
    expect(editor.value).toBe("手动合并后的两全文本");
    await advanceUntil(() =>
      expect(screen.getByTestId("save-badge")).toHaveTextContent("已保存（修订 #3）"),
    );
  });
});
