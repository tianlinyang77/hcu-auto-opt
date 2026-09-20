// Copyright (c) 2026 Hygon Information Technology Co., Ltd.
import { useState } from "react";
import { freezeScriptedStart, startSession, submitScriptedStart } from "./scripted-start.js";
import { StartIntentWorkspace } from "./StartIntentWorkspace.jsx";

export function ScriptedStartPanel({ preview, demoMode }) {
  const session = startSession(preview.preview_id);
  const [actor, setActor] = useState(session.request?.actor || "");
  const [acks, setAcks] = useState(session.request?.acknowledged_warning_codes || []);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [receipt, setReceipt] = useState(session.receipt);
  const [audit, setAudit] = useState(false);
  const [request, setRequest] = useState(session.request);
  async function start() {
    if (demoMode || !confirmed || busy) return;
    setError("");
    setBusy(true);
    try {
      const frozen = session.request || freezeScriptedStart(preview, actor, acks, `ui-start-${crypto.randomUUID()}`);
      setRequest(frozen);
      setReceipt(await submitScriptedStart(session, frozen));
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  return <section className="wizard-card full-width">
    <h2>启动 Scripted 演练</h2>
    <p>创建演练轮次并冻结候选。此步骤不启动真实 HCU，也不代表评测已完成。</p>
    {demoMode ? <p role="status">当前为 Demo，只能预览，不能提交启动请求。</p> : <>
      <label className="plan-field">操作者
        <input value={actor} maxLength={200} disabled={!!request || busy} onChange={(e) => setActor(e.target.value)} />
      </label>
      {preview.required_ack_codes.map((code) => <label className="plan-field" key={code}>
        <span><input type="checkbox" checked={acks.includes(code)} disabled={!!request || busy}
          onChange={(e) => setAcks(e.target.checked ? [...acks, code] : acks.filter((v) => v !== code))} />
        {preview.checks.find((check) => check.code === code)?.message || code}</span>
      </label>)}
      <label className="plan-field"><span><input type="checkbox" checked={confirmed}
        onChange={(e) => setConfirmed(e.target.checked)} /> 已核对候选与预算，确认创建演练轮次</span></label>
      <button className="primary-button" type="button" onClick={start}
        disabled={busy || !confirmed || !!receipt || (!request && !preview.start_allowed)}>
        {busy ? "正在提交…" : request ? "使用原请求重试" : "确认启动演练"}
      </button>
      {request && <p className="mono">请求编号：{request.idempotency_key}</p>}
      {error && <p role="alert">{error}。请重试原请求；刷新后可在首页恢复，勿重新建计划。</p>}
      {receipt && <div role="status"><p>控制面状态：{receipt.state} · Round：{receipt.round_id}</p>
        <button className="outline-button" type="button" onClick={() => setAudit(true)}>查看启动记录</button>
      </div>}
    </>}
    {audit && <StartIntentWorkspace round={{intent_id: receipt.intent_id}} demoMode={false} onClose={() => setAudit(false)} />}
  </section>;
}
