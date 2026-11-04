export {};

type DesktopUpdateStatus = {
  state: "disabled" | "checking" | "available" | "not-available" | "downloading" | "downloaded" | "error";
  currentVersion: string;
  version?: string;
  releaseDate?: string;
  percent?: number;
  transferred?: number;
  total?: number;
  error?: string;
};

declare global {
  interface Window {
    coworkDesktop: {
      platform: string;
      selectFolder(): Promise<string | null>;
      getWorkspace(): Promise<{ path: string | null }>;
      runtimeStatus(): Promise<{ python: { available: boolean; version?: string; path?: string; error?: string }; node: { available: boolean; version?: string; path?: string; error?: string } }>;
      updateStatus(): Promise<DesktopUpdateStatus>;
      checkForUpdates(options?: { interactive?: boolean }): Promise<DesktopUpdateStatus>;
      downloadUpdate(): Promise<DesktopUpdateStatus>;
      installUpdate(): Promise<DesktopUpdateStatus>;
      onUpdateStatus(listener: (status: DesktopUpdateStatus) => void): () => void;
      onSystemResume(listener: () => void): () => void;
      requestCapability(request: { requestId: string; capability: "workspace.list" | "workspace.read" | "workspace.write" | "workspace.mkdir" | "workspace.trash" | "command.run" | "browser.open" | "browser.session_start" | "browser.navigate" | "browser.snapshot" | "browser.click" | "browser.type" | "browser.press" | "browser.scroll" | "browser.screenshot" | "browser.session_close" | "browser.read" | "browser.wait" | "browser.hover" | "browser.select" | "browser.upload" | "browser.downloads" | "browser.back" | "browser.reload" | "browser.tab_list" | "browser.tab_new" | "browser.tab_select" | "browser.tab_close"; args: Record<string, unknown> }): Promise<{ requestId: string; ok: boolean; value?: unknown; error?: string; canceled?: boolean; requiresApproval?: boolean; approval?: { scope: "external"; question: string; target?: string } }>;
      resolveCapability(request: { requestId: string; approved: boolean }): Promise<{ requestId: string; ok: boolean; value?: unknown; error?: string; canceled?: boolean; acknowledged?: boolean }>;
      onCapabilityResult(listener: (result: { requestId: string; ok: boolean; value?: unknown; error?: string; canceled?: boolean }) => void): () => void;
      clearWorkspace(): Promise<boolean>;
      listDirectory(path?: string): Promise<Array<{ name: string; kind: "file" | "directory"; size?: number }>>;
      readTextFile(path: string): Promise<string>;
      readHtmlPreview(path: string): Promise<string>;
      openHtmlPreview(html: string): Promise<boolean>;
      writeTextFile(path: string, content: string): Promise<boolean>;
      createDirectory(path: string): Promise<boolean>;
      trashFile(path: string): Promise<boolean>;
      openFile(path: string): Promise<boolean>;
      openBrowser(url: string): Promise<boolean>;
      runCommand(command: string): Promise<{ exitCode: number | null; signal: string | null; stdout: string; stderr: string; timedOut: boolean; canceled?: boolean }>;
    };
  }
}
