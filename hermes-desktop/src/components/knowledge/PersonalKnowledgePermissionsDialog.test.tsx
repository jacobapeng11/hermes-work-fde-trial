import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { PersonalKnowledgePermissionResponse } from "@hermes-cloud/schemas";
import { userApi } from "../../api/client";
import { PersonalKnowledgePermissionsDialog } from "./PersonalKnowledgePermissionsDialog";

const permissions: PersonalKnowledgePermissionResponse = {
  download: { userIds: [], departmentIds: ["d1", "d2"], all: false },
  preview: { userIds: [12], departmentIds: [], all: false },
  retrieve: { userIds: [99], departmentIds: [], all: false },
  normalizedTargets: [],
  grants: [
    { targetType: "department", targetId: "d1", name: "研发中心", departmentPath: ["研发中心"], role: "download" },
    { targetType: "department", targetId: "d2", name: "市场部", departmentPath: ["市场部"], role: "download" },
    { targetType: "user", targetId: 12, name: "示例用户乙", userCode: "zhangyi", departmentId: "d1", departmentPath: ["研发中心"], status: "active", role: "preview" },
    { targetType: "user", targetId: 99, name: null, userCode: null, departmentPath: [], status: "missing", role: "retrieve" }
  ]
};

describe("PersonalKnowledgePermissionsDialog", () => {
  it("separates authorized departments and users and searches only the active category", async () => {
    vi.spyOn(userApi, "personalKnowledgePermissions").mockResolvedValue(permissions);
    render(<PersonalKnowledgePermissionsDialog knowledgeBaseId={9} knowledgeBaseName="测试库" onClose={vi.fn()}/>);

    await waitFor(() => expect(screen.getByRole("tabpanel", { name: /已授权部门/ })).toBeVisible());
    await waitFor(() => expect(screen.getByText("研发中心")).toBeVisible());
    await waitFor(() => expect(screen.getByText("市场部")).toBeVisible());
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索已授权部门" }), { target: { value: "研发" } });
    await waitFor(() => expect(screen.getByText("研发中心")).toBeVisible());
    await waitFor(() => expect(screen.queryByText("市场部")).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole("tab", { name: /已授权人员/ }));
    await waitFor(() => expect(screen.getByText("示例用户乙（zhangyi）")).toBeVisible());
    await waitFor(() => expect(screen.getByText("已失效用户（99）")).toBeVisible());
    expect(screen.queryByDisplayValue("研发")).not.toBeInTheDocument();
  });

  it("removes only after confirmation and replaces the list with the server response", async () => {
    vi.spyOn(userApi, "personalKnowledgePermissions").mockResolvedValue(permissions);
    const updated: PersonalKnowledgePermissionResponse = {
      ...permissions,
      download: { ...permissions.download, departmentIds: ["d2"] },
      grants: permissions.grants.filter((grant) => !(grant.targetType === "department" && grant.targetId === "d1"))
    };
    const replace = vi.spyOn(userApi, "replacePersonalKnowledgePermissions").mockResolvedValue(updated);
    render(<PersonalKnowledgePermissionsDialog knowledgeBaseId={9} knowledgeBaseName="测试库" onClose={vi.fn()}/>);

    await screen.findByText("研发中心");
    fireEvent.click(screen.getAllByRole("button", { name: "移除" })[0]);
    await waitFor(() => expect(screen.getByRole("dialog", { name: /确认移除/ })).toBeVisible());
    fireEvent.click(screen.getByRole("button", { name: "确认移除" }));

    await waitFor(() => expect(replace).toHaveBeenCalledWith(9, expect.objectContaining({
      download: { userIds: [], departmentIds: ["d2"], all: false }
    })));
    await waitFor(() => expect(screen.queryByText("研发中心")).not.toBeInTheDocument());
    expect(screen.getByText("市场部")).toBeVisible();
  });

  it("keeps the grant and confirmation draft when removal fails", async () => {
    vi.spyOn(userApi, "personalKnowledgePermissions").mockResolvedValue(permissions);
    vi.spyOn(userApi, "replacePersonalKnowledgePermissions").mockRejectedValue(new Error("保存冲突"));
    render(<PersonalKnowledgePermissionsDialog knowledgeBaseId={9} knowledgeBaseName="测试库" onClose={vi.fn()}/>);

    await screen.findByText("研发中心");
    fireEvent.click(screen.getAllByRole("button", { name: "移除" })[0]);
    fireEvent.click(screen.getByRole("button", { name: "确认移除" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("保存冲突");
    await waitFor(() => expect(screen.getByRole("dialog", { name: /确认移除/ })).toBeVisible());
    expect(screen.getAllByText("研发中心").length).toBeGreaterThan(0);
  });
});
