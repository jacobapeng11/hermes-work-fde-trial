import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ProfileMcpAssetItem } from "@hermes-cloud/schemas";
import { McpWorkspace } from "./McpWorkspace";

const asset = (overrides: Partial<ProfileMcpAssetItem> = {}) => ({
  assetId: 9,
  slug: "ticket-system",
  name: "工单系统",
  description: "连接企业工单系统",
  status: "published",
  transport: "streamable_http",
  desired: false,
  applied: false,
  convergenceStatus: "applied",
  ...overrides
});

describe("McpWorkspace", () => {
  it("uses the same catalog and detail interaction for application bindings", async () => {
    const onAction = vi.fn().mockResolvedValue(undefined);
    const item = asset();
    render(
      <McpWorkspace
        items={[item]}
        onAction={onAction}
        loadDetail={async () => ({
          asset: item,
          detail: {
            transport: "streamable_http",
            endpoint: "https://mcp.example.test",
            permissionHint: "按调用用户权限访问"
          }
        })}
      />
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "安装" }));
    expect(onAction).toHaveBeenCalledWith(item, "install");
    await user.click(screen.getByRole("button", { name: /工单系统/ }));
    const dialog = screen.getByRole("dialog", { name: "工单系统 详情" });
    // antd Modal 外壳在 jsdom 中 rc-motion appear 需异步完成（prepare 阶段 opacity:0），
    // 与 ExpertCatalogPanels.test 同款 waitFor 等待模式。
    await waitFor(() => expect(within(dialog).getByText("https://mcp.example.test")).toBeVisible());
  });

  it("shows desired/applied convergence failures without old operation aliases", () => {
    render(
      <McpWorkspace
        items={[asset({
          desired: true,
          applied: false,
          convergenceStatus: "failed",
          errorMessage: "reload failed",
        })]}
        onAction={vi.fn().mockResolvedValue(undefined)}
        loadDetail={vi.fn()}
      />
    );

    expect(screen.getByRole("button", {
      name: "删除"
    })).toBeEnabled();
    expect(screen.getByText(/配置更新失败：reload failed/)).toBeVisible();
  });

  it("shows an immediate spinner and prevents duplicate install submissions", () => {
    render(
      <McpWorkspace
        items={[asset()]}
        activeOperations={{ 9: "install" }}
        onAction={vi.fn().mockResolvedValue(undefined)}
        loadDetail={vi.fn()}
      />
    );

    expect(screen.getByRole("button", { name: "安装中" })).toBeDisabled();
    expect(document.querySelector(".uploadSpinner")).toBeInTheDocument();
  });

  it("keeps the install spinner visible while desired is not yet applied", () => {
    render(
      <McpWorkspace
        items={[asset({
          desired: true,
          applied: false,
          convergenceStatus: "updating",
        })]}
        onAction={vi.fn().mockResolvedValue(undefined)}
        loadDetail={vi.fn()}
      />
    );

    expect(screen.getByRole("button", { name: "安装中" })).toBeDisabled();
    expect(document.querySelector(".uploadSpinner")).toBeInTheDocument();
  });
});
