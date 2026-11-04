import { useEffect, useState } from "react";
import { Alert, Button, Input, Modal } from "antd";
import type { AuthPrincipalSummary } from "@hermes-cloud/schemas";
import { avatarValueForProfile, type UserProfile } from "../../profile";
import { avatarFileFromDataUrl, builtinAvatarId } from "../../avatar";
import { userApi } from "../../api/client";
import { usePlatformTheme } from "../../theme/platform-theme";
import { AvatarPicker } from "../AvatarPicker";
import { DigitalHumanAvatar } from "../DigitalHumanAvatar";

interface ProfileDialogProps {
  profile: UserProfile;
  principal: AuthPrincipalSummary | null;
  onSave: (profile: UserProfile) => void;
  onClose: () => void;
}

export function ProfileDialog({ profile, principal, onSave, onClose }: ProfileDialogProps) {
  const [draft, setDraft] = useState<UserProfile>(profile);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const { theme } = usePlatformTheme();

  useEffect(() => setDraft(profile), [profile]);
  // Escape 关闭与遮罩点击关闭由 antd Modal 内置（keyboard / maskClosable），不再自实现。

  const updateField = (field: "displayName" | "jobTitle" | "team" | "signature") => (value: string) => {
    setDraft((current) => ({ ...current, [field]: value }));
  };
  const selectAvatar = (value: string) => {
    setUploadError(null);
    const avatarId = builtinAvatarId(value);
    setDraft((current) => avatarId
      ? { ...current, avatarKind: "default", avatarId, customAvatarUrl: null }
      : { ...current, avatarKind: "custom", customAvatarUrl: value });
  };
  const save = async () => {
    const displayName = draft.displayName.trim();
    if (!displayName) {
      setUploadError("请填写显示名称。");
      return;
    }
    let customAvatarUrl = draft.customAvatarUrl;
    if (draft.avatarKind === "custom" && customAvatarUrl?.startsWith("data:image/")) {
      try {
        const uploaded = await userApi.uploadAvatarAsset(avatarFileFromDataUrl(customAvatarUrl));
        customAvatarUrl = uploaded.avatarValue;
      } catch (reason) {
        setUploadError(reason instanceof Error ? reason.message : "旧头像迁移失败，请重新上传。");
        return;
      }
    }
    onSave({ ...draft, customAvatarUrl, displayName, jobTitle: draft.jobTitle.trim(), team: draft.team.trim(), signature: draft.signature.trim() });
  };
  const primaryIdentity = principal?.name || "未获取到账号信息";

  return <Modal
    open
    title={<div className="profileModalTitle">
      <strong>个人资料</strong>
      <p id="profile-dialog-description">资料仅保存在当前浏览器，不会同步到平台账号。</p>
    </div>}
    centered
    width={700}
    rootClassName={theme === "dark" ? "profileAntDialog profileAntDialogDark" : "profileAntDialog"}
    className="profileModal"
    onCancel={onClose}
    footer={[
      <Button key="cancel" autoInsertSpace={false} onClick={onClose}>取消</Button>,
      <Button key="save" type="primary" autoInsertSpace={false} onClick={() => void save()}>保存资料</Button>
    ]}
  >
    <div className="profileCards">
      <section className="profileCard profileBasicCard" aria-label="基础信息">
        <div className="profileBasicAvatar">
          <DigitalHumanAvatar profile={draft} className="profilePreviewAvatar"/>
        </div>
        <div className="profileBasicForm">
          <div className="profileFormRow">
            <div className="profileField">
              <span className="profileFieldLabel"><span className="profileRequired">*</span>显示名称</span>
              <Input aria-label="显示名称" maxLength={24} value={draft.displayName} onChange={(event) => updateField("displayName")(event.target.value)}/>
            </div>
            <div className="profileField">
              <span className="profileFieldLabel">岗位</span>
              <Input aria-label="岗位" maxLength={32} placeholder="例如：前端工程师" value={draft.jobTitle} onChange={(event) => updateField("jobTitle")(event.target.value)}/>
            </div>
          </div>
          <div className="profileField">
            <span className="profileFieldLabel">团队/部门</span>
            <Input aria-label="团队/部门" maxLength={32} placeholder="例如：数字化转型部" value={draft.team} onChange={(event) => updateField("team")(event.target.value)}/>
          </div>
          <div className="profileField profileFieldAlignTop">
            <span className="profileFieldLabel">个性签名</span>
            <Input.TextArea aria-label="个性签名" rows={2} maxLength={80} placeholder="例如：专注把事情做好" value={draft.signature} onChange={(event) => updateField("signature")(event.target.value)}/>
          </div>
        </div>
      </section>
      <AvatarPicker value={avatarValueForProfile(draft)} name={draft.displayName || primaryIdentity} onChange={selectAvatar}/>
      {uploadError && <Alert className="profileDialogAlert" type="error" showIcon message={uploadError}/>}
    </div>
  </Modal>;
}
