import React from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/jetbrains-mono";
import "@fontsource-variable/noto-sans-sc";
import { App } from "./App.jsx";
import { FormalStartEntry } from "./FormalStartEntry.jsx";
import { ScriptedStartRecovery } from "./ScriptedStartRecovery.jsx";
import { FrameworkSmokeInspection } from "./FrameworkSmokeInspection.jsx";
import { InspectionEntry } from "./InspectionEntry.jsx";
import { ManualCandidateInspection } from "./ManualCandidateInspection.jsx";
import { EndpointCampaignInspection } from "./EndpointCampaignInspection.jsx";
import "./styles.css";

const query = new URLSearchParams(window.location.search);
const frameworkSmokeTask = query.get("frameworkSmoke");
const inspectionRun = query.get("agentInspection");
const evidenceRun = query.get("agentEvidence");
const manualCandidateTask = query.get("manualCandidate");
const endpointCampaign = query.get("endpointCampaign");
const formalStart = query.get("formalStart");
const selectedEvidenceEntries = [frameworkSmokeTask, inspectionRun, evidenceRun, manualCandidateTask, endpointCampaign, formalStart].filter(Boolean);

let content = <>{query.get("demo") !== "1" && <ScriptedStartRecovery />}<App /></>;
if (selectedEvidenceEntries.length > 1) {
  content = <main role="alert">请选择一个证据入口，不能同时指定多种审核或终态报告。</main>;
} else if (formalStart) {
  content = query.get("demo") === "1" ? <main role="alert">Demo 不提供正式启动入口。</main> : <FormalStartEntry />;
} else if (frameworkSmokeTask) {
  content = <FrameworkSmokeInspection taskId={frameworkSmokeTask} />;
} else if (manualCandidateTask) {
  content = <ManualCandidateInspection taskId={manualCandidateTask} />;
} else if (endpointCampaign) {
  content = <EndpointCampaignInspection campaignId={endpointCampaign} />;
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
