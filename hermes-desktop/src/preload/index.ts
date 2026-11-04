import { contextBridge, ipcRenderer } from "electron";

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

contextBridge.exposeInMainWorld("coworkDesktop", {
  platform: process.platform,
  selectFolder: (): Promise<string | null> => ipcRenderer.invoke("desktop.select-folder"),
  getWorkspace: (): Promise<{ path: string | null }> => ipcRenderer.invoke("desktop.get-workspace"),
  runtimeStatus: (): Promise<{ python: { available: boolean; version?: string; path?: string; error?: string }; node: { available: boolean; version?: string; path?: string; error?: string } }> => ipcRenderer.invoke("desktop.runtime-status"),
  updateStatus: (): Promise<DesktopUpdateStatus> => ipcRenderer.invoke("desktop.update-status"),
  checkForUpdates: (options?: { interactive?: boolean }): Promise<DesktopUpdateStatus> => ipcRenderer.invoke("desktop.update-check", options?.interactive !== false),
  downloadUpdate: (): Promise<DesktopUpdateStatus> => ipcRenderer.invoke("desktop.update-download"),
  installUpdate: (): Promise<DesktopUpdateStatus> => ipcRenderer.invoke("desktop.update-install"),
  onUpdateStatus: (listener: (status: DesktopUpdateStatus) => void): (() => void) => {
    const handler = (_event: Electron.IpcRendererEvent, value: DesktopUpdateStatus) => listener(value);
    ipcRenderer.on("desktop.update-status", handler);
    return () => ipcRenderer.removeListener("desktop.update-status", handler);
  },
  onSystemResume: (listener: () => void): (() => void) => {
    const handler = () => listener();
    ipcRenderer.on("desktop.system-resumed", handler);
    return () => ipcRenderer.removeListener("desktop.system-resumed", handler);
  },
  requestCapability: (request: { requestId: string; capability: "workspace.list" | "workspace.read" | "workspace.write" | "workspace.mkdir" | "workspace.trash" | "command.run" | "browser.open" | "browser.session_start" | "browser.navigate" | "browser.snapshot" | "browser.click" | "browser.type" | "browser.press" | "browser.scroll" | "browser.screenshot" | "browser.session_close" | "browser.read" | "browser.wait" | "browser.hover" | "browser.select" | "browser.upload" | "browser.downloads" | "browser.back" | "browser.reload" | "browser.tab_list" | "browser.tab_new" | "browser.tab_select" | "browser.tab_close"; args: Record<string, unknown> }): Promise<{ requestId: string; ok: boolean; value?: unknown; error?: string; canceled?: boolean; requiresApproval?: boolean; approval?: { scope: "external"; question: string; target?: string } }> => ipcRenderer.invoke("desktop.request-capability", request),
  resolveCapability: (request: { requestId: string; approved: boolean }): Promise<{ requestId: string; ok: boolean; value?: unknown; error?: string; canceled?: boolean; acknowledged?: boolean }> => ipcRenderer.invoke("desktop.resolve-capability", request),
  onCapabilityResult: (listener: (result: { requestId: string; ok: boolean; value?: unknown; error?: string; canceled?: boolean }) => void): (() => void) => {
    const handler = (_event: Electron.IpcRendererEvent, value: { requestId: string; ok: boolean; value?: unknown; error?: string; canceled?: boolean }) => listener(value);
    ipcRenderer.on("desktop.capability-result", handler);
    return () => ipcRenderer.removeListener("desktop.capability-result", handler);
  },
  clearWorkspace: (): Promise<boolean> => ipcRenderer.invoke("desktop.clear-workspace"),
  listDirectory: (relativePath = ""): Promise<Array<{ name: string; kind: "file" | "directory"; size?: number }>> =>
    ipcRenderer.invoke("desktop.list-directory", relativePath),
  readTextFile: (relativePath: string): Promise<string> => ipcRenderer.invoke("desktop.read-text-file", relativePath),
  readHtmlPreview: (path: string): Promise<string> => ipcRenderer.invoke("desktop.read-html-preview", path),
  openHtmlPreview: (html: string): Promise<boolean> => ipcRenderer.invoke("desktop.open-html-preview", html),
  writeTextFile: (relativePath: string, content: string): Promise<boolean> => ipcRenderer.invoke("desktop.write-text-file", relativePath, content),
  createDirectory: (relativePath: string): Promise<boolean> => ipcRenderer.invoke("desktop.create-directory", relativePath),
  trashFile: (relativePath: string): Promise<boolean> => ipcRenderer.invoke("desktop.trash-file", relativePath),
  openFile: (relativePath: string): Promise<boolean> => ipcRenderer.invoke("desktop.open-file", relativePath),
  openBrowser: (url: string): Promise<boolean> => ipcRenderer.invoke("desktop.open-browser", url),
  runCommand: (command: string): Promise<{ exitCode: number | null; signal: string | null; stdout: string; stderr: string; timedOut: boolean; canceled?: boolean }> =>
    ipcRenderer.invoke("desktop.run-command", command)
});
