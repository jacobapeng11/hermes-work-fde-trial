import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { PlatformThemeProvider } from "./theme/platform-theme";
import "./theme/tokens.css";
import "./theme/motion.css";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <PlatformThemeProvider>
      <App />
    </PlatformThemeProvider>
  </React.StrictMode>
);
