import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Input, Modal, Tag, Tree } from "antd";
import { PlusOutlined } from "@ant-design/icons";
import type { ExpertShareScope, OrganizationDirectoryNode } from "@hermes-cloud/schemas";
import { userApi } from "../../api/client";
import { usePlatformTheme } from "../../theme/platform-theme";
import {
  buildScopeTreeData,
  checkedKeysToScope,
  filterScopeTreeData,
  flattenScopeDirectory,
  scopeToCheckedKeys,
  type ScopeNodeMeta,
  type ScopeTreeNode
} from "./appScopeModel";

interface Props {
  scope: ExpertShareScope;
  onChange: (scope: ExpertShareScope) => void;
  directory: OrganizationDirectoryNode[];
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}

function collectExpandableKeys(nodes: OrganizationDirectoryNode[], result: string[] = []): string[] {
  nodes.forEach((node) => {
    if (node.nodeType !== "user") {
      if (node.children.length > 0) {
        if (node.nodeType === "organization") result.push(`organization:${node.organizationId}`);
        else result.push(`department:${node.departmentId}`);
      }
      collectExpandableKeys(node.children, result);
    }
  });
  return result;
}

function ancestorKeysOf(keys: string[], directory: OrganizationDirectoryNode[], metas: Map<string, ScopeNodeMeta>): string[] {
  const result = new Set<string>();
  for (const key of keys) {
    for (const ancestorKey of metas.get(key)?.ancestorKeys ?? []) result.add(ancestorKey);
  }
  const organization = directory.find((node) => node.nodeType === "organization");
  if (organization?.nodeType === "organization") result.add(`organization:${organization.organizationId}`);
  return [...result];
}

// 看板区分享范围编辑器（T5，替代 ExpertVisibilityEditor 的看板区用法）：
// 已选目标 Tag 行 + 「添加可见对象」树弹窗（antd Tree，确认前不改草稿）。
// 与知识库权限弹窗基线一致：无人员搜索双通道（树内含全量部门/人员）；
// 看板区特有契约保留——勾选未激活用户时先 provisionDirectoryUsers 开户再入草稿。
export function AppScopeEditor({ scope, onChange, directory, loading, error, onRetry }: Props) {
  const { theme } = usePlatformTheme();
  const [treeOpen, setTreeOpen] = useState(false);
  const [draftKeys, setDraftKeys] = useState<string[]>([]);
  const [treeQuery, setTreeQuery] = useState("");
  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);
  const [provisioning, setProvisioning] = useState(false);
  const [provisionError, setProvisionError] = useState<string | null>(null);
  // 本次会话内已 provision 的未激活用户（工号 → 真实 id）。
  // 回填进目录预处理：树节点 key、metas 与草稿统一使用真实 id，避免
  // 「树上勾选 key ≠ scope 规范化 key」导致确认时被静默丢弃。
  const [provisionedIdByCode, setProvisionedIdByCode] = useState<Map<string, number>>(new Map());

  const resolvedDirectory = useMemo(() => {
    if (provisionedIdByCode.size === 0) return directory;
    const walk = (nodes: OrganizationDirectoryNode[]): OrganizationDirectoryNode[] =>
      nodes.map((node) => {
        if (node.nodeType === "user" && node.userId == null) {
          const id = provisionedIdByCode.get(node.userCode ?? "");
          if (id != null) return { ...node, userId: id, loggedIn: true };
          return node;
        }
        if (node.nodeType !== "user") return { ...node, children: walk(node.children) };
        return node;
      });
    return walk(directory);
  }, [directory, provisionedIdByCode]);
  const metas = useMemo(() => flattenScopeDirectory(resolvedDirectory), [resolvedDirectory]);

  const query = treeQuery.trim().toLocaleLowerCase();
  const visibleDirectory = useMemo(
    () => filterScopeTreeData(resolvedDirectory, query),
    [resolvedDirectory, query]
  );
  const searching = query.length > 0;
  const effectiveExpandedKeys = searching ? collectExpandableKeys(visibleDirectory) : expandedKeys;

  const selectedTags = useMemo(() => {
    if (scope.organization) {
      const organization = directory.find((node) => node.nodeType === "organization");
      return [{
        key: "organization",
        label: organization?.nodeType === "organization" ? `${organization.name}（整个组织）` : "整个组织",
        onRemove: () => onChange({ organization: false, departmentIds: [], userIds: [] })
      }];
    }
    return [
      ...scope.departmentIds.map((id) => {
        const node = metas.get(`department:${id}`)?.node;
        return {
          key: `department:${id}`,
          label: node?.nodeType === "department" ? node.name : `部门 ${id}`,
          onRemove: () => onChange({ ...scope, departmentIds: scope.departmentIds.filter((item) => item !== id) })
        };
      }),
      ...scope.userIds.map((id) => {
        const node = metas.get(`user:${id}`)?.node;
        return {
          key: `user:${id}`,
          label: node?.nodeType === "user" ? node.name : `用户 ${id}`,
          onRemove: () => onChange({ ...scope, userIds: scope.userIds.filter((item) => item !== id) })
        };
      })
    ];
  }, [scope, directory, metas, onChange]);

  const openTree = () => {
    const keys = scopeToCheckedKeys(scope, directory);
    setDraftKeys(keys);
    setTreeQuery("");
    setProvisionError(null);
    setExpandedKeys(ancestorKeysOf(keys, directory, metas));
    setTreeOpen(true);
  };

  // antd Tree 父子联动与规范化语义兼容：勾祖先联动勾后代，确认时由
  // checkedKeysToScope 去冗余。未激活用户（无 userId）走乐观勾选：点击立即入草稿
  // 并显示勾选态，异步 provision 成功后把 key 替换为真实 id；失败只回退本次新增项。
  // （受控勾选若等 provision 才更新，点击会因异步延迟回弹，表现为「勾不上」。）
  const handleCheck = (checked: React.Key[] | { checked: React.Key[] }) => {
    const next = (Array.isArray(checked) ? checked : checked.checked).map(String);
    setProvisionError(null);
    const addedUnprovisioned = next.filter(
      (key) => !draftKeys.includes(key) && key.startsWith("user:unprovisioned:")
    );
    if (addedUnprovisioned.length === 0) {
      setDraftKeys(next);
      return;
    }
    setDraftKeys(next);
    const codes = addedUnprovisioned
      .map((key) => metas.get(key)?.node)
      .filter((node): node is OrganizationDirectoryNode & { nodeType: "user" } => node?.nodeType === "user")
      .map((node) => node.userCode ?? "")
      .filter(Boolean);
    setProvisioning(true);
    void (async () => {
      try {
        const provisioned = await userApi.provisionDirectoryUsers(codes);
        const idByCode = new Map(provisioned.map((item) => [item.userCode ?? "", item.id]));
        setProvisionedIdByCode((current) => {
          const merged = new Map(current);
          addedUnprovisioned.forEach((key, index) => {
            const code = key.slice("user:unprovisioned:".length);
            const byCode = idByCode.get(code);
            const byIndex = provisioned[index]?.id;
            const id = byCode ?? byIndex;
            if (id != null && code) merged.set(code, id);
          });
          return merged;
        });
        // 草稿里的 unprovisioned key 换成真实 id（树数据随 resolvedDirectory 同步重建）
        setDraftKeys((current) => current.map((key) => {
          if (!key.startsWith("user:unprovisioned:")) return key;
          const code = key.slice("user:unprovisioned:".length);
          const byCode = idByCode.get(code);
          if (byCode != null) return `user:${byCode}`;
          const index = addedUnprovisioned.indexOf(key);
          const byIndex = provisioned[index]?.id;
          return byIndex != null ? `user:${byIndex}` : key;
        }));
      } catch (reason) {
        setProvisionError(reason instanceof Error ? reason.message : "预创建账号失败，请稍后重试");
        setDraftKeys((current) => current.filter((key) => !addedUnprovisioned.includes(key)));
      } finally {
        setProvisioning(false);
      }
    })();
  };

  const confirmTree = () => {
    onChange(checkedKeysToScope(draftKeys, metas));
    setTreeOpen(false);
  };

  const titleRender = (node: ScopeTreeNode) => {
    const meta = metas.get(node.key);
    const isUnactivated = meta?.node.nodeType === "user" && meta.node.loggedIn !== true;
    return <span className="appScopeTreeTitle">{node.title}{isUnactivated && <small className="appScopeUnprovisioned" title="勾选后将自动创建平台账号；标记在用户首次登录后消失">未激活</small>}</span>;
  };

  return <div className="appScopeEditor">
    <div className="appScopeTags">
      {selectedTags.length > 0
        ? selectedTags.map((tag) => <Tag key={tag.key} closable onClose={tag.onRemove}>{tag.label}</Tag>)
        : <span className="appScopeEmptyHint">未选择分享范围，看板不会出现在他人的「看板广场」。</span>}
      <Button
        className="appScopeAdd"
        size="small"
        type="dashed"
        autoInsertSpace={false}
        icon={<PlusOutlined aria-hidden />}
        onClick={openTree}
      >添加可见对象</Button>
    </div>

    <Modal
      open={treeOpen}
      title="添加可见对象"
      centered
      width={520}
      rootClassName={theme === "dark" ? "appCenterAntDialog appCenterAntDialogDark" : "appCenterAntDialog"}
      className="appScopeTreeModal"
      closable={!provisioning}
      maskClosable={!provisioning}
      keyboard={!provisioning}
      onCancel={() => setTreeOpen(false)}
      footer={[
        <Button key="cancel" autoInsertSpace={false} disabled={provisioning} onClick={() => setTreeOpen(false)}>取消</Button>,
        <Button
          key="confirm"
          type="primary"
          autoInsertSpace={false}
          loading={provisioning}
          onClick={confirmTree}
        >确认</Button>
      ]}
    >
      <Input.Search
        className="appScopeTreeSearch"
        aria-label="搜索部门或人员"
        placeholder="搜索部门或人员"
        value={treeQuery}
        onChange={(event) => setTreeQuery(event.target.value)}
        allowClear
      />
      <div className="appScopeSelectionSummary">已选择 <strong>{draftKeys.length}</strong> 个可见对象</div>
      {provisionError && <Alert className="appCenterFormError" type="error" showIcon message={provisionError} />}
      {loading && <p className="appScopeState" role="status">正在加载组织目录…</p>}
      {!loading && error && (
        <Alert
          className="appCenterFormError"
          type="error"
          showIcon
          message={error}
          action={<Button size="small" autoInsertSpace={false} onClick={onRetry}>重试</Button>}
        />
      )}
      {!loading && !error && visibleDirectory.length > 0 && (
        <div className="appScopeTreeShell">
          <Tree
            checkable
            showLine={{ showLeafIcon: false }}
            blockNode
            selectable={false}
            checkedKeys={draftKeys}
            expandedKeys={effectiveExpandedKeys}
            onCheck={handleCheck}
            onExpand={(keys) => setExpandedKeys(keys.map(String))}
            treeData={buildScopeTreeData(visibleDirectory)}
            titleRender={titleRender}
          />
        </div>
      )}
      {!loading && !error && visibleDirectory.length === 0 && (
        <p className="appScopeState">未找到匹配的部门或人员</p>
      )}
    </Modal>
  </div>;
}
