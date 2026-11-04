import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AdminWeKnoraRerankConfigResponse } from "@hermes-cloud/schemas";

const api = vi.hoisted(() => ({
  weKnoraRerankConfig: vi.fn(),
  updateWeKnoraRerankConfig: vi.fn(),
}));

vi.mock("../api/client", () => ({ adminApi: api }));

import { AdminWeKnoraRerankConfig } from "./AdminWeKnoraRerankConfig";

const autoConfig: AdminWeKnoraRerankConfigResponse = {
  provider: "weknora",
  mode: "model",
  configuredModelId: "",
  effectiveModelId: "rerank-a",
  configSource: "auto",
  models: [
    { id: "rerank-a", name: "qwen3-rerank", displayName: "qwen3-rerank", source: "remote", status: "active" },
    { id: "rerank-b", name: "bge-rerank", displayName: "bge-rerank", source: "remote", status: "active" }
  ]
};

const savedConfig: AdminWeKnoraRerankConfigResponse = {
  ...autoConfig,
  mode: "model",
  configuredModelId: "rerank-b",
  effectiveModelId: "rerank-b",
  configSource: "tenant"
};

const disabledConfig: AdminWeKnoraRerankConfigResponse = {
  ...autoConfig,
  mode: "disabled",
  effectiveModelId: "",
  configSource: "disabled"
};

describe("AdminWeKnoraRerankConfig", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.weKnoraRerankConfig.mockResolvedValue(autoConfig);
    api.updateWeKnoraRerankConfig.mockResolvedValue(savedConfig);
  });

  it("shows automatic selection and prompts an explicit default", async () => {
    render(<AdminWeKnoraRerankConfig />);

    expect(await screen.findByRole("region", { name: "WeKnora Rerank 配置" })).toBeVisible();
    expect(screen.getAllByText("qwen3-rerank").length).toBeGreaterThan(0);
    expect(screen.getByText(/自动选择第一个可用 Rerank 模型/)).toBeVisible();
    expect(screen.getByText(/建议显式保存一个默认模型/)).toBeVisible();
    expect(api.weKnoraRerankConfig).toHaveBeenCalledTimes(1);
  });

  it("saves the selected workspace default", async () => {
    render(<AdminWeKnoraRerankConfig />);
    const user = userEvent.setup();

    await user.selectOptions(await screen.findByLabelText("Rerank 模型"), "rerank-b");
    await user.click(screen.getByRole("button", { name: /保存配置/ }));

    await waitFor(() => expect(api.updateWeKnoraRerankConfig).toHaveBeenCalledWith({
      mode: "model",
      rerankModelId: "rerank-b"
    }));
    expect(await screen.findByRole("status")).toHaveTextContent("Rerank 默认模型已保存");
    expect(screen.getByText("当前配置")).toBeVisible();
    expect(screen.getAllByText("bge-rerank").length).toBeGreaterThan(1);
  });

  it("saves and explains the disabled mode", async () => {
    api.updateWeKnoraRerankConfig.mockResolvedValue(disabledConfig);
    render(<AdminWeKnoraRerankConfig />);
    const user = userEvent.setup();

    await user.selectOptions(await screen.findByLabelText("Rerank 模型"), "disabled");
    await user.click(screen.getByRole("button", { name: /保存配置/ }));

    await waitFor(() => expect(api.updateWeKnoraRerankConfig).toHaveBeenCalledWith({
      mode: "disabled"
    }));
    expect(await screen.findByRole("status")).toHaveTextContent("已关闭 WeKnora 重排");
    expect(screen.getAllByText("不使用重排模型").length).toBeGreaterThan(0);
    expect(screen.getByText("已关闭")).toBeVisible();
    expect(screen.getByText(/企业知识中间平台不自动跟随/)).toBeVisible();
  });

  it("keeps the failure local when WeKnora config cannot be loaded", async () => {
    api.weKnoraRerankConfig.mockRejectedValue(new Error("知识库底座 Rerank 配置暂不可用，请稍后重试"));
    render(<AdminWeKnoraRerankConfig />);

    expect(await screen.findByText("知识库底座 Rerank 配置暂不可用，请稍后重试")).toBeVisible();
    expect(screen.queryByLabelText("Rerank 模型")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /刷新/ })).toBeEnabled();
  });

  it("still allows disabling when no usable rerank model exists", async () => {
    api.weKnoraRerankConfig.mockResolvedValue({
      ...autoConfig,
      effectiveModelId: "",
      configSource: "none",
      models: []
    });
    api.updateWeKnoraRerankConfig.mockResolvedValue({
      ...disabledConfig,
      models: []
    });
    render(<AdminWeKnoraRerankConfig />);
    const user = userEvent.setup();

    expect(await screen.findByText("无可用模型")).toBeVisible();
    await user.selectOptions(screen.getByLabelText("Rerank 模型"), "disabled");
    await user.click(screen.getByRole("button", { name: /保存配置/ }));

    expect(await screen.findByRole("status")).toHaveTextContent("已关闭 WeKnora 重排");
  });
});
