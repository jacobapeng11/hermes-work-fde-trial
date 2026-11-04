import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { OrganizationDirectoryNode } from "@hermes-cloud/schemas";
import { userApi } from "../../api/client";
import { ExpertVisibilityEditor } from "./ExpertVisibilityEditor";

const directory: OrganizationDirectoryNode[] = [{
  nodeType: "organization",
  organizationId: 1,
  name: "示例业务技术",
  children: [{
    nodeType: "department",
    departmentId: "root",
    name: "总部",
    children: [{
      nodeType: "department",
      departmentId: "dev",
      parentId: "root",
      name: "研发中心",
      children: [{
        nodeType: "user",
        loggedIn: true,
        userId: 990095,
        name: "示例用户甲",
        userCode: "10001",
        departmentId: "dev",
        status: "active"
      }]
    }]
  }]
}, {
  nodeType: "department",
  departmentId: "product",
  name: "产品部",
  children: [{
    nodeType: "department",
    departmentId: "design",
    parentId: "product",
    name: "设计组",
    children: [{
      nodeType: "user",
      loggedIn: true,
      userId: 20,
      name: "李明",
      userCode: "liming",
      departmentId: "design",
      status: "active"
    }]
  }]
}];

// antd Tree 契约（同看板区 AppScopeEditor.test）：节点勾选点击 title 文本触发，
// 展开点击行内 .ant-tree-switcher，勾选态看 .ant-tree-checkbox-checked。
async function openTree(modify = false) {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: modify ? "修改" : "添加可见对象" }));
  const modal = await screen.findByRole("dialog", { name: "添加可见对象" });
  await waitFor(() => expect(modal).toBeVisible());
  return { user, modal };
}

function nodeRow(modal: HTMLElement, name: string) {
  const title = within(modal).getByText(name);
  return title.closest(".ant-tree-treenode") ?? title.closest(".ant-list-item");
}

function expandNode(modal: HTMLElement, name: string) {
  const row = nodeRow(modal, name);
  const switcher = row?.querySelector(".ant-tree-switcher");
  if (switcher) fireEvent.click(switcher as HTMLElement);
}

function clickCheckbox(modal: HTMLElement, name: string) {
  const row = nodeRow(modal, name);
  fireEvent.click(row?.querySelector(".ant-tree-checkbox") as HTMLElement);
}

function expectChecked(modal: HTMLElement, name: string, checked: boolean) {
  const row = nodeRow(modal, name);
  expect(row?.querySelector(".ant-tree-checkbox-checked") != null).toBe(checked);
}

describe("ExpertVisibilityEditor", () => {
  it("selects company roots and departments in parallel company forest", async () => {
    const companyDirectory: OrganizationDirectoryNode[] = [{
      nodeType: "organization",
      organizationId: 1,
      name: "示例业务技术",
      children: [
        {
          nodeType: "department",
          departmentId: "49",
          name: "示例公司",
          children: [{
            nodeType: "department",
            departmentId: "sxf-hq",
            name: "示例总部",
            children: []
          }]
        },
        {
          nodeType: "department",
          departmentId: "51",
          name: "示例业务公司",
          children: [{
            nodeType: "department",
            departmentId: "xrx-hq",
            name: "示例业务总部",
            children: []
          }]
        }
      ]
    }];
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ExpertVisibilityEditor
      scope={{ organization: false, departmentIds: [], userIds: [] }}
      onChange={onChange}
      directory={companyDirectory}
      loading={false}
      error={null}
      onRetry={vi.fn()}
    />);

    await user.click(screen.getByRole("button", { name: "添加可见对象" }));
    const dialog = await screen.findByRole("dialog", { name: "添加可见对象" });
    await waitFor(() => expect(dialog).toBeVisible());
    expandNode(dialog, "示例业务技术");
    await user.click(within(dialog).getByText("示例公司"));
    await user.click(within(dialog).getByText("示例业务公司"));
    await user.click(within(dialog).getByRole("button", { name: "确认" }));

    await waitFor(() => expect(onChange).toHaveBeenCalledWith({
      organization: false,
      departmentIds: ["49", "51"],
      userIds: []
    }));
  });

  it("selects the unique organization root through the unified popup", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ExpertVisibilityEditor
      scope={{ organization: false, departmentIds: [], userIds: [] }}
      onChange={onChange}
      directory={directory}
      loading={false}
      error={null}
      onRetry={vi.fn()}
    />);

    await user.click(screen.getByRole("button", { name: "添加可见对象" }));
    const dialog = await screen.findByRole("dialog", { name: "添加可见对象" });
    await waitFor(() => expect(dialog).toBeVisible());
    expect(within(dialog).getByText("选择部门或人员")).toBeVisible();
    expect(within(dialog).queryByText("总部")).not.toBeInTheDocument();
    await user.click(within(dialog).getByText("示例业务技术"));
    await user.click(within(dialog).getByRole("button", { name: "确认" }));

    await waitFor(() => expect(onChange).toHaveBeenCalledWith({
      organization: true,
      departmentIds: [],
      userIds: []
    }));
  });

  it("keeps the original scope when popup edits are cancelled", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ExpertVisibilityEditor
      scope={{ organization: false, departmentIds: ["dev"], userIds: [] }}
      onChange={onChange}
      directory={directory}
      loading={false}
      error={null}
      onRetry={vi.fn()}
    />);

    await user.click(screen.getByRole("button", { name: "添加可见对象" }));
    const dialog = await screen.findByRole("dialog", { name: "添加可见对象" });
    await waitFor(() => expect(dialog).toBeVisible());
    expect(within(dialog).getByText("研发中心")).toBeVisible();
    expect(within(dialog).queryByText("示例用户甲（10001）")).not.toBeInTheDocument();
    // antd Tree：dev 已勾选时子节点显示联动勾选；总部（indeterminate）点击为取消语义。
    expandNode(dialog, "研发中心");
    expectChecked(dialog, "示例用户甲（10001）", true);
    await clickCheckbox(dialog, "总部");
    expectChecked(dialog, "示例用户甲（10001）", false);
    await user.click(within(dialog).getByRole("button", { name: "取消" }));

    expect(onChange).not.toHaveBeenCalled();
    // antd Modal 关闭有 leave 动画，先等弹窗卸载再查外层列表。
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "添加可见对象" })).not.toBeInTheDocument());
    expect(within(screen.getByRole("tabpanel")).getByText("研发中心")).toBeVisible();
  });

  it("opens only the ancestors required to reveal a selected person", async () => {
    const user = userEvent.setup();
    render(<ExpertVisibilityEditor
      scope={{ organization: false, departmentIds: [], userIds: [990095] }}
      onChange={vi.fn()}
      directory={directory}
      loading={false}
      error={null}
      onRetry={vi.fn()}
    />);

    await user.click(screen.getByRole("button", { name: "添加可见对象" }));
    const dialog = await screen.findByRole("dialog", { name: "添加可见对象" });
    await waitFor(() => expect(dialog).toBeVisible());

    // 默认只展开选中人员的祖先链：总部/研发中心可见、示例用户甲已勾选，设计组不可见。
    expect(within(dialog).getByText("总部")).toBeVisible();
    expect(within(dialog).getByText("研发中心")).toBeVisible();
    expectChecked(dialog, "示例用户甲（10001）", true);
    expect(within(dialog).queryByText("设计组")).not.toBeInTheDocument();
  });

  it("provisions all inactive users under a department in bounded batches", async () => {
    const unprovisionedDirectory: OrganizationDirectoryNode[] = [{
      nodeType: "organization",
      organizationId: 1,
      name: "示例业务技术",
      children: [{
        nodeType: "department",
        departmentId: "sales",
        name: "销售部",
        children: Array.from({ length: 201 }, (_, index) => ({
          nodeType: "user" as const,
          loggedIn: false,
          userId: null,
          name: `未激活人员${index}`,
          userCode: `u${index}`,
          departmentId: "sales",
          status: "active" as const
        }))
      }]
    }];
    const provisionSpy = vi.spyOn(userApi, "provisionDirectoryUsers").mockImplementation(async (userCodes) =>
      userCodes.map((userCode) => ({
        id: 1000 + Number(userCode.slice(1)),
        name: `未激活人员${userCode.slice(1)}`,
        userCode
      }))
    );
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ExpertVisibilityEditor
      scope={{ organization: false, departmentIds: [], userIds: [] }}
      onChange={onChange}
      directory={unprovisionedDirectory}
      loading={false}
      error={null}
      onRetry={vi.fn()}
    />);

    await user.click(screen.getByRole("button", { name: "添加可见对象" }));
    const dialog = await screen.findByRole("dialog", { name: "添加可见对象" });
    await waitFor(() => expect(dialog).toBeVisible());
    expandNode(dialog, "示例业务技术");
    clickCheckbox(dialog, "销售部");
    await waitFor(() => expect(provisionSpy).toHaveBeenCalledTimes(3));
    expect(provisionSpy.mock.calls.map(([userCodes]) => userCodes.length)).toEqual([100, 100, 1]);
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "确认" }).className).not.toContain("ant-btn-loading"));
    await user.click(within(dialog).getByRole("button", { name: "确认" }));

    await waitFor(() => expect(onChange).toHaveBeenCalledWith({
      organization: false,
      departmentIds: ["sales"],
      userIds: []
    }));
    expect(provisionSpy.mock.calls.every(([userCodes]) => userCodes.length <= 100)).toBe(true);
  });

  it("provisions only the unprovisioned user clicked explicitly", async () => {
    const unprovisionedDirectory: OrganizationDirectoryNode[] = [{
      nodeType: "organization",
      organizationId: 1,
      name: "示例业务技术",
      children: [{
        nodeType: "department",
        departmentId: "sales",
        name: "销售部",
        children: [
          { nodeType: "user", loggedIn: false, userId: null, name: "王未激活", userCode: "u001", departmentId: "sales", status: "active" },
          { nodeType: "user", loggedIn: false, userId: null, name: "赵未激活", userCode: "u002", departmentId: "sales", status: "active" }
        ]
      }]
    }];
    const provisionSpy = vi.spyOn(userApi, "provisionDirectoryUsers").mockResolvedValue([
      { id: 301, name: "王未激活", userCode: "u001" }
    ]);
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ExpertVisibilityEditor
      scope={{ organization: false, departmentIds: [], userIds: [] }}
      onChange={onChange}
      directory={unprovisionedDirectory}
      loading={false}
      error={null}
      onRetry={vi.fn()}
    />);

    await user.click(screen.getByRole("button", { name: "添加可见对象" }));
    const dialog = await screen.findByRole("dialog", { name: "添加可见对象" });
    await waitFor(() => expect(dialog).toBeVisible());
    expandNode(dialog, "示例业务技术");
    await waitFor(() => expect(within(dialog).getByText("销售部")).toBeVisible());
    expandNode(dialog, "销售部");
    await waitFor(() => expect(within(dialog).getByText("王未激活（u001）")).toBeVisible());
    clickCheckbox(dialog, "王未激活（u001）");

    await waitFor(() => expect(provisionSpy).toHaveBeenCalledWith(["u001"]));
    await user.click(within(dialog).getByRole("button", { name: "确认" }));

    await waitFor(() => expect(onChange).toHaveBeenCalledWith({
      organization: false,
      departmentIds: [],
      userIds: [301]
    }));
  });

  it("keeps department selection silent when background provisioning fails", async () => {
    const unprovisionedDirectory: OrganizationDirectoryNode[] = [{
      nodeType: "organization",
      organizationId: 1,
      name: "示例业务技术",
      children: [{
        nodeType: "department",
        departmentId: "sales",
        name: "销售部",
        children: [
          { nodeType: "user", loggedIn: false, userId: null, name: "王未激活", userCode: "u001", departmentId: "sales", status: "active" }
        ]
      }]
    }];
    vi.spyOn(userApi, "provisionDirectoryUsers").mockRejectedValue(new Error("请求参数与服务版本不一致，请刷新页面或稍后重试"));
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ExpertVisibilityEditor
      scope={{ organization: false, departmentIds: [], userIds: [] }}
      onChange={onChange}
      directory={unprovisionedDirectory}
      loading={false}
      error={null}
      onRetry={vi.fn()}
    />);

    await user.click(screen.getByRole("button", { name: "添加可见对象" }));
    const dialog = await screen.findByRole("dialog", { name: "添加可见对象" });
    await waitFor(() => expect(dialog).toBeVisible());
    expandNode(dialog, "示例业务技术");
    clickCheckbox(dialog, "销售部");

    await waitFor(() => expect(within(dialog).getByRole("button", { name: "确认" }).className).not.toContain("ant-btn-loading"));
    expect(within(dialog).queryByText("请求参数与服务版本不一致，请刷新页面或稍后重试")).not.toBeInTheDocument();
    expectChecked(dialog, "销售部", true);
    await user.click(within(dialog).getByRole("button", { name: "确认" }));

    await waitFor(() => expect(onChange).toHaveBeenCalledWith({
      organization: false,
      departmentIds: ["sales"],
      userIds: []
    }));
  });

  it("normalizes descendants when an ancestor department is selected", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ExpertVisibilityEditor
      scope={{ organization: false, departmentIds: [], userIds: [990095] }}
      onChange={onChange}
      directory={directory}
      loading={false}
      error={null}
      onRetry={vi.fn()}
    />);

    await user.click(screen.getByRole("button", { name: "添加可见对象" }));
    const dialog = await screen.findByRole("dialog", { name: "添加可见对象" });
    await waitFor(() => expect(dialog).toBeVisible());
    // antd Tree indeterminate 父点击为取消语义：先取消已勾子，再勾父完成接管。
    expectChecked(dialog, "示例用户甲（10001）", true);
    await clickCheckbox(dialog, "示例用户甲（10001）");
    expectChecked(dialog, "示例用户甲（10001）", false);
    await clickCheckbox(dialog, "总部");
    expect(within(dialog).getByText("研发中心")).toBeVisible();
    expectChecked(dialog, "示例用户甲（10001）", true);
    await user.click(within(dialog).getByRole("button", { name: "确认" }));

    // 人员不再嵌入部门树：已选人员独立保留，部门冗余规范化只作用于部门。
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({
      organization: false,
      departmentIds: ["root"],
      userIds: [990095]
    }));
  });

  it("removes a selected person only after confirmation", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ExpertVisibilityEditor
      scope={{ organization: false, departmentIds: [], userIds: [990095] }}
      onChange={onChange}
      directory={directory}
      loading={false}
      error={null}
      onRetry={vi.fn()}
    />);

    await user.click(screen.getByRole("tab", { name: /已分享人员/ }));
    const row = screen.getByText("示例用户甲").closest(".ant-list-item");
    expect(row).not.toBeNull();
    await user.click(within(row as HTMLElement).getByRole("button", { name: "移除" }));
    const confirmation = await screen.findByRole("dialog", { name: "确认移除可见对象" });
    await waitFor(() => expect(confirmation).toBeVisible());
    await user.click(within(confirmation).getByRole("button", { name: "确认移除" }));

    await waitFor(() => expect(onChange).toHaveBeenCalledWith({
      organization: false,
      departmentIds: [],
      userIds: []
    }));
  });
});
