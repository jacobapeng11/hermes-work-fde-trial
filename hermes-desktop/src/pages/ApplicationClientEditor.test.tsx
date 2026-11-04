import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApplicationClientEditor } from "./ApplicationClientEditor";


describe("ApplicationClientEditor", () => {
  it("hides callback values and preserves them when saving", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);

    render(<ApplicationClientEditor
      client={{
        applicationClientId: 91,
        clientId: "app_43_sqlbot",
        name: "SQLBot",
        status: "active",
        allowUserDelegation: true,
        allowServiceInvocation: false,
        authMode: "delegated_user",
        scopes: ["responses:create"],
        allowedRedirectUris: [
          "https://sqlbot.example.com/auth/hermes/callback"
        ],
        createdAt: null
      }}
      busy={false}
      onSave={onSave}
    />);

    expect(screen.queryByText(/授权回调地址/)).not.toBeInTheDocument();
    expect(screen.queryByText(/sqlbot\.example\.com/)).not.toBeInTheDocument();

    await user.click(screen.getByLabelText("允许无用户身份的服务调用"));
    await user.click(screen.getByRole("button", {
      name: "保存调用方设置"
    }));

    expect(onSave).toHaveBeenCalledWith({
      name: "SQLBot",
      allowUserDelegation: false,
      allowServiceInvocation: true,
      allowedRedirectUris: [
        "https://sqlbot.example.com/auth/hermes/callback"
      ]
    });
  });
});
