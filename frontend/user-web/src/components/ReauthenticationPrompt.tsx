type ReauthenticationPromptProps = {
  theme: "light" | "dark";
  onAuthenticate: () => void;
};

export function ReauthenticationPrompt({
  theme,
  onAuthenticate
}: ReauthenticationPromptProps) {
  return (
    <div className="authReauthBackdrop" data-theme={theme}>
      <section
        className="authReauthDialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="auth-reauth-title"
      >
        <h2 id="auth-reauth-title">需要重新认证</h2>
        <p>自动认证未成功。当前页面和任务可在认证后从原地址继续恢复。</p>
        <button type="button" onClick={onAuthenticate}>
          重新认证
        </button>
      </section>
    </div>
  );
}
