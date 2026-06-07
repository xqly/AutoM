const state = {
  user: null,
  tasks: [],
  selectedTaskId: null,
  selectedTask: null,
  events: [],
  statusFilter: "",
  loading: false,
  error: "",
};

const app = document.getElementById("app");

async function api(path, options = {}) {
  const headers = options.headers || {};
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, { credentials: "same-origin", ...options, headers });
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "请求失败");
  }
  return data;
}

function statusLabel(status) {
  const labels = {
    queued: "待处理",
    running: "生成中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
    needs_clarification: "需补充",
  };
  return labels[status] || status;
}

function fmtBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function bootstrap() {
  const data = await api("/api/me");
  state.user = data.user;
  if (state.user) {
    await loadTasks();
  }
  render();
}

async function login(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  state.error = "";
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: form.get("username"),
        password: form.get("password"),
      }),
    });
    state.user = data.user;
    await loadTasks();
  } catch (error) {
    state.error = error.message;
  }
  render();
}

async function logout() {
  await api("/api/auth/logout", { method: "POST", body: JSON.stringify({}) });
  state.user = null;
  state.tasks = [];
  state.selectedTask = null;
  state.selectedTaskId = null;
  render();
}

async function loadTasks() {
  const query = state.statusFilter ? `?status=${encodeURIComponent(state.statusFilter)}` : "";
  const data = await api(`/api/tasks${query}`);
  state.tasks = data.tasks;
  if (!state.selectedTaskId && state.tasks.length > 0) {
    state.selectedTaskId = state.tasks[0].id;
  }
  if (state.selectedTaskId) {
    await loadTask(state.selectedTaskId);
  }
}

async function loadTask(id) {
  state.selectedTaskId = id;
  const [detail, events] = await Promise.all([api(`/api/tasks/${id}`), api(`/api/tasks/${id}/events`)]);
  state.selectedTask = detail;
  state.events = events.events;
}

async function fileToPayload(file) {
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
  const dataBase64 = String(dataUrl).split(",", 2)[1] || "";
  return {
    name: file.name,
    mime_type: file.type || "application/octet-stream",
    data_base64: dataBase64,
  };
}

async function createTask(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const files = Array.from(form.querySelector('input[type="file"]').files || []);
  state.loading = true;
  state.error = "";
  render();
  try {
    const attachments = [];
    for (const file of files) {
      attachments.push(await fileToPayload(file));
    }
    const created = await api("/api/tasks", {
      method: "POST",
      body: JSON.stringify({
        customer_name: data.get("customer_name"),
        description: data.get("description"),
        attachments,
      }),
    });
    form.reset();
    state.selectedTaskId = created.id;
    await loadTasks();
  } catch (error) {
    state.error = error.message;
  } finally {
    state.loading = false;
    render();
  }
}

async function retryTask(id) {
  await api(`/api/tasks/${id}/retry`, { method: "POST", body: JSON.stringify({}) });
  await loadTasks();
  render();
}

async function cancelTask(id) {
  await api(`/api/tasks/${id}/cancel`, { method: "POST", body: JSON.stringify({}) });
  await loadTasks();
  render();
}

function renderLogin() {
  app.innerHTML = `
    <div class="login">
      <form class="login-box" onsubmit="login(event)">
        <h2>AutoM 登录</h2>
        <div class="field">
          <label>用户名</label>
          <input name="username" autocomplete="username" required value="support" />
        </div>
        <div class="field">
          <label>密码</label>
          <input name="password" type="password" autocomplete="current-password" required value="support123" />
        </div>
        ${state.error ? `<div class="error">${escapeHtml(state.error)}</div>` : ""}
        <button class="primary" type="submit">登录</button>
      </form>
    </div>
  `;
}

function render() {
  if (!state.user) {
    renderLogin();
    return;
  }
  const selected = state.selectedTask;
  app.innerHTML = `
    <div class="layout">
      <header class="topbar">
        <div class="brand">AutoM</div>
        <div class="topbar-actions">
          <span>${escapeHtml(state.user.display_name)}</span>
          <button onclick="logout()">退出</button>
        </div>
      </header>
      <main class="main">
        <aside class="sidebar">
          ${renderCreateForm()}
          ${renderTaskList()}
        </aside>
        <section class="content">
          ${state.error ? `<div class="panel error">${escapeHtml(state.error)}</div>` : ""}
          ${selected ? renderTaskDetail(selected) : '<div class="panel empty">暂无任务</div>'}
        </section>
      </main>
    </div>
  `;
}

function renderCreateForm() {
  return `
    <form class="panel" onsubmit="createTask(event)">
      <h3>新建绘图需求</h3>
      <div class="field">
        <label>客户</label>
        <input name="customer_name" placeholder="客户名称，可选" />
      </div>
      <div class="field">
        <label>需求描述</label>
        <textarea name="description" required placeholder="请写明尺寸、孔位、厚度、材料、用途、约束条件等"></textarea>
      </div>
      <div class="field">
        <label>参考图片</label>
        <input type="file" multiple accept="image/png,image/jpeg,image/webp" />
      </div>
      <button class="primary" type="submit" ${state.loading ? "disabled" : ""}>提交任务</button>
    </form>
  `;
}

function renderTaskList() {
  const options = ["", "queued", "running", "completed", "failed", "cancelled"]
    .map((status) => `<option value="${status}" ${state.statusFilter === status ? "selected" : ""}>${status ? statusLabel(status) : "全部状态"}</option>`)
    .join("");
  const items = state.tasks
    .map(
      (task) => `
      <button class="task-item ${state.selectedTaskId === task.id ? "active" : ""}" onclick="selectTask(${task.id})">
        <div class="task-title">${escapeHtml(task.title)}</div>
        <div class="meta">
          <span class="status ${task.status}">${statusLabel(task.status)}</span>
          ${escapeHtml(task.created_by_name_snapshot)} · ${escapeHtml(task.created_at)}
        </div>
      </button>
    `,
    )
    .join("");
  return `
    <div class="panel">
      <h3>任务列表</h3>
      <div class="field">
        <select onchange="setStatusFilter(this.value)">${options}</select>
      </div>
      <div class="task-list">${items || '<div class="empty">没有任务</div>'}</div>
    </div>
  `;
}

function renderTaskDetail(detail) {
  const task = detail.task;
  const artifacts = detail.artifacts || [];
  const preview = artifacts.find((item) => item.kind === "preview_png");
  const downloads = artifacts
    .map(
      (item) => `
      <a class="download" href="/api/artifacts/${item.id}/download">
        <span>${escapeHtml(item.original_name)} · ${escapeHtml(item.kind)}</span>
        <span>${fmtBytes(item.size_bytes)}</span>
      </a>
    `,
    )
    .join("");
  const events = state.events
    .map((event) => `<div class="event">[${escapeHtml(event.created_at)}] ${escapeHtml(event.level)} ${escapeHtml(event.event_type)}\n${escapeHtml(event.message || "")}</div>`)
    .join("");
  return `
    <div class="detail-grid">
      <div>
        <div class="panel">
          <h2>${escapeHtml(task.title)}</h2>
          <div class="meta">
            <span class="status ${task.status}">${statusLabel(task.status)}</span>
            客服：${escapeHtml(task.created_by_name_snapshot)}
            客户：${escapeHtml(task.customer_name || "-")}
            单位：${escapeHtml(task.unit)}
          </div>
          <p>${escapeHtml(task.description).replaceAll("\\n", "<br />")}</p>
          <div class="actions">
            <button onclick="refreshSelected()">刷新</button>
            <button onclick="retryTask(${task.id})">重试</button>
            <button onclick="cancelTask(${task.id})" ${task.status !== "queued" ? "disabled" : ""}>取消</button>
          </div>
        </div>
        <div class="panel">
          <h3>事件日志</h3>
          <div class="events">${events || "暂无事件"}</div>
        </div>
      </div>
      <div>
        <div class="panel">
          <h3>预览</h3>
          <div class="preview">
            ${preview ? `<img src="/api/artifacts/${preview.id}/download" alt="preview" />` : '<span class="meta">任务完成后显示预览图</span>'}
          </div>
        </div>
        <div class="panel">
          <h3>下载</h3>
          <div class="downloads">${downloads || '<div class="empty">暂无产物</div>'}</div>
        </div>
      </div>
    </div>
  `;
}

async function selectTask(id) {
  await loadTask(id);
  render();
}

async function refreshSelected() {
  await loadTasks();
  render();
}

async function setStatusFilter(value) {
  state.statusFilter = value;
  state.selectedTaskId = null;
  await loadTasks();
  render();
}

setInterval(async () => {
  if (!state.user || !state.selectedTaskId) return;
  try {
    await loadTasks();
    render();
  } catch (_error) {
    // Keep the current screen if a transient refresh fails.
  }
}, 3000);

bootstrap().catch((error) => {
  state.error = error.message;
  render();
});
