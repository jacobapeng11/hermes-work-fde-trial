import type { RuntimeAgentProfileSummary } from "@hermes-cloud/schemas";


const projectionLabel = (profile: RuntimeAgentProfileSummary): string => {
  if (profile.convergenceStatus === "applied") return "已投影";
  if (profile.convergenceStatus === "updating") return "自动更新中";
  if (profile.convergenceStatus === "revoked") return "专家已撤回";
  if (profile.convergenceStatus === "disabled") return "已卸载";
  if (profile.convergenceStatus === "error") return "投影失败";
  return "等待首次配置";
};

const effectiveLabel = (profile: RuntimeAgentProfileSummary): string => {
  if (profile.effectiveStatus === "awaiting_activation") return "下一次使用时生效";
  if (profile.effectiveStatus === "updating") return "等待投影";
  if (profile.effectiveStatus === "revoked") return "不可使用";
  if (profile.effectiveStatus === "disabled") return "已停用";
  if (profile.effectiveStatus === "error") return "生效失败";
  return "等待配置";
};

export function ExpertProjectionStatus({ profile }: { profile: RuntimeAgentProfileSummary }) {
  const configured = profile.configuredExpertVersionId
    ? `专家版本 #${profile.configuredExpertVersionId}`
    : profile.desiredRevision
      ? `Profile #${profile.desiredRevision}`
      : "尚未配置";
  const applied = profile.appliedExpertVersionId
    ? `专家版本 #${profile.appliedExpertVersionId}`
    : profile.appliedRevision
      ? `Profile #${profile.appliedRevision}`
      : "尚未投影";
  return <dl className="expertProjectionStatus" aria-label="专家能力状态">
    <div><dt>已配置</dt><dd>{configured}</dd></div>
    <div><dt>已投影</dt><dd data-state={profile.convergenceStatus}>{applied} · {projectionLabel(profile)}</dd></div>
    <div><dt>运行生效</dt><dd data-state={profile.effectiveStatus}>{effectiveLabel(profile)}</dd></div>
  </dl>;
}
