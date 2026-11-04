import { beforeEach, describe, expect, it } from "vitest";
import type { AuthPrincipalSummary } from "@hermes-cloud/schemas";
import { DEFAULT_AVATARS, loadUserProfile, normalizeAvatarFile, profileStorageKey, saveUserProfile } from "./profile";

const principal: AuthPrincipalSummary = { orgId: 3, userId: 8, name: "林晨", email: "linchen@example.com", userCode: "lc08" };

describe("local digital-human profile", () => {
  beforeEach(() => window.localStorage.clear());

  it("uses a principal-scoped key and falls back to the SSO name", () => {
    expect(profileStorageKey(principal)).toBe("hermes.user-profile.v1.3.8");
    expect(loadUserProfile(principal)).toMatchObject({
      displayName: "林晨",
      avatarKind: "default",
      avatarId: DEFAULT_AVATARS[0].id
    });
  });

  it("persists a profile only for the current principal", () => {
    const saved = { ...loadUserProfile(principal), displayName: "晨星", jobTitle: "产品经理", avatarId: "avatar-02" as const };
    saveUserProfile(principal, saved);

    expect(loadUserProfile(principal)).toMatchObject(saved);
    expect(loadUserProfile({ ...principal, userId: 9, name: "另一位用户" }).displayName).toBe("另一位用户");
  });

  it("falls back safely when saved data is malformed", () => {
    window.localStorage.setItem(profileStorageKey(principal), "not-json");
    expect(loadUserProfile(principal)).toMatchObject({ displayName: "林晨", avatarKind: "default" });
  });

  it("rejects unsupported avatar uploads before replacing a current avatar", async () => {
    await expect(normalizeAvatarFile(new File(["text"], "avatar.txt", { type: "text/plain" }))).rejects.toThrow("请选择 PNG、JPEG 或 WebP 图片。");
  });

  it("does not center-crop a supported avatar before the user confirms a crop", async () => {
    const file = new File(["png"], "avatar.png", { type: "image/png" });
    await expect(normalizeAvatarFile(file)).resolves.toBe(file);
  });

  it("keeps legacy custom data URLs readable during migration", () => {
    window.localStorage.setItem(profileStorageKey(principal), JSON.stringify({
      ...loadUserProfile(principal),
      avatarKind: "custom",
      customAvatarDataUrl: "data:image/jpeg;base64,legacy"
    }));

    expect(loadUserProfile(principal)).toMatchObject({
      avatarKind: "custom",
      customAvatarUrl: "data:image/jpeg;base64,legacy"
    });
  });
});
