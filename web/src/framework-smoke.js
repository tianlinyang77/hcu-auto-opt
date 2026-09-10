// Copyright (c) 2026 Hygon Information Technology Co., Ltd.

const states = {
  rejected: "验收未通过", awaiting_signoff: "等待人工签核", completed: "人工签核已完成",
  framework_executing: "正在执行", framework_retesting: "正在复测",
  artifact_preparing: "正在构建", source_preparing: "正在准备源码", cancelled: "已取消",
};

export function frameworkState(state) {
  return states[state] || `尚未完成（${state || "未知状态"}）`;
}

export function checkLabel(value) {
  return value === true ? "通过" : value === false ? "未通过" : "尚无证据";
}

export async function loadFrameworkInspection(taskId, credential, fetcher = fetch) {
  if (!/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(taskId)) {
    throw new Error("任务编号无效，请使用验收任务的链接。");
  }
  const response = await fetcher(`/v1/framework-smoke-inspection/${taskId}`, {
    headers: { Accept: "application/json", Authorization: `Bearer ${credential}` },
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(response.status === 401 ? "访问凭据不正确或已失效。" :
      response.status === 403 ? "该凭据无权访问此任务。" : "暂时无法读取任务，请稍后刷新。");
  }
  const data = await response.json();
  if (data.schema !== "framework-smoke-inspection-v1" || data.task?.task_id !== taskId ||
      data.performance_conclusion !== "not_measured" || data.automatic_release_allowed !== false) {
    throw new Error("结果身份或验收边界不匹配，已停止展示。");
  }
  return data;
}
