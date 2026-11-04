import { useCallback, useEffect, useState } from "react";

export const ERROR_NOTICE_AUTO_DISMISS_MS = 5000;

/**
 * 聊天工作区右上角报错框（.errorNotice）的错误状态。
 * 设置错误后 autoDismissMs 毫秒自动清除，避免报错框一直停留；
 * 相同文案重复出现时通过 tick 重新计时；显式 setError(null) 立即清除。
 */
export function useAutoDismissError(autoDismissMs: number = ERROR_NOTICE_AUTO_DISMISS_MS) {
  const [error, setErrorState] = useState<string | null>(null);
  const [errorTick, setErrorTick] = useState(0);
  const setError = useCallback((message: string | null) => {
    setErrorState(message);
    setErrorTick((tick) => tick + 1);
  }, []);
  useEffect(() => {
    if (!error) return;
    const timer = setTimeout(() => setErrorState(null), autoDismissMs);
    return () => clearTimeout(timer);
  }, [error, errorTick, autoDismissMs]);
  return [error, setError] as const;
}
