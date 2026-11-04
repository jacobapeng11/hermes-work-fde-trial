import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Switch, Tabs, Tag } from "antd";
import { CheckOutlined, DatabaseOutlined, PlusOutlined } from "@ant-design/icons";
import { userApi } from "../../api/client";
import {
  PersonalKnowledgePanel,
  type PersonalKnowledgeWorkspaceAdapter
} from "./PersonalKnowledgePanel";
import "./knowledge.css";

// 企业知识库能力清单（源自 enterprise-knowledge/PRIMER.md 与 workflows/query.md，
// 面向用户的能力口径；内部路由/SQL 契约细节不在此暴露）。
const ENTERPRISE_CAPABILITIES = [
  { title: "企业认知底座", detail: "内置公司全景与 10 大部门知识路由，对话自动定位相关业务领域。" },
  { title: "企业术语理解", detail: "渠道、销管等领域私有术语与业务黑话的准确释义。" },
  { title: "业务数据问答", detail: "业绩、订单、渠道、项目等经营指标的安全统计与排名分析。" },
  { title: "文档智能检索", detail: "产品方案、招投标、品牌物料、财务制度等文档检索，答案带来源引用。" },
  { title: "方法论与流程", detail: "部门分析方法论与标准操作流程按需应用。" },
  { title: "安全权限管控", detail: "严格遵循企业权限体系，仅能访问当前账号被授权的知识范围。" }
] as const;

interface KnowledgePageProps {
  runtimeWorkspaceId: number | null;
  adapter?: KnowledgeWorkspaceAdapter;
}

export interface KnowledgeWorkspaceAdapter {
  getEnterpriseSettings: () => Promise<{ enabled: boolean }>;
  updateEnterpriseSettings: (
    enabled: boolean
  ) => Promise<{ enabled: boolean }>;
  personalKnowledge?: PersonalKnowledgeWorkspaceAdapter;
  context?: "personal" | "application";
}

// 顶部布局对齐技能广场：模块页头（图标+标题+副标题+主操作）在 Tabs 之上，
// tab 名保持「企业知识库 / 个人知识库」不变。新建按钮在页头，经递增信号
// 触发 Panel 内的创建弹窗（弹窗与创建流程仍归 Panel 所有）。
// 按钮仅在「个人知识库 tab 的列表层」展示：企业 tab 或进入详情页时隐藏。
export function KnowledgePage({
  runtimeWorkspaceId,
  adapter
}: KnowledgePageProps) {
  const [tab, setTab] = useState<"enterprise" | "personal">("enterprise");
  const [enabled, setEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [createTicket, setCreateTicket] = useState(0);
  const [personalDetailActive, setPersonalDetailActive] = useState(false);
  const handleDetailViewChange = useCallback((active: boolean) => setPersonalDetailActive(active), []);
  const applicationContext = adapter?.context === "application";
  const personalAvailable = !applicationContext || Boolean(adapter?.personalKnowledge);
  const personalCanManage = adapter?.personalKnowledge?.canManageAssets ?? true;

  const load = async () => {
    if (!runtimeWorkspaceId && !adapter) return;
    setLoading(true);
    setMessage(null);
    try {
      const settings = adapter
        ? await adapter.getEnterpriseSettings()
        : await userApi.enterpriseKnowledgeSettings(
            runtimeWorkspaceId as number
          );
      setEnabled(settings.enabled);
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "读取知识库设置失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, [adapter, runtimeWorkspaceId]);

  const toggle = async () => {
    if ((!runtimeWorkspaceId && !adapter) || saving) return;
    const previous = enabled;
    const next = !previous;
    setEnabled(next);
    setSaving(true);
    setMessage(null);
    try {
      const settings = adapter
        ? await adapter.updateEnterpriseSettings(next)
        : await userApi.updateEnterpriseKnowledgeSettings(
            runtimeWorkspaceId as number,
            { enabled: next }
          );
      setEnabled(settings.enabled);
      setMessage(settings.enabled ? "已开启" : "已关闭");
    } catch (reason) {
      setEnabled(previous);
      setMessage(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const hasError = Boolean(message && !["已开启", "已关闭"].includes(message));
  const statusLabel = loading ? "读取中" : enabled ? "已启用" : "未启用";
  const headerSubtitle = applicationContext && !personalAvailable
    ? "使用企业统一维护的系统、业务数据与内部知识，为任务和对话提供可靠依据。"
    : "统一管理企业知识访问与个人知识库，选择参与对话检索的知识范围。";

  return <section className="knowledgePage">
    <div className="knowledgePageInner knowledgeWorkspace">
      <header className="knowledgePageHeader">
        <div className="knowledgePageHeaderMain">
          <span className="knowledgeHeaderIcon"><DatabaseOutlined aria-hidden /></span>
          <div className="knowledgePageHeaderText">
            <h2 className="knowledgePageHeaderTitle">知识库</h2>
            <p className="knowledgePageHeaderSubtitle">{headerSubtitle}</p>
          </div>
        </div>
        {personalAvailable && personalCanManage && tab === "personal" && !personalDetailActive && (
          <Button
            type="primary"
            autoInsertSpace={false}
            icon={<PlusOutlined aria-hidden />}
            onClick={() => setCreateTicket((ticket) => ticket + 1)}
          >新建知识库</Button>
        )}
      </header>
      <Tabs
        className="knowledgeTabs"
        activeKey={tab}
        onChange={(key) => setTab(key as "enterprise" | "personal")}
        items={[
          {
            key: "enterprise",
            label: "企业知识库",
            children: <div className="knowledgePanel">
              <section className="knowledgeEnterpriseCard" aria-labelledby="enterprise-access-title">
                <div className="knowledgeEnterpriseCardMain">
                  <div className="knowledgeCapabilityIdentity">
                    <span className="knowledgeCapabilityIcon"><DatabaseOutlined aria-hidden /></span>
                    <div>
                      <h3 id="enterprise-access-title">企业知识访问控制</h3>
                      <ul className="knowledgeCapabilityList">
                        {ENTERPRISE_CAPABILITIES.map((capability) => <li className="knowledgeCapabilityItem" key={capability.title}>
                          <CheckOutlined aria-hidden />
                          <span><strong>{capability.title}：</strong>{capability.detail}</span>
                        </li>)}
                      </ul>
                    </div>
                  </div>
                  <div className="knowledgeEnterpriseSide">
                    <Tag className="knowledgeAccessBadge" data-enabled={enabled || undefined}>{statusLabel}</Tag>
                    <div className="knowledgeToggleRow">
                      <span className="knowledgeToggleRowLabel">用于对话检索</span>
                      <Switch
                        checked={enabled}
                        loading={saving}
                        disabled={loading || (!runtimeWorkspaceId && !adapter)}
                        onChange={() => void toggle()}
                        aria-label={applicationContext ? "用于应用 Agent 检索" : "用于对话检索"}
                      />
                    </div>
                    <p className="knowledgeToggleCaption">{applicationContext ? "只影响当前应用 Agent 的企业知识检索。" : "仅根据你的身份权限，访问可授权的企业知识。"}</p>
                  </div>
                </div>
                <div className="knowledgeFeedback" aria-live="polite">
                  {loading && <p className="knowledgeStatus" role="status">正在读取设置…</p>}
                  {!loading && saving && <p className="knowledgeStatus" role="status">正在保存设置…</p>}
                  {!loading && !saving && !hasError && message && <p className="knowledgeStatus" role="status">{message}</p>}
                </div>
                {!loading && !saving && hasError && (
                  <Alert
                    className="knowledgeErrorAlert"
                    type="error"
                    showIcon
                    message={message}
                    action={<Button size="small" autoInsertSpace={false} onClick={() => void load()}>重新读取</Button>}
                  />
                )}
              </section>
            </div>
          },
          ...(personalAvailable
            ? [{
                key: "personal" as const,
                label: "个人知识库",
                children: <PersonalKnowledgePanel
                  runtimeWorkspaceId={runtimeWorkspaceId}
                  adapter={adapter?.personalKnowledge}
                  createTrigger={createTicket}
                  onDetailViewChange={handleDetailViewChange}
                />
              }]
            : [])
        ]}
      />
    </div>
  </section>;
}
