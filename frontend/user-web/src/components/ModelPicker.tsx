import { Check, ChevronDown, Search } from "lucide-react";
import type { KeyboardEvent } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { RuntimeModelSummary } from "@hermes-cloud/schemas";
import "./composer-dropdowns.css";

interface ModelPickerProps {
  modelId: number | null;
  models: RuntimeModelSummary[];
  onModelChange: (modelId: number) => void;
  disabled?: boolean;
}

export function ModelPicker({ modelId, models, onModelChange, disabled = false }: ModelPickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const selectedModel = models.find((model) => model.modelEntryId === modelId);
  const unavailable = disabled || models.length === 0;

  const groups = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    const filteredModels = normalizedQuery
      ? models.filter((model) => [model.displayName, model.modelKey, model.providerDisplayName]
        .some((value) => value.toLocaleLowerCase().includes(normalizedQuery)))
      : models;

    return Array.from(filteredModels.reduce((result, model) => {
      const key = `${model.providerId}:${model.providerDisplayName}`;
      const group = result.get(key);
      if (group) group.models.push(model);
      else result.set(key, { name: model.providerDisplayName, models: [model] });
      return result;
    }, new Map<string, { name: string; models: RuntimeModelSummary[] }>()).values());
  }, [models, query]);

  const close = (restoreFocus = false) => {
    setOpen(false);
    setQuery("");
    if (restoreFocus) triggerRef.current?.focus();
  };

  useEffect(() => {
    if (!open) return;
    const handleOutsidePointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) close();
    };
    document.addEventListener("pointerdown", handleOutsidePointer);
    return () => document.removeEventListener("pointerdown", handleOutsidePointer);
  }, [open]);

  useEffect(() => {
    if (open) searchRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (unavailable && open) close();
  }, [open, unavailable]);

  const visibleOptions = () => Array.from(rootRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') ?? []);

  const handleDropdownKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close(true);
      return;
    }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;

    const options = visibleOptions();
    if (options.length === 0) return;
    event.preventDefault();
    const currentIndex = options.findIndex((option) => option === document.activeElement);
    const nextIndex = event.key === "ArrowDown"
      ? Math.min(currentIndex + 1, options.length - 1)
      : currentIndex < 0 ? options.length - 1 : Math.max(currentIndex - 1, 0);
    options[nextIndex]?.focus();
  };

  return <div className="modelPicker" ref={rootRef}>
    <button
      ref={triggerRef}
      className="modelPickerTrigger"
      type="button"
      aria-label="模型"
      aria-haspopup="listbox"
      aria-expanded={open}
      disabled={unavailable}
      title={selectedModel?.displayName ?? "暂无模型"}
      onClick={() => setOpen((current) => !current)}
      onKeyDown={(event) => {
        if (!open && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
          event.preventDefault();
          setOpen(true);
        }
      }}
    >
      <span>{selectedModel?.displayName ?? "暂无模型"}</span>
      <ChevronDown size={12}/>
    </button>
    {open && <div className="modelPickerDropdown composerDropdownPanel" onKeyDown={handleDropdownKeyDown}>
      <div className="modelPickerSearchArea">
        <div className="modelPickerSearchBox">
          <Search size={14} aria-hidden="true"/>
          <input
            ref={searchRef}
            className="modelPickerSearch"
            type="search"
            aria-label="搜索模型"
            placeholder="搜索模型"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
      </div>
      <div className="modelPickerList" role="listbox" aria-label="模型列表">
        {groups.map((group) => <section className="modelPickerGroup" key={group.name}>
          <div className="modelPickerGroupLabel dropdownGroupLabel">{group.name}</div>
          {group.models.map((model) => <button
            className="modelPickerOption dropdownOption"
            type="button"
            role="option"
            aria-selected={model.modelEntryId === modelId}
            key={model.modelEntryId}
            onClick={() => {
              onModelChange(model.modelEntryId);
              close(true);
            }}
          >
            <span className="modelPickerOptionCopy">
              <span className="modelPickerOptionName">{model.displayName}</span>
            </span>
            {model.modelEntryId === modelId && <Check size={14}/>}
          </button>)}
        </section>)}
        {groups.length === 0 && <div className="modelPickerEmpty">没有匹配的模型</div>}
      </div>
    </div>}
  </div>;
}
