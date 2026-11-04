import { useState, type FormEvent } from "react";
import { adminApi } from "../api/client";

export function AdminLoginPage({ onAuthenticated }: { onAuthenticated: () => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (username !== "admin") {
      setError("仅支持管理员账号 admin");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await adminApi.login(username, password);
      onAuthenticated();
    } catch {
      setError("用户名或密码错误");
    } finally {
      setSubmitting(false);
    }
  }

  return <main className="adminLoginShell">
    <form className="adminLoginCard" onSubmit={submit}>
      <div>
        <p className="adminLoginEyebrow">HERMES CLOUD</p>
        <h1>管理端登录</h1>
        <p className="adminLoginHint">请输入管理员凭据以继续。</p>
      </div>
      <label>
        用户名
        <input
          autoComplete="username"
          disabled={submitting}
          onChange={(event) => setUsername(event.target.value)}
          placeholder="admin"
          value={username}
        />
      </label>
      <label>
        密码
        <input
          autoComplete="current-password"
          disabled={submitting}
          onChange={(event) => setPassword(event.target.value)}
          required
          type="password"
          value={password}
        />
      </label>
      {error ? <p aria-live="polite" className="adminLoginError">{error}</p> : null}
      <button disabled={submitting} type="submit">{submitting ? "正在登录…" : "登录"}</button>
    </form>
  </main>;
}
