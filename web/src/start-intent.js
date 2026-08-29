export const startIntentStateCopy = {
  preparing: ["准备中", "StartIntent 已落库，等待冻结计划"],
  plans_frozen: ["计划已冻结", "Search 与 Holdout Authority 已固定"],
  round_created: ["Round 已创建", "候选正在绑定到本轮"],
  intake_closed: ["接入已关闭", "Candidate Family 已冻结"],
  finalized: ["启动已完成", "durable Start 闭环已完成"],
  failed: ["启动失败", "已保留安全错误与已写入证据"],
};

export const startAuditStepDefinitions = [
  ["intent", "请求落库", "Intent"],
  ["plans", "冻结计划", "Plan Authority"],
  ["round", "创建轮次", "Search Round"],
  ["members", "绑定候选", "Candidate Family"],
  ["finalized", "完成启动", "Finalized"],
];

export function deriveStartAuditSteps(intent) {
  if (!intent) return [];
  const plansFrozen = Boolean(
    intent.search_plan_hash &&
      intent.holdout_plan_commitment &&
      intent.holdout_plan_authority_hash,
  );
  const boundCount = intent.candidate_members.filter(
    (member) => member.state === "round_member_bound",
  ).length;
  const allBound = boundCount === intent.candidate_members.length;
  const roundObserved = ["round_created", "intake_closed", "finalized"].includes(
    intent.state,
  ) || boundCount > 0;
  const intakeClosed = allBound && Boolean(intent.candidate_family_hash);

  const complete = {
    intent: true,
    plans: plansFrozen,
    round: roundObserved,
    members: intakeClosed,
    finalized: intent.state === "finalized",
  };
  const firstPending = startAuditStepDefinitions.findIndex(([key]) => !complete[key]);

  return startAuditStepDefinitions.map(([key, label, eyebrow], index) => ({
    key,
    label,
    eyebrow,
    status: complete[key]
      ? "complete"
      : intent.state === "failed" && index === firstPending
        ? "failed"
        : index === firstPending
          ? "current"
          : "pending",
  }));
}

export function summarizeStartIntent(intent) {
  const boundCount = intent.candidate_members.filter(
    (member) => member.state === "round_member_bound",
  ).length;
  return {
    label: startIntentStateCopy[intent.state]?.[0] || intent.state,
    detail: startIntentStateCopy[intent.state]?.[1] || "后端返回了未知状态",
    boundCount,
    memberCount: intent.candidate_members.length,
    finalized: intent.state === "finalized",
    failed: intent.state === "failed",
    releaseLocked:
      intent.synthetic === true && intent.automatic_release_allowed === false,
  };
}
