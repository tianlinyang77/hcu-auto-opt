// Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import { useEffect, useRef, useState } from "react";
import { checkLabel, frameworkState, loadFrameworkInspection } from "./framework-smoke.js";
import "./framework-smoke.css";
import { FrameworkSmokeSignoff } from "./FrameworkSmokeSignoff.jsx";

const checks = [
  ["baseline_execution_succeeded", "Baseline 基线执行"],
  ["noop_execution_succeeded", "No-op 对照执行"],
  ["output_equivalent", "输出一致性"],
  ["cleanup_healthy", "资源清理与健康检查"],
];

export function FrameworkSmokeInspection({ taskId }) {
  const [credential, setCredential] = useState("");
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [signingPending, setSigningPending] = useState(false);
  const activeCredential = useRef("");
  const requestVersion = useRef(0);

  useEffect(() => {
    if (!signingPending) return;
    const warn = (event) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [signingPending]);

  async function refresh(key) {
    const version = ++requestVersion.current;
    setBusy(true);
    setError("");
    try {
      const result = await loadFrameworkInspection(taskId, key);
      if (version !== requestVersion.current) return;
      activeCredential.current = key;
      setCredential("");
      setData(result);
    } catch (failure) {
      if (version !== requestVersion.current) return;
      setData(null);
      activeCredential.current = "";
      setError(failure.message);
    } finally {
      if (version === requestVersion.current) setBusy(false);
    }
  }

  function logout() {
    requestVersion.current += 1;
    activeCredential.current = "";
    setCredential("");
    setData(null);
    setBusy(false);
    setError("");
  }

  return <main className="smoke-inspection">
    <header><div><p className="smoke-eyebrow">HCU AUTO OPT · 框架验收</p>
      <h1>实机验收结果</h1><p className="smoke-id">{taskId}</p></div>
      {data && <div className="smoke-actions">
        <button disabled={busy || signingPending} onClick={() => refresh(activeCredential.current)}>
          {busy ? "正在刷新…" : "刷新数据库结果"}</button>
        <button disabled={signingPending} onClick={logout}>退出查看</button></div>}
    </header>
    <p className="smoke-boundary">本页验证源码、构建、执行、输出一致性与清理闭环，不代表性能提升，也不允许自动发布。</p>
    {error && <p role="alert" className="smoke-error">{error}</p>}
    {!data ? <form className="smoke-login" onSubmit={(event) => {
      event.preventDefault(); refresh(credential);
    }}><h2>查看此任务</h2><p>使用独立的任务访问凭据，不是模型 API Key。</p>
      <label htmlFor="smoke-credential">任务访问凭据</label>
      <input id="smoke-credential" type="password" autoComplete="off" required
        value={credential} onChange={(event) => setCredential(event.target.value)} />
      <button disabled={busy || !credential}>{busy ? "正在读取…" : "查看真实结果"}</button>
      <p>凭据仅保留在当前页面内存中，退出或关闭页面后需重新输入。</p></form> : <>
      <section className="smoke-verdict" aria-label="验收结论">
        <div><p>当前任务状态</p><h2>{frameworkState(data.task.state)}</h2></div>
        <p>{data.checks.cleanup_healthy === false ?
          "输出一致不能替代资源清理。健康检查未通过，系统没有放行本次验收。" :
          data.ready_for_human_review ? "技术检查已通过，仍需人工检查证据并签核。" :
          "以任务状态和完整证据为准；未完成的检查不会自动视为通过。"}</p>
      </section>
      <section className="smoke-checks" aria-label="分项检查">{checks.map(([key, title]) =>
        <article key={key} data-result={String(data.checks[key])}>
          <h3>{title}</h3><strong>{checkLabel(data.checks[key])}</strong></article>)}</section>
      <div className="smoke-columns"><section className="smoke-panel"><h2>冻结环境</h2>
        <dl><dt>主机 / 设备</dt><dd>{data.target.host} / HCU {data.target.device_index} / {data.target.architecture}</dd>
          <dt>执行 Profile</dt><dd>{data.task.profile}</dd>
          <dt>源码 Commit</dt><dd className="smoke-id">{data.target.source_commit}</dd>
          <dt>镜像 Digest</dt><dd className="smoke-id">{data.target.image_digest}</dd></dl>
      </section><section className="smoke-panel"><h2>证据与签核</h2>
        <p>源码快照 {data.counts.sources} · 制品 {data.counts.artifacts} · 执行记录 {data.counts.executions}</p>
        <p>评估记录 {data.counts.evaluations} · 证据包 {data.counts.evidence_bundles}</p>
        <p>仅挂载 No-op 源码归档，不代表候选代码已激活。</p>
        <FrameworkSmokeSignoff key={taskId} data={data} onPendingChange={setSigningPending}
          onRefresh={() => refresh(activeCredential.current)} />
        <p>Stage 0、优化收益与发布审批均不由本页放行。</p>
      </section></div>
      <section className="smoke-panel"><h2>已保存制品</h2>{data.artifacts.map((artifact) =>
        <div key={artifact.artifact_id}><p>{artifact.kind}</p>
          <p className="smoke-id">{artifact.content_hash}</p></div>)}</section>
      <section className="smoke-panel"><h2>任务事件</h2><ol>{data.events.map((event, index) =>
        <li key={`${event.created_at}-${index}`}><time>{event.created_at}</time><span>{event.event_type}</span></li>)}</ol></section>
      <footer>实时数据库读取 · {data.adapter_mode === "real" ? "真实 Adapter" : "模拟 Adapter"} · 最近读取 {data.observed_at}</footer>
    </>}
  </main>;
}
