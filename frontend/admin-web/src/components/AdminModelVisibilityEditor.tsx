import { useEffect, useMemo, useState } from "react";
import {
  Building2,
  ChevronDown,
  ChevronRight,
  Folder,
  Search,
  UserRound,
  X
} from "lucide-react";
import { adminApi } from "../api/client";
import type {
  AdminModelVisibilityUserSummary,
  ModelVisibilityScope,
  OrganizationDirectoryNode
} from "@hermes-cloud/schemas";
import "./AdminModelVisibilityEditor.css";

interface Props {
  value: ModelVisibilityScope;
  selectedUsers: AdminModelVisibilityUserSummary[];
  directory: OrganizationDirectoryNode[];
  loading: boolean;
  error: string | null;
  onRetry?: () => void;
  onChange: (
    value: ModelVisibilityScope,
    selectedUsers: AdminModelVisibilityUserSummary[]
  ) => void;
  disabled?: boolean;
}

interface NodeMeta {
  node: OrganizationDirectoryNode;
  ancestorDepartmentIds: string[];
  ancestorKeys: string[];
}

interface VisibilitySummaryItem {
  key: string;
  label: string;
  fallback: boolean;
}

function nodeKey(node: OrganizationDirectoryNode): string {
  if (node.nodeType === "organization") {
    return `organization:${node.organizationId}`;
  }
  if (node.nodeType === "department") {
    return `department:${node.departmentId}`;
  }
  return node.userId != null
    ? `user:${node.userId}`
    : `user:unprovisioned:${node.userCode ?? node.name}`;
}

function flattenDirectory(
  nodes: OrganizationDirectoryNode[],
  ancestorDepartmentIds: string[] = [],
  ancestorKeys: string[] = [],
  result = new Map<string, NodeMeta>()
): Map<string, NodeMeta> {
  nodes.forEach((node) => {
    const key = nodeKey(node);
    result.set(key, { node, ancestorDepartmentIds, ancestorKeys });
    if (node.nodeType !== "user") {
      const nextDepartments = node.nodeType === "department"
        ? [...ancestorDepartmentIds, node.departmentId]
        : ancestorDepartmentIds;
      flattenDirectory(
        node.children,
        nextDepartments,
        [...ancestorKeys, key],
        result
      );
    }
  });
  return result;
}

export function normalizeVisibilitySelection(
  value: ModelVisibilityScope,
  directory: OrganizationDirectoryNode[]
): ModelVisibilityScope {
  if (value.organization || (!value.departmentIds.length && !value.userIds.length)) {
    return { organization: true, departmentIds: [], userIds: [] };
  }
  const metadata = flattenDirectory(directory);
  const selectedDepartments = new Set(
    value.departmentIds.filter((departmentId) => {
      const node = metadata.get(`department:${departmentId}`)?.node;
      return node?.nodeType === "department" && node.virtual !== true;
    })
  );
  const departmentIds = [...selectedDepartments]
    .filter((departmentId) => {
      const ancestors = metadata.get(`department:${departmentId}`)?.ancestorDepartmentIds ?? [];
      return !ancestors.some((ancestor) => selectedDepartments.has(ancestor));
    })
    .sort();
  const effectiveDepartments = new Set(departmentIds);
  const userIds = [...new Set(value.userIds)]
    .filter((userId) => {
      const ancestors = metadata.get(`user:${userId}`)?.ancestorDepartmentIds ?? [];
      return !ancestors.some((ancestor) => effectiveDepartments.has(ancestor));
    })
    .sort((a, b) => a - b);
  return { organization: false, departmentIds, userIds };
}

function scopeKeys(
  value: ModelVisibilityScope,
  directory: OrganizationDirectoryNode[]
): string[] {
  const normalized = normalizeVisibilitySelection(value, directory);
  if (normalized.organization) return [];
  return [
    ...normalized.departmentIds.map((departmentId) => `department:${departmentId}`),
    ...normalized.userIds.map((userId) => `user:${userId}`)
  ];
}

function filteredDirectory(
  nodes: OrganizationDirectoryNode[],
  query: string
): OrganizationDirectoryNode[] {
  if (!query) return nodes;
  return nodes.flatMap((node) => {
    const children = node.nodeType === "user"
      ? []
      : filteredDirectory(node.children, query);
    const haystack = node.nodeType === "user"
      ? `${node.name} ${node.userCode || ""}`
      : node.name;
    if (!haystack.toLocaleLowerCase().includes(query) && children.length === 0) {
      return [];
    }
    return [node.nodeType === "user" ? node : { ...node, children }];
  });
}

function selectedAncestorKeys(
  keys: string[],
  metadata: Map<string, NodeMeta>
): string[] {
  const result = new Set<string>();
  keys.forEach((key) => {
    (metadata.get(key)?.ancestorKeys ?? []).forEach((ancestor) => {
      result.add(ancestor);
    });
  });
  return [...result];
}

function scopeSummary(value: ModelVisibilityScope): string {
  if (value.organization || (!value.departmentIds.length && !value.userIds.length)) {
    return "当前：全员可见";
  }
  const parts: string[] = [];
  if (value.departmentIds.length) parts.push(`${value.departmentIds.length} 个部门`);
  if (value.userIds.length) parts.push(`${value.userIds.length} 名人员`);
  return `当前：${parts.join(" / ")}`;
}

function departmentSummaryItems(
  departmentIds: string[],
  metadata: Map<string, NodeMeta>,
  loading: boolean,
  error: string | null
): VisibilitySummaryItem[] {
  return departmentIds.map((departmentId) => {
    const node = metadata.get(`department:${departmentId}`)?.node;
    if (node?.nodeType === "department") {
      return { key: `department:${departmentId}`, label: node.name, fallback: false };
    }
    if (loading) {
      return {
        key: `department:${departmentId}`,
        label: "部门名称加载中",
        fallback: true
      };
    }
    if (error) {
      return {
        key: `department:${departmentId}`,
        label: "部门名称暂不可用",
        fallback: true
      };
    }
    return {
      key: `department:${departmentId}`,
      label: `未知部门（${departmentId}）`,
      fallback: true
    };
  });
}

function selectedUserLabel(user: AdminModelVisibilityUserSummary): string {
  if (user.status === "active") {
    return user.userCode ? `${user.name}（${user.userCode}）` : user.name;
  }
  return user.userCode
    ? `${user.name}（${user.userCode}，已失效）`
    : `${user.name}（已失效）`;
}

function userSummaryItems(
  userIds: number[],
  metadata: Map<string, NodeMeta>,
  selectedUsers: AdminModelVisibilityUserSummary[]
): VisibilitySummaryItem[] {
  const selectedById = new Map(selectedUsers.map((user) => [user.id, user]));
  return userIds.map((userId) => {
    const node = metadata.get(`user:${userId}`)?.node;
    if (node?.nodeType === "user") {
      return {
        key: `user:${userId}`,
        label: node.userCode ? `${node.name}（${node.userCode}）` : node.name,
        fallback: false
      };
    }
    const selected = selectedById.get(userId);
    if (selected) {
      return {
        key: `user:${userId}`,
        label: selectedUserLabel(selected),
        fallback: selected.status !== "active"
      };
    }
    return {
      key: `user:${userId}`,
      label: `未知人员（${userId}）`,
      fallback: true
    };
  });
}

function selectedUserSummaries(
  userIds: number[],
  metadata: Map<string, NodeMeta>,
  previousUsers: AdminModelVisibilityUserSummary[]
): AdminModelVisibilityUserSummary[] {
  const previousById = new Map(previousUsers.map((user) => [user.id, user]));
  return userIds.map((userId) => {
    const node = metadata.get(`user:${userId}`)?.node;
    if (node?.nodeType === "user") {
      return {
        id: userId,
        name: node.name,
        userCode: node.userCode,
        status: node.status
      };
    }
    return previousById.get(userId) ?? {
      id: userId,
      name: `用户 ${userId}`,
      userCode: null,
      status: "inactive"
    };
  });
}

export function AdminModelVisibilityEditor({
  value,
  selectedUsers,
  directory,
  loading,
  error,
  onRetry,
  onChange,
  disabled = false
}: Props) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [draftKeys, setDraftKeys] = useState<string[]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [draftUserIds, setDraftUserIds] = useState<number[]>([]);
  const [draftUserLabels, setDraftUserLabels] = useState<Map<number, { name: string; userCode?: string | null }>>(new Map());
  const [userQuery, setUserQuery] = useState("");
  const [userOptions, setUserOptions] = useState<{ id: number; name: string; userCode?: string | null }[]>([]);
  const [userSearching, setUserSearching] = useState(false);
  const [provisionedCodes, setProvisionedCodes] = useState<Map<string, number>>(new Map());
  const [provisionError, setProvisionError] = useState<string | null>(null);
  const metadata = useMemo(() => flattenDirectory(directory), [directory]);
  const visibleDirectory = useMemo(
    () => filteredDirectory(directory, query.trim().toLocaleLowerCase()),
    [directory, query]
  );
  const organizationKey = directory[0]?.nodeType === "organization"
    ? nodeKey(directory[0])
    : "organization:current";
  const customScope = !value.organization
    && (value.departmentIds.length > 0 || value.userIds.length > 0);
  const summaryDepartments = customScope
    ? departmentSummaryItems(value.departmentIds, metadata, loading, error)
    : [];
  const summaryUsers = customScope
    ? userSummaryItems(value.userIds, metadata, selectedUsers)
    : [];

  useEffect(() => {
    if (!dialogOpen) return;
    setExpanded((current) => new Set([
      ...current,
      organizationKey,
      ...selectedAncestorKeys(draftKeys, metadata)
    ]));
  }, [dialogOpen, draftKeys, metadata, organizationKey]);

  useEffect(() => {
    const keyword = userQuery.trim();
    if (!keyword) {
      setUserOptions([]);
      return;
    }
    let active = true;
    const timer = window.setTimeout(() => {
      setUserSearching(true);
      void adminApi.searchDirectoryUsers(keyword, 20).then((items) => {
        if (active) setUserOptions(items);
      }).catch(() => {
        if (active) setUserOptions([]);
      }).finally(() => {
        if (active) setUserSearching(false);
      });
    }, 300);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [userQuery]);

  function openDialog() {
    const keys = scopeKeys(value, directory);
    setDraftKeys(keys);
    setExpanded(new Set([
      organizationKey,
      ...selectedAncestorKeys(keys, metadata)
    ]));
    setQuery("");
    setUserQuery("");
    setUserOptions([]);
    setDraftUserIds(value.organization ? [] : [...value.userIds]);
    setDraftUserLabels(
      new Map(selectedUsers.map((user) => [user.id, { name: user.name, userCode: user.userCode }]))
    );
    setDialogOpen(true);
  }

  function addDraftUser(item: { id: number; name: string; userCode?: string | null }) {
    setDraftUserIds((current) => current.includes(item.id) ? current : [...current, item.id]);
    setDraftUserLabels((current) => new Map(current).set(item.id, { name: item.name, userCode: item.userCode }));
  }

  function removeDraftUser(userId: number) {
    setDraftUserIds((current) => current.filter((item) => item !== userId));
    setDraftKeys((current) => current.filter((item) => item !== `user:${userId}`));
  }

  function closeDialog() {
    setDialogOpen(false);
    setQuery("");
  }

  function resolveUserId(node: OrganizationDirectoryNode): number | null {
    if (node.nodeType !== "user") return null;
    if (node.userId != null) return node.userId;
    const code = node.userCode ?? "";
    return provisionedCodes.get(code) ?? null;
  }

  async function handleToggleDraftKey(node: OrganizationDirectoryNode, key: string) {
    if (node.nodeType === "user" && resolveUserId(node) == null) {
      const code = node.userCode ?? "";
      if (!code) return;
      setProvisionError(null);
      try {
        const [provisioned] = await adminApi.provisionDirectoryUsers([code]);
        if (!provisioned) return;
        setProvisionedCodes((current) => new Map(current).set(code, provisioned.id));
        setDraftUserIds((current) => current.includes(provisioned.id) ? current : [...current, provisioned.id]);
        setDraftUserLabels((current) => new Map(current).set(provisioned.id, { name: provisioned.name, userCode: provisioned.userCode }));
      } catch (reason) {
        setProvisionError(reason instanceof Error ? reason.message : "预创建账号失败，请稍后重试");
      }
      return;
    }
    toggleDraftKey(key);
  }

  function toggleDraftKey(key: string) {
    const selected = draftKeys.includes(key);
    const next = selected
      ? draftKeys.filter((item) => item !== key)
      : [...draftKeys, key];
    const departmentIds = next
      .filter((item) => item.startsWith("department:"))
      .map((item) => item.slice("department:".length));
    const userIds = next
      .filter((item) => item.startsWith("user:"))
      .map((item) => Number(item.slice("user:".length)));
    const normalized = normalizeVisibilitySelection(
      { organization: false, departmentIds, userIds },
      directory
    );
    setDraftKeys(scopeKeys(normalized, directory));
  }

  function confirmDialog() {
    if (!draftKeys.length && !draftUserIds.length) {
      onChange({ organization: true, departmentIds: [], userIds: [] }, []);
      closeDialog();
      return;
    }
    const departmentIds = draftKeys
      .filter((item) => item.startsWith("department:"))
      .map((item) => item.slice("department:".length));
    const treeUserIds = draftKeys
      .filter((item) => item.startsWith("user:") && !item.startsWith("user:unprovisioned:"))
      .map((item) => Number(item.slice("user:".length)))
      .filter((id) => Number.isSafeInteger(id) && id > 0);
    const normalized = normalizeVisibilitySelection(
      { organization: false, departmentIds, userIds: [...new Set([...treeUserIds, ...draftUserIds])] },
      directory
    );
    // 搜索新增的人员摘要：优先用已有 selectedUsers，其次用本次搜索标签。
    const knownById = new Map(selectedUsers.map((user) => [user.id, user]));
    const nextSelectedUsers = normalized.userIds.map((userId) => {
      const known = knownById.get(userId);
      if (known) return known;
      const label = draftUserLabels.get(userId);
      return {
        id: userId,
        name: label?.name ?? `用户 ${userId}`,
        userCode: label?.userCode ?? null,
        status: "active" as const
      };
    });
    onChange(normalized, nextSelectedUsers);
    closeDialog();
  }

  function toggleExpanded(key: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function renderNodes(nodes: OrganizationDirectoryNode[], depth = 0) {
    return nodes.map((node) => {
      const resolvedUserId = resolveUserId(node);
      const key = node.nodeType === "user" && resolvedUserId != null
        ? `user:${resolvedUserId}`
        : nodeKey(node);
      const hasChildren = node.nodeType !== "user" && node.children.length > 0;
      const showChildren = Boolean(query.trim()) || expanded.has(key);
      const label = node.nodeType === "user" && node.userCode
        ? `${node.name}（${node.userCode}）`
        : node.name;
      const isVirtualDepartment = node.nodeType === "department" && node.virtual === true;
      const isUnactivatedUser = node.nodeType === "user" && node.loggedIn !== true;
      return (
        <li key={key}>
          <div style={{ paddingLeft: `${depth * 20 + 8}px` }}>
            {hasChildren
              ? (
                <button
                  type="button"
                  aria-label={`${showChildren ? "收起" : "展开"} ${node.name}`}
                  onClick={() => toggleExpanded(key)}
                >
                  {showChildren ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                </button>
              )
              : <span className="modelVisibilityTreeSpacer" />}
            {node.nodeType === "organization" ? <Building2 size={16} />
              : node.nodeType === "department" ? <Folder size={15} />
                : <UserRound size={15} />}
            {node.nodeType === "organization" ? (
              <span className="modelVisibilityOrganization">{label}</span>
            ) : isVirtualDepartment ? (
              <span className="modelVisibilityVirtualNode">
                {label}
                <small>分组节点，不可选择</small>
              </span>
            ) : (
              <label>
                <input
                  type="checkbox"
                  aria-label={label}
                  checked={draftKeys.includes(key) || (node.nodeType === "user" && resolvedUserId != null && draftUserIds.includes(resolvedUserId))}
                  disabled={disabled}
                  onChange={() => void handleToggleDraftKey(node, key)}
                />
                <span>{label}</span>
                {isUnactivatedUser && <small className="modelVisibilityUnprovisioned" title="勾选后将自动创建平台账号；标记在用户首次登录后消失">未激活</small>}
              </label>
            )}
          </div>
          {node.nodeType !== "user" && hasChildren && showChildren && (
            <ul>{renderNodes(node.children, depth + 1)}</ul>
          )}
        </li>
      );
    });
  }

  return (
    <div className="modelVisibilityEditor">
      <button
        type="button"
        className="modelVisibilityEntryButton"
        onClick={openDialog}
        disabled={disabled}
      >
        <strong>配置可见权限</strong>
        <span>{scopeSummary(value)}</span>
      </button>

      {customScope && (
        <div className="modelVisibilitySummary" aria-label="模型可见权限摘要">
          {summaryDepartments.length > 0 && (
            <div className="modelVisibilitySummaryGroup">
              <p>可见部门（{summaryDepartments.length}）</p>
              <ul>
                {summaryDepartments.map((item) => (
                  <li key={item.key} data-fallback={item.fallback}>
                    <Folder size={14} aria-hidden="true" />
                    <span>{item.label}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {summaryUsers.length > 0 && (
            <div className="modelVisibilitySummaryGroup">
              <p>可见人员（{summaryUsers.length}）</p>
              <ul>
                {summaryUsers.map((item) => (
                  <li key={item.key} data-fallback={item.fallback}>
                    <UserRound size={14} aria-hidden="true" />
                    <span>{item.label}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {dialogOpen && (
        <div
          className="modelVisibilityBackdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeDialog();
          }}
        >
          <section
            className="modelVisibilityDialog"
            role="dialog"
            aria-modal="true"
            aria-label="配置可见权限"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <header>
              <div>
                <h3>配置可见权限</h3>
                <p>选择部门或人员；清空全部选择表示全员可见</p>
              </div>
              <button type="button" aria-label="关闭" onClick={closeDialog}>
                <X size={18} />
              </button>
            </header>
            <div className="modelVisibilityDialogBody">
              <label className="modelVisibilityTreeSearch">
                <Search size={15} />
                <span className="srOnly">搜索部门或人员</span>
                <input
                  type="search"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="搜索部门、姓名或工号"
                />
              </label>
              {provisionError && (
                <p className="modelVisibilityState" role="alert">{provisionError}</p>
              )}
              <div className="modelVisibilitySelectionSummary">
                已选择 <strong>{draftKeys.length + draftUserIds.length}</strong> 个对象
              </div>
              {loading && (
                <p className="modelVisibilityState" role="status">
                  正在加载组织目录…
                </p>
              )}
              {!loading && error && (
                <div className="modelVisibilityState" role="alert">
                  <span>{error}</span>
                  {onRetry && (
                    <button type="button" onClick={onRetry}>重试</button>
                  )}
                </div>
              )}
              {!loading && !error && visibleDirectory.length > 0 && (
                <ul className="modelVisibilityTree">
                  {renderNodes(visibleDirectory)}
                </ul>
              )}
              {!loading && !error && visibleDirectory.length === 0 && (
                <p className="modelVisibilityState">未找到匹配的部门或人员</p>
              )}
              <div className="modelVisibilityUserPicker">
                <label className="modelVisibilityUserSearch">
                  <Search size={14} aria-hidden="true" />
                  <span className="srOnly">搜索人员</span>
                  <input
                    type="search"
                    value={userQuery}
                    onChange={(event) => setUserQuery(event.target.value)}
                    placeholder="按姓名或工号搜索人员"
                  />
                </label>
                {userSearching && <p className="modelVisibilityUserState" role="status">搜索中…</p>}
                {!userSearching && userQuery.trim() && userOptions.length === 0 && (
                  <p className="modelVisibilityUserState">没有匹配的平台用户</p>
                )}
                {!userSearching && userOptions.length > 0 && (
                  <ul className="modelVisibilityUserOptions">
                    {userOptions.map((item) => (
                      <li key={item.id}>
                        <button
                          type="button"
                          disabled={draftUserIds.includes(item.id)}
                          onClick={() => addDraftUser(item)}
                        >
                          <UserRound size={14} aria-hidden="true" />
                          <span>{item.userCode ? `${item.name}（${item.userCode}）` : item.name}</span>
                          {draftUserIds.includes(item.id) ? <small>已选</small> : null}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                {draftUserIds.length > 0 && (
                  <ul className="modelVisibilitySelectedUsers">
                    {draftUserIds.map((userId) => {
                      const label = draftUserLabels.get(userId);
                      const name = label
                        ? (label.userCode ? `${label.name}（${label.userCode}）` : label.name)
                        : `用户 ${userId}`;
                      return (
                        <li key={userId}>
                          <span><UserRound size={14} aria-hidden="true" />{name}</span>
                          <button
                            type="button"
                            onClick={() => removeDraftUser(userId)}
                            aria-label={`移除 ${name}`}
                          >×</button>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </div>
            </div>
            <footer>
              <button type="button" className="secondary" onClick={closeDialog}>
                取消
              </button>
              <button
                type="button"
                className="primary"
                onClick={confirmDialog}
                disabled={disabled || loading || Boolean(error)}
              >
                确认
              </button>
            </footer>
          </section>
        </div>
      )}
    </div>
  );
}
