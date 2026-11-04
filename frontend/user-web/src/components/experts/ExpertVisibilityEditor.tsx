import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Input, List, Modal, Tabs, Tag, Tree, Typography } from "antd";
import { PlusOutlined } from "@ant-design/icons";
import { Search, UserRound } from "lucide-react";
import { BankOutlined, FolderOutlined, SearchOutlined, UserOutlined } from "@ant-design/icons";
const { Text } = Typography;
import { userApi } from "../../api/client";
import type {
  DirectoryUserSearchItem,
  ExpertShareScope,
  OrganizationDirectoryNode,
  PersonalKnowledgeSelectedUser
} from "@hermes-cloud/schemas";
import { usePlatformTheme } from "../../theme/platform-theme";
import {
  buildScopeTreeData,
  checkedKeysToScope,
  filterScopeTreeData,
  flattenScopeDirectory,
  scopeToCheckedKeys,
  type ScopeNodeMeta,
  type ScopeTreeNode
} from "./expertScopeModel";

interface Props {
  scope: ExpertShareScope;
  onChange: (scope: ExpertShareScope) => void;
  directory: OrganizationDirectoryNode[];
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  selectedUsers?: PersonalKnowledgeSelectedUser[];
}

type RemovalTarget =
  | { type: "organization"; id: number; name: string; desc: string }
  | { type: "department"; id: string; name: string; desc: string }
  | { type: "user"; id: number; name: string; desc: string };

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
  // 与看板区不同：专家树默认不展开组织根（原自研树行为），仅展开已选目标的祖先链。
  return [...result];
}

const DIRECTORY_PROVISION_BATCH_SIZE = 100;

// 专家可见范围统一弹窗（第四期 antd 化）：
// 外层为「已分享部门/已分享人员」双 Tab 列表 + 树弹窗（antd Tree 受控勾选）+
// 树外人员搜索通道（保留原双通道语义：树上勾选与搜索添加在确认时合并去重）。
// 未激活用户乐观勾选 + provisionDirectoryUsers 回填（同看板区 AppScopeEditor 模式）。
export function ExpertVisibilityEditor({
  scope,
  onChange,
  directory,
  loading,
  error,
  onRetry,
  selectedUsers = []
}: Props) {
  const { theme } = usePlatformTheme();
  const [activeTab, setActiveTab] = useState<"departments" | "users">("departments");
  const [listQuery, setListQuery] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [treeQuery, setTreeQuery] = useState("");
  const [draftKeys, setDraftKeys] = useState<string[]>([]);
  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);
  const [removal, setRemoval] = useState<RemovalTarget | null>(null);
  const [draftUserIds, setDraftUserIds] = useState<number[]>([]);
  const [draftUserLabels, setDraftUserLabels] = useState<Map<number, { name: string; userCode?: string | null }>>(new Map());
  const [userQuery, setUserQuery] = useState("");
  const [userOptions, setUserOptions] = useState<{ id: number; name: string; userCode?: string | null }[]>([]);
  const [userSearching, setUserSearching] = useState(false);
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

  useEffect(() => {
    const keyword = userQuery.trim();
    if (!keyword) {
      setUserOptions([]);
      return;
    }
    let active = true;
    const timer = window.setTimeout(() => {
      setUserSearching(true);
      void userApi.searchDirectoryUsers(keyword, 20).then((items) => {
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

  const organization = directory.find((node) => node.nodeType === "organization");
  const selectedUserLabels = new Map(selectedUsers.map((user) => [user.id, user]));
  const departmentNameOfUser = (ancestorKeys: string[] | undefined): string => {
    const departmentKeys = (ancestorKeys ?? []).filter((key) => key.startsWith("department:"));
    const last = departmentKeys[departmentKeys.length - 1];
    if (!last) return "";
    const node = metas.get(last)?.node;
    return node?.nodeType === "department" ? node.name : "";
  };
  const departmentItems: RemovalTarget[] = scope.organization
    ? [{
        type: "organization",
        id: organization?.nodeType === "organization" ? organization.organizationId : 0,
        name: organization?.name || "整个组织",
        desc: "全员可见"
      }]
    : scope.departmentIds.map((id) => {
      const node = metas.get(`department:${id}`)?.node;
      return { type: "department", id, name: node?.nodeType === "department" ? node.name : `部门 ${id}`, desc: "共享部门" };
    });
  const userItems: RemovalTarget[] = scope.organization ? [] : scope.userIds.map((id) => {
    const meta = metas.get(`user:${id}`);
    const node = meta?.node;
    const fallback = selectedUserLabels.get(id);
    return {
      type: "user",
      id,
      name: node?.nodeType === "user" ? node.name : fallback?.name || `用户 ${id}`,
      desc: departmentNameOfUser(meta?.ancestorKeys) || "平台用户"
    };
  });
  const normalizedListQuery = listQuery.trim().toLocaleLowerCase();
  const currentItems = (activeTab === "departments" ? departmentItems : userItems)
    .filter((item) => item.name.toLocaleLowerCase().includes(normalizedListQuery));

  const openDialog = () => {
    const keys = scopeToCheckedKeys(scope, directory);
    setDraftKeys(keys);
    setExpandedKeys(ancestorKeysOf(keys, directory, metas));
    setTreeQuery("");
    setUserQuery("");
    setUserOptions([]);
    setProvisionError(null);
    setDraftUserIds(scope.organization ? [] : [...scope.userIds]);
    setDraftUserLabels(
      new Map(selectedUsers.map((user) => [user.id, { name: user.name ?? `用户 ${user.id}`, userCode: user.userCode ?? null }]))
    );
    setDialogOpen(true);
  };

  const addDraftUser = (item: { id: number; name: string; userCode?: string | null }) => {
    setDraftUserIds((current) => current.includes(item.id) ? current : [...current, item.id]);
    setDraftUserLabels((current) => new Map(current).set(item.id, { name: item.name, userCode: item.userCode }));
  };

  const removeDraftUser = (userId: number) => {
    setDraftUserIds((current) => current.filter((item) => item !== userId));
    setDraftKeys((current) => current.filter((item) => item !== `user:${userId}`));
  };

  // antd Tree 父子联动与规范化语义兼容：勾祖先联动勾后代，确认时由
  // checkedKeysToScope 去冗余。部门授权覆盖目录内人员；未激活人员仍需按 100 人
  // 一批顺序 provision，成功后回填真实平台账号，避免触发接口单次上限。
  const handleCheck = (
    checked: React.Key[] | { checked: React.Key[] },
    info?: { checked?: boolean; node?: { key?: React.Key } }
  ) => {
    let next = (Array.isArray(checked) ? checked : checked.checked).map(String);
    // rc-tree 父子联动会把「勾全部公司根」聚合成组织根 key，触发 organization:true
    // 短路（语义扩为全组织，未来新增公司也被授权）。这里还原为精确的顶层公司根
    // 部门集合——只有用户真的点了组织根才走全组织短路。
    const organizationKey = next.find((key) => key.startsWith("organization:"));
    const clickedKey = info?.node?.key != null ? String(info.node.key) : "";
    if (
      organizationKey != null
      && !clickedKey.startsWith("organization:")
      && !draftKeys.some((key) => key.startsWith("organization:"))
    ) {
      const organizationNode = resolvedDirectory.find((node) => node.nodeType === "organization");
      const companyRootIds = organizationNode?.nodeType === "organization"
        ? organizationNode.children
          .filter((node): node is Extract<OrganizationDirectoryNode, { nodeType: "department" }> => node.nodeType === "department" && node.virtual !== true)
          .map((node) => `department:${node.departmentId}`)
        : [];
      next = [...next.filter((key) => !key.startsWith("organization:")), ...companyRootIds];
    }
    if (info?.checked === true && clickedKey.startsWith("department:") && !next.includes(clickedKey)) {
      next = [...next, clickedKey];
    }
    setProvisionError(null);
    const addedUnprovisioned = next.filter(
      (key) => !draftKeys.includes(key) && key.startsWith("user:unprovisioned:")
    );
    setDraftKeys(next);
    if (addedUnprovisioned.length === 0) return;
    const codes = addedUnprovisioned
      .map((key) => metas.get(key)?.node)
      .filter((node): node is OrganizationDirectoryNode & { nodeType: "user" } => node?.nodeType === "user")
      .map((node) => node.userCode ?? "")
      .filter(Boolean);
    setProvisioning(true);
    void (async () => {
      try {
        const provisioned: DirectoryUserSearchItem[] = [];
        for (let offset = 0; offset < codes.length; offset += DIRECTORY_PROVISION_BATCH_SIZE) {
          provisioned.push(...await userApi.provisionDirectoryUsers(
            codes.slice(offset, offset + DIRECTORY_PROVISION_BATCH_SIZE)
          ));
        }
        const idByCode = new Map(provisioned.map((item) => [item.userCode ?? "", item.id]));
        setProvisionedIdByCode((current) => {
          const merged = new Map(current);
          addedUnprovisioned.forEach((key) => {
            const code = key.slice("user:unprovisioned:".length);
            const byCode = idByCode.get(code);
            const id = byCode;
            if (id != null && code) merged.set(code, id);
          });
          return merged;
        });
        // 草稿里的 unprovisioned key 换成真实 id（树数据随 resolvedDirectory 同步重建），
        // 并同步搜索添加通道的已选人员。
        setDraftKeys((current) => current.map((key) => {
          if (!key.startsWith("user:unprovisioned:")) return key;
          const code = key.slice("user:unprovisioned:".length);
          const byCode = idByCode.get(code);
          if (byCode != null) return `user:${byCode}`;
          return key;
        }));
        if (clickedKey.startsWith("user:unprovisioned:") && next.includes(clickedKey)) {
          const code = clickedKey.slice("user:unprovisioned:".length);
          const id = idByCode.get(code);
          if (id != null) addDraftUser({ id, name: metas.get(clickedKey)?.node.name ?? code, userCode: code });
        }
      } catch (reason) {
        if (clickedKey.startsWith("user:unprovisioned:")) {
          setProvisionError(reason instanceof Error ? reason.message : "预创建账号失败，请稍后重试");
        }
        setDraftKeys((current) => current.filter((key) => !addedUnprovisioned.includes(key)));
      } finally {
        setProvisioning(false);
      }
    })();
  };

  const confirmDialog = () => {
    const nextScope = checkedKeysToScope(draftKeys, metas);
    // 树上勾选的已开户人员（nextScope.userIds）与搜索添加的人员合并去重。
    const merged = nextScope.organization
      ? []
      : [...new Set([...nextScope.userIds, ...draftUserIds])];
    onChange({
      ...nextScope,
      userIds: merged
    });
    setDialogOpen(false);
  };

  const removeTarget = () => {
    if (!removal) return;
    if (removal.type === "organization") {
      onChange({ organization: false, departmentIds: [], userIds: [] });
    } else if (removal.type === "department") {
      onChange({ ...scope, departmentIds: scope.departmentIds.filter((id) => id !== removal.id) });
    } else {
      onChange({ ...scope, userIds: scope.userIds.filter((id) => id !== removal.id) });
    }
    setRemoval(null);
  };

  const titleRender = (node: ScopeTreeNode) => {
    const meta = metas.get(node.key);
    const isUnactivated = meta?.node.nodeType === "user" && meta.node.loggedIn !== true;
    const nodeIcon = meta?.node.nodeType === "organization"
      ? <BankOutlined aria-hidden/>
      : meta?.node.nodeType === "department"
        ? <FolderOutlined aria-hidden/>
        : <UserOutlined aria-hidden/>;
    return <span className="expertScopeTreeTitle">{nodeIcon}{node.title}{isUnactivated && <small className="expertScopeUnprovisioned" title="勾选后将自动创建平台账号；标记在用户首次登录后消失">未激活</small>}</span>;
  };

  const removalDialog = removal && <Modal
    open
    title="确认移除可见对象"
    centered
    width={400}
    rootClassName={theme === "dark" ? "expertAntDialog expertAntDialogDark" : "expertAntDialog"}
    onCancel={() => setRemoval(null)}
    footer={[
      <Button key="cancel" autoInsertSpace={false} onClick={() => setRemoval(null)}>取消</Button>,
      <Button key="confirm" danger autoInsertSpace={false} onClick={removeTarget}>确认移除</Button>
    ]}
  >
    <p className="expertNoticeBody">确认移除“{removal.name}”的专家可见权限吗？</p>
  </Modal>;

  return <div className="expertVisibilityEditor">
    <div className="expertVisibilityPanel">
      <Tabs
        className="expertVisibilityAntTabs"
        activeKey={activeTab}
        onChange={(key) => setActiveTab(key as "departments" | "users")}
        destroyInactiveTabPane
        items={[
          {
            key: "departments",
            label: <>已分享部门 <span className="expertVisibilityTabCount">{departmentItems.length}</span></>,
            children: <ExpertVisibilityList
              items={currentItems}
              listQuery={listQuery}
              onListQueryChange={setListQuery}
              activeTab="departments"
              onModify={openDialog}
              onRemove={setRemoval}
            />
          },
          {
            key: "users",
            label: <>已分享人员 <span className="expertVisibilityTabCount">{userItems.length}</span></>,
            children: <ExpertVisibilityList
              items={currentItems}
              listQuery={listQuery}
              onListQueryChange={setListQuery}
              activeTab="users"
              onModify={openDialog}
              onRemove={setRemoval}
            />
          }
        ]}
      />
    </div>
    {!scope.organization && scope.userIds.length === 0 && scope.departmentIds.length === 0 && <p className="expertPrivateHint">未选择分享范围，专家只供自己使用，不进入管理员审核。</p>}
    <Modal
      open={dialogOpen}
      title="添加可见对象"
      centered
      width={690}
      rootClassName={theme === "dark" ? "expertAntDialog expertAntDialogDark" : "expertAntDialog"}
      className="expertVisibilityTreeModal"
      closable={!provisioning}
      maskClosable={!provisioning}
      keyboard={!provisioning}
      onCancel={() => setDialogOpen(false)}
      footer={[
        <Button key="cancel" autoInsertSpace={false} disabled={provisioning} onClick={() => setDialogOpen(false)}>取消</Button>,
        <Button key="confirm" type="primary" autoInsertSpace={false} loading={provisioning} onClick={confirmDialog}>确认</Button>
      ]}
    >
      <p className="expertVisibilityDialogSubtitle">选择部门或人员</p>
      <div className="expertVisibilitySelectionSummary">已选择 <strong>{draftKeys.length + draftUserIds.length}</strong> 个可见对象</div>
      {provisionError && <Alert className="expertDialogInlineError" type="error" showIcon message={provisionError}/>}
      {loading && <p className="expertVisibilityState" role="status">正在加载组织目录…</p>}
      {!loading && error && (
        <Alert
          className="expertDialogInlineError"
          type="error"
          showIcon
          message={error}
          action={<Button size="small" autoInsertSpace={false} onClick={onRetry}>重试</Button>}
        />
      )}
      {!loading && !error && visibleDirectory.length > 0 && (
        <div className="expertVisibilityTreeShell">
          <div className="expertVisibilityTreeSearch">
            <Input
              variant="borderless"
              size="small"
              type="search"
              aria-label="搜索部门或人员"
              placeholder="搜索部门或人员"
              prefix={<SearchOutlined aria-hidden/>}
              value={treeQuery}
              onChange={(event) => setTreeQuery(event.target.value)}
            />
          </div>
          <div className="expertVisibilityTreeViewport">
            <Tree
              className="expertVisibilityAntTree"
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
        </div>
      )}
      {!loading && !error && visibleDirectory.length === 0 && <p className="expertVisibilityState">未找到匹配的部门或人员</p>}
      <div className="expertVisibilityUserPicker">
        <label className="expertVisibilityUserSearch">
          <Search size={14} aria-hidden="true"/>
          <span className="srOnly">搜索人员</span>
          <input
            type="search"
            value={userQuery}
            onChange={(event) => setUserQuery(event.target.value)}
            placeholder="按姓名或工号搜索人员"
          />
        </label>
        {userSearching && <p className="expertVisibilityUserState" role="status">搜索中…</p>}
        {!userSearching && userQuery.trim() && userOptions.length === 0 && <p className="expertVisibilityUserState">没有匹配的平台用户</p>}
        {!userSearching && userOptions.length > 0 && <ul className="expertVisibilityUserOptions">
          {userOptions.map((item) => <li key={item.id}>
            <button type="button" disabled={draftUserIds.includes(item.id)} onClick={() => addDraftUser(item)}>
              <UserRound size={14} aria-hidden="true"/>
              <span>{item.userCode ? `${item.name}（${item.userCode}）` : item.name}</span>
              {draftUserIds.includes(item.id) ? <small>已选</small> : null}
            </button>
          </li>)}
        </ul>}
        {draftUserIds.length > 0 && <ul className="expertVisibilitySelectedUsers">
          {draftUserIds.map((id) => {
            const label = draftUserLabels.get(id);
            const name = label ? (label.userCode ? `${label.name}（${label.userCode}）` : label.name) : `用户 ${id}`;
            return <li key={id}>
              <span><UserRound size={14} aria-hidden="true"/>{name}</span>
              <button type="button" onClick={() => removeDraftUser(id)} aria-label={`移除 ${name}`}>×</button>
            </li>;
          })}
        </ul>}
      </div>
    </Modal>
    {removalDialog}
  </div>;
}

function ExpertVisibilityList({
  items,
  listQuery,
  onListQueryChange,
  activeTab,
  onModify,
  onRemove
}: {
  items: RemovalTarget[];
  listQuery: string;
  onListQueryChange: (value: string) => void;
  activeTab: "departments" | "users";
  onModify: () => void;
  onRemove: (item: RemovalTarget) => void;
}) {
  return <div className="expertVisibilityListArea">
    <div className="expertVisibilityToolbar">
      <Input.Search
        className="expertVisibilityListSearch"
        type="text"
        aria-label="搜索已分享对象"
        placeholder={activeTab === "departments" ? "搜索已分享部门" : "搜索已分享人员"}
        value={listQuery}
        onChange={(event) => onListQueryChange(event.target.value)}
        allowClear
      />
      <Button
        className="expertVisibilityAdd"
        type="primary"
        autoInsertSpace={false}
        icon={<PlusOutlined aria-hidden/>}
        onClick={onModify}
      >添加可见对象</Button>
    </div>
    {items.length > 0 ? <div className="expertScopeListShell">
      <List
        dataSource={items}
        rowKey={(item) => `${item.type}:${item.id}`}
        renderItem={(item) => (
          <List.Item
            className="scope-list-item"
            actions={[
              <Button key="remove" type="text" danger autoInsertSpace={false} className="scope-remove-btn" onClick={() => onRemove(item)}>移除</Button>
            ]}
          >
            <span className="expertScopeItemMain">
              <span className={item.type === "user" ? "expertScopeIconUser" : "expertScopeIconDept"}>
                {item.type === "user" ? <UserOutlined aria-hidden/> : <FolderOutlined aria-hidden/>}
              </span>
              <span className="expertScopeItemText">
                <span className="expertScopeItemTitleRow">
                  <Text strong className="expertScopeItemName">{item.name}</Text>
                  <Tag className="expertScopeTypeTag">{item.type === "user" ? "人员" : item.type === "organization" ? "组织" : "部门"}</Tag>
                </span>
                <Text type="secondary" className="expertScopeItemDesc">{item.desc}</Text>
              </span>
            </span>
          </List.Item>
        )}
      />
    </div> : <p className="expertVisibilityEmpty">{listQuery ? "没有匹配的可见对象" : activeTab === "departments" ? "尚未分享给部门" : "尚未分享给人员"}</p>}
  </div>;
}
