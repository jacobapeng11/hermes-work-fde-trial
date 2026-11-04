import { LoaderCircle, Shield } from "lucide-react";

export function AdminAuthenticationTransition() {
  return (
    <main className="adminAuthTransition" aria-busy="true">
      <div className="adminAuthTransitionFrame" aria-hidden="true">
        <aside className="adminAuthTransitionSidebar">
          <div className="adminAuthTransitionBrand">
            <Shield size={24} />
            <span>Hermes Console</span>
          </div>
          <div className="adminAuthTransitionNav">
            {["wide", "medium", "wide", "short", "medium"].map((width, index) => (
              <i data-width={width} key={index} />
            ))}
          </div>
        </aside>
        <section className="adminAuthTransitionWorkspace">
          <header><div><i /><span /></div><i /></header>
          <div className="adminAuthTransitionPanels"><i /><i /><i /><span /></div>
        </section>
      </div>
      <div className="adminAuthTransitionVeil">
        <section
          className="adminAuthTransitionStatus"
          role="status"
          aria-label="正在重新认证…"
        >
          <LoaderCircle size={28} aria-hidden="true" />
          <strong>正在重新认证…</strong>
          <span>认证完成后将返回管理后台</span>
        </section>
      </div>
    </main>
  );
}
