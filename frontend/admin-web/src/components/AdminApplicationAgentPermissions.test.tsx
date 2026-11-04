import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  applicationAgentAdmins: vi.fn(),
  applicationAgentAdminCandidates: vi.fn(),
  replaceApplicationAgentAdmins: vi.fn()
}));

vi.mock("../api/client", () => ({ adminApi: api }));

import { AdminApplicationAgentPermissions } from "./AdminApplicationAgentPermissions";


const currentMember = {
  userId: 11,
  userCode: "zhangyi",
  name: "示例用户乙",
  departmentName: "研发部",
  grantedByUserId: 1,
  grantedAt: "2026-07-28T10:00:00"
};


describe("AdminApplicationAgentPermissions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.applicationAgentAdmins.mockResolvedValue([currentMember]);
    api.applicationAgentAdminCandidates.mockImplementation(async (query: string) => {
      if (query === "李") {
        return [{
          userId: 12,
          userCode: "lisi",
          name: "李四",
          departmentName: "市场部",
          status: "active",
          isApplicationAgentAdmin: false
        }];
      }
      if (query === "王") {
        return [{
          userId: 13,
          userCode: "wangwu",
          name: "王五",
          departmentName: "销售部",
          status: "active",
          isApplicationAgentAdmin: false
        }];
      }
      return [];
    });
    api.replaceApplicationAgentAdmins.mockResolvedValue([
      currentMember,
      { ...currentMember, userId: 12, userCode: "lisi", name: "李四" },
      { ...currentMember, userId: 13, userCode: "wangwu", name: "王五" }
    ]);
  });

  it("selects multiple people by name or employee code and saves the full list", async () => {
    render(<AdminApplicationAgentPermissions />);
    const user = userEvent.setup();
    const search = await screen.findByRole("combobox", { name: "搜索人员" });

    expect(screen.getByText("示例用户乙（zhangyi）")).toBeVisible();

    await user.type(search, "李");
    await user.click(await screen.findByRole("option", { name: "李四（lisi）" }));
    await user.type(screen.getByRole("combobox", { name: "搜索人员" }), "王");
    await user.click(await screen.findByRole("option", { name: "王五（wangwu）" }));
    expect(screen.queryByText("市场部")).not.toBeInTheDocument();
    expect(screen.queryByText("销售部")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "保存管理员名单" }));

    await waitFor(() => expect(api.replaceApplicationAgentAdmins).toHaveBeenCalledWith([
      "zhangyi",
      "lisi",
      "wangwu"
    ]));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "已保存，共 3 名应用 Agent 管理员"
    );
  });

  it("removes a selected person and replaces the saved list", async () => {
    api.replaceApplicationAgentAdmins.mockResolvedValue([]);
    render(<AdminApplicationAgentPermissions />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "移除 示例用户乙（zhangyi）" }));
    await user.click(screen.getByRole("button", { name: "保存管理员名单" }));

    await waitFor(() => expect(api.replaceApplicationAgentAdmins).toHaveBeenCalledWith([]));
  });
});
