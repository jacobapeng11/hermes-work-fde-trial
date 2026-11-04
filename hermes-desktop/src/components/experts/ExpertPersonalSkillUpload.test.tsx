import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { userApi } from "../../api/client";
import { ExpertPersonalSkillUpload } from "./ExpertPersonalSkillUpload";


vi.mock("../../api/client", () => ({
  userApi: { uploadPersonalSkill: vi.fn() }
}));

describe("ExpertPersonalSkillUpload", () => {
  beforeEach(() => {
    vi.mocked(userApi.uploadPersonalSkill).mockResolvedValue({
      skillId: 61,
      skillVersionId: 81,
      slug: "my-skill",
      version: 1,
      name: "My Skill",
      status: "private",
      source: "personal_asset",
      applied: false
    });
  });

  it("uploads a ZIP as a personal Skill and refreshes the capability catalog", async () => {
    const onUploaded = vi.fn();
    render(<ExpertPersonalSkillUpload onUploaded={onUploaded}/>);
    const file = new File(["zip"], "my-skill.zip", { type: "application/zip" });

    await userEvent.click(screen.getByRole("button", { name: /上传个人技能/ }));
    fireEvent.change(screen.getByLabelText("选择专家个人技能 zip 包"), {
      target: { files: [file] }
    });
    await userEvent.click(screen.getByRole("button", { name: "开始上传" }));

    await waitFor(() => expect(userApi.uploadPersonalSkill).toHaveBeenCalledWith({
      file,
      categoryKey: "uncategorized",
    }));
    expect(onUploaded).toHaveBeenCalledWith(expect.objectContaining({ skillId: 61, skillVersionId: 81 }));
    expect(await screen.findByRole("status")).toHaveTextContent("已选入当前专家");
  });
});
