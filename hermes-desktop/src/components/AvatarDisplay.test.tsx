import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DEFAULT_AVATARS } from "../avatar";
import { AvatarDisplay } from "./AvatarDisplay";

describe("AvatarDisplay", () => {
  it("uses eight pre-split square default-avatar assets", () => {
    expect(DEFAULT_AVATARS).toHaveLength(8);
    DEFAULT_AVATARS.forEach((avatar, index) => {
      expect(avatar.source).toContain(`avatar-${String(index + 1).padStart(2, "0")}`);
      expect(avatar.source).toContain("individual");
    });
  });

  it("renders a built-in avatar as a standalone image instead of a sprite crop", () => {
    render(<AvatarDisplay value="builtin:avatar-03" name="写作专家"/>);

    const avatar = screen.getByRole("img", { name: "写作专家的头像" });
    expect(avatar.tagName).toBe("IMG");
    expect(avatar).toHaveAttribute("src", expect.stringContaining("individual/avatar-03"));
  });
});
