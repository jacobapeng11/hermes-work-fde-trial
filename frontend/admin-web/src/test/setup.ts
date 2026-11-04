import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

vi.stubEnv(
  "VITE_LEGACY_SSO_LOGIN_BASE_URL",
  "https://login.example.com/sd/index.php/api/clue/login_mobile"
);
vi.stubEnv("VITE_LEGACY_SSO_APP_NAME", "agent-uat-v2");
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
