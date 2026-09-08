import React from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/jetbrains-mono";
import "@fontsource-variable/noto-sans-sc";
import { App } from "./App.jsx";
import { InspectionEntry } from "./InspectionEntry.jsx";
import "./styles.css";

const inspectionRun = new URLSearchParams(window.location.search).get("agentInspection");
const evidenceRun = new URLSearchParams(window.location.search).get("agentEvidence");

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    {inspectionRun && evidenceRun ? <main role="alert">请选择一个证据入口，不能同时指定审核前快照和终态报告。</main>
      : inspectionRun || evidenceRun ? <InspectionEntry generationRunId={inspectionRun || evidenceRun} terminalMode={!!evidenceRun} /> : <App />}
  </React.StrictMode>,
);
