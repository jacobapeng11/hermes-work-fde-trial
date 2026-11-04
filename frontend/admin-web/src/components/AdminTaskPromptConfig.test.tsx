import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TaskPromptConfigState } from "@hermes-cloud/schemas";

const api = vi.hoisted(() => ({
  taskPromptConfig: vi.fn(),
  saveTaskPromptConfig: vi.fn()
}));

vi.mock("../api/client", () => ({ adminApi: api }));

import { AdminTaskPromptConfig } from "./AdminTaskPromptConfig";

const state: TaskPromptConfigState = {
  revision: 3,
  config: {
    tabs: [
      {
        label: "看板",
        description: "生成经营看板。",
        isDefault: true,
        showBoardGallery: true,
        topics: [
          {
            title: "销售分析",
            tasks: [{ summary: "销售趋势", detail: "生成销售趋势看板。" }]
          }
        ]
      },
      {
        label: "办公",
        description: "处理办公协作。",
        isDefault: false,
        showBoardGallery: false,
        topics: [
          {
            title: "文档创作",
            tasks: [{ summary: "写周报", detail: "生成项目周报。" }]
          }
        ]
      }
    ]
  }
};

describe("AdminTaskPromptConfig", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.taskPromptConfig.mockResolvedValue(state);
  });

  it("loads the organization task prompt configuration", async () => {
    render(<AdminTaskPromptConfig />);

    expect(await screen.findByRole("region", { name: "任务提示配置" })).toBeVisible();
    expect(await screen.findByDisplayValue("看板")).toBeVisible();
    expect(screen.getByDisplayValue("生成经营看板。")).toBeVisible();
    expect(screen.getByRole("tab", { name: "销售分析" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("v3")).toBeVisible();
  });

  it("edits topic task copy and publishes the complete document", async () => {
    api.saveTaskPromptConfig.mockResolvedValue({
      revision: 4,
      config: state.config
    });
    const user = userEvent.setup();
    render(<AdminTaskPromptConfig />);

    const summary = await screen.findByDisplayValue("销售趋势");
    await user.clear(summary);
    await user.type(summary, "生成销售趋势");
    await user.click(screen.getByRole("button", { name: /保存并发布/ }));

    await waitFor(() => expect(api.saveTaskPromptConfig).toHaveBeenCalledWith({
      revision: 3,
      config: expect.objectContaining({
        tabs: expect.arrayContaining([
          expect.objectContaining({
            label: "看板",
            topics: [
              expect.objectContaining({
                title: "销售分析",
                tasks: [{ summary: "生成销售趋势", detail: "生成销售趋势看板。" }]
              })
            ]
          })
        ])
      })
    }));
    expect(await screen.findByText("已发布任务提示配置 v4")).toBeVisible();
  });

  it("keeps the publish action visible and saves an edited tab description", async () => {
    api.saveTaskPromptConfig.mockResolvedValue({
      revision: 4,
      config: state.config
    });
    const user = userEvent.setup();
    render(<AdminTaskPromptConfig />);

    const description = await screen.findByDisplayValue("生成经营看板。");
    const saveButton = screen.getByRole("button", { name: /保存并发布/ });
    expect(saveButton).toBeVisible();
    await user.clear(description);
    await user.type(description, "生成经营分析和项目进度看板。");
    expect(saveButton).toBeVisible();
    await user.click(saveButton);

    await waitFor(() => expect(api.saveTaskPromptConfig).toHaveBeenCalledWith({
      revision: 3,
      config: expect.objectContaining({
        tabs: expect.arrayContaining([
          expect.objectContaining({
            label: "看板",
            description: "生成经营分析和项目进度看板。"
          })
        ])
      })
    }));
  });

  it("keeps exactly one default tab when the default is changed", async () => {
    const user = userEvent.setup();
    render(<AdminTaskPromptConfig />);

    await screen.findByDisplayValue("看板");
    const officeButton = screen.getByRole("button", { name: /^办公\s+1 个主题$/ });
    await user.click(officeButton);
    await user.click(screen.getByLabelText("默认Tab"));

    const kanbanButton = screen.getByRole("button", { name: /^看板\s+1 个主题$/ });
    const officeDefault = screen.getByLabelText("默认Tab");
    expect(officeDefault).toBeChecked();
    await user.click(kanbanButton);
    expect(within(kanbanButton).getByText("看板")).toBeVisible();
    expect(screen.getByLabelText("默认Tab")).not.toBeChecked();
  });

  it("maps an optimistic locking conflict to a readable notice", async () => {
    api.saveTaskPromptConfig.mockRejectedValue(
      new Error("任务提示配置已被其他管理员更新，请刷新后重试。")
    );
    const user = userEvent.setup();
    render(<AdminTaskPromptConfig />);

    await screen.findByDisplayValue("看板");
    await user.click(screen.getByRole("button", { name: /保存并发布/ }));

    expect(await screen.findByText(/已被其他管理员更新/)).toBeVisible();
  });
});
