import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ login: vi.fn() }));
vi.mock("../api/client", () => ({ adminApi: api }));

import { AdminLoginPage } from "./AdminLoginPage";

describe("AdminLoginPage", () => {
  beforeEach(() => {
    api.login.mockReset();
  });

  it("blocks any username other than the fixed admin account", async () => {
    const user = userEvent.setup();
    render(<AdminLoginPage onAuthenticated={vi.fn()} />);

    await user.clear(screen.getByLabelText("用户名"));
    await user.type(screen.getByLabelText("用户名"), "operator");
    await user.type(screen.getByLabelText("密码"), "managed-password");
    fireEvent.submit(screen.getByRole("button", { name: "登录" }).closest("form")!);

    expect(await screen.findByText("仅支持管理员账号 admin")).toBeInTheDocument();
    expect(api.login).not.toHaveBeenCalled();
  });

  it("shows the generic error when the dedicated login endpoint rejects credentials", async () => {
    api.login.mockRejectedValueOnce(new Error("401"));
    render(<AdminLoginPage onAuthenticated={vi.fn()} />);

    await userEvent.setup().type(screen.getByLabelText("密码"), "wrong-password");
    fireEvent.submit(screen.getByRole("button", { name: "登录" }).closest("form")!);

    await waitFor(() => expect(screen.getByText("用户名或密码错误")).toBeInTheDocument());
  });
});
