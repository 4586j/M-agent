const API = "/collab/v1";
const DECISIONS = {
  delivery_confirmation: {title: "产出确认", field: "decision", reason: true,
                          choices: [["confirmed", "确认交付"], ["changes_requested", "退回"]]},
  business_acceptance: {title: "业务验收", field: "decision", reason: true,
                        choices: [["accepted", "验收通过"], ["changes_requested", "退回"]]},
  attempt_reconciliation: {title: "执行对账", field: "outcome", reason: true,
                          choices: [["cancelled", "确认已停止且无副作用"],
                                    ["failed", "已产生副作用"]]},
  final_acceptance: {title: "最终验收", field: "decision", reason: true,
                     choices: [["accepted", "签字验收"]]},
  rework_queue: {title: "重新排队", field: null, reason: false, choices: []},
  worker_attention: {title: "执行机失联（租约已过期）", field: null, reason: false,
                     choices: [], readonly: true},
};

let csrf = null;
let controller = new AbortController();

const fields = {};
for (const id of ["login", "workspace", "who", "logout", "project", "requirement", "todos",
                  "detail", "status"]) {
  fields[id] = document.getElementById(id);
}

function text(node, value) {
  node.textContent = value;
  return node;
}

function add(parent, tag, className, value) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (value !== undefined) {
    node.textContent = value;
  }
  parent.appendChild(node);
  return node;
}

function say(message, kind) {
  text(fields.status, message);
  fields.status.className = kind || "";
}

async function call(path, options) {
  const response = await fetch(API + path, {
    credentials: "same-origin",
    ...options,
    headers: {Accept: "application/json", ...(options.headers || {})},
    signal: controller.signal,
  });
  if (!response.ok) {
    let label = `请求失败 (${response.status})`;
    try {
      label = (await response.json()).detail || label;
    } catch (error) {
      // A gateway or a non-JSON body still only says the status; never echo the body.
    }
    throw new Error(label);
  }
  return response.status === 204 ? null : response.json();
}

function get(path) {
  return call(path, {method: "GET"});
}

function post(path, body) {
  return call(path, {method: "POST", headers: {"Content-Type": "application/json",
                                               "X-CSRF-Token": csrf},
                     body: JSON.stringify(body)});
}

function stop() {
  if (controller) {
    controller.abort();
  }
  controller = new AbortController();
}

async function refresh() {
  stop();
  const projectId = fields.project.value;
  const requirementId = fields.requirement.value;
  fields.todos.replaceChildren();
  fields.detail.replaceChildren();
  if (!projectId) {
    return;
  }
  try {
    const inbox = await get("/inbox");
    for (const entry of inbox.filter(item => DECISIONS[item.item_type])) {
      renderTodo(entry);
    }
    if (!fields.todos.children.length) {
      add(fields.todos, "li", null, "当前没有属于你的待办裁定。");
    }
    if (requirementId) {
      await renderRequirement(projectId, requirementId);
    }
    say("");
  } catch (error) {
    if (error.name !== "AbortError") {
      say(error.message, "error");
    }
  }
}

function renderTodo(entry) {
  const spec = DECISIONS[entry.item_type];
  const row = add(fields.todos, "li");
  add(row, "div", "kind", spec.title);
  add(row, "div", "locator", `${entry.title} · ${entry.item_type} · 版本 ${entry.expected_version}`);
  if (spec.readonly) {
    return;
  }
  const form = add(row, "div", "form-row");
  let choice;
  if (spec.choices.length) {
    choice = add(form, "select");
    for (const [value, label] of spec.choices) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      choice.appendChild(option);
    }
  }
  let reason;
  if (spec.reason) {
    reason = add(form, "input");
    reason.placeholder = "写下理由（退回必填）";
    reason.size = 34;
  }
  const button = add(form, "button", null, "提交");
  button.type = "button";
  button.addEventListener("click", async () => {
    button.disabled = true;
    const body = {expected_version: entry.expected_version};
    if (spec.field) {
      body[spec.field] = choice.value;
    }
    if (reason) {
      body.reason = reason.value;
    }
    try {
      await post(entry.href, body);
      say(`${spec.title}已记录。`, "ok");
      await load();
    } catch (error) {
      button.disabled = false;
      if (error.name !== "AbortError") {
        say(`${spec.title}失败：${error.message}`, "error");
      }
    }
  });
}

async function renderRequirement(projectId, requirementId) {
  const requirement = await get(`/projects/${projectId}/requirements/${requirementId}`);
  const block = add(fields.detail, "div");
  add(block, "h3", null, requirement.title);
  const facts = add(block, "dl");
  for (const [key, label] of [["status", "状态"], ["version", "版本"]]) {
    add(facts, "dt", null, label);
    add(facts, "dd", null, String(requirement[key]));
  }
  const advance = add(block, "button", null, "按工作项事实汇总需求进度");
  advance.type = "button";
  advance.addEventListener("click", async () => {
    advance.disabled = true;
    try {
      await post(`/projects/${projectId}/requirements/${requirementId}/actions`,
                 {action: "advance", expected_version: requirement.version});
      say("需求状态已按工作项事实重算。", "ok");
      await load();
    } catch (error) {
      advance.disabled = false;
      if (error.name !== "AbortError") {
        say(`汇总失败：${error.message}`, "error");
      }
    }
  });
  if (requirement.current_revision_id) {
    const revision = await get(`/projects/${projectId}/requirements/${requirementId}`
                               + `/revisions/${requirement.current_revision_id}`);
    add(block, "dt", null, "需求原文（完整，只读）");
    add(block, "pre", "original", revision.content);
  }
  try {
    const delivery = await get(`/projects/${projectId}/requirements/${requirementId}/delivery`);
    add(block, "dt", null, "交付证据");
    for (const item of delivery.delivery.items) {
      const row = add(block, "dl", null);
      add(row, "dd", null, `${item.key} · ${item.status} · 已接受尝试 `
                        + `${item.attempt_number ?? "无"} · 摘要 ${item.result_hash ?? "无"}`);
      add(row, "dd", null, `验收项：${item.acceptance.map(entry => entry.text).join("、") || "无"}`);
      add(row, "dd", null, `产物：${item.artifacts.map(entry => entry.name).join("、") || "无"}`);
    }
    for (const verdict of delivery.acceptances) {
      add(block, "dd", null, `提出人裁定：${verdict.decision} · ${verdict.reason || "无理由文本"}`);
    }
  } catch (error) {
    if (error.name !== "AbortError") {
      add(block, "p", "locator", `交付视图暂不可用：${error.message}`);
    }
  }
}

async function loadProjects() {
  const projects = await get("/projects");
  fields.project.replaceChildren();
  for (const project of projects) {
    const option = document.createElement("option");
    option.value = project.project_id;
    option.textContent = project.name;
    fields.project.appendChild(option);
  }
  await loadRequirements();
}

async function loadRequirements() {
  fields.requirement.replaceChildren();
  if (!fields.project.value) {
    return;
  }
  const list = await get(`/projects/${fields.project.value}/requirements`);
  for (const requirement of list) {
    const option = document.createElement("option");
    option.value = requirement.requirement_id;
    option.textContent = `${requirement.title}（${requirement.status}）`;
    fields.requirement.appendChild(option);
  }
  await refresh();
}

async function load() {
  await loadRequirements();
}

function enter(session) {
  csrf = session.csrf_token;
  fields.login.hidden = true;
  fields.workspace.hidden = false;
  fields.logout.hidden = false;
  text(fields.who, session.user.display_name);
}

async function resume() {
  try {
    enter(await get("/me"));
    await loadProjects();
  } catch (error) {
    if (error.name !== "AbortError") {
      fields.login.hidden = false;
    }
  }
}

document.getElementById("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = new FormData(event.target);
  try {
    const session = await call("/auth/login", {method: "POST",
                                               headers: {"Content-Type": "application/json"},
                                               body: JSON.stringify({
                                                 username: form.get("username"),
                                                 password: form.get("password")})});
    enter(session);
    await loadProjects();
  } catch (error) {
    say(`登录失败：${error.message}`, "error");
  }
});

fields.logout.addEventListener("click", async () => {
  stop();
  await post("/auth/logout", {});
  csrf = null;
  fields.workspace.hidden = true;
  fields.login.hidden = false;
  fields.logout.hidden = true;
  text(fields.who, "");
});

fields.project.addEventListener("change", loadRequirements);
fields.requirement.addEventListener("change", refresh);
document.getElementById("refresh").addEventListener("click", refresh);
resume();
