import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";
import { vi } from "vitest";

declare const process: { env: { NODE_ENV?: string } };

// vitest 默认 NODE_ENV=test，rc-util 的 useId 在 test 下返回常量 "test-id"，
// 同屏多个 antd 弹窗 aria-labelledby 指向同一元素、可访问名互撞。改回
// development 让 useId 走 React.useId（唯一 id）。对源码无影响（不读 NODE_ENV）。
process.env.NODE_ENV = "development";

// 技能面板等模块经 React.lazy 动态加载，jsdom 下 chunk 解析慢于静态导入；
// 放宽异步查询超时（查询命中即返回，仅在需要等待时生效）。
configure({ asyncUtilTimeout: 5000 });

vi.stubEnv(
  "VITE_LEGACY_SSO_LOGIN_BASE_URL",
  "https://login.example.com/sd/index.php/api/clue/login_mobile"
);
vi.stubEnv("VITE_LEGACY_SSO_APP_NAME", "agent-uat-v2");

// 本机（Windows + Node 25 + vitest jsdom）环境中 window.localStorage/sessionStorage
// 是无方法的空对象，导致依赖 Web Storage 的既有测试预先失败（与本次改动无关）。
// 守卫式兜底：仅当实现缺失时替换为内存版；健康环境下此代码为空操作。
class MemoryStorage {
  private store = new Map<string, string>();
  get length(): number {
    return this.store.size;
  }
  key(index: number): string | null {
    return [...this.store.keys()][index] ?? null;
  }
  getItem(key: string): string | null {
    return this.store.has(key) ? this.store.get(key) as string : null;
  }
  setItem(key: string, value: string): void {
    this.store.set(String(key), String(value));
  }
  removeItem(key: string): void {
    this.store.delete(key);
  }
  clear(): void {
    this.store.clear();
  }
}

function ensureWebStorage(name: "localStorage" | "sessionStorage"): void {
  const current = (globalThis as unknown as Record<string, Storage | undefined>)[name];
  if (typeof current?.getItem === "function") return;
  const storage: Storage = new MemoryStorage() as unknown as Storage;
  Object.defineProperty(globalThis, name, { value: storage, writable: true, configurable: true });
  const win = (globalThis as { window?: typeof globalThis }).window;
  if (win && win !== globalThis) {
    Object.defineProperty(win, name, { value: storage, writable: true, configurable: true });
  }
}

ensureWebStorage("localStorage");
ensureWebStorage("sessionStorage");

// jsdom 不支持 getComputedStyle 的伪元素两参形式；antd/rc-motion 的弹层动画会
// 调用它并抛「Not implemented」，导致 Select 下拉、Modal 进场等状态机在测试中
// 卡住（弹层不可见、选项点击无效）。忽略伪元素参数即可。
const computedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (elt: Element, _pseudoElt?: string | null): CSSStyleDeclaration =>
  computedStyle(elt);

// jsdom 缺少 matchMedia，antd 响应式组件（Grid/useBreakpoint 等）依赖它。
if (typeof window !== "undefined" && !window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }),
  });
}
