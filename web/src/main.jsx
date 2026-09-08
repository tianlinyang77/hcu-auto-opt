import React from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/jetbrains-mono";
import "@fontsource-variable/noto-sans-sc";
import { App } from "./App.jsx";
import { InspectionEntry } from "./InspectionEntry.jsx";
import "./styles.css";

const inspectionRun = new URLSearchParams(window.location.search).get("agentInspection");

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    {inspectionRun ? <InspectionEntry generationRunId={inspectionRun} /> : <App />}
  </React.StrictMode>,
);
