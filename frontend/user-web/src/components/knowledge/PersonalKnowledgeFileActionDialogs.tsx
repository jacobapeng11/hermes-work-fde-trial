import { useState } from "react";
import { Alert, Button, Input, Modal } from "antd";
import { FileTextOutlined, WarningOutlined } from "@ant-design/icons";
import { usePlatformTheme } from "../../theme/platform-theme";

interface RenameProps {
  filename: string;
  onClose: () => void;
  onRename: (filename: string) => Promise<void>;
}

interface DeleteProps {
  filename: string;
  onClose: () => void;
  onDelete: () => Promise<void>;
}

export function PersonalKnowledgeFileRenameDialog({ filename: initialFilename, onClose, onRename }: RenameProps) {
  const [filename, setFilename] = useState(initialFilename);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { theme } = usePlatformTheme();

  const close = () => {
    if (!saving) onClose();
  };

  const submit = async () => {
    const normalized = filename.trim();
    if (!normalized) {
      setError("请输入文件名");
      return;
    }
    if (normalized === initialFilename) {
      onClose();
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onRename(normalized);
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "文件重命名失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open
      title="重命名文件"
      centered
      width={420}
      rootClassName={theme === "dark" ? "knowledgeDialog knowledgeDialogDark" : "knowledgeDialog"}
      className="pkbRenameModal"
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
        >{saving ? "保存中…" : "保存文件名"}</Button>,
      ]}
    >
      <div className="pkbForm">
        <p className="pkbFormHint">修改文件在当前知识库中的显示名称。</p>
        <div className="pkbFormItem">
          <label className="pkbFormLabel" htmlFor="pkb-rename-filename">文件名</label>
          <Input
            id="pkb-rename-filename"
            aria-label="文件名"
            value={filename}
            onChange={(event) => setFilename(event.target.value)}
            maxLength={255}
            autoFocus
            prefix={<FileTextOutlined aria-hidden />}
            onPressEnter={() => void submit()}
          />
        </div>
        {error && <Alert className="pkbFormError" type="error" showIcon message={error} />}
      </div>
    </Modal>
  );
}

export function PersonalKnowledgeFileDeleteDialog({ filename, onClose, onDelete }: DeleteProps) {
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { theme } = usePlatformTheme();

  const close = () => {
    if (!deleting) onClose();
  };

  const remove = async () => {
    setDeleting(true);
    setError(null);
    try {
      await onDelete();
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "删除文件失败");
    } finally {
      setDeleting(false);
    }
  };

  return (
    <Modal
      open
      title="永久删除文件"
      centered
      width={420}
      rootClassName={theme === "dark" ? "knowledgeDialog knowledgeDialogDark" : "knowledgeDialog"}
      className="pkbDeleteModal"
      closable={!deleting}
      maskClosable={!deleting}
      keyboard={!deleting}
      onCancel={close}
      footer={[
        <Button key="cancel" autoInsertSpace={false} disabled={deleting} onClick={close}>取消</Button>,
        <Button
          key="delete"
          danger
          type="primary"
          autoInsertSpace={false}
          loading={deleting}
          onClick={() => void remove()}
        >{deleting ? "删除中…" : "确认永久删除"}</Button>,
      ]}
    >
      <div className="pkbDeleteConfirm">
        <p className="pkbDeleteWarning">删除后，文件将无法继续预览、下载或参与知识检索。</p>
        <div className="pkbDeleteTarget">
          <span className="pkbDeleteTargetIcon"><WarningOutlined aria-hidden /></span>
          <div><small>即将删除</small><strong>{filename}</strong></div>
        </div>
        {error && <Alert className="pkbFormError" type="error" showIcon message={error} />}
      </div>
    </Modal>
  );
}
