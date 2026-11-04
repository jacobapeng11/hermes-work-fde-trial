import { useEffect, useMemo } from "react";
import { LoaderCircle } from "lucide-react";

const LOADING_MESSAGE = "身份认证，获取权限内数据，马上好...";

function dataAppReturnPath(rawReturnTo: string | null): string | null {
  if (!rawReturnTo) return null;
  try {
    const target = new URL(rawReturnTo, window.location.origin);
    if (target.origin !== window.location.origin) return null;
    if (!target.pathname.startsWith("/api/v1/user/data-apps/")) return null;
    return `${target.pathname}${target.search}${target.hash}`;
  } catch {
    return null;
  }
}

export function DataAppAuthenticationPage() {
  const returnTo = useMemo(
    () => dataAppReturnPath(new URLSearchParams(window.location.search).get("return_to")),
    []
  );

  useEffect(() => {
    if (returnTo) window.location.replace(returnTo);
  }, [returnTo]);

  return (
    <main className="authLoadingMinimal">
      <div
        className="authLoadingSpinner"
        role="status"
        aria-label={LOADING_MESSAGE}
      >
        <LoaderCircle size={28} aria-hidden="true" />
      </div>
      <p className="dataAppAuthMessage">{LOADING_MESSAGE}</p>
      {!returnTo && <p className="dataAppAuthError">看板链接无效，请从平台重新打开</p>}
    </main>
  );
}
