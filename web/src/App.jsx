import { useEffect, useMemo, useState } from "react";
import {
  ArrowClockwise,
  Bell,
  BracketsCurly,
  CalendarBlank,
  CaretDown,
  ChartBar,
  ChartLineUp,
  Check,
  CheckCircle,
  CheckSquareOffset,
  CircleNotch,
  Clock,
  Database,
  DownloadSimple,
  Eye,
  FileText,
  Flask,
  GearSix,
  Hammer,
  Info,
  List,
  ListChecks,
  LockKey,
  LockSimple,
  MagnifyingGlass,
  Question,
  Robot,
  Scales,
  ShieldCheck,
  SidebarSimple,
  Signature,
  Sparkle,
  Stack,
  UserCircle,
  WarningCircle,
  Wrench,
  X,
} from "@phosphor-icons/react";

import { loadOperatorDashboard } from "./api.js";
import { demoDashboard } from "./demo-data.js";

const navigation = [
  ["round", "轮次总览", ChartLineUp],
  ["candidates", "候选管理", Stack],
  ["build", "构建中心", Hammer],
  ["correctness", "正确性评估", CheckSquareOffset],
  ["search", "Search 分析", MagnifyingGlass],
  ["holdout", "Holdout 评估", Flask],
  ["fwer", "FWER 校验", Scales],
  ["evidence", "证据管理", FileText],
  ["signoff", "人工签核", Signature],
];

const secondaryNavigation = [
  ["settings", "配置中心", GearSix],
  ["audit", "审计日志", ListChecks],
];

const phaseDefinitions = [
  ["hotspot", "热点", MagnifyingGlass],
  ["intake", "候选接入", Stack],
  ["build", "Build", Wrench],
  ["correctness", "正确性", CheckSquareOffset],
  ["search", "Search", ChartBar],
  ["holdout", "Holdout", Flask],
  ["fwer", "FWER", Scales],
  ["evidence", "Evidence", FileText],
  ["signoff", "人工签核", Signature],
];

const phaseIndexByRoundState = {
  intake_open: 1,
  intake_closed: 2,
  building: 2,
  correctness: 3,
  search_measuring: 4,
  search_barrier: 4,
  holdout_measuring: 5,
  holdout_barrier: 5,
  awaiting_signoff: 8,
  scripted_completed: 8,
  completed: 8,
  rejected: 8,
  cancelled: 8,
};

const roundStateText = {
  intake_open: "候选接入中",
  intake_closed: "等待 Build",
  building: "等待 Build 终态",
  correctness: "正确性评估中",
  search_measuring: "Search 测量中",
  search_barrier: "Search Barrier",
  holdout_measuring: "Holdout 测量中",
  holdout_barrier: "Holdout Barrier",
  awaiting_signoff: "等待人工签核",
  scripted_completed: "Scripted 已完成",
  completed: "已完成",
  rejected: "已拒绝",
  cancelled: "已取消",
};

const candidateStateMeta = {
  intake_accepted: ["已接入", "neutral", Clock],
  building: ["构建中", "info", CircleNotch],
  build_failed: ["构建失败", "danger", WarningCircle],
  built: ["构建成功", "success", CheckCircle],
  correctness_failed: ["正确性失败", "danger", WarningCircle],
  correctness_passed: ["正确性通过", "success", CheckCircle],
  search_failed: ["Search 失败", "danger", WarningCircle],
  search_measured: ["Search 已测量", "success", CheckCircle],
  not_promoted: ["未晋级", "neutral", LockSimple],
  holdout_failed: ["Holdout 失败", "danger", WarningCircle],
  holdout_measured: ["Holdout 已测量", "success", CheckCircle],
  invalid: ["无效", "danger", WarningCircle],
};

function shortId(value, length = 8) {
  if (!value) return "—";
  return String(value).replace("sha256:", "").slice(0, length);
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function downloadJson(filename, value) {
  const blob = new Blob([`${JSON.stringify(value, null, 2)}\n`], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function IconButton({ label, children, onClick, className = "" }) {
  return (
    <button
      className={`icon-button ${className}`}
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
    >
      {children}
    </button>
  );
}

function TopBar({ dashboard, demoMode, onRefresh, onToggleSidebar }) {
  const workload = dashboard.workloads[0];
  return (
    <header className="topbar">
      <div className="brand-lockup">
        <IconButton
          label="打开导航"
          className="mobile-menu"
          onClick={onToggleSidebar}
        >
          <List size={22} />
        </IconButton>
        <img
          className="brand-mark"
          src="/assets/hcu-mission-control-mark.png"
          alt="HCU Mission Control"
        />
        <span>HCU Optimization Mission Control</span>
      </div>

      <div className="topbar-context">
        <span className="topbar-label">工作负载</span>
        <button className="context-select" type="button">
          <span>{workload?.display_name || "未发现 Workload"}</span>
          <CaretDown size={15} />
        </button>
        <span className="topbar-label">当前视图</span>
        <button className="context-select view-select" type="button">
          <span>M2 多候选优化轮次</span>
          <CaretDown size={15} />
        </button>
      </div>

      <div className="topbar-actions">
        <span className={`source-chip ${demoMode ? "demo" : "api"}`}>
          {demoMode ? "Synthetic demo" : "Operator API"}
        </span>
        <span className="header-time">
          <CalendarBlank size={19} />
          {new Intl.DateTimeFormat("zh-CN", {
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            hour12: false,
          }).format(new Date())}
        </span>
        <IconButton label="刷新 Authority" onClick={onRefresh}>
          <ArrowClockwise size={19} />
        </IconButton>
        <IconButton label="帮助">
          <Question size={20} />
        </IconButton>
        <IconButton label="通知">
          <Bell size={20} />
        </IconButton>
        <UserCircle className="user-avatar" size={33} weight="fill" />
      </div>
    </header>
  );
}

function SideBar({ open, active, onSelect, onClose }) {
  const renderItem = ([key, label, Icon]) => (
    <button
      className={`nav-item ${active === key ? "active" : ""}`}
      key={key}
      type="button"
      onClick={() => onSelect(key, label)}
    >
      <Icon size={20} />
      <span>{label}</span>
      {active === key && <span className="nav-active-bar" />}
    </button>
  );

  return (
    <aside className={`sidebar ${open ? "open" : ""}`}>
      <div className="mobile-sidebar-head">
        <span>导航</span>
        <IconButton label="关闭导航" onClick={onClose}>
          <X size={20} />
        </IconButton>
      </div>
      <nav aria-label="优化轮次导航">{navigation.map(renderItem)}</nav>
      <div className="nav-divider" />
      <nav aria-label="系统导航">{secondaryNavigation.map(renderItem)}</nav>
      <button className="sidebar-collapse" type="button">
        <SidebarSimple size={20} />
        <span>收起</span>
      </button>
    </aside>
  );
}

function PhaseRail({ round }) {
  const currentIndex = phaseIndexByRoundState[round?.state] ?? 0;
  return (
    <section className="phase-rail" aria-label="Round 阶段进度">
      {phaseDefinitions.map(([key, label, Icon], index) => {
        const complete = index < currentIndex;
        const current = index === currentIndex;
        return (
          <div
            className={`phase ${complete ? "complete" : ""} ${current ? "current" : ""}`}
            key={key}
          >
            <div className="phase-track">
              <span className="phase-node">
                {complete ? (
                  <Check size={20} weight="bold" />
                ) : current ? (
                  <Icon size={21} weight="bold" />
                ) : (
                  <LockSimple size={19} />
                )}
              </span>
            </div>
            <strong>{label}</strong>
            <span>{complete ? "已完成" : current ? "进行中" : "待开始"}</span>
          </div>
        );
      })}
    </section>
  );
}

function BuildSummary({ round, onExplain }) {
  const failures = round.candidates.filter((item) =>
    ["build_failed", "invalid"].includes(item.state),
  ).length;
  const successes = round.candidates.filter((item) => item.state === "built").length;
  return (
    <section className="build-summary">
      <div className="build-metrics">
        <span>构建终态：<strong>{round.build_terminal_count}/{round.candidate_count}</strong></span>
        <span>成功：<strong className="success-text">{successes}</strong></span>
        <span>失败：<strong className="danger-text">{failures}</strong></span>
        <span>下一动作：<strong>{round.next_action}</strong></span>
      </div>
      <button className="outline-button" type="button" onClick={onExplain}>
        <Eye size={18} />
        查看阻塞原因
      </button>
    </section>
  );
}

function CandidateTable({ round, hotspot, selectedId, onSelect }) {
  const packages = new Map(
    (hotspot?.candidate_packages || []).map((item) => [item.candidate_id, item]),
  );
  return (
    <section className="candidate-section">
      <div className="section-heading">
        <h2>Candidate Family（{round.candidate_count}）</h2>
        <Info size={18} />
      </div>
      <div className="candidate-table-wrap">
        <table className="candidate-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>输入来源</th>
              <th>替换点</th>
              <th>Build 状态</th>
              <th>后续评估</th>
              <th>制品 / 失败证据</th>
            </tr>
          </thead>
          <tbody>
            {round.candidates.map((candidate) => {
              const [label, tone, StateIcon] =
                candidateStateMeta[candidate.state] || candidateStateMeta.intake_accepted;
              const packageView = packages.get(candidate.candidate_id);
              const active = selectedId === candidate.candidate_id;
              return (
                <tr
                  className={active ? "selected" : ""}
                  key={candidate.round_candidate_id}
                  onClick={() => onSelect(candidate.candidate_id)}
                >
                  <td>
                    <button className="candidate-id" type="button">
                      M2-{String(candidate.ordinal + 1).padStart(2, "0")}
                    </button>
                  </td>
                  <td>
                    <span className="source-label">
                      <BracketsCurly size={18} />
                      Reviewed fixture
                    </span>
                  </td>
                  <td>
                    <span className="replacement-point">
                      {packageView?.replacement_path || hotspot?.hotspot?.replacement_point || "—"}
                    </span>
                  </td>
                  <td>
                    <span className={`state-label ${tone}`}>
                      <StateIcon
                        size={18}
                        weight={tone === "success" ? "fill" : "regular"}
                        className={candidate.state === "building" ? "spin" : ""}
                      />
                      {label}
                    </span>
                  </td>
                  <td>
                    <span className="muted-state">
                      <LockSimple size={17} />
                      {candidate.state === "built" ? "等待正确性" : "未进入"}
                    </span>
                  </td>
                  <td className="mono">
                    {candidate.artifact_hash
                      ? shortId(candidate.artifact_hash)
                      : candidate.terminal_failure_code || "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="table-note">
        <Info size={16} />
        UI 只展示 Operator Read Model，不重算 Candidate 状态、预算或裁决。
      </p>
    </section>
  );
}

function EvidencePanel({ round, report, selectedCandidate, onExport }) {
  const available = round.evidence_status === "available" && report?.evidence_bundle;
  return (
    <aside className="evidence-panel">
      <div className="panel-heading">
        <h2>证据轨道</h2>
        <span className="read-only-tag"><Eye size={15} />只读</span>
      </div>

      <div className={`evidence-status-card ${available ? "available" : "locked"}`}>
        {available ? (
          <ShieldCheck size={30} weight="fill" />
        ) : (
          <LockKey size={30} />
        )}
        <div>
          <strong>{available ? "Evidence available" : "Evidence 尚未形成"}</strong>
          <span>
            {available
              ? "展示已由 D 线生成的不可变证据"
              : "Build、正确性、Search、Holdout 与 FWER 尚未完成"}
          </span>
        </div>
      </div>

      <div className="boundary-callout">
        <Info size={20} />
        <div>
          <strong>不产生真实性能结论</strong>
          <p>
            当前为 Synthetic Scripted 控制流。页面不展示或推导模型吞吐、延迟、speedup 或发布结论。
          </p>
        </div>
      </div>

      <dl className="evidence-facts">
        <div><dt>Round ID</dt><dd className="mono">{shortId(round.round_id, 12)}</dd></div>
        <div><dt>Report</dt><dd>{report?.report_status || "not available"}</dd></div>
        <div><dt>Evidence</dt><dd>{round.evidence_status}</dd></div>
        <div><dt>Formal Signoff</dt><dd className="locked-value">disabled</dd></div>
        <div><dt>自动发布</dt><dd className="locked-value">false</dd></div>
      </dl>

      {selectedCandidate && (
        <div className="selected-candidate-card">
          <span>当前候选</span>
          <strong className="mono">{shortId(selectedCandidate.candidate_id, 12)}</strong>
          <small>{candidateStateMeta[selectedCandidate.state]?.[0] || selectedCandidate.state}</small>
        </div>
      )}

      <button className="export-button" type="button" onClick={onExport}>
        <DownloadSimple size={19} />
        导出当前只读报告
      </button>
    </aside>
  );
}

function CapabilityFooter({ onAgentInfo, onApexInfo }) {
  return (
    <footer className="capability-footer">
      <div className="capability-block">
        <div className="capability-title">
          <Robot size={22} />
          <strong>Agent Generation 状态</strong>
          <span className="capability-state locked"><LockSimple size={17} />未启用</span>
        </div>
        <p>M2a Formal acceptance 与人工签核闭环前，Agent 不能接入候选生成。</p>
        <button className="outline-button" type="button" onClick={onAgentInfo}>
          查看解锁条件
        </button>
      </div>
      <div className="capability-divider" />
      <div className="capability-block">
        <div className="capability-title">
          <Sparkle size={22} />
          <strong>Apex Adapter 状态</strong>
          <span className="capability-state planned"><WarningCircle size={17} />规划中</span>
        </div>
        <p>Apex-like 调度只负责候选生成、去重和预算建议，不控制测量、裁决或发布。</p>
        <button className="outline-button" type="button" onClick={onApexInfo}>
          查看责任边界
        </button>
      </div>
    </footer>
  );
}

function EmptyState({ title, detail, actionLabel, onAction, icon: Icon = Database }) {
  return (
    <section className="empty-state">
      <Icon size={44} />
      <h1>{title}</h1>
      <p>{detail}</p>
      {actionLabel && (
        <button className="primary-button" type="button" onClick={onAction}>
          {actionLabel}
        </button>
      )}
    </section>
  );
}

function Modal({ modal, onClose }) {
  if (!modal) return null;
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="modal-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="modal-header">
          <div>
            <span>{modal.eyebrow}</span>
            <h2 id="modal-title">{modal.title}</h2>
          </div>
          <IconButton label="关闭" onClick={onClose}>
            <X size={20} />
          </IconButton>
        </div>
        <p>{modal.body}</p>
        {modal.items && (
          <ul>
            {modal.items.map((item) => (
              <li key={item}><Check size={17} />{item}</li>
            ))}
          </ul>
        )}
        <button className="primary-button" type="button" onClick={onClose}>
          我知道了
        </button>
      </section>
    </div>
  );
}

export function App() {
  const urlDemo = new URLSearchParams(window.location.search).get("demo") === "1";
  const [demoMode, setDemoMode] = useState(urlDemo);
  const [dashboard, setDashboard] = useState(urlDemo ? demoDashboard : null);
  const [loading, setLoading] = useState(!urlDemo);
  const [error, setError] = useState(null);
  const [selectedRoundId, setSelectedRoundId] = useState(
    urlDemo ? demoDashboard.rounds[0]?.round_id : null,
  );
  const [selectedCandidateId, setSelectedCandidateId] = useState(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [activeNavigation, setActiveNavigation] = useState("round");
  const [modal, setModal] = useState(null);

  const load = async (useDemo = demoMode) => {
    setLoading(true);
    setError(null);
    try {
      const next = useDemo ? demoDashboard : await loadOperatorDashboard();
      setDashboard(next);
      setSelectedRoundId((current) =>
        next.rounds.some((item) => item.round_id === current)
          ? current
          : next.rounds[0]?.round_id || null,
      );
    } catch (loadError) {
      setDashboard(null);
      setError(loadError instanceof Error ? loadError.message : String(loadError));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (urlDemo) return undefined;

    let cancelled = false;
    loadOperatorDashboard()
      .then((next) => {
        if (cancelled) return;
        setDashboard(next);
        setSelectedRoundId(next.rounds[0]?.round_id || null);
      })
      .catch((loadError) => {
        if (cancelled) return;
        setDashboard(null);
        setError(loadError instanceof Error ? loadError.message : String(loadError));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [urlDemo]);

  const round = useMemo(
    () => dashboard?.rounds.find((item) => item.round_id === selectedRoundId),
    [dashboard, selectedRoundId],
  );
  const report = round ? dashboard?.reports[round.round_id] : null;
  const hotspot = dashboard?.hotspots[0];
  const selectedCandidate = round?.candidates.find(
    (item) => item.candidate_id === selectedCandidateId,
  );

  const enterDemo = () => {
    const url = new URL(window.location.href);
    url.searchParams.set("demo", "1");
    window.history.replaceState({}, "", url);
    setDemoMode(true);
    load(true);
  };

  const handleNavigation = (key, label) => {
    setSidebarOpen(false);
    if (key === "round") {
      setActiveNavigation(key);
      return;
    }
    setModal({
      eyebrow: "UI-0 只读范围",
      title: `${label} 将在后续只读切片展开`,
      body: "当前版本先完成单 Round 总览，所有数据仍来自同一 Operator Read Model。页面不会为了导航完整而复制或推演领域状态。",
      items: ["不写数据库", "不触发 Round 状态迁移", "不重新计算 verdict 或性能结论"],
    });
  };

  if (loading && !dashboard) {
    return (
      <div className="app-loading">
        <CircleNotch className="spin" size={34} />
        <span>正在读取 Operator Authority…</span>
      </div>
    );
  }

  if (error && !dashboard) {
    return (
      <EmptyState
        title="Operator API 暂不可用"
        detail={`${error}。UI 不会静默伪造 Authority；你可以显式进入 Synthetic 演示查看界面。`}
        actionLabel="进入 Synthetic 演示"
        onAction={enterDemo}
        icon={WarningCircle}
      />
    );
  }

  if (!dashboard) return null;

  return (
    <div className="app-shell">
      <TopBar
        dashboard={dashboard}
        demoMode={demoMode}
        onRefresh={() => load(demoMode)}
        onToggleSidebar={() => setSidebarOpen((value) => !value)}
      />
      <SideBar
        open={sidebarOpen}
        active={activeNavigation}
        onSelect={handleNavigation}
        onClose={() => setSidebarOpen(false)}
      />

      <main className="dashboard">
        <section className="dashboard-head">
          <div>
            <div className="title-row">
              <h1>M2 多候选优化轮次</h1>
              <span className="synthetic-badge">Synthetic · 不产生真实性能结论</span>
            </div>
            <div className="authority-line">
              <ShieldCheck size={17} />
              Operator Contract {dashboard.identity.operator_contract_version || "m2-operator-v1"}
              <span className="mono">commit {shortId(dashboard.identity.source_commit, 9)}</span>
            </div>
          </div>
          {round && (
            <div className="round-status">
              <div>
                <span>当前状态</span>
                <strong>{roundStateText[round.state] || round.state}</strong>
                {loading && <CircleNotch className="spin" size={22} />}
              </div>
              <p>
                Authority：{formatDate(round.generated_at)}
                <span>Round v{round.round_version}</span>
              </p>
            </div>
          )}
        </section>

        {dashboard.rounds.length > 1 && (
          <div className="round-switcher">
            <label htmlFor="round-select">选择 Round</label>
            <select
              id="round-select"
              value={selectedRoundId || ""}
              onChange={(event) => setSelectedRoundId(event.target.value)}
            >
              {dashboard.rounds.map((item) => (
                <option key={item.round_id} value={item.round_id}>
                  {shortId(item.round_id, 12)} · {roundStateText[item.state] || item.state}
                </option>
              ))}
            </select>
          </div>
        )}

        {!round ? (
          <EmptyState
            title="尚无可展示的 Scripted Round"
            detail="只会列出 finalized StartIntent 对应的 Round。先使用 CLI 建立 Round，再刷新本页面。"
            icon={Database}
          />
        ) : (
          <>
            <PhaseRail round={round} />
            <BuildSummary
              round={round}
              onExplain={() =>
                setModal({
                  eyebrow: "下一动作",
                  title: round.next_action,
                  body: round.reason,
                  items: [
                    "等待所有 Build terminal 进入权威表",
                    "Candidate Family 不允许静默增删",
                    "失败候选保留 immutable failure evidence",
                  ],
                })
              }
            />

            <div className="dashboard-grid">
              <CandidateTable
                round={round}
                hotspot={hotspot}
                selectedId={selectedCandidateId}
                onSelect={(candidateId) =>
                  setSelectedCandidateId((current) =>
                    current === candidateId ? null : candidateId,
                  )
                }
              />
              <EvidencePanel
                round={round}
                report={report}
                selectedCandidate={selectedCandidate}
                onExport={() =>
                  downloadJson(`operator-report-${shortId(round.round_id)}.json`, report)
                }
              />
            </div>

            <CapabilityFooter
              onAgentInfo={() =>
                setModal({
                  eyebrow: "Agent 解锁条件",
                  title: "候选生成不能越过测量与签核边界",
                  body: "Agent 只允许读取热点和运行证据并提交 Candidate Intake，不能控制 Worker、Lease、Harness、Barrier、FWER、Signoff 或发布。",
                  items: ["完成真实 M2a Formal Round", "EvidenceBundle 与人工签核闭环通过", "独立 Candidate Generator Adapter 获得授权"],
                })
              }
              onApexInfo={() =>
                setModal({
                  eyebrow: "Apex-like Adapter",
                  title: "调度候选，不调度可信裁决",
                  body: "Apex-like 层负责多 Agent 编排、候选去重、预算建议和知识复用。它通过 Candidate Intake API 接入现有控制面。",
                  items: ["不直写 SearchRound", "不获取 HCU Lease", "不修改 Holdout 或 FWER 结果"],
                })
              }
            />
          </>
        )}
      </main>

      {sidebarOpen && (
        <button
          className="sidebar-scrim"
          type="button"
          aria-label="关闭导航"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      <Modal modal={modal} onClose={() => setModal(null)} />
    </div>
  );
}
