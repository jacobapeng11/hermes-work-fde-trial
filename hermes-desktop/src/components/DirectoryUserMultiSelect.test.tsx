import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DirectoryUserMultiSelect } from "./DirectoryUserMultiSelect";


describe("DirectoryUserMultiSelect", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("searches after 300ms and keeps selected values as removable tags", async () => {
    vi.useFakeTimers();
    const search = vi.fn().mockResolvedValue([{ id: 12, name: "示例用户乙", userCode: "zhangyi" }]);
    const onChange = vi.fn();
    const { rerender } = render(<DirectoryUserMultiSelect value={[35]} onChange={onChange} searchUsers={search}/>);

    expect(screen.getByText("已失效用户（35）")).toBeVisible();
    const input = screen.getByRole("combobox", { name: "搜索用户" });
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "张" } });
    await act(async () => { vi.advanceTimersByTime(299); });
    expect(search).not.toHaveBeenCalled();
    await act(async () => {
      vi.advanceTimersByTime(1);
      await Promise.resolve();
    });
    expect(search).toHaveBeenCalledWith("张", 50);
    vi.useRealTimers();
    const option = await screen.findByRole("option", { name: "示例用户乙（zhangyi）" });
    expect(option).toBeVisible();
    fireEvent.click(option);
    expect(onChange).toHaveBeenCalledWith([35, 12]);
    expect(input).toHaveValue("");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

    rerender(<DirectoryUserMultiSelect value={[35, 12]} onChange={onChange} searchUsers={search}/>);
    expect(screen.getByText("示例用户乙（zhangyi）")).toBeVisible();

    vi.useFakeTimers();
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "李" } });
    await act(async () => {
      vi.advanceTimersByTime(300);
      await Promise.resolve();
    });
    expect(search).toHaveBeenLastCalledWith("李", 50);

    fireEvent.click(screen.getByRole("button", { name: "移除 已失效用户（35）" }));
    expect(onChange).toHaveBeenCalledWith([12]);
  });

  it("does not search for an empty keyword", () => {
    vi.useFakeTimers();
    const search = vi.fn();
    render(<DirectoryUserMultiSelect value={[]} onChange={vi.fn()} searchUsers={search}/>);
    fireEvent.focus(screen.getByRole("combobox", { name: "搜索用户" }));
    act(() => { vi.advanceTimersByTime(500); });
    expect(search).not.toHaveBeenCalled();
    expect(screen.getByText("请输入姓名或工号搜索")).toBeVisible();
  });

  it("clears the query and closes the popup when clicking outside", async () => {
    vi.useFakeTimers();
    let resolveSearch: ((items: { id: number; name: string; userCode: string }[]) => void) | undefined;
    const search = vi.fn().mockImplementation(() => new Promise((resolve) => {
      resolveSearch = resolve;
    }));
    render(<div>
      <DirectoryUserMultiSelect value={[]} onChange={vi.fn()} searchUsers={search}/>
      <button type="button">选择器外部</button>
    </div>);

    const input = screen.getByRole("combobox", { name: "搜索用户" });
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "zhang" } });
    act(() => { vi.advanceTimersByTime(300); });
    expect(search).toHaveBeenCalledWith("zhang", 50);

    fireEvent.pointerDown(screen.getByRole("button", { name: "选择器外部" }));
    expect(input).toHaveValue("");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

    await act(async () => {
      resolveSearch?.([{ id: 12, name: "示例用户乙", userCode: "zhangyi" }]);
      await Promise.resolve();
    });
    expect(screen.queryByText("示例用户乙")).not.toBeInTheDocument();
  });

  it("hydrates active, inactive, and missing selected user labels", () => {
    render(<DirectoryUserMultiSelect
      value={[12, 13, 14]}
      onChange={vi.fn()}
      selectedUsers={[
        { id: 12, name: "示例用户乙", userCode: "zhangyi", status: "active" },
        { id: 13, name: "李明", userCode: "liming", status: "inactive" },
        { id: 14, name: null, userCode: null, status: "missing" }
      ]}
    />);

    expect(screen.getByText("示例用户乙（zhangyi）")).toBeVisible();
    expect(screen.getByText("李明（liming，已失效）")).toBeVisible();
    expect(screen.getByText("已失效用户（14）")).toBeVisible();
  });
});
