import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AdminPresenceSummary } from "./AdminPresenceSummary";

const summary = {
  webOnline: {
    count: 12,
    users: ["张三（10001）", "李四（10002）"]
  },
  desktopOnline: {
    count: 5,
    users: ["王五（10003）"]
  },
  dailyLogins: {
    count: 38,
    users: ["赵六（10004）", "张三（10001）"]
  },
  windowSeconds: 120,
  dailyLoginSince: "2026-10-13T00:00:00+08:00",
  observedAt: "2026-10-13T08:30:00Z",
  source: "platform-session-user-directory"
};

describe("AdminPresenceSummary", () => {
  it("shows web, desktop, and daily login user groups", () => {
    render(<AdminPresenceSummary summary={summary} />);
    const region = screen.getByRole("status", { name: "平台在线状态" });

    expect(region).toHaveAttribute("data-state", "ready");
    expect(screen.getByText("网页端在线用户数")).toBeVisible();
    expect(screen.getByText("桌面端在线用户数")).toBeVisible();
    expect(screen.getByText("当天登录用户数")).toBeVisible();
    expect(screen.getAllByText("12").length).toBeGreaterThan(0);
    expect(screen.getAllByText("5").length).toBeGreaterThan(0);
    expect(screen.getAllByText("38").length).toBeGreaterThan(0);
    expect(screen.getAllByText("近 2 分钟活跃 · 个人 Runtime 运行中")).toHaveLength(2);
    expect(screen.getAllByText("张三（10001）")).toHaveLength(2);
    expect(screen.getByText("李四（10002）")).toBeVisible();
    expect(screen.getByText("王五（10003）")).toBeVisible();
    expect(screen.getByText("赵六（10004）")).toBeVisible();
    expect(screen.getByText(/跨天在线计入/)).toBeVisible();
    expect(screen.getByText(/更新于 /)).toBeVisible();
  });

  it("keeps the usage panel usable when presence data is unavailable", () => {
    render(<AdminPresenceSummary summary={null} error="session store unavailable" />);
    const region = screen.getByRole("status", { name: "平台在线状态" });

    expect(region).toHaveAttribute("data-state", "error");
    expect(screen.getByText("session store unavailable")).toBeVisible();
    expect(screen.getAllByText("暂无用户")).toHaveLength(3);
  });
});
