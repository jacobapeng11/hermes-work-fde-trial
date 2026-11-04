import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Button, Modal, Radio } from "antd";
import type {
  PersonalKnowledgePermissionDirectoryNode,
  PersonalKnowledgePermissionGrant,
  PersonalKnowledgePermissionResponse
} from "@hermes-cloud/schemas";
import { userApi } from "../../api/client";
import { usePlatformTheme } from "../../theme/platform-theme";
import { PersonalKnowledgePermissionDirectoryTree } from "./PersonalKnowledgePermissionDirectoryTree";
import {
  applyPermissionRole,
  commonPermissionRole,
  grantsToPermissionReplace,
  permissionDirectoryTargets,
  permissionTargetKey,
  personalKnowledgePermissionLabels,
  personalKnowledgePermissionRoles,
  type PersonalKnowledgePermissionTargetOption,
  type SharedPersonalKnowledgeRole
} from "./personalKnowledgePermissionModel";

interface Props {
  knowledgeBaseId: number;
  knowledgeBaseName: string;
  permissions: PersonalKnowledgePermissionResponse;
  mode: "add" | "edit";
  initialGrant?: PersonalKnowledgePermissionGrant | null;
  onSaved: (response: PersonalKnowledgePermissionResponse) => void;
  onClose: () => void;
}

export function PersonalKnowledgePermissionEditorDialog({
  knowledgeBaseId,
  knowledgeBaseName,
  permissions,
  mode,
  initialGrant = null,
  onSaved,
  onClose
}: Props) {
  const [nodes, setNodes] = useState<PersonalKnowledgePermissionDirectoryNode[]>([]);
  const [selected, setSelected] = useState<PersonalKnowledgePermissionTargetOption[]>([]);
  const [role, setRole] = useState<SharedPersonalKnowledgeRole | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const { theme } = usePlatformTheme();

  const loadDirectory = useCallback(() => {
    let active = true;
    setLoading(true);
    setLoadError(null);
    void userApi.personalKnowledgePermissionDirectory(knowledgeBaseId).then((directory) => {
      if (!active) return;
      setNodes(directory);
      if (mode === "edit" && initialGrant) {
        if (initialGrant.targetType === "user") {
          // 人员不嵌入部门树（对齐旧平台），直接按授权记录回显。
          setSelected([{
            targetType: "user",
            targetId: initialGrant.targetId,
            name: initialGrant.name ?? `用户 ${initialGrant.targetId}`,
            userCode: initialGrant.userCode ?? null,
            departmentId: null,
            departmentPath: [],
            status: "active"
          }]);
          setRole(initialGrant.role);
        } else {
          const target = permissionDirectoryTargets(directory).find((item) => permissionTargetKey(item) === permissionTargetKey(initialGrant));
          if (target) {
            setSelected([target]);
            setRole(initialGrant.role);
          } else {
            setLoadError("目标已不在组织目录中，请返回后直接移除该权限");
          }
        }
      }
    }).catch((reason) => {
      if (active) setLoadError(reason instanceof Error ? reason.message : "组织目录加载失败");
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [initialGrant, knowledgeBaseId, mode]);

  useEffect(() => loadDirectory(), [loadDirectory]);

  const selectedKeys = useMemo(() => selected.map(permissionTargetKey), [selected]);
  const focusTargetKey = mode === "edit" && initialGrant && initialGrant.targetType === "department"
    ? permissionTargetKey(initialGrant)
    : null;

  const changeSelected = (targets: PersonalKnowledgePermissionTargetOption[]) => {
    setSelected(targets);
    setRole(commonPermissionRole(targets, permissions.grants));
    setSaveError(null);
  };

  const close = () => {
    if (!saving) onClose();
  };

  const save = async () => {
    if (!role || selected.length === 0) return;
    setSaving(true);
    setSaveError(null);
    try {
      const grants = applyPermissionRole(permissions.grants, selected, role);
      const response = await userApi.replacePersonalKnowledgePermissions(
        knowledgeBaseId,
        grantsToPermissionReplace(grants, permissions)
      );
      onSaved(response);
    } catch (reason) {
      setSaveError(reason instanceof Error ? reason.message : "保存权限失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open
      title={mode === "add" ? "添加权限" : "修改权限"}
      centered
      width={690}
      rootClassName={theme === "dark" ? "knowledgeDialog knowledgeDialogDark" : "knowledgeDialog"}
      className="pkbPermissionEditorModal"
      closable={!saving}
      maskClosable={!saving}
      keyboard={!saving}
      onCancel={close}
      footer={[
        <Button key="cancel" autoInsertSpace={false} disabled={saving} onClick={close}>取消</Button>,
        <Button
          key="save"
          type="primary"
          autoInsertSpace={false}
          disabled={loading || Boolean(loadError) || selected.length === 0 || !role}
          loading={saving}
          onClick={() => void save()}
        >{saving ? "保存中…" : "保存"}</Button>,
      ]}
    >
      <p className="pkbFormHint">{knowledgeBaseName}</p>
      <div className="pkbPermissionEditorBody">
        <section aria-labelledby="pkb-permission-target-title">
          <p id="pkb-permission-target-title" className="pkbPermissionSectionLabel">选择部门或人员（已选 {selected.length}）</p>
          {loading && <p className="pkbDialogLoading" role="status">正在加载组织目录…</p>}
          {!loading && loadError && (
            <Alert
              className="pkbPermissionInitialError"
              type="error"
              showIcon
              message={loadError}
              action={<Button size="small" autoInsertSpace={false} onClick={loadDirectory}>重试</Button>}
            />
          )}
          {!loading && !loadError && <PersonalKnowledgePermissionDirectoryTree
            nodes={nodes}
            selectedKeys={selectedKeys}
            grants={permissions.grants}
            onChange={changeSelected}
            focusTargetKey={focusTargetKey}
          />}
        </section>
        <fieldset className="pkbPermissionRoleOptions">
          <legend>设置统一权限</legend>
          <Radio.Group
            value={role ?? undefined}
            onChange={(event) => setRole(event.target.value as SharedPersonalKnowledgeRole)}
            options={personalKnowledgePermissionRoles.map((item) => ({ value: item, label: personalKnowledgePermissionLabels[item] }))}
          />
          {selected.length > 1 && role === null && <p>所选对象当前权限不一致，请重新选择统一权限。</p>}
        </fieldset>
        {saveError && <Alert className="pkbFormError" type="error" showIcon message={saveError} />}
      </div>
    </Modal>
  );
}
