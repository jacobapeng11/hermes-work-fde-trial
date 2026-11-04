import { describe, expect, it } from "vitest";
import type {
  PersonalKnowledgePermissionGrant,
  PersonalKnowledgePermissionReplace
} from "@hermes-cloud/schemas";
import {
  applyPermissionRole,
  commonPermissionRole,
  grantsToPermissionReplace,
  personalKnowledgePermissionLabels,
  type PersonalKnowledgePermissionTargetOption
} from "./personalKnowledgePermissionModel";

const department: PersonalKnowledgePermissionTargetOption = {
  targetType: "department",
  targetId: "d1",
  name: "研发中心",
  departmentPath: ["总部", "研发中心"]
};
const user: PersonalKnowledgePermissionTargetOption = {
  targetType: "user",
  targetId: 12,
  name: "示例用户乙",
  userCode: "zhangyi",
  departmentId: "d1",
  departmentPath: ["总部", "研发中心"],
  status: "active"
};

describe("personalKnowledgePermissionModel", () => {
  it("uses the requirement permission labels", () => {
    expect(personalKnowledgePermissionLabels).toEqual({
      download: "可导出 / 预览 / 引用",
      preview: "仅预览和引用",
      retrieve: "仅引用"
    });
  });

  it("leaves mixed or partially unauthorized selections without a common role", () => {
    const grants: PersonalKnowledgePermissionGrant[] = [
      { ...department, role: "download" },
      { ...user, role: "preview" }
    ];
    expect(commonPermissionRole([department, user], grants)).toBeNull();
    expect(commonPermissionRole([department], grants)).toBe("download");
    expect(commonPermissionRole([department, { ...user, targetId: 99 }], grants)).toBeNull();
  });

  it("applies one role to all selected targets", () => {
    const updated = applyPermissionRole([], [department, user], "retrieve");
    expect(updated).toEqual([
      { ...department, role: "retrieve" },
      { ...user, role: "retrieve" }
    ]);
  });

  it("converts grants to buckets and preserves legacy all flags", () => {
    const preserved: PersonalKnowledgePermissionReplace = {
      download: { userIds: [], departmentIds: [], all: true },
      preview: { userIds: [], departmentIds: [], all: false },
      retrieve: { userIds: [], departmentIds: [], all: false }
    };
    const result = grantsToPermissionReplace(
      [{ ...department, role: "preview" }, { ...user, role: "retrieve" }],
      preserved
    );
    expect(result).toEqual({
      download: { userIds: [], departmentIds: [], all: true },
      preview: { userIds: [], departmentIds: ["d1"], all: false },
      retrieve: { userIds: [12], departmentIds: [], all: false }
    });
  });
});
