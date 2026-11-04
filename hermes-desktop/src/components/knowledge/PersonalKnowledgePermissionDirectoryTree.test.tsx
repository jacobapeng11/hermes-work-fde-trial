import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { PersonalKnowledgePermissionDirectoryNode, PersonalKnowledgePermissionGrant } from "@hermes-cloud/schemas";
import { PersonalKnowledgePermissionDirectoryTree } from "./PersonalKnowledgePermissionDirectoryTree";
import { userApi } from "../../api/client";

const nodes: PersonalKnowledgePermissionDirectoryNode[] = [{
  nodeType: "department",
  departmentId: "d1",
  name: "研发中心",
  children: [{
    nodeType: "department",
    departmentId: "d2",
    name: "平台组",
    parentId: "d1",
    children: [{ nodeType: "user", loggedIn: true, userId: 12, name: "示例用户乙", userCode: "zhangyi", departmentId: "d2", status: "active" }]
  }]
}, {
  nodeType: "department",
  departmentId: "m1",
  name: "市场部",
  children: [{
    nodeType: "department",
    departmentId: "m2",
    name: "增长组",
    parentId: "m1",
    children: [{ nodeType: "user", loggedIn: true, userId: 14, name: "王丽", userCode: "wangli", departmentId: "m2", status: "active" }]
  }]
}];

const grants: PersonalKnowledgePermissionGrant[] = [{
  targetType: "user",
  targetId: 12,
  name: "示例用户乙",
  userCode: "zhangyi",
  departmentId: "d2",
  departmentPath: ["研发中心", "平台组"],
  status: "active",
  role: "preview"
}];

describe("PersonalKnowledgePermissionDirectoryTree", () => {
  it("provisions an unprovisioned user on selection and returns the real target", async () => {
    const nodes: PersonalKnowledgePermissionDirectoryNode[] = [{
      nodeType: "department",
      departmentId: "51",
      name: "示例业务公司",
      children: [{ nodeType: "user", userId: null, name: "吴思聪", userCode: "70948", departmentId: null, status: "active" }]
    }];
    const provision = vi.spyOn(userApi, "provisionDirectoryUsers").mockResolvedValue([
      { id: 990999, name: "吴思聪", userCode: "70948" }
    ]);
    const onChange = vi.fn();
    const { rerender } = render(<PersonalKnowledgePermissionDirectoryTree nodes={nodes} selectedKeys={[]} grants={[]} onChange={onChange}/>);

    fireEvent.click(screen.getByRole("button", { name: "展开 示例业务公司" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: /吴思聪（70948）/ }));

    await waitFor(() => expect(provision).toHaveBeenCalledWith(["70948"]));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith([
      expect.objectContaining({ targetType: "user", targetId: 990999, name: "吴思聪" })
    ]));

    // 父组件采纳勾选后回传 selectedKeys，再点击=取消；取消后“未激活”标记必须保留（直到用户首次登录）。
    rerender(<PersonalKnowledgePermissionDirectoryTree nodes={nodes} selectedKeys={["user:990999"]} grants={[]} onChange={onChange}/>);
    const checkbox = screen.getByRole("checkbox", { name: /吴思聪（70948）/ });
    fireEvent.click(checkbox);
    expect(onChange).toHaveBeenCalledTimes(2);
    expect(onChange).toHaveBeenLastCalledWith([]);
    expect(screen.getByText("未激活")).toBeVisible();

    // 回归：provision 过的用户取消后再勾选，必须仍产出目标
    // （此前 targets Map 未并入 provisioned id，重选被静默丢弃、checkbox 勾不上）。
    rerender(<PersonalKnowledgePermissionDirectoryTree nodes={nodes} selectedKeys={[]} grants={[]} onChange={onChange}/>);
    fireEvent.click(checkbox);
    await waitFor(() => expect(onChange).toHaveBeenLastCalledWith([
      expect.objectContaining({ targetType: "user", targetId: 990999, name: "吴思聪" })
    ]));
  });

  it("keeps parallel company roots selectable", () => {
    const companyNodes: PersonalKnowledgePermissionDirectoryNode[] = [
      {
        nodeType: "department",
        departmentId: "49",
        name: "示例公司",
        children: []
      },
      {
        nodeType: "department",
        departmentId: "51",
        name: "示例业务公司",
        children: [{ nodeType: "user", loggedIn: true, userId: 21, name: "赵六", userCode: "zhaoliu", departmentId: null, status: "active" }]
      }
    ];
    render(<PersonalKnowledgePermissionDirectoryTree nodes={companyNodes} selectedKeys={[]} grants={[]} onChange={vi.fn()}/>);

    fireEvent.click(screen.getByRole("button", { name: "展开 示例业务公司" }));
    expect(screen.getByRole("checkbox", { name: "示例公司" })).toBeEnabled();
    expect(screen.getByRole("checkbox", { name: "示例业务公司" })).toBeEnabled();
    expect(screen.getByRole("checkbox", { name: "赵六（zhaoliu）" })).toBeEnabled();
  });

  it("expands saved grant ancestors and keeps unrelated branches collapsed", () => {
    render(<PersonalKnowledgePermissionDirectoryTree nodes={nodes} selectedKeys={[]} grants={grants} onChange={vi.fn()}/>);

    expect(screen.getByRole("button", { name: "收起 研发中心" })).toBeVisible();
    expect(screen.getByRole("button", { name: "收起 平台组" })).toBeVisible();
    expect(screen.getByRole("checkbox", { name: "示例用户乙（zhangyi）" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "展开 增长组" })).not.toBeInTheDocument();
  });

  it("selects departments and users independently and shows direct grants", () => {
    const onChange = vi.fn();
    const { rerender } = render(<PersonalKnowledgePermissionDirectoryTree nodes={nodes} selectedKeys={[]} grants={grants} onChange={onChange}/>);

    fireEvent.click(screen.getByRole("checkbox", { name: "研发中心" }));
    expect(onChange.mock.calls[0][0]).toEqual([expect.objectContaining({ targetType: "department", targetId: "d1" })]);

    rerender(<PersonalKnowledgePermissionDirectoryTree nodes={nodes} selectedKeys={["department:d1"]} grants={grants} onChange={onChange}/>);
    expect(screen.getByText("仅预览和引用")).toBeVisible();
    fireEvent.click(screen.getByRole("checkbox", { name: "示例用户乙（zhangyi）" }));
    expect(onChange.mock.calls[onChange.mock.calls.length - 1]?.[0]).toEqual([
      expect.objectContaining({ targetType: "department", targetId: "d1" }),
      expect.objectContaining({ targetType: "user", targetId: 12 })
    ]);
  });

  it("searches names and user codes while preserving ancestors", () => {
    render(<PersonalKnowledgePermissionDirectoryTree nodes={nodes} selectedKeys={[]} grants={grants} onChange={vi.fn()}/>);

    fireEvent.change(screen.getByRole("searchbox", { name: "搜索部门或人员" }), { target: { value: "zhangyi" } });
    expect(screen.getByText("研发中心")).toBeVisible();
    expect(screen.getByText("平台组")).toBeVisible();
    expect(screen.getByText("示例用户乙（zhangyi）")).toBeVisible();
  });
});
