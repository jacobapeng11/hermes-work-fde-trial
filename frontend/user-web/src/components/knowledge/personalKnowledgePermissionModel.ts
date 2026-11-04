import type {
  PersonalKnowledgePermissionDirectoryNode,
  PersonalKnowledgePermissionGrant,
  PersonalKnowledgePermissionReplace,
  PersonalKnowledgeRole
} from "@hermes-cloud/schemas";

export type SharedPersonalKnowledgeRole = Exclude<PersonalKnowledgeRole, "owner">;

export type PersonalKnowledgePermissionTargetOption =
  | {
      targetType: "department";
      targetId: string;
      name: string;
      departmentPath: string[];
    }
  | {
      targetType: "user";
      targetId: number;
      name: string;
      userCode?: string | null;
      departmentId?: string | null;
      departmentPath: string[];
      status: "active";
    };

export const personalKnowledgePermissionRoles: SharedPersonalKnowledgeRole[] = [
  "download",
  "preview",
  "retrieve"
];

export const personalKnowledgePermissionLabels: Record<SharedPersonalKnowledgeRole, string> = {
  download: "可导出 / 预览 / 引用",
  preview: "仅预览和引用",
  retrieve: "仅引用"
};

export function permissionTargetKey(
  target: Pick<PersonalKnowledgePermissionGrant, "targetType" | "targetId">
    | Pick<PersonalKnowledgePermissionTargetOption, "targetType" | "targetId">
): string {
  return `${target.targetType}:${target.targetId}`;
}

export function permissionDirectoryTargets(
  nodes: PersonalKnowledgePermissionDirectoryNode[],
  parentPath: string[] = []
): PersonalKnowledgePermissionTargetOption[] {
  return nodes.flatMap((node) => {
    if (node.nodeType === "user") {
      if (node.userId == null) return [];
      return [{
        targetType: "user" as const,
        targetId: node.userId,
        name: node.name,
        userCode: node.userCode,
        departmentId: node.departmentId,
        departmentPath: parentPath,
        status: node.status
      }];
    }
    const departmentPath = [...parentPath, node.name];
    return [{
      targetType: "department" as const,
      targetId: node.departmentId,
      name: node.name,
      departmentPath
    }, ...permissionDirectoryTargets(node.children, departmentPath)];
  });
}

export function commonPermissionRole(
  selected: PersonalKnowledgePermissionTargetOption[],
  grants: PersonalKnowledgePermissionGrant[]
): SharedPersonalKnowledgeRole | null {
  if (selected.length === 0) return null;
  const byKey = new Map(grants.map((grant) => [permissionTargetKey(grant), grant]));
  const roles = selected.map((target) => byKey.get(permissionTargetKey(target))?.role);
  if (roles.some((role) => !role)) return null;
  return roles.every((role) => role === roles[0]) ? roles[0] || null : null;
}

export function applyPermissionRole(
  grants: PersonalKnowledgePermissionGrant[],
  selected: PersonalKnowledgePermissionTargetOption[],
  role: SharedPersonalKnowledgeRole
): PersonalKnowledgePermissionGrant[] {
  const selectedKeys = new Set(selected.map(permissionTargetKey));
  const preserved = grants.filter((grant) => !selectedKeys.has(permissionTargetKey(grant)));
  const updated = selected.map((target): PersonalKnowledgePermissionGrant => target.targetType === "department"
    ? { ...target, role }
    : { ...target, role });
  return [...preserved, ...updated];
}

export function removePermissionGrant(
  grants: PersonalKnowledgePermissionGrant[],
  target: Pick<PersonalKnowledgePermissionGrant, "targetType" | "targetId">
): PersonalKnowledgePermissionGrant[] {
  const key = permissionTargetKey(target);
  return grants.filter((grant) => permissionTargetKey(grant) !== key);
}

export function grantsToPermissionReplace(
  grants: PersonalKnowledgePermissionGrant[],
  preserved: PersonalKnowledgePermissionReplace
): PersonalKnowledgePermissionReplace {
  const result: PersonalKnowledgePermissionReplace = {
    download: { userIds: [], departmentIds: [], all: preserved.download.all },
    preview: { userIds: [], departmentIds: [], all: preserved.preview.all },
    retrieve: { userIds: [], departmentIds: [], all: preserved.retrieve.all }
  };
  const seen = new Set<string>();
  personalKnowledgePermissionRoles.forEach((role) => {
    grants.filter((grant) => grant.role === role).forEach((grant) => {
      const key = permissionTargetKey(grant);
      if (seen.has(key)) return;
      seen.add(key);
      if (grant.targetType === "department") {
        result[role].departmentIds.push(grant.targetId);
      } else {
        result[role].userIds.push(grant.targetId);
      }
    });
  });
  return result;
}
