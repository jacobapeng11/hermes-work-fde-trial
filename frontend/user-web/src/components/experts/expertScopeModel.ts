import type { ExpertShareScope, OrganizationDirectoryNode } from "@hermes-cloud/schemas";

// 看板区分享范围（ExpertShareScope ⇄ antd Tree checkedKeys）的纯函数模型。
// 规范化语义逐条复制自 ExpertVisibilityEditor（T5 起看板区改用本模型 + antd Tree，
// ExpertVisibilityEditor 原样留给专家模块）：
// - 勾选组织根短路为 { organization: true, departmentIds: [], userIds: [] }
// - 公司根使用真实 com_id，是可勾选的合法部门目标；virtual:* 合成节点不可提交
// - 部门祖先已选时去冗余后代；userIds 去重
// - 未 provision 人员（无 userId）不进入 scope——组件层负责先 provision 再入草稿

export interface ScopeNodeMeta {
  node: OrganizationDirectoryNode;
  ancestorDepartmentIds: string[];
  ancestorKeys: string[];
}

export function scopeNodeKey(node: OrganizationDirectoryNode): string {
  if (node.nodeType === "organization") return `organization:${node.organizationId}`;
  if (node.nodeType === "department") return `department:${node.departmentId}`;
  return node.userId != null
    ? `user:${node.userId}`
    : `user:unprovisioned:${node.userCode ?? node.name}`;
}

export function flattenScopeDirectory(
  nodes: OrganizationDirectoryNode[],
  ancestorDepartmentIds: string[] = [],
  ancestorKeys: string[] = [],
  result = new Map<string, ScopeNodeMeta>()
): Map<string, ScopeNodeMeta> {
  nodes.forEach((node) => {
    const key = scopeNodeKey(node);
    result.set(key, { node, ancestorDepartmentIds, ancestorKeys });
    if (node.nodeType !== "user") {
      const nextAncestors = node.nodeType === "department"
        ? [...ancestorDepartmentIds, node.departmentId]
        : ancestorDepartmentIds;
      flattenScopeDirectory(node.children, nextAncestors, [...ancestorKeys, key], result);
    }
  });
  return result;
}

export function scopeToCheckedKeys(
  scope: ExpertShareScope,
  directory: OrganizationDirectoryNode[]
): string[] {
  if (scope.organization) {
    const organization = directory.find((node) => node.nodeType === "organization");
    return organization?.nodeType === "organization"
      ? [`organization:${organization.organizationId}`]
      : ["organization:current"];
  }
  return [
    ...scope.departmentIds.map((id) => `department:${id}`),
    ...scope.userIds.map((id) => `user:${id}`)
  ];
}

export function checkedKeysToScope(
  keys: Iterable<string>,
  metas: Map<string, ScopeNodeMeta>
): ExpertShareScope {
  const selected = [...new Set(keys)];
  if (selected.some((key) => key.startsWith("organization:"))) {
    return { organization: true, departmentIds: [], userIds: [] };
  }
  const selectedDepartmentIds = new Set(
    selected
      .filter((key) => key.startsWith("department:"))
      .map((key) => key.slice("department:".length))
      // Defense-in-depth: never emit a synthetic directory node id into the share
      // scope. Company roots use their real com_id and remain valid targets.
      .filter((departmentId) => !departmentId.startsWith("virtual:"))
  );
  const departmentIds = selectedDepartmentIds.size
    ? [...selectedDepartmentIds].filter((departmentId) => {
        const meta = metas.get(`department:${departmentId}`);
        return !meta?.ancestorDepartmentIds.some((ancestor) => selectedDepartmentIds.has(ancestor));
      })
    : [];
  const effectiveDepartments = new Set(departmentIds);
  const selectedUserMetas = selected
    .filter((key) => key.startsWith("user:"))
    .map((key) => metas.get(key))
    .filter((meta): meta is ScopeNodeMeta => meta?.node.nodeType === "user")
    .filter((meta) => {
      const id = (meta.node as { userId?: number | null }).userId;
      return id != null
        && !meta.ancestorDepartmentIds.some((ancestor) => effectiveDepartments.has(ancestor));
    });
  const userIds = selectedUserMetas.map((meta) => (meta.node as { userId: number }).userId);
  return { organization: false, departmentIds, userIds: [...new Set(userIds)] };
}

export interface ScopeTreeNode {
  key: string;
  title: string;
  children?: ScopeTreeNode[];
  disabled?: boolean;
}

export function buildScopeTreeData(nodes: OrganizationDirectoryNode[]): ScopeTreeNode[] {
  return nodes.map((node) => {
    const key = scopeNodeKey(node);
    const isVirtualDepartment = node.nodeType === "department" && node.virtual === true;
    const base: ScopeTreeNode = {
      key,
      title: node.nodeType === "user" && node.userCode ? `${node.name}（${node.userCode}）` : node.name,
      disabled: isVirtualDepartment
    };
    if (node.nodeType !== "user" && node.children.length > 0) {
      base.children = buildScopeTreeData(node.children);
    }
    return base;
  });
}

export function filterScopeTreeData(
  nodes: OrganizationDirectoryNode[],
  query: string
): OrganizationDirectoryNode[] {
  if (!query) return nodes;
  const result: OrganizationDirectoryNode[] = [];
  nodes.forEach((node) => {
    const children = node.nodeType === "user" ? [] : filterScopeTreeData(node.children, query);
    const haystack = node.nodeType === "user"
      ? `${node.name} ${node.userCode || ""}`.toLocaleLowerCase()
      : node.name.toLocaleLowerCase();
    if (!haystack.includes(query) && children.length === 0) return;
    result.push(node.nodeType === "user" ? node : { ...node, children });
  });
  return result;
}
