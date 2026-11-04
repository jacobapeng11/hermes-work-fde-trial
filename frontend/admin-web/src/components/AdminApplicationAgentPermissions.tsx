import { useEffect, useMemo, useState } from "react";
import { Save } from "lucide-react";
import type { ApplicationAgentAdminSummary } from "@hermes-cloud/schemas";
import { adminApi } from "../api/client";
import { ApplicationAgentAdminUserMultiSelect } from "./ApplicationAgentAdminUserMultiSelect";


export function AdminApplicationAgentPermissions() {
  const [members, setMembers] = useState<ApplicationAgentAdminSummary[]>([]);
  const [selectedUserCodes, setSelectedUserCodes] = useState<string[]>([]);
  const [savedUserCodes, setSavedUserCodes] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const dirty = useMemo(
    () => JSON.stringify([...selectedUserCodes].sort()) !== JSON.stringify([...savedUserCodes].sort()),
    [savedUserCodes, selectedUserCodes]
  );

  useEffect(() => {
    void refreshMembers();
  }, []);

  async function refreshMembers() {
    setLoading(true);
    setError(null);
    setNotice(null);
    try {
      const nextMembers = await adminApi.applicationAgentAdmins();
      const nextCodes = nextMembers
        .map((member) => member.userCode)
        .filter((userCode): userCode is string => Boolean(userCode));
      setMembers(nextMembers);
      setSelectedUserCodes(nextCodes);
      setSavedUserCodes(nextCodes);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "加载应用 Agent 管理员失败");
    } finally {
      setLoading(false);
    }
  }

  async function save() {
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const nextMembers = await adminApi.replaceApplicationAgentAdmins(selectedUserCodes);
      const nextCodes = nextMembers
        .map((member) => member.userCode)
        .filter((userCode): userCode is string => Boolean(userCode));
      setMembers(nextMembers);
      setSelectedUserCodes(nextCodes);
      setSavedUserCodes(nextCodes);
      setNotice(`已保存，共 ${nextCodes.length} 名应用 Agent 管理员`);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="adminPanel applicationAgentPermissionPanel" aria-label="应用 Agent 权限">
      <div className="panelHeader">
        <div>
          <h2>应用 Agent 权限</h2>
          <span>只有这里选择的人员，才能在个人 Web 左下角看到“应用 Agent”入口。</span>
        </div>
        <strong>{selectedUserCodes.length} 人</strong>
      </div>

      <div className="applicationAgentPermissionEditor">
        <div className="applicationAgentPermissionSectionHeader">
          <div>
            <h3>应用 Agent 管理员</h3>
            <small>按姓名或工号搜索，可一次选择多个人员。</small>
          </div>
          <button type="button" onClick={() => void refreshMembers()} disabled={loading || saving}>
            刷新
          </button>
        </div>
        {loading ? (
          <p className="muted">正在加载…</p>
        ) : (
          <ApplicationAgentAdminUserMultiSelect
            value={selectedUserCodes}
            onChange={(value) => {
              setSelectedUserCodes(value);
              setNotice(null);
              setError(null);
            }}
            selectedUsers={members}
            disabled={saving}
          />
        )}
        <p className="applicationAgentPermissionHint">
          保存后，新选择人员获得权限，移除人员失去管理入口；已发布的应用 Agent 不受影响。
        </p>
        <div className="applicationAgentPermissionActions">
          <button
            type="button"
            onClick={() => void save()}
            disabled={loading || saving || !dirty}
          >
            <Save size={15} aria-hidden />
            {saving ? "保存中…" : "保存管理员名单"}
          </button>
        </div>
      </div>

      {notice && <p className="adminInlineNotice" role="status">{notice}</p>}
      {error && <p className="adminInlineError" role="alert">{error}</p>}
    </section>
  );
}
