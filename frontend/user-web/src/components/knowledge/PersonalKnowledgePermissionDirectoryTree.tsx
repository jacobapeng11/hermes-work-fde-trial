import { useEffect, useMemo, useRef, useState } from "react";
import { Input } from "antd";
import { CaretDownOutlined, CaretRightOutlined, FolderOutlined, SearchOutlined, UserOutlined } from "@ant-design/icons";
import type {
  PersonalKnowledgePermissionDirectoryNode,
  PersonalKnowledgePermissionGrant
} from "@hermes-cloud/schemas";
import { userApi } from "../../api/client";
import {
  permissionTargetKey,
  personalKnowledgePermissionLabels,
  type PersonalKnowledgePermissionTargetOption
} from "./personalKnowledgePermissionModel";

interface Props {
  nodes: PersonalKnowledgePermissionDirectoryNode[];
  selectedKeys: string[];
  grants: PersonalKnowledgePermissionGrant[];
  onChange: (targets: PersonalKnowledgePermissionTargetOption[]) => void;
  focusTargetKey?: string | null;
}

interface VisibleNode {
  node: PersonalKnowledgePermissionDirectoryNode;
  path: string[];
  children: VisibleNode[];
}

function nodeKey(node: PersonalKnowledgePermissionDirectoryNode): string {
  if (node.nodeType === "department") return `department:${node.departmentId}`;
  return node.userId != null
    ? `user:${node.userId}`
    : `user:unprovisioned:${node.userCode ?? node.name}`;
}

function toTarget(node: PersonalKnowledgePermissionDirectoryNode, parentPath: string[]): PersonalKnowledgePermissionTargetOption {
  if (node.nodeType === "user") {
    if (node.userId == null) {
      // flattenTargets 已跳过未开户人员；此处仅作类型收敛兜底。
      throw new Error("unprovisioned directory user cannot become a permission target");
    }
    return {
      targetType: "user",
      targetId: node.userId,
      name: node.name,
      userCode: node.userCode,
      departmentId: node.departmentId,
      departmentPath: parentPath,
      status: node.status
    };
  }
  return {
    targetType: "department",
    targetId: node.departmentId,
    name: node.name,
    departmentPath: [...parentPath, node.name]
  };
}

function flattenTargets(
  nodes: PersonalKnowledgePermissionDirectoryNode[],
  parentPath: string[] = [],
  result = new Map<string, PersonalKnowledgePermissionTargetOption>()
): Map<string, PersonalKnowledgePermissionTargetOption> {
  nodes.forEach((node) => {
    if (node.nodeType === "user" && node.userId == null) {
      // 未开户人员仅展示，不进入可授权目标。
      return;
    }
    result.set(nodeKey(node), toTarget(node, parentPath));
    if (node.nodeType === "department") {
      flattenTargets(node.children, [...parentPath, node.name], result);
    }
  });
  return result;
}

// provision 过的未开户人员不在 flattenTargets 产出里（userId 为空被跳过）；
// 若不按 provisionedCodes 补齐，后续按 `user:${provisionedId}` toggle 时解析不到目标，
// 表现为“首次勾选（provision 分支直接构造）成功、取消后再勾选被静默丢弃”。
function applyProvisionedTargets(
  nodes: PersonalKnowledgePermissionDirectoryNode[],
  provisionedCodes: Map<string, number>,
  parentPath: string[],
  result: Map<string, PersonalKnowledgePermissionTargetOption>
) {
  nodes.forEach((node) => {
    if (node.nodeType === "department") {
      applyProvisionedTargets(node.children, provisionedCodes, [...parentPath, node.name], result);
      return;
    }
    if (node.userId != null) return;
    const provisionedId = provisionedCodes.get(node.userCode ?? "");
    if (provisionedId == null) return;
    result.set(`user:${provisionedId}`, {
      targetType: "user",
      targetId: provisionedId,
      name: node.name,
      userCode: node.userCode,
      departmentId: node.departmentId,
      departmentPath: parentPath,
      status: "active"
    });
  });
}

function filterNodes(
  nodes: PersonalKnowledgePermissionDirectoryNode[],
  query: string,
  parentPath: string[] = []
): VisibleNode[] {
  return nodes.flatMap((node) => {
    const currentPath = node.nodeType === "department" ? [...parentPath, node.name] : parentPath;
    const children = node.nodeType === "department" ? filterNodes(node.children, query, currentPath) : [];
    const haystack = node.nodeType === "user"
      ? `${node.name} ${node.userCode || ""}`.toLowerCase()
      : node.name.toLowerCase();
    if (query && !haystack.includes(query) && children.length === 0) return [];
    return [{ node, path: parentPath, children }];
  });
}

function ancestorDepartmentKeys(
  nodes: PersonalKnowledgePermissionDirectoryNode[],
  targetKey: string,
  ancestors: string[] = []
): string[] | null {
  for (const node of nodes) {
    if (nodeKey(node) === targetKey) return ancestors;
    if (node.nodeType === "department") {
      const found = ancestorDepartmentKeys(node.children, targetKey, [...ancestors, nodeKey(node)]);
      if (found) return found;
    }
  }
  return null;
}

export function PersonalKnowledgePermissionDirectoryTree({
  nodes,
  selectedKeys,
  grants,
  onChange,
  focusTargetKey = null
}: Props) {
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [provisionedCodes, setProvisionedCodes] = useState<Map<string, number>>(new Map());
  const [provisionError, setProvisionError] = useState<string | null>(null);
  const provisioningRef = useRef(false);
  const nodeRefs = useRef(new Map<string, HTMLInputElement>());
  const targets = useMemo(() => {
    const map = flattenTargets(nodes);
    applyProvisionedTargets(nodes, provisionedCodes, [], map);
    return map;
  }, [nodes, provisionedCodes]);
  const grantByKey = useMemo(() => new Map(grants.map((grant) => [permissionTargetKey(grant), grant])), [grants]);
  const visibleNodes = useMemo(() => filterNodes(nodes, query.trim().toLowerCase()), [nodes, query]);

  useEffect(() => {
    if (nodes.length === 0 || grants.length === 0) return;
    const required = new Set<string>();
    grants.forEach((grant) => {
      const ancestors = ancestorDepartmentKeys(nodes, permissionTargetKey(grant));
      ancestors?.forEach((key) => required.add(key));
    });
    if (required.size === 0) return;
    setExpanded((current) => new Set([...current, ...required]));
  }, [nodes, grants]);

  useEffect(() => {
    if (!focusTargetKey) return;
    const ancestors = ancestorDepartmentKeys(nodes, focusTargetKey);
    if (ancestors) setExpanded((current) => new Set([...current, ...ancestors]));
    const timer = window.setTimeout(() => {
      const input = nodeRefs.current.get(focusTargetKey);
      input?.scrollIntoView?.({ block: "nearest" });
      input?.focus();
    });
    return () => window.clearTimeout(timer);
  }, [focusTargetKey, nodes]);

  const resolveUserId = (node: PersonalKnowledgePermissionDirectoryNode): number | null => {
    if (node.nodeType !== "user") return null;
    if (node.userId != null) return node.userId;
    const code = node.userCode ?? "";
    return provisionedCodes.get(code) ?? null;
  };

  const toggleSelected = (key: string) => {
    const nextKeys = selectedKeys.includes(key)
      ? selectedKeys.filter((item) => item !== key)
      : [...selectedKeys, key];
    onChange(nextKeys.flatMap((item) => {
      const target = targets.get(item);
      return target ? [target] : [];
    }));
  };

  const handleToggleNode = async (node: PersonalKnowledgePermissionDirectoryNode, key: string) => {
    if (node.nodeType === "user" && resolveUserId(node) == null) {
      const code = node.userCode ?? "";
      if (!code) return;
      // provision 请求飞行中 checkbox 无即时反馈，重复点击会并发触发并重复追加选择。
      if (provisioningRef.current) return;
      provisioningRef.current = true;
      setProvisionError(null);
      try {
        const [provisioned] = await userApi.provisionDirectoryUsers([code]);
        if (!provisioned) return;
        setProvisionedCodes((current) => new Map(current).set(code, provisioned.id));
        const currentTargets = selectedKeys.flatMap((item) => {
          const target = targets.get(item);
          return target ? [target] : [];
        });
        onChange([
          ...currentTargets.filter((item) => item.targetType !== "user" || item.targetId !== provisioned.id),
          {
            targetType: "user",
            targetId: provisioned.id,
            name: provisioned.name,
            userCode: provisioned.userCode,
            departmentId: node.nodeType === "user" ? node.departmentId ?? null : null,
            departmentPath: [],
            status: "active"
          }
        ]);
      } catch (reason) {
        setProvisionError(reason instanceof Error ? reason.message : "预创建账号失败，请稍后重试");
      } finally {
        provisioningRef.current = false;
      }
      return;
    }
    toggleSelected(key);
  };

  const toggleExpanded = (key: string) => setExpanded((current) => {
    const next = new Set(current);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  const renderNodes = (items: VisibleNode[], depth = 0): JSX.Element[] => items.map(({ node, path, children }) => {
    const resolvedUserId = resolveUserId(node);
    const key = node.nodeType === "user" && resolvedUserId == null
      ? nodeKey(node)
      : node.nodeType === "user"
        ? `user:${resolvedUserId}`
        : nodeKey(node);
    const hasChildren = node.nodeType === "department" && node.children.length > 0;
    const showChildren = Boolean(query.trim()) || expanded.has(key);
    const grant = grantByKey.get(key);
    const label = node.nodeType === "user" && node.userCode ? `${node.name}（${node.userCode}）` : node.name;
    const isVirtualDepartment = node.nodeType === "department" && node.virtual === true;
    const isUnactivatedUser = node.nodeType === "user" && node.loggedIn !== true;
    return <li key={key}>
      <div className="pkbPermissionTreeRow" style={{ paddingLeft: `${depth * 22 + 8}px` }}>
        {hasChildren
          ? <button type="button" className="pkbPermissionTreeToggle" aria-label={`${showChildren ? "收起" : "展开"} ${node.name}`} onClick={() => toggleExpanded(key)}>{showChildren ? <CaretDownOutlined aria-hidden /> : <CaretRightOutlined aria-hidden />}</button>
          : <span className="pkbPermissionTreeSpacer"/>}
        {node.nodeType === "department" ? <FolderOutlined aria-hidden="true"/> : <UserOutlined aria-hidden="true"/>}
        {isVirtualDepartment
          ? <label className="pkbPermissionVirtualNode" aria-disabled="true"><span>{label}</span><small>分组节点，不可选择</small></label>
          : <label>
            <input
              ref={(element) => { if (element) nodeRefs.current.set(key, element); else nodeRefs.current.delete(key); }}
              type="checkbox"
              checked={selectedKeys.includes(key)}
              onChange={() => void handleToggleNode(node, key)}
            />
            <span>{label}</span>
            {isUnactivatedUser && <small className="pkbPermissionUnprovisioned" title="勾选后将自动创建平台账号；标记在用户首次登录后消失">未激活</small>}
          </label>}
        {grant && <span className="pkbPermissionTreeGrant">{personalKnowledgePermissionLabels[grant.role]}</span>}
      </div>
      {node.nodeType === "department" && hasChildren && showChildren && <ul>{renderNodes(children, depth + 1)}</ul>}
    </li>;
  });

  return <div className="pkbPermissionTree">
    {provisionError && <p className="pkbPermissionUserState" role="alert">{provisionError}</p>}
    <div className="pkbPermissionTreeShell">
      <div className="pkbPermissionTreeSearch">
        <Input
          variant="borderless"
          size="small"
          type="search"
          aria-label="搜索部门或人员"
          placeholder="搜索部门或人员"
          prefix={<SearchOutlined aria-hidden />}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      <div className="pkbPermissionTreeViewport">
        {visibleNodes.length > 0
          ? <ul className="pkbPermissionTreeNodes">{renderNodes(visibleNodes)}</ul>
          : <p className="pkbPermissionEmpty">未找到匹配的部门或人员</p>}
      </div>
    </div>
  </div>;
}
