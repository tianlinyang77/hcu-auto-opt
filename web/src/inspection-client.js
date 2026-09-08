// Copyright (c) 2026 Hygon Information Technology Co., Ltd.

export function inspectionAuthorization(username, password) {
  if (!/^[a-zA-Z0-9_-]{1,64}$/.test(username) || !/^[a-zA-Z0-9_-]{20,128}$/.test(password)) {
    throw new Error('请输入独立访问凭据文件中的用户名和密码，不要使用模型 API Key。');
  }
  return `Basic ${btoa(`${username}:${password}`)}`;
}

export async function readInspection(runId, authorization, {
  origin = window.location.origin,
  fetchImpl = fetch,
  kind = 'inspection',
} = {}) {
  if (!['inspection', 'evidence'].includes(kind)) throw new Error('不支持的只读证据类型。');
  const destination = new URL(origin);
  if (destination.protocol !== 'https:' && !(destination.protocol === 'http:'
    && ['127.0.0.1', 'localhost', '[::1]'].includes(destination.hostname))) {
    throw new Error('只读登录仅允许 HTTPS 或本机 SSH 隧道。');
  }
  if (!/^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(runId) || !authorization?.startsWith('Basic ')) {
    throw new Error('缺少有效 Run ID 或访问凭据。');
  }
  // Deliberately ignore VITE_HCUOPT_API_BASE: never send this credential cross-origin.
  const response = await fetchImpl(`${destination.origin}/v1/operator/agent-generations/${runId}/${kind}`, {
    headers: { Accept: 'application/json', Authorization: authorization },
    credentials: 'omit', cache: 'no-store', redirect: 'error',
    signal: AbortSignal.timeout(30000),
  });
  if (!response.ok) {
    if ([401, 403].includes(response.status)) throw new Error('访问凭据无效、已过期，或未授权读取此 Run。请退出后重新登录。');
    throw new Error(`只读证据暂不可用（HTTP ${response.status}），不会回退到演示数据。`);
  }
  return response.json();
}

export function readTerminalEvidence(runId, authorization, options = {}) {
  return readInspection(runId, authorization, { ...options, kind: 'evidence' });
}
