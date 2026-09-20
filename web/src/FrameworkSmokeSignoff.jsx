// Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import { useRef, useState } from "react";
import { freezeSignoff, loadSigningStatus, matchesSignoff, submitSignoff } from "./framework-signoff.js";

export function FrameworkSmokeSignoff({ data, onPendingChange, onRefresh }) {
  const [key, setKey] = useState("");
  const [identity, setIdentity] = useState(null);
  const [decision, setDecision] = useState("approved");
  const [reason, setReason] = useState("");
  const [intent, setIntent] = useState(null);
  const [receipt, setReceipt] = useState(null);
  const [phase, setPhase] = useState("editing");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const activeKey = useRef("");
  const locked = useRef(false);

  async function operation(action) {
    if (locked.current) return;
    locked.current = true;
    setBusy(true); setError("");
    try { await action(); } catch (failure) { setError(failure.message); }
    finally { locked.current = false; setBusy(false); }
  }

  async function check() {
    const status = await loadSigningStatus(data.task.task_id, activeKey.current);
    if (status.signoff) {
      if (intent && !matchesSignoff(intent, status.signoff)) {
        setReceipt(status.signoff); activeKey.current = ""; onPendingChange(false);
        throw new Error("已有另一份签核决定，请刷新数据库结果核对，不能覆盖。");
      }
      setReceipt(status.signoff); activeKey.current = ""; onPendingChange(false);
    } else {
      setPhase(phase === "conflict" ? "recheck" : "retry");
    }
  }

  if (receipt) return <div role="status" className="smoke-signoff">
    <h3>数据库已保存人工签核</h3>
    {error && <p role="alert" className="smoke-error">{error}</p>}
    <p>{receipt.decision === "approved" ? "功能验收批准" : "功能验收拒绝"} · {receipt.actor}</p>
    <p>{receipt.reason}</p><p className="smoke-id">证据 {receipt.evidence_bundle_id}</p>
    <p className="smoke-id">签核 {receipt.signoff_id}</p>
    <p>请刷新数据库结果查看最终任务状态。此决定不放行性能结论或自动发布。</p>
  </div>;

  if (!data.write_actions_available && !intent) return <p>只读视图，签核未启用或凭据已到期。</p>;
  return <div className="smoke-signoff">
    <h3>人工签核 · 仅功能验收</h3>
    {error && <p role="alert" className="smoke-error">{error}</p>}
    {!identity ? <form onSubmit={(event) => {
      event.preventDefault(); operation(async () => {
        const status = await loadSigningStatus(data.task.task_id, key);
        if (intent && status.actor !== intent.payload.actor) {
          throw new Error("新凭据签署人不同，不能用于重试本次请求。");
        }
        activeKey.current = key; setKey(""); setIdentity(status);
        if (status.signoff) {
          if (intent && !matchesSignoff(intent, status.signoff)) {
            setReceipt(status.signoff); activeKey.current = ""; onPendingChange(false);
            throw new Error("数据库已有其他签核决定，请核对原任务；不会覆盖已有决定。");
          }
          setReceipt(status.signoff); activeKey.current = ""; onPendingChange(false);
        }
      });
    }}><label htmlFor="signing-key">独立签核凭据（不是查看密码）</label>
      <input id="signing-key" type="password" autoComplete="off" required value={key}
        onChange={(event) => setKey(event.target.value)} />
      <button disabled={busy || !key}>验证签核身份</button></form> : !intent ?
      <form onSubmit={(event) => {
        event.preventDefault();
        try { setIntent(freezeSignoff(data, identity, decision, reason)); setPhase("confirm"); setError(""); }
        catch (failure) { setError(failure.message); }
      }}><p>签署人：{identity.actor} · 有效至 {identity.expires_at}</p>
        <label htmlFor="signing-decision">决定</label>
        <select id="signing-decision" value={decision} onChange={(event) => setDecision(event.target.value)}>
          <option value="approved">批准功能验收</option><option value="rejected">拒绝功能验收</option>
        </select><label htmlFor="signing-reason">签核理由</label>
        <textarea id="signing-reason" required maxLength={2000} value={reason}
          onChange={(event) => setReason(event.target.value)} />
        <button disabled={busy || !data.ready_for_human_review || !reason.trim()}>核对本次签核</button>
      </form> : <div>
        <dl><dt>任务</dt><dd className="smoke-id">{intent.taskId}</dd>
          <dt>评测</dt><dd className="smoke-id">{intent.evaluationId}</dd>
          <dt>冻结证据</dt><dd className="smoke-id">{intent.payload.evidence_bundle_id}</dd>
          <dt>请求编号（重试不变）</dt><dd className="smoke-id">{intent.payload.idempotency_key}</dd>
          <dt>签署人 / 决定</dt><dd>{intent.payload.actor} / {intent.payload.decision === "approved" ? "批准" : "拒绝"}</dd>
          <dt>理由</dt><dd>{intent.payload.reason}</dd></dl>
        <p>仅确认 Framework Smoke 功能链路；不确认候选激活、性能提升或发布。点击下方按钮后才会写入。</p>
        {["confirm", "retry"].includes(phase) && <button disabled={busy} onClick={() => operation(async () => {
          setPhase("unknown");
          onPendingChange(true);
          try {
            const result = await submitSignoff(intent, activeKey.current);
            setReceipt(result); activeKey.current = ""; onPendingChange(false);
          } catch (failure) {
            if (failure.status === 409) setPhase("conflict");
            throw failure;
          }
        })}>{phase === "retry" ? "以原请求重新提交" : "我已核对，确认提交"}</button>}
        {phase === "confirm" && <button disabled={busy} onClick={() => setIntent(null)}>返回修改</button>}
        {["unknown", "retry", "conflict"].includes(phase) && <>
          <p>请勿关闭或重新加载页面；先查询结果。未确认的请求与证据不会自动替换。</p>
          <button disabled={busy} onClick={() => operation(check)}>查询数据库签核结果</button>
          <button disabled={busy} onClick={() => {
            setIdentity(null); activeKey.current = "";
          }}>重新输入签核凭据（保留原请求）</button>
        </>}
        {phase === "recheck" && <button disabled={busy} onClick={() => {
          setIntent(null); setIdentity(null); activeKey.current = "";
          onPendingChange(false); onRefresh();
        }}>冲突且尚无决定，重新读取证据</button>}
      </div>}
  </div>;
}
