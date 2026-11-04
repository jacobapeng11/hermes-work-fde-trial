import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { PersonalKnowledgePermissionResponse } from "@hermes-cloud/schemas";
import { userApi } from "../../api/client";
import { PersonalKnowledgePermissionEditorDialog } from "./PersonalKnowledgePermissionEditorDialog";

const permissions: PersonalKnowledgePermissionResponse = {
  download: { userIds: [], departmentIds: ["d1"], all: true },
  preview: { userIds: [12], departmentIds: [], all: false },
  retrieve: { userIds: [], departmentIds: [], all: false },
  normalizedTargets: [],
  grants: [
    { targetType: "department", targetId: "d1", name: "研发中心", departmentPath: ["研发中心"], role: "download" },
    { targetType: "user", targetId: 12, name: "示例用户乙", userCode: "zhangyi", departmentId: "d1", departmentPath: ["研发中心"], status: "active", role: "preview" }
  ]
};

const directory = [{
  nodeType: "department" as const,
  departmentId: "d1",
  name: "研发中心",
  children: [{ nodeType: "user" as const, loggedIn: true, userId: 12, name: "示例用户乙", userCode: "zhangyi", departmentId: "d1", status: "active" as const }]
}, {
  nodeType: "department" as const,
  departmentId: "m1",
  name: "市场部",
  children: [{
    nodeType: "department" as const,
    departmentId: "m2",
    name: "增长组",
    parentId: "m1",
    children: [{ nodeType: "user" as const, loggedIn: true, userId: 14, name: "王丽", userCode: "wangli", departmentId: "m2", status: "active" as const }]
  }]
}];

describe("PersonalKnowledgePermissionEditorDialog", () => {
  it("requires a new common role for mixed selections and preserves hidden all", async () => {
    vi.spyOn(userApi, "personalKnowledgePermissionDirectory").mockResolvedValue(directory);
    const replace = vi.spyOn(userApi, "replacePersonalKnowledgePermissions").mockResolvedValue(permissions);
    const onSaved = vi.fn();
    render(<PersonalKnowledgePermissionEditorDialog knowledgeBaseId={9} knowledgeBaseName="测试库" permissions={permissions} mode="add" onSaved={onSaved} onClose={vi.fn()}/>);

    // antd Modal 打开动画（rc-motion）在 jsdom 中需等待推进后再断言可见性。
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "研发中心" })).toBeVisible());
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "示例用户乙（zhangyi）" })).toBeVisible());
    expect(screen.queryByText("增长组")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "研发中心" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "示例用户乙（zhangyi）" }));
    expect(screen.getByText("所选对象当前权限不一致，请重新选择统一权限。")).toBeVisible();
    expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();

    fireEvent.click(screen.getByRole("radio", { name: "仅引用" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith(9, expect.objectContaining({
      download: expect.objectContaining({ all: true }),
      retrieve: { userIds: [12], departmentIds: ["d1"], all: false }
    })));
    expect(onSaved).toHaveBeenCalledWith(permissions);
  });

  it("toggles tree users without residue or duplicates and saves the selection", async () => {
    vi.spyOn(userApi, "personalKnowledgePermissionDirectory").mockResolvedValue(directory);
    const replace = vi.spyOn(userApi, "replacePersonalKnowledgePermissions").mockResolvedValue(permissions);
    render(<PersonalKnowledgePermissionEditorDialog knowledgeBaseId={9} knowledgeBaseName="测试库" permissions={permissions} mode="add" onSaved={vi.fn()} onClose={vi.fn()}/>);

    fireEvent.click(await screen.findByRole("button", { name: "展开 市场部" }));
    fireEvent.click(screen.getByRole("button", { name: "展开 增长组" }));
    const userCheckbox = screen.getByRole("checkbox", { name: "王丽（wangli）" });
    fireEvent.click(userCheckbox);
    await waitFor(() => expect(screen.getByText(/已选 1/)).toBeVisible());
    // 回归：树内取消后已选数必须归零（此前 selectedUsers 拼接导致取消不掉、再选重复）。
    fireEvent.click(userCheckbox);
    await waitFor(() => expect(screen.getByText(/已选 0/)).toBeVisible());
    fireEvent.click(userCheckbox);
    fireEvent.click(screen.getByRole("checkbox", { name: "研发中心" }));
    await waitFor(() => expect(screen.getByText(/已选 2/)).toBeVisible());

    fireEvent.click(screen.getByRole("radio", { name: "仅引用" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith(9, expect.objectContaining({
      retrieve: { userIds: [14], departmentIds: ["d1"], all: false }
    })));
  });

  it("locates an existing target and keeps the draft after a save failure", async () => {
    vi.spyOn(userApi, "personalKnowledgePermissionDirectory").mockResolvedValue(directory);
    vi.spyOn(userApi, "replacePersonalKnowledgePermissions").mockRejectedValue(new Error("保存冲突"));
    render(<PersonalKnowledgePermissionEditorDialog knowledgeBaseId={9} knowledgeBaseName="测试库" permissions={permissions} mode="edit" initialGrant={permissions.grants[1]} onSaved={vi.fn()} onClose={vi.fn()}/>);

    expect(await screen.findByRole("checkbox", { name: "示例用户乙（zhangyi）" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "仅预览和引用" })).toBeChecked();
    fireEvent.click(screen.getByRole("radio", { name: "仅引用" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("保存冲突");
    expect(screen.getByRole("checkbox", { name: "示例用户乙（zhangyi）" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "仅引用" })).toBeChecked();
  });
});
