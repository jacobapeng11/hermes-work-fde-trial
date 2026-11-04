import { useEffect, useState, type ReactNode } from "react";

document.documentElement.dataset.desktopPlatform = window.coworkDesktop?.platform ?? "web";

function readTheme(): "dark" | "light" {
  // Prefer the live shell attribute: the workspace applies theme switches to
  // .appShell first, and this layer must follow even before localStorage is
  // updated in the same tick.
  const current = document.querySelector(".appShell[data-theme]")?.getAttribute("data-theme");
  if (current === "light" || current === "dark") return current;
  // Light is the product default; dark applies only when explicitly chosen.
  return window.localStorage.getItem("hermes.theme") === "dark" ? "dark" : "light";
}

// Desktop-only overlay components (workspace bar, files panel, update notice)
// are siblings of the page shell, so the theme tokens defined on
// .appShell[data-theme] never reach them and they used to fall back to dark
// colors even in light mode. This wrapper mirrors the app theme onto its own
// data-theme attribute — the token blocks in tokens.css also match
// .desktopLayer — and follows runtime theme switches via a MutationObserver.
export function DesktopLayer({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<"dark" | "light">(readTheme);
  useEffect(() => {
    const sync = () => setTheme(readTheme());
    window.addEventListener("storage", sync);
    const observer = new MutationObserver(sync);
    observer.observe(document.body, { subtree: true, attributeFilter: ["data-theme"] });
    return () => {
      window.removeEventListener("storage", sync);
      observer.disconnect();
    };
  }, []);
  return <div className="desktopLayer" data-theme={theme}>{children}</div>;
}
