import React from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/jetbrains-mono";
import "@fontsource-variable/noto-sans-sc";
import { App } from "./App.jsx";
import { FrameworkSmokeInspection } from "./FrameworkSmokeInspection.jsx";
import "./styles.css";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    {new URLSearchParams(window.location.search).has("frameworkSmoke") ?
      <FrameworkSmokeInspection taskId={new URLSearchParams(window.location.search).get("frameworkSmoke")} /> :
      <App />}
  </React.StrictMode>,
);
