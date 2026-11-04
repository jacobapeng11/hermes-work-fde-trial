/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_LEGACY_SSO_LOGIN_BASE_URL?: string;
  readonly VITE_LEGACY_SSO_APP_NAME?: string;
  readonly VITE_TTFT_FIRST_VISIBLE_IMMEDIATE?: string;
  readonly VITE_TTFT_DELTA_FRAME_BATCHING?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
