import { useCallback, useState } from "react";
import { Alert, Button, Modal } from "antd";
import { InfoCircleOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { usePlatformTheme } from "../../theme/platform-theme";
import type { AppCenterListItem } from "../../api/client";
import { CreatePromptSection, type CreatePromptTabId } from "./CreatePromptSection";
import { CreateTemplateSection, type TemplateSelection } from "./CreateTemplateSection";
import "./create-app-dialog.css";

interface Props {
  onClose: () => void;
  onCreateAppSession: (prompt: string) => Promise<boolean>;
  // 2026-11-02-005：方式二选中真实广场看板时走 ZIP 模板注入链路
  //（下载 ZIP → 新建独立会话 → 附件注入 + 预填模板修改提示词，不自动发送）。
  onCreateFromTemplate: (app: AppCenterListItem) => Promise<boolean>;
  plazaBoards: AppCenterListItem[];
  plazaLoading: boolean;
}

// 统一选中态：方式一 Tag 与方式二模板卡互斥单选，最后一次点击生效。
type Selection =
  | { source: "prompt"; prompt: string }
  | { source: "template"; id: string; prompt: string; blank: boolean; app?: AppCenterListItem }
  | null;

// 新建看板弹窗（2026-11-02-001 重构，005 方式二接回真实广场看板）：顶部连接说明
// Banner + 方式一（内置字典分段控制器 + Tag 单选）+ 方式二（真实广场看板横向滑动
// 卡轨 + 从空白开始），底部固定操作栏「取消 / 生成看板」统一提交。提交分流：
// Tag/空白卡 → 独立会话 + 预填提示词；广场看板卡 → ZIP 附件注入 + 模板修改提示词。
// 均不自动发送；创建中互斥，失败保留全部选中状态可重试。
export function CreateAppDialog({ onClose, onCreateAppSession, onCreateFromTemplate, plazaBoards, plazaLoading }: Props) {
  const { theme } = usePlatformTheme();
  const [activeTabId, setActiveTabId] = useState<CreatePromptTabId>("ai-lead");
  const [selection, setSelection] = useState<Selection>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleTabChange = useCallback((tabId: CreatePromptTabId) => {
    setActiveTabId(tabId);
    // 任务书红线：切换 Tab 动态更新 Tag 列表并清空方式一已选 Tag（方式二不受影响）。
    setSelection((current) => current?.source === "prompt" ? null : current);
  }, []);

  const handleSelectPrompt = useCallback((prompt: string) => {
    setSelection({ source: "prompt", prompt });
  }, []);

  const handleSelectTemplate = useCallback((template: TemplateSelection) => {
    setSelection({ source: "template", id: template.id, prompt: template.prompt, blank: template.blank, app: template.app });
  }, []);

  const submit = useCallback(async () => {
    if (!selection || creating) return;
    setCreating(true);
    setError(null);
    try {
      // 方式二选中广场看板：走 ZIP 模板注入；其余（Tag / 空白卡）走纯提示词会话。
      const created = selection.source === "template" && !selection.blank && selection.app
        ? await onCreateFromTemplate(selection.app)
        : await onCreateAppSession(selection.prompt);
      if (created) {
        onClose();
      } else {
        setError("任务会话创建失败，请重试");
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "任务会话创建失败，请重试");
    } finally {
      setCreating(false);
    }
  }, [creating, onClose, onCreateAppSession, onCreateFromTemplate, selection]);

  const footer = (
    <div className="appCenterCreateFooter">
      <Button autoInsertSpace={false} disabled={creating} onClick={onClose}>取消</Button>
      <Button
        type="primary"
        autoInsertSpace={false}
        icon={<ThunderboltOutlined aria-hidden />}
        loading={creating}
        disabled={!selection}
        onClick={() => void submit()}
      >生成看板</Button>
    </div>
  );

  return (
    <Modal
      open
      title="新建看板"
      centered
      width={840}
      rootClassName={theme === "dark"
        ? "appCenterAntDialog appCenterAntDialogDark portalThemeScope"
        : "appCenterAntDialog portalThemeScope"}
      className="appCenterCreateModal"
      data-theme={theme}
      closable={!creating}
      maskClosable={!creating}
      keyboard={!creating}
      onCancel={onClose}
      footer={footer}
    >
      <div className="appCenterCreateBanner">
        <InfoCircleOutlined className="appCenterCreateBannerIcon" aria-hidden />
        <p>
          <strong>平台已连接：</strong>
          销售系统、订单系统、iCare工单系统、市场知识库、AI线索平台、剑鱼招投标系统、SCP报价系统。
          权限与各平台权限一致，您可以根据需求生成可视化看板报表。
        </p>
      </div>
      <CreatePromptSection
        activeTabId={activeTabId}
        selectedPrompt={selection?.source === "prompt" ? selection.prompt : null}
        disabled={creating}
        onTabChange={handleTabChange}
        onSelectPrompt={handleSelectPrompt}
      />
      <CreateTemplateSection
        boards={plazaBoards}
        loading={plazaLoading}
        selectedTemplateId={selection?.source === "template" ? selection.id : null}
        disabled={creating}
        onSelectTemplate={handleSelectTemplate}
      />
      {error && <Alert className="appCenterFormError appCenterCreateError" type="error" showIcon message={error} />}
    </Modal>
  );
}
