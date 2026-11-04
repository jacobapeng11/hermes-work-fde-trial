import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { DirectoryUserSearchItem, PersonalKnowledgeSelectedUser } from "@hermes-cloud/schemas";
import { userApi } from "../api/client";


interface Props {
  value: number[];
  onChange: (value: number[]) => void;
  searchUsers?: (keyword: string, limit: number) => Promise<DirectoryUserSearchItem[]>;
  selectedUsers?: PersonalKnowledgeSelectedUser[];
}


function userLabel(item: DirectoryUserSearchItem): string {
  return item.userCode ? `${item.name}（${item.userCode}）` : item.name;
}


function selectedUserLabel(item: PersonalKnowledgeSelectedUser): string {
  if (item.status === "missing" || !item.name) return `已失效用户（${item.id}）`;
  if (item.status === "inactive") {
    return item.userCode
      ? `${item.name}（${item.userCode}，已失效）`
      : `${item.name}（已失效）`;
  }
  return item.userCode ? `${item.name}（${item.userCode}）` : item.name;
}


export function DirectoryUserMultiSelect({
  value,
  onChange,
  searchUsers = userApi.searchDirectoryUsers,
  selectedUsers
}: Props) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [options, setOptions] = useState<DirectoryUserSearchItem[]>([]);
  const [labels, setLabels] = useState<Record<number, string>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestRef = useRef(0);
  const selectedLabels = useMemo(() => Object.fromEntries(
    (selectedUsers || []).map((item) => [item.id, selectedUserLabel(item)])
  ) as Record<number, string>, [selectedUsers]);

  const closeSearch = useCallback(() => {
    requestRef.current += 1;
    setOpen(false);
    setQuery("");
    setOptions([]);
    setLoading(false);
    setError(null);
  }, []);

  useEffect(() => {
    const keyword = query.trim();
    if (!open || !keyword) {
      setOptions([]);
      setLoading(false);
      setError(null);
      return;
    }
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    const timer = window.setTimeout(() => {
      setLoading(true);
      setError(null);
      void searchUsers(keyword, 50).then((items) => {
        if (requestRef.current !== requestId) return;
        setOptions(items);
        setLabels((current) => {
          const next = { ...current };
          items.forEach((item) => { next[item.id] = userLabel(item); });
          return next;
        });
      }).catch(() => {
        if (requestRef.current === requestId) setError("用户搜索失败，请重试");
      }).finally(() => {
        if (requestRef.current === requestId) setLoading(false);
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [open, query, searchUsers]);

  useEffect(() => {
    const handleOutsidePointerDown = (event: PointerEvent) => {
      if (rootRef.current?.contains(event.target as Node)) return;
      closeSearch();
    };
    document.addEventListener("pointerdown", handleOutsidePointerDown);
    return () => {
      document.removeEventListener("pointerdown", handleOutsidePointerDown);
      requestRef.current += 1;
    };
  }, [closeSearch]);

  return <div className="directoryMultiSelect" ref={rootRef}>
    <div className="directoryMultiSelectTags">
      {value.map((id) => {
        const label = selectedLabels[id] || labels[id] || `已失效用户（${id}）`;
        return <span key={id}>{label}<button type="button" aria-label={`移除 ${label}`} onClick={() => onChange(value.filter((item) => item !== id))}>×</button></span>;
      })}
      <input
        role="combobox"
        aria-label="搜索用户"
        aria-expanded={open}
        value={query}
        placeholder={value.length ? "继续搜索" : "输入姓名或工号"}
        onFocus={() => setOpen(true)}
        onChange={(event) => setQuery(event.target.value)}
      />
    </div>
    {open && <div className="directoryMultiSelectPopup" role="listbox">
      {!query.trim() && <p>请输入姓名或工号搜索</p>}
      {loading && <p>正在搜索…</p>}
      {error && <p className="pkbError">{error}</p>}
      {!loading && !error && query.trim() && options.length === 0 && <p>未找到匹配用户</p>}
      {options.map((item) => {
        const label = userLabel(item);
        const selected = value.includes(item.id);
        return <button
          type="button"
          role="option"
          aria-label={label}
          aria-selected={selected}
          key={item.id}
          onClick={() => {
            onChange(selected ? value.filter((id) => id !== item.id) : [...value, item.id]);
            closeSearch();
          }}
        ><strong>{item.name}</strong>{item.userCode && <span>{item.userCode}</span>}</button>;
      })}
    </div>}
  </div>;
}
