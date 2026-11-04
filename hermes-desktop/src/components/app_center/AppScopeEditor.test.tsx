import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ExpertShareScope, OrganizationDirectoryNode } from "@hermes-cloud/schemas";
import { userApi } from "../../api/client";
import { AppScopeEditor } from "./AppScopeEditor";

vi.mock("../../api/client", () => ({
  userApi: {
    provisionDirectoryUsers: vi.fn()
  }
}));

const directory: OrganizationDirectoryNode[] = [
  {
    nodeType: "organization",
    organizationId: 49,
    name: "示例业务技术",
    children: [
      {
        nodeType: "department",
        departmentId: "49",
        name: "总经办",
        children: [
          {
            nodeType: "user",
            userId: 1,
            name: "张三",
            userCode: "z001",
            loggedIn: true,
            children: []
          },
          {
            nodeType: "user",
            name: "赵六",
            userCode: "z006",
            loggedIn: false,
            children: []
          }
        ]
      }
    ]
  }
];

function renderEditor(scope: ExpertShareScope = { organization: false, departmentIds: [], userIds: [] }) {
  const onChange = vi.fn();
  render(<AppScopeEditor
    scope={scope}
    onChange={onChange}
    directory={directory}
    loading={false}
    error={null}
    onRetry={() => undefined}
  />);
  return onChange;
}

async function openTree() {
  fireEvent.click(screen.getByRole("button", { name: "添加可见对象" }));
  const modal = await screen.findByRole("dialog", { name: "添加可见对象" });
  // 默认只展开组织根；先手动展开部门，用户节点才可见。
  const departmentSwitcher = within(modal).getByText("总经办")
    .closest(".ant-tree-treenode")
    ?.querySelector(".ant-tree-switcher");
  if (departmentSwitcher) fireEvent.click(departmentSwitcher);
  return modal;
}

describe("AppScopeEditor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("勾选已激活用户并确认后写回 scope", async () => {
    const onChange = renderEditor();
    const modal = await openTree();

    const zhang = within(modal).getByText("张三（z001）");
    fireEvent.click(zhang);
    await waitFor(() =>
      expect(zhang.closest(".ant-tree-treenode")?.querySelector(".ant-tree-checkbox-checked")).not.toBeNull()
    );
    fireEvent.click(within(modal).getByRole("button", { name: "确认" }));

    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ organization: false, departmentIds: [], userIds: [1] }));
  });

  it("勾选未激活用户：立即反馈勾选，provision 成功后以真实 id 写回", async () => {
    vi.mocked(userApi.provisionDirectoryUsers).mockResolvedValue([
      { id: 77, name: "赵六", userCode: "z006" }
    ]);
    const onChange = renderEditor();
    const modal = await openTree();

    const zhao = within(modal).getByText("赵六（z006）");
    fireEvent.click(zhao);

    // 乐观勾选：点击后树节点立即处于勾选态（不因 provision 异步而回弹）
    await waitFor(() =>
      expect(zhao.closest(".ant-tree-treenode")?.querySelector(".ant-tree-checkbox-checked")).not.toBeNull()
    );
    await waitFor(() => expect(userApi.provisionDirectoryUsers).toHaveBeenCalledWith(["z006"]));

    fireEvent.click(within(modal).getByRole("button", { name: "确认" }));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ organization: false, departmentIds: [], userIds: [77] }));
  });

  it("provision 失败：勾选回退并提示错误", async () => {
    vi.mocked(userApi.provisionDirectoryUsers).mockRejectedValue(new Error("后端不可用"));
    const onChange = renderEditor();
    const modal = await openTree();

    const zhao = within(modal).getByText("赵六（z006）");
    fireEvent.click(zhao);

    await waitFor(() => expect(screen.getByText("后端不可用")).toBeVisible());
    await waitFor(() =>
      expect(zhao.closest(".ant-tree-treenode")?.querySelector(".ant-tree-checkbox-checked")).toBeNull()
    );

    fireEvent.click(within(modal).getByRole("button", { name: "确认" }));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ organization: false, departmentIds: [], userIds: [] }));
  });
});
