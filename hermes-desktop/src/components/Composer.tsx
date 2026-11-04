import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Check, ChevronDown, FileText, LoaderCircle, Mic, Paperclip, RotateCcw, SendHorizontal, Sparkles, Square, UserRound, X } from "lucide-react";
import type { ChatFileSummary, RuntimeAgentProfileSummary, RuntimeModelSummary } from "@hermes-cloud/schemas";
import { AvatarDisplay } from "./AvatarDisplay";
import { shouldSendOnKeyDown } from "../ui-state";
import { ModelPicker } from "./ModelPicker";
import "./composer-dropdowns.css";

interface ComposerProps {
  input: string;
  inputTone?: "normal" | "preview";
  onInputChange: (value: string) => void;
  onSend: () => void;
  onStop: () => void;
  streaming: boolean;
  stopping?: boolean;
  action?: "send" | "stop";
  actionDisabled?: boolean;
  modelId: number | null;
  models: RuntimeModelSummary[];
  onModelChange: (modelId: number) => void;
  disabled?: boolean;
  attachments?: ChatFileSummary[];
  uploads?: AttachmentUploadItem[];
  onPasteFiles?: (files: File[]) => void;
  canSendWithoutText?: boolean;
  onAddAttachments?: () => void;
  onRemoveAttachment?: (fileId: number) => void;
  onRetryUpload?: (uploadId: string) => void;
  onRemoveUpload?: (uploadId: string) => void;
  focusRequest?: number;
  installedExperts?: RuntimeAgentProfileSummary[];
  activeAgentProfileId?: number | null;
  activeAgentKind?: "default" | "expert" | null;
  onAgentChange?: (runtimeAgentProfileId: number | null) => void;
  showAgentPicker?: boolean;
  showAttachmentButton?: boolean;
  showModelPicker?: boolean;
}

function LocalAttachmentImage({ file }: { file: File }) {
  const [url, setUrl] = useState<string>();
  useEffect(() => {
    const nextUrl = URL.createObjectURL(file);
    setUrl(nextUrl);
    return () => URL.revokeObjectURL(nextUrl);
  }, [file]);
  if (!url) return <FileText size={15}/>;
  return <img src={url} alt="" style={{ width: 32, height: 32, objectFit: "cover", borderRadius: 6 }}/>;
}

export interface AttachmentUploadItem {
  id: string;
  name: string;
  progress: number;
  status: "pending" | "uploading" | "failed";
  error?: string;
  file?: File;
}

export function Composer({ input, inputTone = "normal", onInputChange, onSend, onStop, streaming, stopping = false, action, actionDisabled, modelId, models, onModelChange, disabled = false, attachments = [], uploads = [], onPasteFiles, canSendWithoutText = false, onAddAttachments, onRemoveAttachment, onRetryUpload, onRemoveUpload, focusRequest, installedExperts = [], activeAgentProfileId = null, activeAgentKind = "default", onAgentChange, showAgentPicker = true, showAttachmentButton = true, showModelPicker = true }: ComposerProps) {
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const agentPickerButtonRef = useRef<HTMLButtonElement>(null);
  const agentPickerRef = useRef<HTMLDivElement>(null);
  const [agentPickerOpen, setAgentPickerOpen] = useState(false);
  const handledFocusRequestRef = useRef<number | undefined>(undefined);
  const activeExpert = activeAgentKind === "expert"
    ? installedExperts.find((item) => item.runtimeAgentProfileId === activeAgentProfileId)
    : undefined;
  const deletedExpertHistory = activeAgentKind === "expert"
    && activeAgentProfileId !== null
    && !activeExpert;
  const isDisabled = disabled || deletedExpertHistory;
  const closeAgentPicker = useCallback((restoreFocus = false) => {
    setAgentPickerOpen(false);
    if (restoreFocus) agentPickerButtonRef.current?.focus();
  }, []);
  const autoResize = useCallback(() => {
    const element = inputRef.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, 120)}px`;
  }, []);

  useLayoutEffect(() => {
    autoResize();
  }, [input, autoResize]);

  useLayoutEffect(() => {
    if (focusRequest === undefined || isDisabled || handledFocusRequestRef.current === focusRequest) return;
    inputRef.current?.focus();
    handledFocusRequestRef.current = focusRequest;
  }, [focusRequest, isDisabled]);

  useEffect(() => {
    if (!agentPickerOpen) return;
    const close = (event: PointerEvent) => {
      if (!agentPickerRef.current?.contains(event.target as Node)) closeAgentPicker();
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeAgentPicker(true);
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [agentPickerOpen, closeAgentPicker]);

  const uploadInProgress = uploads.some((upload) => upload.status === "uploading");
  const cannotSend = isDisabled
    || (inputTone === "preview" && !canSendWithoutText)
    || (!input.trim() && !canSendWithoutText)
    || uploadInProgress;
  const resolvedAction = action ?? (!streaming || input.trim() || canSendWithoutText ? "send" : "stop");
  const showSendButton = resolvedAction === "send";

  return <><section className="composerCard" aria-label="消息编辑器">
    {(attachments.length > 0 || uploads.length > 0) && <div className="attachmentQueue" aria-label="本次消息附件">
      {attachments.map((file) => <span className="attachmentChip" key={file.fileId}><FileText size={15}/><span title={file.displayName}>{file.displayName}</span><button type="button" aria-label={`移除附件 ${file.displayName}`} onClick={() => onRemoveAttachment?.(file.fileId)}><X size={14}/></button></span>)}
      {uploads.map((upload) => {
        const statusText = upload.status === "uploading"
          ? `${upload.progress}%`
          : upload.status === "pending"
            ? "待发送"
            : "上传失败";
        return <span className="attachmentChip" data-status={upload.status} key={upload.id}>
          {upload.file && upload.file.type.startsWith("image/")
            ? <LocalAttachmentImage file={upload.file}/>
            : upload.status === "uploading"
              ? <LoaderCircle className="uploadSpinner" size={15}/>
              : <FileText size={15}/>}
          <span title={upload.error || upload.name}>{upload.name}<small>{statusText}</small></span>
          {upload.status === "failed" && <button type="button" aria-label={`重试发送 ${upload.name}`} onClick={() => onRetryUpload?.(upload.id)}><RotateCcw size={14}/></button>}
          {upload.status !== "uploading" && <button type="button" aria-label={`移除待发送附件 ${upload.name}`} onClick={() => onRemoveUpload?.(upload.id)}><X size={14}/></button>}
        </span>;
      })}
    </div>}
    <textarea ref={inputRef} aria-label="消息输入" placeholder={deletedExpertHistory ? "该专家已删除，此会话仅可查看历史。" : "描述你希望数字人完成的任务……"} rows={1} value={input} data-input-tone={inputTone} disabled={isDisabled} onChange={(event) => onInputChange(event.target.value)} onPaste={(event) => {
      const files = Array.from(event.clipboardData?.files ?? []);
      const images = files.filter((file) => file.type.startsWith("image/"));
      if (images.length === 0 || !onPasteFiles) return;
      event.preventDefault();
      onPasteFiles(images);
    }} onKeyDown={(event) => {
      if (shouldSendOnKeyDown({ key: event.key, shiftKey: event.shiftKey, isComposing: event.nativeEvent.isComposing })) {
        event.preventDefault();
        if (!cannotSend) onSend();
      }
    }} />
    <div className="composerActions">
      {showAgentPicker && <div className="composerAgentPicker" ref={agentPickerRef}>
        <button
          ref={agentPickerButtonRef}
          type="button"
          className="composerAgentButton"
          aria-label={`当前对话对象：${activeExpert?.displayName || "个人"}`}
          aria-haspopup="menu"
          aria-expanded={agentPickerOpen}
          disabled={isDisabled || streaming}
          onClick={() => {
            if (agentPickerOpen) closeAgentPicker();
            else setAgentPickerOpen(true);
          }}
        >
          {activeExpert ? <Sparkles size={15}/> : <UserRound size={15}/>}
          <span>{activeExpert?.displayName || "个人"}</span>
          <ChevronDown size={13}/>
        </button>
        {agentPickerOpen && <div className="composerAgentMenu composerDropdownPanel" role="menu" aria-label="可选对象">
          <div className="composerAgentPrimaryMenu">
            <p className="composerAgentMenuLabel dropdownGroupLabel">可选对象</p>
            <p className="composerAgentMenuGroupLabel dropdownGroupLabel">个人</p>
            <button type="button" className="dropdownOption" role="menuitemradio" aria-checked={activeAgentKind !== "expert"} onClick={() => { closeAgentPicker(); onAgentChange?.(null); }}>
              <UserRound size={16}/><span><strong>个人</strong></span>
              {activeAgentKind !== "expert" && <span className="dropdownCheck" aria-hidden="true"><Check size={15}/></span>}
            </button>
            <p className="composerAgentMenuGroupLabel dropdownGroupLabel">专家</p>
            <div className="composerExpertSubmenuList">
              {installedExperts.map((expert) => <button type="button" className="dropdownOption" role="menuitemradio" aria-checked={activeAgentProfileId === expert.runtimeAgentProfileId} key={expert.runtimeAgentProfileId} onClick={() => { closeAgentPicker(); onAgentChange?.(expert.runtimeAgentProfileId); }}>
                <span className="composerExpertAvatar"><AvatarDisplay value={expert.avatarUrl} name={expert.displayName} label={`${expert.displayName}的头像`}/></span>
                <span><strong>{expert.displayName}</strong><small>{expert.description || "独立专家 Profile"}</small></span>
                {activeAgentProfileId === expert.runtimeAgentProfileId && <span className="dropdownCheck" aria-hidden="true"><Check size={15}/></span>}
              </button>)}
              {installedExperts.length === 0 && <p className="composerExpertEmpty">尚未安装专家</p>}
            </div>
          </div>
        </div>}
      </div>}
      {showAttachmentButton && <button className="iconButton" type="button" disabled={isDisabled} aria-label="添加附件" title="添加附件" onClick={onAddAttachments}><Paperclip size={17} /></button>}
      <button className="iconButton" type="button" disabled aria-label="语音输入（服务尚未启用）" title="语音转写将通过平台服务提供"><Mic size={17} /></button>
      {showModelPicker && <ModelPicker modelId={modelId} models={models} onModelChange={onModelChange} disabled={isDisabled || streaming}/>}
      {showSendButton ? <button type="button" className="sendButton" data-action="send" aria-label="发送消息" onClick={onSend} disabled={isDisabled || (actionDisabled ?? cannotSend)}>
        <SendHorizontal size={17} />
      </button> : <button type="button" className="sendButton" data-action="stop" aria-label="停止生成" onClick={onStop} disabled={isDisabled || (actionDisabled ?? stopping)}>
        <Square size={16} />
      </button>}
    </div>
  </section>
  {/* 微调（014 §10）：免责提示移出输入容器，位于其正下方居中弱化展示 */}
  <p className="composerDisclaimer">AI 可能会犯错，请核查重要信息。</p></>;
}
