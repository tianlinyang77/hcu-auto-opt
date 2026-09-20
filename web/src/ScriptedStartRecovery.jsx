// Copyright (c) 2026 Hygon Information Technology Co., Ltd.
import { useState } from "react";
import { savedStartRequests, startSession, submitScriptedStart } from "./scripted-start.js";
import { StartIntentWorkspace } from "./StartIntentWorkspace.jsx";

export function ScriptedStartRecovery() {
  const [saved] = useState(() => {
    try { return {requests: savedStartRequests(), error: ""}; }
    catch (error) { return {requests: [], error: error.message}; }
  });
  const [receipts, setReceipts] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [intent, setIntent] = useState(null);
  async function recover(request) {
    setBusy(true);
    setError("");
    try {
      const value = await submitScriptedStart(startSession(request.preview_id), request);
      setReceipts((current) => ({...current, [request.preview_id]: value}));
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  if (!saved.requests.length && !saved.error) return null;
  return <section className="wizard-card" aria-label="恢复演练启动请求">
    <h2>本标签页保存的演练启动请求</h2>
    <p>刷新前的请求编号已保留。确认恢复会重发完全相同的请求，由控制面幂等处理；若原请求未到达，将创建原计划对应的演练轮次。</p>
    {(saved.error || error) && <p role="alert">{saved.error || error}</p>}
    {saved.requests.map((request) => <div key={request.preview_id}>
      <p className="mono">{request.idempotency_key}</p>
      <button type="button" className="outline-button" disabled={busy}
        onClick={() => recover(request)}>确认恢复原演练请求</button>
      {receipts[request.preview_id] && <>
        <p>控制面状态：{receipts[request.preview_id].state}</p>
        <button type="button" className="outline-button"
          onClick={() => setIntent(receipts[request.preview_id].intent_id)}>查看启动记录</button>
      </>}
    </div>)}
    {intent && <StartIntentWorkspace round={{intent_id: intent}} demoMode={false} onClose={() => setIntent(null)} />}
  </section>;
}
