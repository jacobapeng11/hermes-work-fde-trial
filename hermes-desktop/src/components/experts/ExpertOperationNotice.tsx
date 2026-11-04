import { Button, Modal } from "antd";
import { usePlatformTheme } from "../../theme/platform-theme";

// 专家编辑器内的两个轻量确认弹窗（antd Modal danger 基线，
// 对齐技能/知识库/看板区确认形态；原 skillCatalog* 类不再跨模块借用）。
export function ExpertCapabilityRemovalDialog({
  label,
  onCancel,
  onConfirm
}: {
  label: string;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { theme } = usePlatformTheme();
  return <Modal
    open
    title="移除专家能力"
    centered
    width={440}
    rootClassName={theme === "dark" ? "expertAntDialog expertAntDialogDark" : "expertAntDialog"}
    onCancel={onCancel}
    footer={[
      <Button key="cancel" autoInsertSpace={false} onClick={onCancel}>取消</Button>,
      <Button key="confirm" danger autoInsertSpace={false} onClick={onConfirm}>确认移除</Button>
    ]}
  >
    <p className="expertNoticeBody">确认从当前专家配置中移除“{label}”？这不会删除原始资产。</p>
  </Modal>;
}

export function ExpertEditorCloseDialog({
  target,
  onCancel,
  onConfirm
}: {
  target: "create" | "edit";
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { theme } = usePlatformTheme();
  return <Modal
    open
    title="关闭专家编辑器"
    centered
    width={440}
    rootClassName={theme === "dark" ? "expertAntDialog expertAntDialogDark" : "expertAntDialog"}
    onCancel={onCancel}
    footer={[
      <Button key="stay" autoInsertSpace={false} onClick={onCancel}>继续编辑</Button>,
      <Button key="close" danger autoInsertSpace={false} onClick={onConfirm}>确认关闭</Button>
    ]}
  >
    <p className="expertNoticeBody">{target === "create" ? "创建草稿已自动保存在当前浏览器，下次打开时可以继续。确认暂时关闭编辑器吗？" : "当前修改尚未保存，关闭后将丢失本次修改。"}</p>
  </Modal>;
}

export function ExpertStaleSkillSaveDialog({
  skillNames,
  onCancel,
  onConfirm
}: {
  skillNames: string[];
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { theme } = usePlatformTheme();
  return <Modal
    open
    title="保存前确认移除失效技能"
    centered
    width={480}
    rootClassName={theme === "dark" ? "expertAntDialog expertAntDialogDark" : "expertAntDialog"}
    onCancel={onCancel}
    footer={[
      <Button key="cancel" autoInsertSpace={false} onClick={onCancel}>取消</Button>,
      <Button key="confirm" type="primary" danger autoInsertSpace={false} onClick={onConfirm}>确认移除并保存</Button>
    ]}
  >
    <p className="expertNoticeBody">以下个人技能已被删除或不可用，保存时将从该专家中移除：</p>
    <ul className="expertStaleSkillList">
      {skillNames.map((name) => <li key={name}>{name}</li>)}
    </ul>
  </Modal>;
}
