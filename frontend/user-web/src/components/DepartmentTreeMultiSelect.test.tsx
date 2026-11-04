import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DepartmentTreeMultiSelect } from "./DepartmentTreeMultiSelect";


const tree = [{
  departmentId: "51",
  name: "示例业务网科",
  parentId: null,
  children: [{ departmentId: "5101", name: "研发中心", parentId: "51", children: [] }]
}];


describe("DepartmentTreeMultiSelect", () => {
  it("expands, filters, selects, and clears department values", () => {
    const onChange = vi.fn();
    const { rerender } = render(<DepartmentTreeMultiSelect value={[]} onChange={onChange} tree={tree}/>);

    fireEvent.click(screen.getByRole("button", { name: "选择部门" }));
    fireEvent.click(screen.getByRole("button", { name: "展开 示例业务网科" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索部门" }), { target: { value: "研发" } });
    expect(screen.getByText("研发中心")).toBeVisible();
    fireEvent.click(screen.getByRole("checkbox", { name: "研发中心" }));
    expect(onChange).toHaveBeenCalledWith(["5101"]);
    expect(screen.queryByRole("searchbox", { name: "搜索部门" })).not.toBeInTheDocument();

    rerender(<DepartmentTreeMultiSelect value={["5101"]} onChange={onChange} tree={tree}/>);
    expect(screen.getByText("研发中心")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "选择部门" }));
    expect(screen.getByRole("searchbox", { name: "搜索部门" })).toHaveValue("");
    expect(screen.getByRole("button", { name: "收起 示例业务网科" })).toBeVisible();
    fireEvent.click(screen.getByRole("checkbox", { name: "研发中心" }));
    expect(onChange).toHaveBeenLastCalledWith([]);
    expect(screen.queryByRole("searchbox", { name: "搜索部门" })).not.toBeInTheDocument();
  });

  it("shows selected department names and supports removal", () => {
    const onChange = vi.fn();
    render(<DepartmentTreeMultiSelect value={["5101"]} onChange={onChange} tree={tree}/>);
    expect(screen.getByText("研发中心")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "移除 研发中心" }));
    expect(onChange).toHaveBeenCalledWith([]);
  });

  it("clears the filter and closes the popup when clicking outside", () => {
    render(<div>
      <DepartmentTreeMultiSelect value={[]} onChange={vi.fn()} tree={tree}/>
      <button type="button">选择器外部</button>
    </div>);

    fireEvent.click(screen.getByRole("button", { name: "选择部门" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索部门" }), { target: { value: "研发" } });
    fireEvent.pointerDown(screen.getByRole("button", { name: "选择器外部" }));
    expect(screen.queryByRole("searchbox", { name: "搜索部门" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "选择部门" }));
    expect(screen.getByRole("searchbox", { name: "搜索部门" })).toHaveValue("");
  });
});
