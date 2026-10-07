import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act, within } from "@testing-library/react";
import App from "./App.tsx";

/**
 * 页面侧保存行为测试（mock fetch + 假时间 + fireEvent）。
 *
 * 验证的承诺（对应 docs/concurrency.md 第 3 节）：
 *  1. 新建并保存后继续敲字，停一小会儿一定会再发 PUT（回归“一直显示
 *     已保存、再也不发请求”的 bug）；
 *  2. 没有净改动不发 PUT（不白产生新修订）；
 *  3. 顶栏如实在 保存中 / 已保存 / 有未保存的改动 之间切换；
 *  4. 一次保存未返回时继续敲字：不并发第二个 PUT；返回后用“新修订号”
 *     把期间的新改动再存一次，且不会把已存内容当新改动重发；
 *  5. 另一个标签页先存了新修订：本页保存收到 409 → 弹冲突框，框内写明
 *     “我基于第几修订 / 服务器第几修订”，编辑器锁定，本地内容不丢；
 *  6. 采用服务器版本：内容与基准换成服务端版本，不再另存；
 *  7. 保留我的：以服务端当前修订号为基准把本地内容另存为新修订；
 *  8. 手动合并：合并文本以当前修订号保存。
 */

const PARSE_JSON = () => ({
  document: { kind: "document", pages: [], recoveries: [] },
  page_count: 0,
  recomputed_pages: [],
  reused_pages: [],
});

const META = (rev = 1) => ({
  id: 1,
  title: "无标题讲稿",
  current_revision: rev,
  created_at: 0,
  updated_at: 0,
});

const DOC = (rev: number, content: string) => ({
  ...META(rev),
  revision: rev,
  content,
  page_count: 0,
  document: { kind: "document", pages: [], recoveries: [] },
});

const CONFLICT = () => ({
  error: "revision_conflict",
  message: "冲突",
  base_revision: 1,
  current_revision: 2,
  current_content: "标签页乙的内容",
  document_id: 1,
});

const editor = () =>
  screen.getByPlaceholderText(/在这里写 Markdown/) as HTMLTextAreaElement;

// 推进假时间，并让随后的 fetch/setState Promise 链落定（act 内提交）
async function flush(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
    for (let i = 0; i < 12; i++) await Promise.resolve();
  });
}

async function typeText(text: string) {
  await act(async () => {
    const ta = editor();
    fireEvent.change(ta, { target: { value: ta.value + text } });
    await Promise.resolve();
  });
}

type FetchRoute = (url: string, init: RequestInit) => any;

function mount(route: FetchRoute) {
  const calls = { put: [] as any[], putStarted: 0, post: 0 };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: any, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      // 同步记录“请求已发起”（即便响应体还是个挂起的 Promise）
      if (method === "PUT") calls.putStarted += 1;
      const payload = await route(url, init ?? {});
      if (method === "PUT")
        calls.put.push(JSON.parse((init!.body as string) ?? "{}"));
      if (url.endsWith("/api/documents") && method === "POST") calls.post += 1;
      return {
        ok: payload.status >= 200 && payload.status < 300,
        status: payload.status,
        statusText: String(payload.status),
        json: async () => payload.json,
      } as Response;
    }),
  );
  return calls;
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function createAndSettle(calls: {
  put: any[];
  putStarted: number;
  post: number;
}) {
  render(<App />);
  await flush(300); // 初次解析
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "新建讲稿并保存" }));
    await Promise.resolve();
  });
  await flush(0);
  expect(calls.post).toBe(1);
  await vi.waitFor(() =>
    expect(screen.getByText(/已保存（修订 #1）/)).toBeInTheDocument(),
  );
}

async function clickButton(name: RegExp) {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name }));
    await Promise.resolve();
  });
}

describe("自动保存", () => {
  it("新建后继续敲字，停顿后一定再保存；状态在保存中/已保存间切换", async () => {
    let rev = 1;
    const calls = mount((url, init) => {
      const m = init.method ?? "GET";
      if (url.endsWith("/api/documents") && m === "POST")
        return { status: 201, json: META(1) };
      if (m === "PUT") {
        const body = JSON.parse(init.body as string);
        rev += 1;
        return { status: 200, json: DOC(rev, body.content) };
      }
      return { status: 200, json: PARSE_JSON() };
    });

    await createAndSettle(calls);

    await typeText("X");
    expect(calls.put).toHaveLength(0); // 还在防抖窗口内
    await flush(800);
    await vi.waitFor(() => expect(calls.put).toHaveLength(1));
    await vi.waitFor(() =>
      expect(screen.getByText(/已保存（修订 #2）/)).toBeInTheDocument(),
    );
    expect(calls.put[0].base_revision).toBe(1);

    // 再敲字必须再来一次（旧实现彻底坏掉的路径）
    await typeText("Y");
    await flush(800);
    await vi.waitFor(() => expect(calls.put).toHaveLength(2));
    await vi.waitFor(() =>
      expect(screen.getByText(/已保存（修订 #3）/)).toBeInTheDocument(),
    );
    expect(calls.put.map((b) => b.base_revision)).toEqual([1, 2]);
  });

  it("没有净改动不发保存，不产生新修订", async () => {
    const calls = mount((url, init) => {
      const m = init.method ?? "GET";
      if (url.endsWith("/api/documents") && m === "POST")
        return { status: 201, json: META(1) };
      if (m === "PUT") return { status: 500, json: { detail: "不该保存" } };
      return { status: 200, json: PARSE_JSON() };
    });
    await createAndSettle(calls);
    const original = editor().value;
    await typeText("Z");
    await act(async () => {
      fireEvent.change(editor(), { target: { value: original } });
      await Promise.resolve();
    });
    await flush(900);
    expect(calls.put).toHaveLength(0);
    expect(screen.getByText(/已保存（修订 #1）/)).toBeInTheDocument();
  });

  it("保存未返回时继续敲字：单飞不并发，返回后基于新修订号再存", async () => {
    let releaseFirst: () => void = () => {};
    const firstGate = new Promise<void>((r) => (releaseFirst = r));
    let held = false;
    const putLog: { base: number; tail: string }[] = [];

    const calls = mount((url, init) => {
      const m = init.method ?? "GET";
      if (url.endsWith("/api/documents") && m === "POST")
        return { status: 201, json: META(1) };
      if (m === "PUT") {
        const body = JSON.parse(init.body as string);
        if (!held) {
          held = true;
          return firstGate.then(() => {
            putLog.push({ base: body.base_revision, tail: body.content });
            return { status: 200, json: DOC(2, body.content) };
          });
        }
        putLog.push({ base: body.base_revision, tail: body.content });
        return { status: 200, json: DOC(3, body.content) };
      }
      return { status: 200, json: PARSE_JSON() };
    });

    await createAndSettle(calls);

    await typeText("甲");
    await flush(800); // 触发第一个 PUT（被挂起）
    await vi.waitFor(() => expect(held).toBe(true));
    expect(screen.getByText(/保存中/)).toBeInTheDocument();

    // 在途期间继续敲字；推进时间也不应出现第二个并发 PUT
    await typeText("乙");
    await flush(2000);
    expect(calls.putStarted).toBe(1);
    expect(screen.getByText(/保存中/)).toBeInTheDocument();

    await act(async () => {
      releaseFirst();
      await Promise.resolve();
    });
    await vi.waitFor(() => expect(putLog).toHaveLength(1), { timeout: 100 });
    // 期间的新改动：基于新修订号 2 自动再存一次
    await flush(800);
    await vi.waitFor(() => expect(putLog).toHaveLength(2));
    expect(putLog[0].base).toBe(1);
    expect(putLog[0].tail.endsWith("甲")).toBe(true);
    expect(putLog[1].base).toBe(2);
    expect(putLog[1].tail.endsWith("甲乙")).toBe(true);
    await vi.waitFor(() =>
      expect(screen.getByText(/已保存（修订 #3）/)).toBeInTheDocument(),
    );
  });
});

describe("保存冲突（另一个标签页先存了）", () => {
  it("409 弹冲突框，写明双方修订号，编辑器锁定且本地内容不丢", async () => {
    const calls = mount((url, init) => {
      const m = init.method ?? "GET";
      if (url.endsWith("/api/documents") && m === "POST")
        return { status: 201, json: META(1) };
      if (m === "PUT") return { status: 409, json: CONFLICT() };
      return { status: 200, json: PARSE_JSON() };
    });
    await createAndSettle(calls);

    await typeText("甲页改动");
    await flush(800);
    await vi.waitFor(() => expect(calls.put).toHaveLength(1));

    const dialog = screen.getByRole("dialog");
    // 文案被 <b> 拆成多个文本节点，用整段 textContent 断言
    const desc = dialog.textContent ?? "";
    expect(desc).toMatch(/你基于第\s*1\s*修订在编辑/);
    expect(desc).toMatch(/服务器当前已是第\s*2\s*修订/);
    expect(editor()).toHaveAttribute("readonly");
    expect(screen.getByText(/保存冲突，等待处理/)).toBeInTheDocument();
    expect(editor().value.endsWith("甲页改动")).toBe(true);
  });

  it("采用服务器版本：内容/基准换成服务端，不再另存", async () => {
    const calls = mount((url, init) => {
      const m = init.method ?? "GET";
      if (url.endsWith("/api/documents") && m === "POST")
        return { status: 201, json: META(1) };
      if (m === "PUT") return { status: 409, json: CONFLICT() };
      return { status: 200, json: PARSE_JSON() };
    });
    await createAndSettle(calls);
    await typeText("本地");
    await flush(800);
    screen.getByRole("dialog");

    calls.put.length = 0;
    await clickButton(/采用服务器版本/);
    await flush(1000);
    expect(editor().value).toBe("标签页乙的内容");
    expect(screen.getByText(/已保存（修订 #2）/)).toBeInTheDocument();
    expect(editor()).not.toHaveAttribute("readonly");
    expect(calls.put).toHaveLength(0);
  });

  it("保留我的：以服务端当前修订号(2)另存为新修订(3)", async () => {
    const putLog: { base: number; content: string }[] = [];
    const calls = mount((url, init) => {
      const m = init.method ?? "GET";
      if (url.endsWith("/api/documents") && m === "POST")
        return { status: 201, json: META(1) };
      if (m === "PUT") {
        const body = JSON.parse(init.body as string);
        if (body.base_revision === 1)
          return { status: 409, json: CONFLICT() };
        putLog.push({ base: body.base_revision, content: body.content });
        return { status: 200, json: DOC(3, body.content) };
      }
      return { status: 200, json: PARSE_JSON() };
    });
    await createAndSettle(calls);
    await typeText("甲坚持的内容");
    await flush(800);
    screen.getByRole("dialog");

    await clickButton(/保留我的版本/);
    await vi.waitFor(() => expect(putLog).toHaveLength(1));
    expect(putLog[0].base).toBe(2);
    expect(putLog[0].content.endsWith("甲坚持的内容")).toBe(true);
    await vi.waitFor(() =>
      expect(screen.getByText(/已保存（修订 #3）/)).toBeInTheDocument(),
    );
    // 乙的第 2 修订没被覆盖：本地从未以 1 成功，最终基准前进到 3
    expect(screen.getByText("修订 #3")).toBeInTheDocument();
  });

  it("手动合并：合并文本以当前修订号(2)保存", async () => {
    const putLog: { base: number; content: string }[] = [];
    const calls = mount((url, init) => {
      const m = init.method ?? "GET";
      if (url.endsWith("/api/documents") && m === "POST")
        return { status: 201, json: META(1) };
      if (m === "PUT") {
        const body = JSON.parse(init.body as string);
        if (body.base_revision === 1)
          return { status: 409, json: { ...CONFLICT(), current_content: "BBB" } };
        putLog.push({ base: body.base_revision, content: body.content });
        return { status: 200, json: DOC(3, body.content) };
      }
      return { status: 200, json: PARSE_JSON() };
    });
    await createAndSettle(calls);
    await typeText("AAA");
    await flush(800);
    screen.getByRole("dialog");

    await clickButton(/手动合并后保存/);
    const mergeBox = within(screen.getByRole("dialog")).getByRole(
      "textbox",
    ) as HTMLTextAreaElement;
    await act(async () => {
      fireEvent.change(mergeBox, { target: { value: "AAA+BBB" } });
      await Promise.resolve();
    });
    await clickButton(/用合并结果保存/);
    await vi.waitFor(() => expect(putLog).toHaveLength(1));
    expect(putLog[0]).toEqual({ base: 2, content: "AAA+BBB" });
    await vi.waitFor(() =>
      expect(screen.getByText(/已保存（修订 #3）/)).toBeInTheDocument(),
    );
  });
});
