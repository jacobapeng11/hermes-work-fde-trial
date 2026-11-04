import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { PersonalKnowledgePermissionResponse } from "@hermes-cloud/schemas";
import { userApi } from "../../api/client";
import { PersonalKnowledgePermissionsDialog } from "./PersonalKnowledgePermissionsDialog";

const initial: PersonalKnowledgePermissionResponse = {
  download: { userIds: [], departmentIds: ["d1"], all: true },
  preview: { userIds: [12], departmentIds: [], all: false },
  retrieve: { userIds: [], departmentIds: [], all: false },
  normalizedTargets: [],
  grants: [
    { targetType: "department", targetId: "d1", name: "研发中心", departmentPath: ["研发中心"], role: "download" },
    { targetType: "user", targetId: 12, name: "示例用户乙", userCode: "zhangyi", departmentId: "d1", departmentPath: ["研发中心"], status: "active", role: "preview" }
  ]
};

const afterAdd: PersonalKnowledgePermissionResponse = {
  ...initial,
  retrieve: { userIds: [20], departmentIds: ["d2"], all: false },
  grants: [
    ...initial.grants,
    { targetType: "department", targetId: "d2", name: "市场部", departmentPath: ["市场部"], role: "retrieve" },
    { targetType: "user", targetId: 20, name: "李明", userCode: "liming", departmentId: "d2", departmentPath: ["市场部"], status: "active", role: "retrieve" }
  ]
};

const afterEdit: PersonalKnowledgePermissionResponse = {
  ...afterAdd,
  download: { userIds: [20], departmentIds: ["d1"], all: true },
  retrieve: { userIds: [], departmentIds: ["d2"], all: false },
  grants: afterAdd.grants.map((grant) => grant.targetType === "user" && grant.targetId === 20
    ? { ...grant, role: "download" as const }
    : grant)
};

const directory = [
  {
    nodeType: "department" as const,
    departmentId: "d1",
    name: "研发中心",
    children: [{ nodeType: "user" as const, loggedIn: true, userId: 12, name: "示例用户乙", userCode: "zhangyi", departmentId: "d1", status: "active" as const }]
  },
  {
    nodeType: "department" as const,
    departmentId: "d2",
    name: "市场部",
    children: [{ nodeType: "user" as const, loggedIn: true, userId: 20, name: "李明", userCode: "liming", departmentId: "d2", status: "active" as const }]
  }
];

describe("personal knowledge permission flow", () => {
  it("adds mixed targets and then edits one target through the complete dialog flow", async () => {
    vi.spyOn(userApi, "personalKnowledgePermissions").mockResolvedValue(initial);
    vi.spyOn(userApi, "personalKnowledgePermissionDirectory").mockResolvedValue(directory);
    const replace = vi.spyOn(userApi, "replacePersonalKnowledgePermissions")
      .mockResolvedValueOnce(afterAdd)
      .mockResolvedValueOnce(afterEdit);

    render(<PersonalKnowledgePermissionsDialog knowledgeBaseId={9} knowledgeBaseName="验收知识库" onClose={vi.fn()}/>);

    await screen.findByRole("tabpanel", { name: /已授权部门/ });
    fireEvent.click(screen.getByRole("button", { name: "添加权限" }));
    await screen.findByRole("dialog", { name: "添加权限" });
    fireEvent.click(screen.getByRole("checkbox", { name: "市场部" }));
    fireEvent.click(screen.getByRole("button", { name: "展开 市场部" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "李明（liming）" }));
    fireEvent.click(screen.getByRole("radio", { name: "仅引用" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(replace).toHaveBeenNthCalledWith(1, 9, {
      download: { userIds: [], departmentIds: ["d1"], all: true },
      preview: { userIds: [12], departmentIds: [], all: false },
      retrieve: { userIds: [20], departmentIds: ["d2"], all: false }
    }));
    expect(await screen.findByText("市场部")).toBeVisible();

    fireEvent.click(screen.getByRole("tab", { name: /已授权人员/ }));
    const userRow = screen.getByText("李明（liming）").closest("article");
    expect(userRow).not.toBeNull();
    fireEvent.click(within(userRow as HTMLElement).getByRole("button", { name: "修改" }));

    expect(await screen.findByRole("checkbox", { name: "李明（liming）" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "仅引用" })).toBeChecked();
    fireEvent.click(screen.getByRole("radio", { name: "可导出 / 预览 / 引用" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(replace).toHaveBeenNthCalledWith(2, 9, {
      download: { userIds: [20], departmentIds: ["d1"], all: true },
      preview: { userIds: [12], departmentIds: [], all: false },
      retrieve: { userIds: [], departmentIds: ["d2"], all: false }
    }));
    await waitFor(() => expect(within(userRow as HTMLElement).getByText("可导出 / 预览 / 引用")).toBeVisible());
  });
});
