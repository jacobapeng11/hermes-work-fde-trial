import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AuthPrincipalSummary } from "@hermes-cloud/schemas";
import { defaultUserProfile } from "../../profile";
import { ProfileDialog } from "./ProfileDialog";

const api = vi.hoisted(() => ({
  avatarAssets: vi.fn(),
  uploadAvatarAsset: vi.fn()
}));

vi.mock("../../api/client", () => ({ userApi: api }));

const principal: AuthPrincipalSummary = { orgId: 1, userId: 2, name: "林晨", email: "linchen@example.com", userCode: "lc02" };

describe("ProfileDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.avatarAssets.mockResolvedValue([]);
    api.uploadAvatarAsset.mockResolvedValue({
      avatarValue: "https://assets.example/avatar.jpg",
      label: "自定义头像",
      sourceKind: "user_upload",
      imageUrl: "https://assets.example/avatar.jpg",
      sortOrder: 1000
    });
  });

  it("saves profile fields and the selected default avatar", async () => {
    const onSave = vi.fn();
    const user = userEvent.setup();
    render(<ProfileDialog profile={defaultUserProfile(principal.name)} principal={principal} onSave={onSave} onClose={() => undefined}/>);

    await user.clear(screen.getByLabelText("显示名称"));
    await user.type(screen.getByLabelText("显示名称"), "晨星");
    await user.click(screen.getByRole("radio", { name: "默认头像 2" }));
    await user.click(screen.getByRole("button", { name: "保存资料" }));

    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ displayName: "晨星", avatarKind: "default", avatarId: "avatar-02" }));
    // 「平台账号信息」只读模块已按需求移除（设计文档 §12），不再断言 email/工号展示。
    expect(screen.queryByText("linchen@example.com")).not.toBeInTheDocument();
  });

  it("keeps the current avatar when an unsupported upload fails", async () => {
    const user = userEvent.setup();
    render(<ProfileDialog profile={{ ...defaultUserProfile(principal.name), avatarId: "avatar-03" }} principal={principal} onSave={() => undefined} onClose={() => undefined}/>);
    const upload = screen.getByLabelText("上传自定义头像");

    fireEvent.change(upload, { target: { files: [new File(["text"], "avatar.txt", { type: "text/plain" })] } });

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("请选择 PNG、JPEG 或 WebP 图片。"));
    expect(screen.getByRole("radio", { name: "默认头像 3" })).toHaveAttribute("aria-checked", "true");
  });

  it("opens a movable crop dialog before uploading a supported image", async () => {
    const user = userEvent.setup();
    render(<ProfileDialog profile={defaultUserProfile(principal.name)} principal={principal} onSave={() => undefined} onClose={() => undefined}/>);

    await user.upload(screen.getByLabelText("上传自定义头像"), new File(["png"], "avatar.png", { type: "image/png" }));

    const cropDialog = screen.getByRole("dialog", { name: "调整头像取景" });
    // antd Modal 外壳在 jsdom 中 rc-motion appear 需异步完成（prepare 阶段 opacity:0），
    // 与 ExpertCatalogPanels.test 同款 waitFor 等待模式。
    await waitFor(() => expect(cropDialog).toBeVisible());
    expect(api.uploadAvatarAsset).not.toHaveBeenCalled();
    await user.click(within(cropDialog).getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog", { name: "调整头像取景" })).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "默认头像 1" })).toHaveAttribute("aria-checked", "true");
  });

  it("uses the shared platform avatar picker instead of an avatar URL field", async () => {
    render(<ProfileDialog profile={defaultUserProfile(principal.name)} principal={principal} onSave={() => undefined} onClose={() => undefined}/>);

    const firstAvatar = await screen.findByRole("radio", { name: "默认头像 1" });
    await waitFor(() => expect(firstAvatar).toBeVisible());
    expect(screen.getByLabelText("上传自定义头像")).toHaveAttribute("accept", "image/png,image/jpeg,image/webp");
    expect(screen.queryByText(/头像 URL/)).not.toBeInTheDocument();
  });

  it("migrates a legacy local data URL to a platform avatar on save", async () => {
    const onSave = vi.fn();
    render(<ProfileDialog profile={{
      ...defaultUserProfile(principal.name),
      avatarKind: "custom",
      customAvatarUrl: "data:image/jpeg;base64,YQ=="
    }} principal={principal} onSave={onSave} onClose={() => undefined}/>);

    await userEvent.setup().click(screen.getByRole("button", { name: "保存资料" }));

    await waitFor(() => expect(api.uploadAvatarAsset).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      avatarKind: "custom",
      customAvatarUrl: "https://assets.example/avatar.jpg"
    }));
  });
});
