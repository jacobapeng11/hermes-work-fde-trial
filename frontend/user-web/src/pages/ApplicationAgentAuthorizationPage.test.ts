import { describe, expect, it } from "vitest";
import {
  buildAuthorizationCallbackUrl,
  parseAuthorizationParameters
} from "./ApplicationAgentAuthorizationPage";

describe("ApplicationAgentAuthorizationPage contract", () => {
  it("accepts the SQLBot authorization contract and preserves opaque state", () => {
    const state = "state_abcdefghijklmnopqrstuvwxyz0123456789";
    const target = new URL("https://agent.example.com/user/application-agents/authorize");
    target.searchParams.set("application_agent", "sqlbot2");
    target.searchParams.set("client_id", "app_43_client");
    target.searchParams.set(
      "redirect_uri",
      "https://sqlbot.example.com/auth/callback"
    );
    target.searchParams.set("state", state);

    expect(parseAuthorizationParameters(target.toString())).toEqual({
      applicationAgent: "sqlbot2",
      clientId: "app_43_client",
      redirectUri: "https://sqlbot.example.com/auth/callback",
      state
    });
  });

  it("rejects missing or browser-generated-looking short state", () => {
    expect(parseAuthorizationParameters(
      "https://agent.example.com/user/application-agents/authorize"
    )).toBeNull();

    const target = new URL("https://agent.example.com/user/application-agents/authorize");
    target.searchParams.set("application_agent", "sqlbot2");
    target.searchParams.set("client_id", "app_43_client");
    target.searchParams.set(
      "redirect_uri",
      "https://sqlbot.example.com/auth/callback"
    );
    target.searchParams.set("state", "too-short");
    expect(parseAuthorizationParameters(target.toString())).toBeNull();
  });

  it("adds one-time code and state without discarding safe existing query", () => {
    const callback = new URL(buildAuthorizationCallbackUrl(
      "https://sqlbot.example.com/auth/callback?source=hermes",
      {
        code: "hca_code_once",
        state: "state_abcdefghijklmnopqrstuvwxyz0123456789"
      }
    ));

    expect(callback.searchParams.get("source")).toBe("hermes");
    expect(callback.searchParams.get("code")).toBe("hca_code_once");
    expect(callback.searchParams.get("state")).toBe(
      "state_abcdefghijklmnopqrstuvwxyz0123456789"
    );
  });
});
