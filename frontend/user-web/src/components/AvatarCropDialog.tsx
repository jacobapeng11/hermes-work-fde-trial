import { useEffect, useMemo, useState, type PointerEvent as ReactPointerEvent } from "react";
import { LoaderCircle, Minus, Plus } from "lucide-react";
import { renderAvatarCrop, type AvatarCrop } from "../avatar";

const VIEWPORT_SIZE = 264;
const MAX_ZOOM = 3;

type ImageState = {
  source: string;
  width: number;
  height: number;
};

type DragState = {
  pointerId: number;
  startX: number;
  startY: number;
  offsetX: number;
  offsetY: number;
};

interface AvatarCropDialogProps {
  file: File;
  onCancel: () => void;
  onConfirm: (file: File) => Promise<void>;
}

export function AvatarCropDialog({ file, onCancel, onConfirm }: AvatarCropDialogProps) {
  const [image, setImage] = useState<ImageState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [drag, setDrag] = useState<DragState | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    let active = true;
    setImage(null);
    setError(null);
    setZoom(1);
    setOffset({ x: 0, y: 0 });
    void loadCropImage(file)
      .then((next) => { if (active) setImage(next); })
      .catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : "图片无法读取，请换一张后重试。"); });
    return () => { active = false; };
  }, [file]);

  const baseScale = image ? Math.min(VIEWPORT_SIZE / image.width, VIEWPORT_SIZE / image.height) : 1;
  const scale = baseScale * zoom;
  const renderedSize = useMemo(() => image ? {
    width: image.width * scale,
    height: image.height * scale
  } : null, [image, scale]);

  const updateZoom = (nextZoom: number) => {
    if (!renderedSize || !image) return;
    const clampedZoom = Math.min(MAX_ZOOM, Math.max(1, nextZoom));
    const nextScale = baseScale * clampedZoom;
    setZoom(clampedZoom);
    setOffset((current) => clampOffset(current, {
      width: image.width * nextScale,
      height: image.height * nextScale
    }));
  };

  const startDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!renderedSize || submitting) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    setDrag({ pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, offsetX: offset.x, offsetY: offset.y });
  };

  const moveDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!drag || drag.pointerId !== event.pointerId || !renderedSize) return;
    setOffset(clampOffset({
      x: drag.offsetX + event.clientX - drag.startX,
      y: drag.offsetY + event.clientY - drag.startY
    }, renderedSize));
  };

  const finishDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (drag?.pointerId === event.pointerId) setDrag(null);
  };

  const confirm = async () => {
    if (!image || submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      const crop: AvatarCrop = {
        source: image.source,
        imageWidth: image.width,
        imageHeight: image.height,
        viewportSize: VIEWPORT_SIZE,
        scale,
        offsetX: offset.x,
        offsetY: offset.y
      };
      await onConfirm(await renderAvatarCrop(file, crop));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "头像处理失败，请换一张后重试。");
    } finally {
      setSubmitting(false);
    }
  };

  return <div className="avatarCropBackdrop" role="presentation">
    <section className="avatarCropDialog" role="dialog" aria-modal="true" aria-labelledby="avatar-crop-title">
      <header>
        <div><p>自定义头像</p><h2 id="avatar-crop-title">调整头像取景</h2><span>拖动图片、调整缩放；圆框内为头像展示区域。</span></div>
        <button type="button" aria-label="关闭头像裁剪" onClick={onCancel} disabled={submitting}>×</button>
      </header>
      <div className="avatarCropBody">
        <div className="avatarCropViewport" onPointerDown={startDrag} onPointerMove={moveDrag} onPointerUp={finishDrag} onPointerCancel={finishDrag}>
          {renderedSize && image
            ? <img src={image.source} alt="待裁剪头像" draggable="false" style={{ width: renderedSize.width, height: renderedSize.height, transform: `translate(calc(-50% + ${offset.x}px), calc(-50% + ${offset.y}px))` }}/>
            : <span className="avatarCropLoading"><LoaderCircle size={20}/>{error ? "读取失败" : "正在读取图片…"}</span>}
        </div>
        <label className="avatarCropZoom">缩放
          <button type="button" aria-label="缩小头像" onClick={() => updateZoom(zoom - 0.1)} disabled={!image || zoom <= 1 || submitting}><Minus size={15}/></button>
          <input aria-label="头像缩放" type="range" min="1" max={MAX_ZOOM} step="0.01" value={zoom} onChange={(event) => updateZoom(Number(event.target.value))} disabled={!image || submitting}/>
          <button type="button" aria-label="放大头像" onClick={() => updateZoom(zoom + 0.1)} disabled={!image || zoom >= MAX_ZOOM || submitting}><Plus size={15}/></button>
        </label>
        {error && <p className="profileDialogError avatarCropError" role="alert">{error}</p>}
      </div>
      <footer><button type="button" onClick={onCancel} disabled={submitting}>取消</button><button type="button" className="primary" onClick={() => { void confirm(); }} disabled={!image || submitting}>{submitting && <LoaderCircle className="uploadSpinner" size={15}/>}确认并上传</button></footer>
    </section>
  </div>;
}

function clampOffset(offset: { x: number; y: number }, renderedSize: { width: number; height: number }) {
  const maxX = Math.abs(VIEWPORT_SIZE - renderedSize.width) / 2;
  const maxY = Math.abs(VIEWPORT_SIZE - renderedSize.height) / 2;
  return {
    x: Math.max(-maxX, Math.min(maxX, offset.x)),
    y: Math.max(-maxY, Math.min(maxY, offset.y))
  };
}

function loadCropImage(file: File): Promise<ImageState> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("图片读取失败，请换一张后重试。"));
    reader.onload = () => {
      if (typeof reader.result !== "string") {
        reject(new Error("图片读取失败，请换一张后重试。"));
        return;
      }
      const image = new Image();
      image.onerror = () => reject(new Error("图片无法读取，请换一张后重试。"));
      image.onload = () => {
        const width = image.naturalWidth || image.width;
        const height = image.naturalHeight || image.height;
        if (!width || !height) reject(new Error("图片无法读取，请换一张后重试。"));
        else resolve({ source: reader.result as string, width, height });
      };
      image.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}
