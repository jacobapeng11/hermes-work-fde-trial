import { useEffect, useState } from "react";
import { ApplicationAgentDebugGate } from "./ApplicationAgentDebugGate";
import { ApplicationAgentPage } from "./ApplicationAgentPage";

function route() {
  const match = window.location.pathname.match(
    /^\/user\/application-agents\/(\d+)\/(manage|debug)\/?$/
  );
  return {
    applicationAgentId: match ? Number(match[1]) : null,
    page: match?.[2] === "debug" ? "debug" as const : "manage" as const
  };
}

export function ApplicationAgentArea() {
  const [current, setCurrent] = useState(route);

  useEffect(() => {
    const handlePopState = () => setCurrent(route());
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const navigate = (path: string, replace = false) => {
    window.history[replace ? "replaceState" : "pushState"](null, "", path);
    setCurrent(route());
  };

  if (current.page === "debug" && current.applicationAgentId !== null) {
    return (
      <ApplicationAgentDebugGate
        applicationAgentId={current.applicationAgentId}
        onReturnManage={() => navigate(
          `/user/application-agents/${current.applicationAgentId}/manage`,
          true
        )}
      />
    );
  }

  return (
    <ApplicationAgentPage
      onEnterDebug={(applicationAgentId) => navigate(
        `/user/application-agents/${applicationAgentId}/debug`
      )}
      onPathChange={() => setCurrent(route())}
    />
  );
}
