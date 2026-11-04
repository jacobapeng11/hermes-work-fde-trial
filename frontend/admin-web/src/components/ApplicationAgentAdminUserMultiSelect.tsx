import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  ApplicationAgentAdminCandidateSummary,
  ApplicationAgentAdminSummary
} from "@hermes-cloud/schemas";
import { adminApi } from "../api/client";


interface Props {
  value: string[];
  onChange: (value: string[]) => void;
  selectedUsers: ApplicationAgentAdminSummary[];
  disabled?: boolean;
  searchUsers?: (
    keyword: string,
    limit: number
  ) => Promise<ApplicationAgentAdminCandidateSummary[]>;
}


function userLabel(item: {
  name: string;
  userCode?: string | null;
  status?: string;
}): string {
  if (item.status && item.status !== "active") {
    return item.userCode
      ? `${item.name}（${item.userCode}，已失效）`
      : `${item.name}（已失效）`;
  }
  return item.userCode ? `${item.name}（${item.userCode}）` : item.name;
}


export function ApplicationAgentAdminUserMultiSelect({
  value,
  onChange,
  selectedUsers,
  disabled = false,
  searchUsers = adminApi.applicationAgentAdminCandidates
}: Props) {
  const rootRef = useRef<HTMLDivElement>(null);
  const requestRef = useRef(0);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [options, setOptions] = useState<ApplicationAgentAdminCandidateSummary[]>([]);
  const [labels, setLabels] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selectedLabels = useMemo(
    () => Object.fromEntries(
      selectedUsers
        .filter((item) => item.userCode)
        .map((item) => [item.userCode as string, userLabel(item)])
    ),
    [selectedUsers]
  );

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
      void searchUsers(keyword, 50)
        .then((items) => {
          if (requestRef.current !== requestId) return;
          setOptions(items);
          setLabels((current) => ({
            ...current,
            ...Object.fromEntries(items.map((item) => [item.userCode, userLabel(item)]))
          }));
        })
        .catch(() => {
          if (requestRef.current === requestId) setError("用户搜索失败，请重试");
        })
        .finally(() => {
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

  return (
    <div className="applicationAgentUserSelect" ref={rootRef}>
      <div className="applicationAgentUserSelectTags">
        {value.map((userCode) => {
          const label = selectedLabels[userCode] || labels[userCode] || userCode;
          return (
            <span key={userCode}>
              {label}
              <button
                type="button"
                aria-label={`移除 ${label}`}
                disabled={disabled}
                onClick={() => onChange(value.filter((item) => item !== userCode))}
              >
                ×
              </button>
            </span>
          );
        })}
        <input
          role="combobox"
          aria-label="搜索人员"
          aria-expanded={open}
          disabled={disabled}
          value={query}
          placeholder={value.length ? "继续搜索人员" : "输入姓名或工号"}
          onFocus={() => setOpen(true)}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      {open && !disabled && (
        <div className="applicationAgentUserSelectPopup" role="listbox">
          {!query.trim() && <p>请输入姓名或工号搜索</p>}
          {loading && <p>正在搜索…</p>}
          {error && <p className="adminInlineError" role="alert">{error}</p>}
          {!loading && !error && query.trim() && options.length === 0 && (
            <p>未找到匹配人员</p>
          )}
          {options.map((item) => {
            const selected = value.includes(item.userCode);
            const inactive = item.status !== "active";
            return (
              <button
                type="button"
                role="option"
                aria-label={userLabel(item)}
                aria-selected={selected}
                disabled={inactive}
                key={item.userCode}
                onClick={() => {
                  onChange(
                    selected
                      ? value.filter((userCode) => userCode !== item.userCode)
                      : [...value, item.userCode]
                  );
                  closeSearch();
                }}
              >
                <strong>{item.name}</strong>
                <span>{item.userCode}{inactive ? " · 已失效" : ""}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
