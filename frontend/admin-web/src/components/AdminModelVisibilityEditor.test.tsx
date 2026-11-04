import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { OrganizationDirectoryNode } from "@hermes-cloud/schemas";
import { adminApi } from "../api/client";

import {
  AdminModelVisibilityEditor,
  normalizeVisibilitySelection,
} from "./AdminModelVisibilityEditor";

const directory: OrganizationDirectoryNode[] = [{
  nodeType: "organization",
  organizationId: 9,
  name: "示例业务技术",
  children: [
    {
      nodeType: "department",
      departmentId: "root",
      name: "总部",
      parentId: null,
      children: [
        {
          nodeType: "department",
          departmentId: "dev",
          name: "研发中心",
          parentId: "root",
          children: [
            {
              nodeType: "user",
              loggedIn: true,
              userId: 12,
              name: "示例用户乙",
              userCode: "zhangyi",
              departmentId: "dev",
              status: "active"
            }
          ]
        }
      ]
    },
    {
      nodeType: "department",
      departmentId: "product",
      name: "产品部",
      parentId: null,
      children: [
        {
          nodeType: "department",
          departmentId: "design",
          name: "设计组",
          parentId: "product",
          children: [
            {
              nodeType: "user",
              loggedIn: true,
              userId: 14,
              name: "王丽",
              userCode: "wangli",
              departmentId: "design",
              status: "active"
            }
          ]
        }
      ]
    },
    {
      nodeType: "department",
      departmentId: "virtual:unassigned",
      name: "未归属部门",
      parentId: null,
      virtual: true,
      children: [
        {
          nodeType: "user",
          loggedIn: true,
          userId: 13,
          name: "李明",
          userCode: "liming",
          departmentId: null,
          status: "active"
        }
      ]
    }
  ]
}];

function renderEditor({
  value,
  selectedUsers = [],
  directory: directoryOverride,
  onChange = vi.fn(),
  loading = false,
  error = null,
  onRetry,
}: {
  value: Parameters<typeof AdminModelVisibilityEditor>[0]["value"];
  selectedUsers?: Parameters<typeof AdminModelVisibilityEditor>[0]["selectedUsers"];
  directory?: Parameters<typeof AdminModelVisibilityEditor>[0]["directory"];
  onChange?: ReturnType<typeof vi.fn>;
  loading?: boolean;
  error?: string | null;
  onRetry?: ReturnType<typeof vi.fn>;
}) {
  return render(
    <AdminModelVisibilityEditor
      value={value}
      selectedUsers={selectedUsers}
      directory={directoryOverride ?? directory}
      loading={loading}
      error={error}
      onRetry={onRetry}
      onChange={onChange}
    />
  );
}

describe("AdminModelVisibilityEditor", () => {
  it("shows one long entry button and a collapsed organization tree", async () => {
    const user = userEvent.setup();
    renderEditor({
      value: { organization: true, departmentIds: [], userIds: [] }
    });

    const button = screen.getByRole("button", { name: /配置可见权限/ });
    expect(button).toHaveTextContent("配置可见权限");
    expect(button).toHaveTextContent("当前：全员可见");
    expect(screen.queryByText(/^可见部门/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^可见人员/)).not.toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: "全员可见" })).not.toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: "自定义可见" })).not.toBeInTheDocument();

    await user.click(button);
    const dialog = screen.getByRole("dialog", { name: "配置可见权限" });
    expect(within(dialog).getByText("示例业务技术")).toBeVisible();
    expect(within(dialog).getByText("总部")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "展开 总部" })).toBeVisible();
    expect(within(dialog).queryByText("示例用户乙（zhangyi）")).not.toBeInTheDocument();
  });

  it("shows selected department and user names below the entry button", () => {
    renderEditor({
      value: { organization: false, departmentIds: ["dev", "product"], userIds: [12] },
      selectedUsers: [{
        id: 12,
        name: "示例用户乙",
        userCode: "zhangyi",
        status: "active"
      }]
    });

    expect(screen.getByText("可见部门（2）")).toBeVisible();
    expect(screen.getByText("研发中心")).toBeVisible();
    expect(screen.getByText("产品部")).toBeVisible();
    expect(screen.getByText("可见人员（1）")).toBeVisible();
    expect(screen.getByText("示例用户乙（zhangyi）")).toBeVisible();
    expect(screen.queryByRole("button", { name: /移除/ })).not.toBeInTheDocument();
  });

  it("keeps user names and shows department loading placeholders before the directory loads", () => {
    renderEditor({
      value: { organization: false, departmentIds: ["dev"], userIds: [12] },
      selectedUsers: [{
        id: 12,
        name: "示例用户乙",
        userCode: "zhangyi",
        status: "active"
      }],
      directory: [],
      loading: true
    });

    expect(screen.getByRole("button", { name: /配置可见权限/ }))
      .toHaveTextContent("当前：1 个部门 / 1 名人员");
    expect(screen.getByText("部门名称加载中")).toBeVisible();
    expect(screen.getByText("示例用户乙（zhangyi）")).toBeVisible();
  });

  it("shows a stable department fallback when directory loading fails", () => {
    renderEditor({
      value: { organization: false, departmentIds: ["dev"], userIds: [] },
      directory: [],
      loading: false,
      error: "组织目录暂时不可用"
    });

    expect(screen.getByText("部门名称暂不可用")).toBeVisible();
  });

  it("marks missing departments and inactive users without hiding the saved scope", () => {
    renderEditor({
      value: { organization: false, departmentIds: ["missing"], userIds: [99] },
      selectedUsers: [{
        id: 99,
        name: "王五",
        userCode: "wangwu",
        status: "inactive"
      }]
    });

    expect(screen.getByText("未知部门（missing）")).toBeVisible();
    expect(screen.getByText("王五（wangwu，已失效）")).toBeVisible();
  });

  it("expands ancestors for an existing user selection and cancels without changes", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderEditor({
      value: { organization: false, departmentIds: [], userIds: [12] },
      selectedUsers: [{
        id: 12,
        name: "示例用户乙",
        userCode: "zhangyi",
        status: "active"
      }],
      onChange
    });

    expect(screen.getByRole("button", { name: /配置可见权限/ }))
      .toHaveTextContent("当前：1 名人员");
    await user.click(screen.getByRole("button", { name: /配置可见权限/ }));
    const dialog = screen.getByRole("dialog", { name: "配置可见权限" });
    expect(within(dialog).getByRole("button", { name: "收起 总部" })).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "收起 研发中心" })).toBeVisible();
    expect(
      within(dialog).getByRole("checkbox", { name: "示例用户乙（zhangyi）" })
    ).toBeChecked();
    expect(within(dialog).queryByText("设计组")).not.toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "取消" }));

    expect(onChange).not.toHaveBeenCalled();
  });

  it("normalizes descendants and covered users when an ancestor is selected", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderEditor({
      value: { organization: false, departmentIds: ["dev"], userIds: [12] },
      selectedUsers: [{
        id: 12,
        name: "示例用户乙",
        userCode: "zhangyi",
        status: "active"
      }],
      onChange
    });

    await user.click(screen.getByRole("button", { name: /配置可见权限/ }));
    const dialog = screen.getByRole("dialog", { name: "配置可见权限" });
    await user.click(within(dialog).getByRole("checkbox", { name: "总部" }));
    await user.click(within(dialog).getByRole("button", { name: "确认" }));

    expect(onChange).toHaveBeenCalledWith(
      {
        organization: false,
        departmentIds: ["root"],
        userIds: []
      },
      []
    );
  });

  it("keeps manually expanded branches when a covered user click is normalized", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderEditor({
      value: { organization: false, departmentIds: ["root"], userIds: [] },
      onChange
    });

    await user.click(screen.getByRole("button", { name: /配置可见权限/ }));
    const dialog = screen.getByRole("dialog", { name: "配置可见权限" });
    await user.click(within(dialog).getByRole("button", { name: "展开 总部" }));
    await user.click(within(dialog).getByRole("button", { name: "展开 研发中心" }));

    await user.click(within(dialog).getByRole("checkbox", { name: "示例用户乙（zhangyi）" }));

    expect(
      within(dialog).getByRole("button", { name: "收起 研发中心" })
    ).toBeVisible();
    expect(
      within(dialog).getByRole("checkbox", { name: "示例用户乙（zhangyi）" })
    ).not.toBeChecked();

    await user.click(within(dialog).getByRole("button", { name: "确认" }));
    expect(onChange).toHaveBeenCalledWith(
      {
        organization: false,
        departmentIds: ["root"],
        userIds: []
      },
      []
    );
  });

  it("searches departments and users by name or employee code", async () => {
    const user = userEvent.setup();
    renderEditor({
      value: { organization: true, departmentIds: [], userIds: [] }
    });

    await user.click(screen.getByRole("button", { name: /配置可见权限/ }));
    await user.type(screen.getByLabelText("搜索部门或人员"), "zhangyi");

    expect(await screen.findByText("示例用户乙（zhangyi）")).toBeVisible();
    expect(screen.getByText("研发中心")).toBeVisible();
    expect(screen.getByText("总部")).toBeVisible();
    expect(screen.queryByText("产品部")).not.toBeInTheDocument();
  });

  it("selects parallel company roots as department grants", async () => {
    const companyDirectory: OrganizationDirectoryNode[] = [{
      nodeType: "organization",
      organizationId: 9,
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
          children: []
        }
      ]
    }];
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderEditor({
      directory: companyDirectory,
      value: { organization: true, departmentIds: [], userIds: [] },
      onChange
    });

    await user.click(screen.getByRole("button", { name: /配置可见权限/ }));
    await user.click(screen.getByRole("checkbox", { name: "示例公司" }));
    await user.click(screen.getByRole("button", { name: "确认" }));

    expect(onChange).toHaveBeenCalledWith(
      {
        organization: false,
        departmentIds: ["49"],
        userIds: []
      },
      []
    );
  });

  it("adds users through the independent user search", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    vi.spyOn(adminApi, "searchDirectoryUsers").mockResolvedValue([
      { id: 13, name: "李明", userCode: "liming" }
    ]);
    renderEditor({
      value: { organization: true, departmentIds: [], userIds: [] },
      onChange
    });

    await user.click(screen.getByRole("button", { name: /配置可见权限/ }));
    await user.type(screen.getByLabelText("搜索人员"), "liming");
    await user.click(await screen.findByRole("button", { name: /李明（liming）/ }));
    await user.click(screen.getByRole("button", { name: "确认" }));

    expect(onChange).toHaveBeenCalledWith(
      {
        organization: false,
        departmentIds: [],
        userIds: [13]
      },
      [expect.objectContaining({ id: 13, name: "李明" })]
    );
  });

  it("clearing every selection confirms organization-wide visibility", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderEditor({
      value: { organization: false, departmentIds: ["root"], userIds: [13] },
      selectedUsers: [{
        id: 13,
        name: "李明",
        userCode: "liming",
        status: "active"
      }],
      onChange
    });

    await user.click(screen.getByRole("button", { name: /配置可见权限/ }));
    await user.click(screen.getByRole("checkbox", { name: "总部" }));
    await user.click(screen.getByRole("button", { name: "移除 李明（liming）" }));
    await user.click(screen.getByRole("button", { name: "确认" }));

    expect(onChange).toHaveBeenCalledWith(
      {
        organization: true,
        departmentIds: [],
        userIds: []
      },
      []
    );
  });

  it("shows directory loading and retry states", async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    const { rerender } = renderEditor({
      value: { organization: true, departmentIds: [], userIds: [] },
      loading: true
    });

    await user.click(screen.getByRole("button", { name: /配置可见权限/ }));
    expect(screen.getByText("正在加载组织目录…")).toBeVisible();

    rerender(
      <AdminModelVisibilityEditor
        value={{ organization: true, departmentIds: [], userIds: [] }}
        selectedUsers={[]}
        directory={[]}
        loading={false}
        error="组织目录暂时不可用"
        onRetry={onRetry}
        onChange={vi.fn()}
      />
    );

    expect(screen.getByText("组织目录暂时不可用")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

describe("normalizeVisibilitySelection", () => {
  it("keeps the highest selected department and removes covered users", () => {
    expect(
      normalizeVisibilitySelection(
        {
          organization: false,
          departmentIds: ["root", "dev"],
          userIds: [12, 13]
        },
        directory
      )
    ).toEqual({
      organization: false,
      departmentIds: ["root"],
      userIds: [13]
    });
  });

  it("treats an empty custom scope as organization-wide visibility", () => {
    expect(
      normalizeVisibilitySelection(
        { organization: false, departmentIds: [], userIds: [] },
        directory
      )
    ).toEqual({
      organization: true,
      departmentIds: [],
      userIds: []
    });
  });
});
