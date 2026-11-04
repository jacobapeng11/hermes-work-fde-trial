import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  expertReviews: vi.fn(),
  approveExpertReview: vi.fn(),
  rejectExpertReview: vi.fn(),
  unpublishExpertPackage: vi.fn(),
  forceDisableExpertPackage: vi.fn(),
  enableExpertPackage: vi.fn(),
  directoryDepartmentTree: vi.fn(),
  searchDirectoryUsers: vi.fn()
}));

vi.mock("../api/client", () => ({ adminApi: api }));

import { AdminExpertManagement } from "./AdminExpertManagement";

const review = {
  reviewId: 31,
  expertPackageId: 17,
  expertPackageVersionId: 2,
  submitterUserId: 9,
  name: "写作专家",
  description: "公众号文章写作",
  avatarUrl: null,
  soulContent: "# 写作专家",
  capabilities: {
    skillIds: [3],
    mcpIds: [5],
    knowledgeBaseIds: [7],
    pluginIds: ["personal-knowledge"]
  },
  shareScope: {
    organization: false,
    userIds: [12],
    departmentIds: ["dept-a"]
  },
  shareScopeUsers: [{
    id: 12,
    name: "示例用户乙",
    userCode: "zhangyi",
    status: "active"
  }],
  status: "pending",
  reviewComment: null,
  executionStatus: "enabled",
  executionStatusReason: null,
  activeInstallationCount: 2
};

describe("AdminExpertManagement", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.expertReviews.mockResolvedValue([review]);
    api.approveExpertReview.mockResolvedValue({
      ...review,
      status: "approved"
    });
    api.unpublishExpertPackage.mockResolvedValue(undefined);
    api.forceDisableExpertPackage.mockResolvedValue({
      rolloutId: "expert-security-17",
      affectedUserCount: 2,
      affectedProfileCount: 2,
      status: "waiting",
      created: true
    });
    api.enableExpertPackage.mockResolvedValue({
      rolloutId: "expert-enable-17",
      affectedUserCount: 2,
      affectedProfileCount: 2,
      status: "waiting",
      created: true
    });
    api.directoryDepartmentTree.mockResolvedValue([{
      departmentId: "dept-a",
      parentDepartmentId: null,
      name: "研发部",
      fullName: "研发部",
      children: []
    }]);
    api.searchDirectoryUsers.mockResolvedValue([]);
  });

  it("shows the submitted scope and lets the administrator approve it", async () => {
    render(<AdminExpertManagement />);

    await userEvent.setup().click(await screen.findByRole("button", {
      name: /写作专家/
    }));
    const dialog = screen.getByRole("dialog", { name: "写作专家" });
    expect(within(dialog).getByText("示例用户乙（zhangyi）")).toBeVisible();
    expect(within(dialog).getByText("公众号文章写作")).toBeVisible();
    expect(within(dialog).getByText("# 写作专家")).toBeVisible();

    await userEvent.setup().click(within(dialog).getByRole("button", {
      name: /通过审核/
    }));
    await waitFor(() => expect(api.approveExpertReview).toHaveBeenCalledWith(
      31,
      {
        reviewComment: null
      }
    ));
    expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(within(dialog).queryByPlaceholderText("搜索姓名或工号")).not.toBeInTheDocument();
  });

  it("renders the user-selected standalone built-in avatar in the review list and detail", async () => {
    api.expertReviews.mockResolvedValue([{ ...review, avatarUrl: "builtin:avatar-03" }]);
    render(<AdminExpertManagement />);

    const listAvatar = await screen.findByRole("img", { name: "写作专家的头像" });
    expect(listAvatar.querySelector("img")).toHaveAttribute("src", expect.stringContaining("avatar-03"));
    await userEvent.setup().click(screen.getByRole("button", { name: /写作专家/ }));
    const dialog = screen.getByRole("dialog", { name: "写作专家" });
    expect(within(dialog).getByRole("img", { name: "写作专家的头像" }).querySelector("img")).toHaveAttribute("src", expect.stringContaining("avatar-03"));
  });

  it("keeps the creator-submitted targeted scope read-only", async () => {
    render(<AdminExpertManagement />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /写作专家/ }));
    const dialog = screen.getByRole("dialog", { name: "写作专家" });

    expect(within(dialog).getByText("示例用户乙（zhangyi）")).toBeVisible();
    expect(within(dialog).getByText("dept-a")).toBeVisible();
    expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", {
      name: /通过审核/
    }));

    await waitFor(() => expect(api.approveExpertReview).toHaveBeenCalledWith(
      31,
      {
        reviewComment: null
      }
    ));
  });

  it("labels organization-wide reviews without showing an empty targeted scope", async () => {
    api.expertReviews.mockResolvedValueOnce([{
      ...review,
      shareScope: {
        organization: true,
        userIds: [],
        departmentIds: []
      },
      shareScopeUsers: []
    }]);
    render(<AdminExpertManagement />);

    expect(await screen.findByText("全组织")).toBeVisible();
    await userEvent.setup().click(screen.getByRole("button", { name: /写作专家/ }));
    const dialog = screen.getByRole("dialog", { name: "写作专家" });
    expect(within(dialog).getByText("是")).toBeVisible();
    expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("does not load editable directories for a read-only review scope", async () => {
    api.directoryDepartmentTree.mockRejectedValueOnce(new Error("部门目录暂时不可用"));
    render(<AdminExpertManagement />);

    await userEvent.setup().click(await screen.findByRole("button", {
      name: /写作专家/
    }));

    const dialog = screen.getByRole("dialog", { name: "写作专家" });
    expect(dialog).toBeVisible();
    expect(api.directoryDepartmentTree).not.toHaveBeenCalled();
    expect(api.searchDirectoryUsers).not.toHaveBeenCalled();
  });

  it("removes an approved expert from the management list after unpublishing", async () => {
    const approvedReview = {
      ...review,
      status: "approved"
    };
    api.expertReviews
      .mockResolvedValueOnce([approvedReview])
      .mockResolvedValueOnce([]);
    render(<AdminExpertManagement />);

    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /写作专家/ }));
    await user.click(within(screen.getByRole("dialog", {
      name: "写作专家"
    })).getByRole("button", { name: /正常下架/ }));

    await waitFor(() => expect(api.unpublishExpertPackage).toHaveBeenCalledWith(17));
    await waitFor(() => expect(screen.queryByRole("button", {
      name: /写作专家/
    })).not.toBeInTheDocument());
    expect(api.expertReviews).toHaveBeenCalledTimes(2);
  });

  it("keeps normal unpublish separate from emergency execution revoke", async () => {
    api.expertReviews
      .mockResolvedValueOnce([{ ...review, status: "approved" }])
      .mockResolvedValueOnce([]);
    render(<AdminExpertManagement />);

    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /写作专家/ }));
    const dialog = screen.getByRole("dialog", { name: "写作专家" });
    await user.type(
      within(dialog).getByPlaceholderText("填写强制停用的安全原因"),
      "发现高风险依赖"
    );
    await user.click(within(dialog).getByRole("button", { name: /强制停用/ }));

    await waitFor(() => expect(api.forceDisableExpertPackage).toHaveBeenCalledWith(
      17,
      "发现高风险依赖",
      expect.any(String)
    ));
    expect(api.unpublishExpertPackage).not.toHaveBeenCalled();
    expect(await screen.findByRole("status")).toHaveTextContent(
      "2 个 Profile"
    );
  });
});
