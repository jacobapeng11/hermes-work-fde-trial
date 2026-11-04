import { useState } from "react";
import { Alert, Button, Input, Modal } from "antd";
import { usePlatformTheme } from "../../theme/platform-theme";

interface Props {
  title: string;
  supportingText: string;
  submitLabel: string;
  pendingLabel: string;
  initialName?: string;
  initialDescription?: string | null;
  onClose: () => void;
  onSubmit: (value: { name: string; description: string | null }) => Promise<void>;
}

// 表单弹窗基线：centered Modal 520px + label 上方布局 + footer 取消/提交(loading)。
// 挂载由父组件条件控制，弹窗自身始终完整渲染（open 常开），避免半卸载 portal。
export function PersonalKnowledgeFormDialog({
  title,
  supportingText,
  submitLabel,
  pendingLabel,
  initialName = "",
  initialDescription = "",
  onClose,
  onSubmit
}: Props) {
  const [name, setName] = useState(initialName);
  const [description, setDescription] = useState(initialDescription || "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { theme } = usePlatformTheme();

  const close = () => {
    if (!saving) onClose();
  };

  const submit = async () => {
    const normalizedName = name.trim();
    if (!normalizedName) {
      setError("请输入知识库名称");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSubmit({
        name: normalizedName,
        description: description.trim() || null
      });
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存知识库失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open
      title={title}
      centered
      width={520}
      rootClassName={theme === "dark" ? "knowledgeDialog knowledgeDialogDark" : "knowledgeDialog"}
      className="pkbFormModal"
      closable={!saving}
      maskClosable={!saving}
      keyboard={!saving}
      onCancel={close}
      footer={[
        <Button key="cancel" autoInsertSpace={false} disabled={saving} onClick={close}>取消</Button>,
        <Button
          key="submit"
          type="primary"
          autoInsertSpace={false}
          loading={saving}
          onClick={() => void submit()}
        >{saving ? pendingLabel : submitLabel}</Button>,
      ]}
    >
      <div className="pkbForm">
        <p className="pkbFormHint">{supportingText}</p>
        <div className="pkbFormItem">
          <label className="pkbFormLabel" htmlFor="pkb-form-name">名称</label>
          <Input
            id="pkb-form-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            maxLength={200}
            autoFocus
            placeholder="例如：售前解决方案资料库"
            onPressEnter={() => void submit()}
          />
        </div>
        <div className="pkbFormItem">
          <label className="pkbFormLabel" htmlFor="pkb-form-description">描述</label>
          <Input.TextArea
            id="pkb-form-description"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            maxLength={2000}
            rows={4}
            placeholder="说明资料范围，帮助数字人判断何时检索"
          />
        </div>
        {error && <Alert className="pkbFormError" type="error" showIcon message={error} />}
      </div>
    </Modal>
  );
}
