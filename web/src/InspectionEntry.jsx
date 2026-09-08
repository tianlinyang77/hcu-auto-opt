// Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import { useState } from 'react';
import { AgentProposalWorkspace } from './AgentProposalWorkspace.jsx';
import { inspectionAuthorization } from './inspection-client.js';

export function InspectionEntry({ generationRunId }) {
  const [authorization, setAuthorization] = useState(null);
  const [error, setError] = useState('');
  function login(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      const header = inspectionAuthorization(data.get('username'), data.get('password'));
      form.reset();
      setError('');
      setAuthorization(header);
    } catch (failure) {
      form.elements.password.value = '';
      setError(failure.message);
    }
  }
  if (authorization) return <AgentProposalWorkspace
    demoMode={false} inspectionMode generationRunId={generationRunId}
    inspectionAuthorization={authorization} onClose={() => setAuthorization(null)}
  />;
  return <main className="inspection-entry">
    <form className="inspection-login" onSubmit={login} autoComplete="off">
      <span className="read-only-tag">单 Run · 只读访问</span>
      <h1>查看 Agent 候选证据</h1>
      <p>使用独立访问凭据登录。页面只读取审核前快照，不启动任务、不调用模型、不执行签核。</p>
      <p className="mono inspection-run">{generationRunId}</p>
      <label htmlFor="inspection-username">用户名</label>
      <input id="inspection-username" name="username" defaultValue="operator" required autoComplete="off" spellCheck={false} />
      <label htmlFor="inspection-password">访问密码</label>
      <input id="inspection-password" name="password" type="password" required autoComplete="off" />
      {error && <p role="alert">{error}</p>}
      <button className="primary-button" type="submit">登录并读取</button>
      <small>凭据只保留在当前页面内存；刷新页面或退出后需重新输入。不要填写模型 API Key。</small>
    </form>
  </main>;
}
