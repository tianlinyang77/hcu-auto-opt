import React from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/jetbrains-mono";
import "@fontsource-variable/noto-sans-sc";
import { App } from "./App.jsx";
import { FrameworkSmokeInspection } from "./FrameworkSmokeInspection.jsx";
import { InspectionEntry } from "./InspectionEntry.jsx";
import "./styles.css";

const query = new URLSearchParams(window.location.search);
const frameworkSmokeTask = query.get("frameworkSmoke");
const inspectionRun = query.get("agentInspection");
const evidenceRun = query.get("agentEvidence");
const selectedEvidenceEntries = [frameworkSmokeTask, inspectionRun, evidenceRun].filter(Boolean);

let content = <App />;
if (selectedEvidenceEntries.length > 1) {
  content = <main role="alert">请选择一个证据入口，不能同时指定多种审核或终态报告。</main>;
} else if (frameworkSmokeTask) {
  content = <FrameworkSmokeInspection taskId={frameworkSmokeTask} />;
} else if (inspectionRun || evidenceRun) {
  content = (
    <InspectionEntry
      generationRunId={inspectionRun || evidenceRun}
      terminalMode={!!evidenceRun}
    />
  );
}

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    {content}
  </React.StrictMode>,
);
