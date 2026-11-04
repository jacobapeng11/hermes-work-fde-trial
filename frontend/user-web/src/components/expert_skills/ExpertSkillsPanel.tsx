import { lazy, Suspense, useState } from "react";
import { Button, Tabs } from "antd";
import { PlusOutlined, RobotOutlined, UploadOutlined } from "@ant-design/icons";
import type { RuntimeAgentProfileSummary } from "@hermes-cloud/schemas";
import { AntdModuleProviders } from "../../theme/antd-module";
import "./expert-skills.css";

const ExpertPanelView = lazy(() =>
  import("../experts").then((module) => ({ default: module.ExpertPanelView })));
const PlazaSkillPanelView = lazy(() =>
  import("../skills").then((module) => ({ default: module.PlazaSkillPanelView })));

interface ExpertSkillsPanelProps {
  runtimeWorkspaceId: number | null;
  onInstalledExpertsChange?: (profiles: RuntimeAgentProfileSummary[]) => Promise<void> | void;
  onStartExpertChat: (profile: RuntimeAgentProfileSummary) => void;
}

function PanelLoading({ label }: { label: string }) {
  return <p className="expertSkillsLoading" role="status">正在加载{label}…</p>;
}

// 2026-11-03-016 大卡片重构：视觉框架与知识库模块完全同构——灰底滚动页 +
// 白色大卡片（页头：图标/标题/副标题 + 主操作 → 页面级下划线 Tab → 内容区）。
// 专家与技能面板分别以 embedded / chromeless 模式嵌入，内部不再重复灰底白卡；
// 技能 tab 的「上传技能」主按钮在卡片页头，经 uploadTrigger 信号驱动面板内弹窗
// （同知识库 createTicket 模式）。
export function ExpertSkillsPanelView({
  runtimeWorkspaceId,
  onInstalledExpertsChange,
  onStartExpertChat
}: ExpertSkillsPanelProps) {
  const [tab, setTab] = useState<"experts" | "skills">("experts");
  const [uploadTicket, setUploadTicket] = useState(0);
  const [createTicket, setCreateTicket] = useState(0);

  return (
    <AntdModuleProviders>
      <section className="expertSkillsPage" aria-label="专家-技能">
        <div className="expertSkillsWorkspace">
          <header className="expertSkillsHeader">
            <div className="expertSkillsHeaderMain">
              <span className="expertSkillsHeaderIcon"><RobotOutlined aria-hidden/></span>
              <div className="expertSkillsHeaderText">
                <h2 className="expertSkillsHeaderTitle">专家与技能</h2>
                <p className="expertSkillsHeaderSubtitle">创建和管理专属专家，按需扩展个人技能与市场能力</p>
              </div>
            </div>
            {tab === "experts"
              ? (
                <Button
                  type="primary"
                  autoInsertSpace={false}
                  icon={<PlusOutlined aria-hidden/>}
                  disabled={!runtimeWorkspaceId}
                  onClick={() => setCreateTicket((ticket) => ticket + 1)}
                >创建专家</Button>
              )
              : (
                <Button
                  type="primary"
                  autoInsertSpace={false}
                  icon={<UploadOutlined aria-hidden/>}
                  onClick={() => setUploadTicket((ticket) => ticket + 1)}
                >上传技能</Button>
              )}
          </header>
          <Tabs
            className="expertSkillsTabs"
            activeKey={tab}
            onChange={(key) => setTab(key as "experts" | "skills")}
            items={[
              {
                key: "experts",
                label: "专家",
                children: (
                  <Suspense fallback={<PanelLoading label="专家"/>}>
                    <ExpertPanelView
                      embedded
                      createTrigger={createTicket}
                      runtimeWorkspaceId={runtimeWorkspaceId}
                      onInstalledChange={onInstalledExpertsChange}
                      onStartChat={onStartExpertChat}
                    />
                  </Suspense>
                )
              },
              {
                key: "skills",
                label: "技能",
                children: (
                  <Suspense fallback={<PanelLoading label="技能"/>}>
                    <PlazaSkillPanelView
                      chromeless
                      uploadTrigger={uploadTicket}
                      runtimeWorkspaceId={runtimeWorkspaceId}
                    />
                  </Suspense>
                )
              }
            ]}
          />
        </div>
      </section>
    </AntdModuleProviders>
  );
}
