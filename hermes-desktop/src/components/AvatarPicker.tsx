import { useEffect, useMemo, useRef, useState } from "react";
import { LoaderCircle, RotateCcw } from "lucide-react";
import { UploadOutlined } from "@ant-design/icons";
import type { AvatarAssetSummary } from "@hermes-cloud/schemas";
import {
  builtinAvatarId,
  builtinAvatarOptions,
  DEFAULT_AVATAR_VALUE,
  validateAvatarFile
} from "../avatar";
import { userApi } from "../api/client";
import { AvatarDisplay } from "./AvatarDisplay";
import { AvatarCropDialog } from "./AvatarCropDialog";

interface AvatarPickerProps {
  value: string;
  name: string;
  onChange: (value: string) => void;
  className?: string;
  compact?: boolean;
}

export function AvatarPicker({ value, name, onChange, className = "", compact = false }: AvatarPickerProps) {
  const [remoteOptions, setRemoteOptions] = useState<AvatarAssetSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [cropFile, setCropFile] = useState<File | null>(null);
  const [optionsOpen, setOptionsOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    let active = true;
    userApi.avatarAssets()
      .then((items) => {
        if (active) setRemoteOptions(items.filter((item) => item.sourceKind !== "builtin"));
      })
      .catch(() => {
        if (active) setError("头像目录加载失败，已显示默认头像。");
      });
    return () => { active = false; };
  }, []);

  const options = useMemo(() => {
    const merged = [...builtinAvatarOptions(), ...remoteOptions];
    if (value && !builtinAvatarId(value) && !merged.some((item) => item.avatarValue === value)) {
      merged.push({
        avatarValue: value,
        label: "当前自定义头像",
        sourceKind: "user_upload",
        imageUrl: value,
        sortOrder: 999
      });
    }
    return merged;
  }, [remoteOptions, value]);

  const chooseFile = (file?: File) => {
    if (!file) return;
    setError(null);
    try {
      validateAvatarFile(file);
      setCropFile(file);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "头像处理失败，请换一张后重试。");
    }
  };

  const uploadCroppedAvatar = async (file: File) => {
    setError(null);
    setUploading(true);
    try {
      const uploaded = await userApi.uploadAvatarAsset(file);
      setRemoteOptions((current) => [
        ...current.filter((item) => item.avatarValue !== uploaded.avatarValue),
        uploaded
      ]);
      onChange(uploaded.avatarValue);
      setCropFile(null);
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "头像上传失败，请换一张后重试。";
      setError(message);
      throw new Error(message);
    } finally {
      setUploading(false);
    }
  };

  if (compact) {
    const currentIndex = Math.max(0, options.findIndex((option) => option.avatarValue === value));
    const chooseNext = () => {
      if (options.length === 0) return;
      onChange(options[(currentIndex + 1) % options.length].avatarValue);
      setError(null);
    };
    return <section className={`expertCompactAvatarPicker ${className}`.trim()} aria-label="专家头像">
      <div className="expertCompactAvatarControl">
        <button type="button" className="expertCompactAvatarPreview" aria-label="选择默认头像" aria-expanded={optionsOpen} onClick={() => setOptionsOpen((open) => !open)}>
          <AvatarDisplay value={value} name={name} label={`${name}的头像`}/>
        </button>
        <button type="button" onClick={chooseNext}>换一个</button>
        <span aria-hidden="true"/>
        <button type="button" disabled={uploading} onClick={() => fileInputRef.current?.click()}>{uploading ? "上传中" : "上传"}</button>
        <input ref={fileInputRef} aria-label="上传自定义头像" type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => { chooseFile(event.target.files?.[0]); event.target.value = ""; }}/>
      </div>
      {optionsOpen && <div className="expertCompactAvatarOptions" role="radiogroup" aria-label="选择默认头像">
        {options.map((option) => <button key={option.avatarValue} type="button" role="radio" aria-label={option.label} aria-checked={option.avatarValue === value} onClick={() => { onChange(option.avatarValue); setOptionsOpen(false); setError(null); }}>
          <AvatarDisplay value={option.avatarValue} name={name} label={option.label}/>
        </button>)}
      </div>}
      {error && <p className="profileDialogError sharedAvatarError" role="alert">{error}</p>}
      {cropFile && <AvatarCropDialog file={cropFile} onCancel={() => setCropFile(null)} onConfirm={uploadCroppedAvatar}/>}
    </section>;
  }

  return <section className={`profileCard profileAvatarCard sharedAvatarPicker ${className}`.trim()} aria-label="数字人头像">
    <div className="profileAvatarCardHead">
      <div className="profileAvatarCardHeading">
        <h3>数字人头像</h3>
        <p>选择默认头像，或上传自己的图片。</p>
      </div>
      <div className="profileAvatarCardActions">
        {!builtinAvatarId(value) && <button type="button" className="profileResetAvatar" onClick={() => { setError(null); onChange(DEFAULT_AVATAR_VALUE); }}><RotateCcw size={14}/>恢复默认头像</button>}
        <button type="button" className="profileUploadAvatar" disabled={uploading} onClick={() => fileInputRef.current?.click()}>
          {uploading ? <LoaderCircle className="uploadSpinner" size={16}/> : <UploadOutlined aria-hidden="true"/>}
          {uploading ? "正在上传头像…" : "上传自定义头像"}
        </button>
      </div>
    </div>
    <div className="profileAvatarPicker" role="radiogroup" aria-label="选择默认头像">
      {options.map((option) => <button key={option.avatarValue} type="button" role="radio" aria-label={option.label} aria-checked={option.avatarValue === value} onClick={() => { setError(null); onChange(option.avatarValue); }}>
        <AvatarDisplay value={option.avatarValue} name={name} label={option.label}/>
      </button>)}
    </div>
    <p className="profileAvatarUploadHint">支持 PNG、JPEG、WebP 格式，建议尺寸不小于 200x200 像素。</p>
    <input ref={fileInputRef} className="profileAvatarUploadInput" aria-label="上传自定义头像" type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => { chooseFile(event.target.files?.[0]); event.target.value = ""; }}/>
    {error && <p className="profileDialogError sharedAvatarError" role="alert">{error}</p>}
    {cropFile && <AvatarCropDialog file={cropFile} onCancel={() => setCropFile(null)} onConfirm={uploadCroppedAvatar}/>}
  </section>;
}
