import { AgentWorkspace } from "./AgentWorkspace";

export function UserChatPage() {
  return <AgentWorkspace context={{ kind: "personal" }} />;
}
