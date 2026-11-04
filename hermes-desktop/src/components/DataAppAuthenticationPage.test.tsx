import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DataAppAuthenticationPage } from "./DataAppAuthenticationPage";

describe("DataAppAuthenticationPage", () => {
  let replace: ReturnType<typeof vi.fn>;
  let originalLocation: Location;

  beforeEach(() => {
    replace = vi.fn();
    originalLocation = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      get() {
        return {
          get origin() { return originalLocation.origin; },
          get pathname() { return originalLocation.pathname; },
          get search() { return originalLocation.search; },
          replace
        };
      }
    });
  });

  afterEach(() => {
    window.history.pushState(null, "", "/");
    Object.defineProperty(window, "location", {
      value: originalLocation,
      configurable: true
    });
  });

  it("shows the specified message and returns to the data app", () => {
    window.history.pushState(
      null,
      "",
      "/user/data-app-auth?return_to=%2Fapi%2Fv1%2Fuser%2Fdata-apps%2F11%2Findex.html"
    );

    render(<DataAppAuthenticationPage />);

    expect(
      screen.getByRole("status", { name: "身份认证，获取权限内数据，马上好..." })
    ).toBeVisible();
    expect(screen.getByText("身份认证，获取权限内数据，马上好...")).toBeVisible();
    expect(replace).toHaveBeenCalledWith("/api/v1/user/data-apps/11/index.html");
  });

  it("rejects a cross-origin return target", () => {
    window.history.pushState(
      null,
      "",
      "/user/data-app-auth?return_to=https%3A%2F%2Fexample.invalid%2Freport"
    );

    render(<DataAppAuthenticationPage />);

    expect(screen.getByText("看板链接无效，请从平台重新打开")).toBeVisible();
    expect(replace).not.toHaveBeenCalled();
  });
});
