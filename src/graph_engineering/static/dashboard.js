const API = "/api/v1";
const REFRESH_INTERVAL = 1_000;

const state = {
  workspaces: [],
  selectedId: new URLSearchParams(location.search).get("workspace"),
  refreshing: false,
};

const elements = {
  list: document.querySelector("#workspace-list"),
  detail: document.querySelector("#workspace-detail"),
  counters: document.querySelector("#summary-counters"),
  connection: document.querySelector("#connection-state"),
  refresh: document.querySelector("#refresh-button"),
  template: document.querySelector("#workspace-detail-template"),
};

const statusLabel = {
  registered: "已登记", running: "运行中", paused: "已暂停", completed: "已完成",
  unhealthy: "异常", healthy: "健康", needs_attention: "需关注", degraded: "降级",
  unknown: "未知", ready: "就绪", working: "工作中", idle: "空闲", missing: "缺失",
  failed: "失败", offline: "离线", stopped: "已停止", isolated: "已隔离",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function request(path) {
  const response = await fetch(`${API}${path}`, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* response is not JSON */ }
    throw new Error(detail);
  }
  return response.json();
}

function badge(value) {
  const normalized = String(value || "unknown").toLowerCase();
  return `<span class="badge ${escapeHtml(normalized)}">${escapeHtml(statusLabel[normalized] || normalized)}</span>`;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).format(date);
}

function renderCounters() {
  const all = state.workspaces.length;
  const running = state.workspaces.filter((item) => item.status === "running").length;
  const attention = state.workspaces.filter((item) =>
    ["unhealthy", "degraded", "needs_attention"].includes(item.status) ||
    ["degraded", "needs_attention"].includes(item.health)
  ).length;
  elements.counters.innerHTML = [
    [all, "总计"], [running, "运行中"], [attention, "需关注"],
  ].map(([value, label]) => `<div class="counter"><strong>${value}</strong><span>${label}</span></div>`).join("");
}

function renderWorkspaceList() {
  if (!state.workspaces.length) {
    elements.list.innerHTML = '<div class="empty-copy">暂无已登记工作区</div>';
    return;
  }
  elements.list.innerHTML = state.workspaces.map((workspace) => `
    <button class="workspace-item ${workspace.workspace_id === state.selectedId ? "selected" : ""}"
      type="button" role="option" aria-selected="${workspace.workspace_id === state.selectedId}"
      data-workspace-id="${escapeHtml(workspace.workspace_id)}">
      <span class="workspace-name-row">
        <span class="workspace-name">${escapeHtml(workspace.workspace_id)}</span>
        <span class="status-dot ${escapeHtml(workspace.health || workspace.status)}"></span>
      </span>
      <span class="workspace-meta">
        <span>${escapeHtml(statusLabel[workspace.status] || workspace.status)}</span>
        <span>·</span><span>v${workspace.config_version}</span>
      </span>
    </button>`).join("");
  elements.list.querySelectorAll("[data-workspace-id]").forEach((button) => {
    button.addEventListener("click", () => selectWorkspace(button.dataset.workspaceId));
  });
}

function setField(root, field, value) {
  const target = root.querySelector(`[data-field="${field}"]`);
  if (target) target.textContent = value;
}

function svgElement(tag, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attributes).forEach(([name, value]) => node.setAttribute(name, value));
  return node;
}

function layoutGraph(nodes, edges) {
  const incoming = new Map(nodes.map((node) => [node.id, 0]));
  edges.forEach((edge) => incoming.has(edge.target) && incoming.set(edge.target, incoming.get(edge.target) + 1));
  const roots = nodes.filter((node) => incoming.get(node.id) === 0).map((node) => node.id);
  if (!roots.length && nodes.length) roots.push(nodes[0].id);
  const rank = new Map(roots.map((id) => [id, 0]));
  for (let pass = 0; pass < nodes.length; pass += 1) {
    edges.forEach((edge) => {
      if (rank.has(edge.source) && edge.target !== edge.source) {
        const next = Math.min(rank.get(edge.source) + 1, nodes.length - 1);
        if (!rank.has(edge.target)) rank.set(edge.target, next);
      }
    });
  }
  nodes.forEach((node, index) => { if (!rank.has(node.id)) rank.set(node.id, index % 3); });
  const columns = new Map();
  nodes.forEach((node) => {
    const column = rank.get(node.id);
    if (!columns.has(column)) columns.set(column, []);
    columns.get(column).push(node);
  });
  const orderedColumns = [...columns.entries()].sort(([a], [b]) => a - b);
  const maxRows = Math.max(...orderedColumns.map(([, items]) => items.length), 1);
  const width = Math.max(760, orderedColumns.length * 230 + 100);
  const height = Math.max(420, maxRows * 116 + 100);
  const positions = new Map();
  orderedColumns.forEach(([, items], columnIndex) => {
    const gap = height / (items.length + 1);
    items.forEach((node, rowIndex) => positions.set(node.id, {
      x: 100 + columnIndex * ((width - 200) / Math.max(orderedColumns.length - 1, 1)),
      y: gap * (rowIndex + 1),
    }));
  });
  return { width, height, positions };
}

function renderGraph(container, graph) {
  const { width, height, positions } = layoutGraph(graph.nodes, graph.edges);
  const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });
  const defs = svgElement("defs");
  const marker = svgElement("marker", { id: "arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "5", markerHeight: "5", orient: "auto-start-reverse" });
  marker.append(svgElement("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "rgba(143,163,156,.5)" }));
  const filter = svgElement("filter", { id: "activeGlow", x: "-40%", y: "-60%", width: "180%", height: "220%" });
  filter.innerHTML = '<feGaussianBlur stdDeviation="5" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>';
  defs.append(marker, filter);
  svg.append(defs);

  graph.edges.forEach((edge) => {
    const source = positions.get(edge.source);
    const target = positions.get(edge.target);
    if (!source || !target) return;
    const sameNode = edge.source === edge.target;
    const path = svgElement("path", {
      class: "graph-edge",
      d: sameNode
        ? `M ${source.x + 72} ${source.y} C ${source.x + 125} ${source.y - 72}, ${source.x + 125} ${source.y + 72}, ${source.x + 72} ${source.y + 5}`
        : `M ${source.x + 72} ${source.y} C ${(source.x + target.x) / 2} ${source.y}, ${(source.x + target.x) / 2} ${target.y}, ${target.x - 75} ${target.y}`,
      "marker-end": "url(#arrow)",
    });
    const label = svgElement("text", { class: "graph-edge-label", x: String((source.x + target.x) / 2), y: String((source.y + target.y) / 2 - 7), "text-anchor": "middle" });
    label.textContent = edge.label;
    svg.append(path, label);
  });

  graph.nodes.forEach((node) => {
    const position = positions.get(node.id);
    const group = svgElement("g", { class: `graph-node${node.active ? " active" : ""}`, transform: `translate(${position.x}, ${position.y})` });
    group.append(svgElement("rect", { x: "-72", y: "-31", width: "144", height: "62" }));
    if (node.active) group.append(svgElement("circle", { class: "active-ring", cx: "-61", cy: "-20", r: "3" }));
    const label = svgElement("text", { x: "0", y: "-5" });
    label.textContent = node.label;
    const kind = svgElement("text", { class: "node-kind", x: "0", y: "15" });
    kind.textContent = node.active ? "ACTIVE" : node.kind.toUpperCase();
    group.append(label, kind);
    svg.append(group);
  });
  container.replaceChildren(svg);
}

function renderSessions(tbody, sessions) {
  if (!sessions.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-copy">此工作区还没有 Session 绑定</td></tr>';
    return;
  }
  tbody.innerHTML = sessions.map((session) => `
    <tr>
      <td class="session-identity"><strong>${escapeHtml(session.agent_name || session.agent_id || "未登记 Session")}</strong><code>${escapeHtml(session.session_id)}</code></td>
      <td>${badge(session.status)}${session.requires_attention ? '<span class="attention-mark" title="需要关注">●</span>' : ""}</td>
      <td><span class="binding ${session.registered ? "" : "unregistered"}">${session.registered ? "REGISTERED" : "UNREGISTERED"}</span></td>
      <td class="workdir">${escapeHtml(session.working_dir || "—")}</td>
    </tr>`).join("");
}

async function renderDetail(workspace) {
  const fragment = elements.template.content.cloneNode(true);
  const root = document.createElement("div");
  root.className = "detail-column";
  root.append(fragment);
  setField(root, "name", workspace.workspace_id);
  root.querySelector('[data-field="status-badge"]').innerHTML = badge(workspace.status);
  setField(root, "version", `v${workspace.config_version}`);
  setField(root, "health", statusLabel[workspace.health] || workspace.health || "未知");
  setField(root, "updated-at", formatDate(workspace.updated_at));
  setField(root, "repository", "正在读取配置…");
  root.querySelector('[data-field="graph"]').innerHTML = '<div class="loading-copy">正在加载状态图…</div>';
  root.querySelector('[data-field="sessions"]').innerHTML = '<tr><td colspan="4" class="loading-copy">正在读取 Session…</td></tr>';
  elements.detail.replaceChildren(root);

  const selectedAtRequest = workspace.workspace_id;
  const [graphResult, sessionsResult] = await Promise.allSettled([
    request(`/workspaces/${encodeURIComponent(selectedAtRequest)}/graph`),
    request(`/workspaces/${encodeURIComponent(selectedAtRequest)}/sessions`),
  ]);
  if (state.selectedId !== selectedAtRequest) return;

  const currentRoot = elements.detail.firstElementChild;
  if (graphResult.status === "fulfilled") {
    setField(currentRoot, "name", graphResult.value.workspace.name);
    setField(currentRoot, "repository", graphResult.value.workspace.repository);
    renderGraph(currentRoot.querySelector('[data-field="graph"]'), graphResult.value);
  } else {
    currentRoot.querySelector('[data-field="graph"]').innerHTML = `<div class="error-copy">状态图加载失败：${escapeHtml(graphResult.reason.message)}</div>`;
  }
  if (sessionsResult.status === "fulfilled") {
    setField(currentRoot, "session-count", `${sessionsResult.value.length} SESSIONS`);
    renderSessions(currentRoot.querySelector('[data-field="sessions"]'), sessionsResult.value);
  } else {
    setField(currentRoot, "session-count", "UNAVAILABLE");
    currentRoot.querySelector('[data-field="sessions"]').innerHTML = `<tr><td colspan="4" class="error-copy">Session 加载失败：${escapeHtml(sessionsResult.reason.message)}</td></tr>`;
  }
}

function selectWorkspace(workspaceId, { updateUrl = true } = {}) {
  state.selectedId = workspaceId;
  if (updateUrl) history.replaceState(null, "", `?workspace=${encodeURIComponent(workspaceId)}`);
  renderWorkspaceList();
  const workspace = state.workspaces.find((item) => item.workspace_id === workspaceId);
  if (workspace) renderDetail(workspace);
}

async function refresh({ manual = false } = {}) {
  if (state.refreshing) return;
  state.refreshing = true;
  if (manual) elements.refresh.classList.add("loading");
  try {
    state.workspaces = await request("/workspaces");
    elements.connection.className = "connection online";
    elements.connection.lastElementChild.textContent = "实时数据已连接";
    if (!state.selectedId || !state.workspaces.some((item) => item.workspace_id === state.selectedId)) {
      state.selectedId = state.workspaces[0]?.workspace_id || null;
    }
    renderCounters();
    renderWorkspaceList();
    if (state.selectedId) selectWorkspace(state.selectedId, { updateUrl: false });
  } catch (error) {
    elements.connection.className = "connection offline";
    elements.connection.lastElementChild.textContent = "连接失败";
    if (!state.workspaces.length) elements.list.innerHTML = `<div class="error-copy">${escapeHtml(error.message)}</div>`;
  } finally {
    state.refreshing = false;
    elements.refresh.classList.remove("loading");
  }
}

elements.refresh.addEventListener("click", () => refresh({ manual: true }));
refresh();
setInterval(refresh, REFRESH_INTERVAL);
