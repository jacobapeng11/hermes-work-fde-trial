import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useAnchoredChatScroll } from "./useAnchoredChatScroll";

type TestMessage = {
  id: string;
  top: number;
  bottom: number;
};

function rect(top: number, bottom: number): DOMRect {
  return {
    x: 0,
    y: top,
    top,
    bottom,
    left: 0,
    right: 800,
    width: 800,
    height: bottom - top,
    toJSON: () => ({})
  };
}

function ScrollHarness() {
  const [messages, setMessages] = useState<TestMessage[]>([
    { id: "history", top: 0, bottom: 480 }
  ]);
  const [streaming, setStreaming] = useState(true);
  const scroll = useAnchoredChatScroll({
    sessionId: "session-a",
    view: "chat",
    messages,
    streaming
  });

  return <>
    <button type="button" onClick={() => {
      scroll.anchorTurn("user-new");
      setMessages([
        { id: "history", top: 0, bottom: 480 },
        { id: "user-new", top: 500, bottom: 540 },
        { id: "assistant-new", top: 560, bottom: 580 }
      ]);
    }}>发送</button>
    <button type="button" onClick={() => setMessages((items) => items.map((message) => (
      message.id === "assistant-new"
        ? { ...message, bottom: 650 }
        : message
    )))}>首次溢出</button>
    <button type="button" onClick={() => setMessages((items) => items.map((message) => (
      message.id === "assistant-new"
        ? { ...message, bottom: 670 }
        : message
    )))}>继续输出</button>
    <button type="button" onClick={() => setMessages((items) => items.map((message) => (
      message.id === "assistant-new"
        ? { ...message, bottom: 700 }
        : message
    )))}>回看时输出</button>
    <button type="button" onClick={() => setMessages((items) => items.map((message) => (
      message.id === "assistant-new"
        ? { ...message, bottom: 580 }
        : message
    )))}>内容收缩</button>
    <button type="button" onClick={() => setStreaming(false)}>结束运行</button>
    <div
      className="messageFeed"
      ref={(node) => {
        scroll.messageFeedRef.current = node;
        if (!node) return;
        if (!Object.getOwnPropertyDescriptor(node, "clientHeight")) {
          Object.defineProperties(node, {
            clientHeight: { configurable: true, value: 100 },
            scrollHeight: { configurable: true, value: 1_000 }
          });
        }
        node.getBoundingClientRect = () => rect(0, node.clientHeight);
      }}
      onScroll={scroll.onMessageFeedScroll}
    >
      <div className="messageFeedInner">
        {messages.map((message) => <article
          key={message.id}
          data-message-id={message.id}
          data-testid={message.id}
          ref={(node) => {
            if (!node) return;
            node.getBoundingClientRect = () => {
              const feed = scroll.messageFeedRef.current;
              const scrollTop = feed?.scrollTop ?? 0;
              return rect(message.top - scrollTop, message.bottom - scrollTop);
            };
          }}
        />)}
        {scroll.turnSpacerHeight > 0 && <div
          aria-hidden="true"
          className="turnAnchorSpacer"
          data-testid="turn-spacer"
          style={{ height: scroll.turnSpacerHeight }}
        />}
      </div>
    </div>
    {scroll.showReturnToLatest && <button
      type="button"
      aria-label="回到最新消息"
      onClick={scroll.resumeLatest}
    >回到最新</button>}
  </>;
}

function feed(): HTMLDivElement {
  const node = document.querySelector<HTMLDivElement>(".messageFeed");
  if (!node) throw new Error("message feed is missing");
  return node;
}

function userScroll(node: HTMLDivElement, scrollTop: number) {
  fireEvent.wheel(node, { deltaY: scrollTop - node.scrollTop });
  node.scrollTop = scrollTop;
  fireEvent.scroll(node);
}

function FollowLatestHarness() {
  const [messages, setMessages] = useState<string[]>([]);
  const scroll = useAnchoredChatScroll({
    sessionId: "session-follow",
    view: "chat",
    messages,
    streaming: true
  });

  return <>
    <button type="button" onClick={() => setMessages(["history"])}>加载历史</button>
    <button type="button" onClick={() => setMessages((items) => [...items, `message-${items.length}`])}>追加内容</button>
    <div
      className="messageFeed"
      ref={(node) => {
        scroll.messageFeedRef.current = node;
        if (!node || Object.getOwnPropertyDescriptor(node, "clientHeight")) return;
        Object.defineProperties(node, {
          clientHeight: { configurable: true, value: 100 },
          scrollHeight: {
            configurable: true,
            get: () => {
              const count = node.querySelectorAll("[data-message-id]").length;
              return count === 0 ? 0 : count * 200 + 400;
            }
          }
        });
        node.getBoundingClientRect = () => rect(0, node.clientHeight);
      }}
      onScroll={scroll.onMessageFeedScroll}
    >
      <div className="messageFeedInner">
        {messages.map((message) => <article key={message} data-message-id={message} />)}
      </div>
    </div>
    {scroll.showReturnToLatest && <button
      type="button"
      aria-label="回到最新消息"
      onClick={scroll.resumeLatest}
    >回到最新</button>}
  </>;
}

describe("useAnchoredChatScroll", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("anchors a newly submitted user message with a responsive tail spacer", () => {
    render(<ScrollHarness />);
    feed().scrollTop = 300;

    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(feed().scrollTop).toBe(488);
    expect(screen.getByTestId("turn-spacer")).toHaveStyle({ height: "100px" });
  });

  it("stays anchored for short content and follows only the actual overflow", () => {
    render(<ScrollHarness />);
    feed().scrollTop = 300;
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    expect(feed().scrollTop).toBe(488);

    fireEvent.click(screen.getByRole("button", { name: "首次溢出" }));
    expect(feed().scrollTop).toBe(550);

    fireEvent.click(screen.getByRole("button", { name: "继续输出" }));
    expect(feed().scrollTop).toBe(570);
  });

  it("keeps a completed short turn anchored at its user message", () => {
    render(<ScrollHarness />);
    feed().scrollTop = 300;
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    expect(screen.getByTestId("turn-spacer")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "结束运行" }));

    expect(feed().scrollTop).toBe(488);
    expect(screen.getByTestId("turn-spacer")).toBeInTheDocument();
  });

  it("shows the final output when a completed turn exceeds the viewport", () => {
    render(<ScrollHarness />);
    feed().scrollTop = 300;
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    fireEvent.click(screen.getByRole("button", { name: "首次溢出" }));

    fireEvent.click(screen.getByRole("button", { name: "结束运行" }));

    expect(feed().scrollTop).toBe(550);
    expect(screen.queryByTestId("turn-spacer")).not.toBeInTheDocument();
  });

  it("removes the spacer without moving a user reading history when the run finishes", () => {
    render(<ScrollHarness />);
    feed().scrollTop = 300;
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    fireEvent.click(screen.getByRole("button", { name: "首次溢出" }));
    act(() => {
      userScroll(feed(), 400);
    });

    fireEvent.click(screen.getByRole("button", { name: "结束运行" }));

    expect(screen.queryByTestId("turn-spacer")).not.toBeInTheDocument();
    expect(feed().scrollTop).toBe(400);
  });

  it("pauses while the user reads history and resumes from the latest control", () => {
    render(<ScrollHarness />);
    feed().scrollTop = 300;
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    fireEvent.click(screen.getByRole("button", { name: "首次溢出" }));
    expect(feed().scrollTop).toBe(550);

    act(() => {
      userScroll(feed(), 400);
    });
    expect(screen.getByRole("button", { name: "回到最新消息" })).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "回看时输出" }));
    expect(feed().scrollTop).toBe(400);

    fireEvent.click(screen.getByRole("button", { name: "回到最新消息" }));
    expect(feed().scrollTop).toBe(600);
    expect(screen.queryByRole("button", { name: "回到最新消息" })).not.toBeInTheDocument();
  });

  it("pauses on a one-pixel upward user scroll", () => {
    render(<ScrollHarness />);
    feed().scrollTop = 300;
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    fireEvent.click(screen.getByRole("button", { name: "首次溢出" }));
    expect(feed().scrollTop).toBe(550);

    act(() => {
      userScroll(feed(), 549);
      // Browsers can emit a follow-up scroll event at the same position while
      // the pointer/trackpad gesture settles. It must not resume following.
      fireEvent.scroll(feed());
    });

    expect(screen.getByRole("button", { name: "回到最新消息" })).toBeVisible();
  });

  it("pauses when dragging the scrollbar upward", () => {
    render(<ScrollHarness />);
    feed().scrollTop = 300;
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    fireEvent.click(screen.getByRole("button", { name: "首次溢出" }));
    expect(feed().scrollTop).toBe(550);

    act(() => {
      fireEvent.mouseDown(feed(), { clientY: 100 });
      fireEvent.mouseMove(window, { clientY: 99 });
      feed().scrollTop = 548;
      fireEvent.scroll(feed());
      fireEvent.mouseUp(window, { clientY: 99 });
    });

    expect(screen.getByRole("button", { name: "回到最新消息" })).toBeVisible();
  });

  it("recognizes a scrollbar drag whose mousedown target is outside the feed", () => {
    render(<ScrollHarness />);
    feed().scrollTop = 300;
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    fireEvent.click(screen.getByRole("button", { name: "首次溢出" }));
    expect(feed().scrollTop).toBe(550);

    act(() => {
      fireEvent.mouseDown(document.body, { clientX: 799, clientY: 50 });
      fireEvent.mouseMove(window, { clientY: 49 });
      feed().scrollTop = 548;
      fireEvent.scroll(feed());
      fireEvent.mouseUp(window, { clientY: 49 });
    });

    expect(screen.getByRole("button", { name: "回到最新消息" })).toBeVisible();
  });

  it("realigns a shrinking streamed turn instead of leaving the viewport in its tail spacer", () => {
    render(<ScrollHarness />);
    feed().scrollTop = 300;
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    fireEvent.click(screen.getByRole("button", { name: "首次溢出" }));
    expect(feed().scrollTop).toBe(550);

    fireEvent.click(screen.getByRole("button", { name: "内容收缩" }));

    expect(feed().scrollTop).toBe(488);
  });

  it("keeps following after a tool-group layout change moves scrollTop upward", () => {
    render(<ScrollHarness />);
    feed().scrollTop = 300;
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    fireEvent.click(screen.getByRole("button", { name: "首次溢出" }));
    expect(feed().scrollTop).toBe(550);

    // Tool groups can collapse after their status changes. Browser scroll
    // anchoring may then adjust scrollTop before ResizeObserver runs; this is
    // not the user choosing to inspect history.
    act(() => {
      feed().scrollTop = 520;
      fireEvent.scroll(feed());
    });
    fireEvent.click(screen.getByRole("button", { name: "内容收缩" }));
    fireEvent.click(screen.getByRole("button", { name: "首次溢出" }));

    expect(feed().scrollTop).toBe(550);
    expect(screen.queryByRole("button", { name: "回到最新消息" })).not.toBeInTheDocument();
  });

  it("resumes automatically after the user scrolls within 80px of the latest content", () => {
    render(<ScrollHarness />);
    feed().scrollTop = 300;
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    fireEvent.click(screen.getByRole("button", { name: "首次溢出" }));
    act(() => {
      userScroll(feed(), 400);
    });
    fireEvent.click(screen.getByRole("button", { name: "回看时输出" }));
    expect(screen.getByRole("button", { name: "回到最新消息" })).toBeVisible();

    act(() => {
      feed().scrollTop = 530;
      fireEvent.scroll(feed());
    });

    expect(screen.queryByRole("button", { name: "回到最新消息" })).not.toBeInTheDocument();
  });

  it("switches to the latest output when a resize makes the turn exceed the viewport", () => {
    let resize: ResizeObserverCallback | undefined;
    vi.stubGlobal("ResizeObserver", class {
      constructor(callback: ResizeObserverCallback) {
        resize = callback;
      }
      observe() {}
      disconnect() {}
      unobserve() {}
    });
    render(<ScrollHarness />);
    feed().scrollTop = 300;
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    expect(feed().scrollTop).toBe(488);

    Object.defineProperty(feed(), "clientHeight", {
      configurable: true,
      value: 60
    });
    act(() => resize?.([], {} as ResizeObserver));

    expect(feed().scrollTop).toBe(520);
    expect(screen.queryByTestId("turn-spacer")).not.toBeInTheDocument();
  });

  it("follows the first non-empty history after an empty session mount", () => {
    render(<FollowLatestHarness />);
    expect(feed().scrollTop).toBe(0);

    fireEvent.click(screen.getByRole("button", { name: "加载历史" }));

    expect(feed().scrollTop).toBe(600);
  });

  it("follows reconnect-style content without a turn anchor", () => {
    render(<FollowLatestHarness />);
    fireEvent.click(screen.getByRole("button", { name: "加载历史" }));

    fireEvent.click(screen.getByRole("button", { name: "追加内容" }));

    expect(feed().scrollTop).toBe(800);
  });

  it("preserves manual history and resumes following after reaching the bottom without an anchor", () => {
    render(<FollowLatestHarness />);
    fireEvent.click(screen.getByRole("button", { name: "加载历史" }));
    act(() => {
      userScroll(feed(), 100);
    });
    expect(screen.getByRole("button", { name: "回到最新消息" })).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "追加内容" }));
    expect(feed().scrollTop).toBe(100);

    act(() => {
      feed().scrollTop = 720;
      fireEvent.scroll(feed());
    });
    expect(screen.queryByRole("button", { name: "回到最新消息" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "追加内容" }));
    expect(feed().scrollTop).toBe(1_000);
  });
});
