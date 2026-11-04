/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_LEGACY_SSO_LOGIN_BASE_URL?: string;
  readonly VITE_LEGACY_SSO_APP_NAME?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
