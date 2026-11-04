import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { FileRail } from "./FileRail";

describe("FileRail", () => {
  const file = { fileId: 1, displayName: "迁移设计-v1.md", sourceType: "tool", sizeBytes: 32768 };

  it("renders a header close control and readable indexed file metadata", async () => {
    const onClose = vi.fn();
    render(<FileRail files={[file]} onClose={onClose} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "关闭当前任务文件" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.getByText("当前任务文件")).toBeVisible();
    expect(document.querySelector(".filesTitleIcon")).not.toBeInTheDocument();
    expect(screen.getByText("迁移设计-v1.md")).toBeVisible();
    expect(screen.getByText("Agent 生成 · 32 KB")).toBeVisible();
    expect(screen.getByRole("button", { name: "上传文件" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "文件操作 迁移设计-v1.md" })).toBeEnabled();
  });

  it("closes the file action menu when clicking outside the rail", async () => {
    const user = userEvent.setup();
    render(<><FileRail files={[file]} onClose={vi.fn()} /><button type="button">外部操作</button></>);

    await user.click(screen.getByRole("button", { name: "文件操作 迁移设计-v1.md" }));
    expect(screen.getByRole("button", { name: "下载" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "外部操作" }));

    expect(screen.queryByRole("button", { name: "下载" })).not.toBeInTheDocument();
  });

  it("closes the file action menu when pressing Escape", async () => {
    const user = userEvent.setup();
    render(<FileRail files={[file]} onClose={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "文件操作 迁移设计-v1.md" }));
    expect(screen.getByRole("button", { name: "下载" })).toBeVisible();

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("button", { name: "下载" })).not.toBeInTheDocument();
  });
});
