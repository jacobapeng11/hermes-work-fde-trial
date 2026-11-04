import { AdminDashboardPage } from "./pages/AdminDashboardPage";
import { AuthGate } from "./components/AuthGate";

export function App() {
  return <AuthGate><AdminDashboardPage /></AuthGate>;
}
