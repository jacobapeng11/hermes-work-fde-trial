import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Empty, Flex, Input, Modal, Tabs, Tag, Typography } from "antd";
import { DeleteOutlined, EditOutlined, PlusOutlined, SearchOutlined } from "@ant-design/icons";
import type { PersonalKnowledgePermissionGrant, PersonalKnowledgePermissionResponse } from "@hermes-cloud/schemas";
import { userApi } from "../../api/client";
import { usePlatformTheme } from "../../theme/platform-theme";
import { PersonalKnowledgePermissionEditorDialog } from "./PersonalKnowledgePermissionEditorDialog";
import {
  grantsToPermissionReplace,
  personalKnowledgePermissionLabels,
  removePermissionGrant
} from "./personalKnowledgePermissionModel";

interface Props {
  knowledgeBaseId: number;
  knowledgeBaseName: string;
  onClose: () => void;
}

type PermissionTab = "department" | "user";

const { Text } = Typography;

function grantName(grant: PersonalKnowledgePermissionGrant): string {
  if (grant.targetType === "department") return grant.name;
  if (grant.name && grant.userCode) return `${grant.name}（${grant.userCode}）`;
  if (grant.name) return grant.name;
  return `已失效用户（${grant.targetId}）`;
}

function grantDescription(grant: PersonalKnowledgePermissionGrant): string {
  if (grant.targetType === "department") return grant.departmentPath.slice(0, -1).join(" / ") || "顶级部门";
  const path = grant.departmentPath.join(" / ") || "未归属部门";
  return grant.status === "active" ? path : `${path} · 已失效`;
}

export function PersonalKnowledgePermissionsDialog({ knowledgeBaseId, knowledgeBaseName, onClose }: Props) {
  const [permissions, setPermissions] = useState<PersonalKnowledgePermissionResponse | null>(null);
  const [tab, setTab] = useState<PermissionTab>("department");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editor, setEditor] = useState<{ mode: "add" | "edit"; grant?: PersonalKnowledgePermissionGrant } | null>(null);
  const [removing, setRemoving] = useState<PersonalKnowledgePermissionGrant | null>(null);
  const [removeSaving, setRemoveSaving] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);
  const { theme } = usePlatformTheme();
  const dark = theme === "dark";

  const load = () => {
    let active = true;
    setLoading(true);
    setError(null);
    void userApi.personalKnowledgePermissions(knowledgeBaseId).then((response) => {
      if (active) setPermissions(response);
    }).catch((reason) => {
      if (active) setError(reason instanceof Error ? reason.message : "读取协作权限失败");
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  };

  useEffect(() => load(), [knowledgeBaseId]);

  const grouped = useMemo(() => ({
    department: permissions?.grants.filter((grant) => grant.targetType === "department") || [],
    user: permissions?.grants.filter((grant) => grant.targetType === "user") || []
  }), [permissions]);
  const visible = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    if (!keyword) return grouped[tab];
    return grouped[tab].filter((grant) => `${grantName(grant)} ${grant.departmentPath.join(" ")}`.toLowerCase().includes(keyword));
  }, [grouped, query, tab]);

  const nestedOpen = Boolean(editor || removing) || removeSaving;
  const close = () => {
    if (!nestedOpen) onClose();
  };

  const confirmRemove = async () => {
    if (!permissions || !removing) return;
    setRemoveSaving(true);
    setRemoveError(null);
    try {
      const grants = removePermissionGrant(permissions.grants, removing);
      const response = await userApi.replacePersonalKnowledgePermissions(
        knowledgeBaseId,
        grantsToPermissionReplace(grants, permissions)
      );
      setPermissions(response);
      setRemoving(null);
    } catch (reason) {
      setRemoveError(reason instanceof Error ? reason.message : "移除权限失败");
    } finally {
      setRemoveSaving(false);
    }
  };

  return <>
    <Modal
      open
      title="权限设置"
      centered
      width={730}
      rootClassName={dark ? "knowledgeDialog knowledgeDialogDark" : "knowledgeDialog"}
      className="pkbPermissionModal"
      closable={!nestedOpen}
      maskClosable={!nestedOpen}
      keyboard={!nestedOpen}
      onCancel={close}
      footer={[
        <Button key="close" autoInsertSpace={false} disabled={nestedOpen} onClick={close}>关闭</Button>,
      ]}
    >
      <Text type="secondary" className="pkbPermissionTargetName">{knowledgeBaseName}</Text>
      {loading && <p className="pkbDialogLoading" role="status">正在读取权限…</p>}
      {!loading && error && (
        <Alert
          className="pkbPermissionInitialError"
          type="error"
          showIcon
          message={error}
          action={<Button size="small" autoInsertSpace={false} onClick={load}>重试</Button>}
        />
      )}
      {!loading && !error && permissions && <div className="pkbPermissionManager">
        <Tabs
          className="pkbPermissionTabs"
          activeKey={tab}
          onChange={(key) => { setTab(key as PermissionTab); setQuery(""); }}
          items={[
            { key: "department", label: <>已授权部门 <span className="pkbPermissionTabCount">{grouped.department.length}</span></> },
            { key: "user", label: <>已授权人员 <span className="pkbPermissionTabCount">{grouped.user.length}</span></> },
          ]}
        />
        <Flex className="pkbPermissionToolbar" gap={12}>
          <Button type="primary" autoInsertSpace={false} icon={<PlusOutlined aria-hidden />} onClick={() => setEditor({ mode: "add" })}>添加权限</Button>
          <Input
            className="pkbPermissionSearch"
            type="search"
            prefix={<SearchOutlined aria-hidden />}
            aria-label={`搜索已授权${tab === "department" ? "部门" : "人员"}`}
            placeholder={`搜索已授权${tab === "department" ? "部门" : "人员"}`}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            allowClear
          />
        </Flex>
        <div className="pkbPermissionGrantList">
          {visible.length === 0 && (
            <Empty
              className="pkbPermissionEmpty"
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={query ? "没有匹配的已授权对象" : `暂无已授权${tab === "department" ? "部门" : "人员"}`}
            />
          )}
          {visible.map((grant) => <article className="pkbPermissionGrantRow" key={`${grant.targetType}:${grant.targetId}`}>
            <div className="pkbPermissionGrantInfo">
              <strong>{grantName(grant)}</strong>
              <span>{grantDescription(grant)}</span>
            </div>
            <div className="pkbPermissionGrantSide">
              <Tag className="pkbPermissionGrantRole" color="blue">{personalKnowledgePermissionLabels[grant.role]}</Tag>
              <Button
                type="link"
                size="small"
                autoInsertSpace={false}
                icon={<EditOutlined aria-hidden />}
                onClick={() => setEditor({ mode: "edit", grant })}
                disabled={grant.targetType === "user" && grant.status !== "active"}
              >修改</Button>
              <Button type="link" size="small" autoInsertSpace={false} danger icon={<DeleteOutlined aria-hidden />} onClick={() => { setRemoveError(null); setRemoving(grant); }}>移除</Button>
            </div>
          </article>)}
        </div>
      </div>}
    </Modal>

    {permissions && editor && <PersonalKnowledgePermissionEditorDialog
      knowledgeBaseId={knowledgeBaseId}
      knowledgeBaseName={knowledgeBaseName}
      permissions={permissions}
      mode={editor.mode}
      initialGrant={editor.grant}
      onSaved={(response) => { setPermissions(response); setEditor(null); }}
      onClose={() => setEditor(null)}
    />}

    {removing && (
      <Modal
        open
        title={`确认移除“${grantName(removing)}”的权限？`}
        centered
        width={420}
        rootClassName={dark ? "knowledgeDialog knowledgeDialogDark" : "knowledgeDialog"}
        className="pkbPermissionRemoveModal"
        closable={!removeSaving}
        maskClosable={!removeSaving}
        keyboard={!removeSaving}
        onCancel={() => { if (!removeSaving) setRemoving(null); }}
        footer={[
          <Button key="cancel" autoInsertSpace={false} disabled={removeSaving} onClick={() => setRemoving(null)}>取消</Button>,
          <Button
            key="remove"
            danger
            type="primary"
            autoInsertSpace={false}
            loading={removeSaving}
            onClick={() => void confirmRemove()}
          >{removeSaving ? "移除中…" : "确认移除"}</Button>,
        ]}
      >
        <p className="pkbFormHint">移除后，该对象将不再通过此直接授权访问知识库。</p>
        {removeError && <Alert className="pkbFormError" type="error" showIcon message={removeError} />}
      </Modal>
    )}
  </>;
}
