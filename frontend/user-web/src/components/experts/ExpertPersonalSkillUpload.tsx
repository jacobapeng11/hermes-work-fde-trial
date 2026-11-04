import { useRef, useState } from "react";
import { Alert, Button, Modal, Upload } from "antd";
import { CloudUploadOutlined } from "@ant-design/icons";

import { userApi } from "../../api/client";
import type { PersonalSkillAssetSummary } from "@hermes-cloud/schemas";
import { usePlatformTheme } from "../../theme/platform-theme";

// 个人技能上传弹窗（antd Modal 外壳）；file input 与上传链路保留原生实现，
// 不引入 antd Upload（同看板区 ZIP 上传契约冻结原则）。
export function ExpertPersonalSkillUpload({ onUploaded }: { onUploaded: (asset: PersonalSkillAssetSummary) => Promise<void> | void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const { theme } = usePlatformTheme();

  const upload = async () => {
    if (!file || busy) return;
    setBusy(true);
    setMessage(null);
    try {
      const asset = await userApi.uploadPersonalSkill({
        file,
        categoryKey: "uncategorized",
      });
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
      await onUploaded(asset);
      setMessage("上传成功，已选入当前专家。");
      setOpen(false);
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "个人技能上传失败");
    } finally {
      setBusy(false);
    }
  };

  const close = () => {
    if (busy) return;
    setOpen(false);
    setFile(null);
    setMessage(null);
    if (inputRef.current) inputRef.current.value = "";
  };

  return <div className="expertPersonalSkillUpload">
    <div
      className="expert-dashed-box expert-upload-box"
      role="button"
      tabIndex={0}
      aria-label="上传个人技能"
      onClick={() => { setOpen(true); setMessage(null); }}
      onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setOpen(true); setMessage(null); } }}
    >
      <span className="expert-dashed-icon expert-upload-icon"><CloudUploadOutlined aria-hidden/></span>
      <span className="expert-dashed-text">
        <span className="expert-dashed-title">上传个人技能</span>
        <span className="expert-dashed-desc">上传包含 SKILL.md 的 .zip 技能包</span>
      </span>
    </div>
    {message && !open && <small role="status">{message}</small>}
    <Modal
      open={open}
      title="上传个人技能"
      centered
      width={520}
      rootClassName={theme === "dark" ? "expertAntDialog expertAntDialogDark" : "expertAntDialog"}
      closable={!busy}
      maskClosable={!busy}
      keyboard={!busy}
      onCancel={close}
      footer={[
        <Button key="cancel" autoInsertSpace={false} disabled={busy} onClick={close}>取消</Button>,
        <Button
          key="submit"
          type="primary"
          autoInsertSpace={false}
          icon={<CloudUploadOutlined aria-hidden/>}
          loading={busy}
          disabled={!file || !file.name.toLocaleLowerCase().endsWith(".zip")}
          onClick={() => void upload()}
        >开始上传</Button>
      ]}
    >
      <div className="expertSkillUploadBody">
        <Upload.Dragger
          accept=".zip,application/zip"
          maxCount={1}
          showUploadList={false}
          aria-label="选择专家个人技能 zip 包"
          disabled={busy}
          beforeUpload={(selected) => {
            if (!selected.name.toLocaleLowerCase().endsWith(".zip")) {
              setFile(null);
              setMessage("请选择 .zip 格式的技能包。");
              return false;
            }
            setFile(selected);
            setMessage(null);
            return false;
          }}
        >
          <div className="skillUploadDropInner">
            <CloudUploadOutlined aria-hidden style={{ fontSize: 28 }}/>
            <strong>{file?.name ?? "选择技能 zip 包"}</strong>
            <span>{file ? `${Math.ceil(file.size / 1024)} KB · 等待上传` : "支持 .zip / application/zip"}</span>
          </div>
        </Upload.Dragger>
        {file && <p className="expertSkillUploadState">已选择：{file.name}</p>}
        {message && <Alert type={message.includes("成功") ? "success" : "error"} showIcon message={message}/>}
      </div>
    </Modal>
  </div>;
}
