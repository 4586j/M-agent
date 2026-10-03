const API = "/collab/v1";
const MANAGERS = ["owner", "manager"];
const DECISIONS = {
  delivery_confirmation: {title: "产出确认", field: "decision", reason: true,
                          choices: [["confirmed", "确认交付"], ["changes_requested", "退回"]]},
  business_acceptance: {title: "业务验收", field: "decision", reason: true,
                        choices: [["accepted", "验收通过"], ["changes_requested", "退回"]]},
  attempt_reconciliation: {title: "执行对账", field: "outcome", reason: true,
                           hint: "必须写下你核实到的实情",
                           choices: [["cancelled", "确认已停止且无副作用"],
                                     ["failed", "已产生副作用"]]},
  final_acceptance: {title: "最终验收", field: "decision", reason: true,
                     hint: "写下验收说明（可留空）",
                     choices: [["accepted", "签字验收"]]},
  rework_queue: {title: "重新排队", field: null, reason: false, choices: []},
  worker_attention: {title: "执行机失联（租约已过期）", field: null, reason: true, choices: [],
                     hint: "写下你看到的实情（可留空）",
                     look: "instances", lookLabel: "查看执行机", submitLabel: "撤销执行权"},
};

let csrf = null;
let me = null;
let role = null;
let projectNames = {};
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

function when(seconds) {
  return seconds ? new Date(seconds * 1000).toLocaleString() : "无";
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
  role = null;
  fields.todos.replaceChildren();
  fields.detail.replaceChildren();
  if (!projectId) {
    return;
  }
  try {
    role = (await get(`/projects/${projectId}`)).access_role;
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

function renderInstance(parent, instance) {
  const row = add(parent, "dl", null);
  add(row, "dd", null, `节点 ${instance.node_id} · 状态 ${instance.status}`
                        + ` · 控制纪元 ${instance.control_epoch}`);
  add(row, "dd", null, `心跳 ${when(instance.heartbeat_at)} · 认领于 ${when(instance.claimed_at)}`);
  add(row, "dd", null, `隔离声明 ${instance.isolation_mode || "未声明"} · 负责人已核验 `
                        + `${instance.isolation_verified ? "是" : "否"}`);
}

function renderTodo(entry) {
  const spec = DECISIONS[entry.item_type];
  const row = add(fields.todos, "li");
  add(row, "div", "kind", spec.title);
  add(row, "div", "locator", `${projectNames[entry.project_id] ?? entry.project_id} · `
                             + `${entry.title} · ${entry.item_type} · 版本 ${entry.expected_version}`);
  const facts = add(row, "dl", "evidence");
  facts.hidden = true;
  const form = add(row, "div", "form-row");
  if (spec.look) {
    const peek = add(form, "button", null, spec.lookLabel);
    peek.type = "button";
    peek.addEventListener("click", async () => {
      try {
        const rows = await get(entry.links[spec.look]);
        facts.replaceChildren();
        if (!rows.length) {
          add(facts, "dd", null, "这个执行身份还没有认领到任何实例。");
        }
        for (const instance of rows) {
          renderInstance(facts, instance);
        }
        facts.hidden = false;
      } catch (error) {
        if (error.name !== "AbortError") {
          say(`${spec.lookLabel}失败：${error.message}`, "error");
        }
      }
    });
  }
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
    reason.placeholder = spec.hint || "写下理由（退回时必填）";
    reason.size = 34;
  }
  const button = add(form, "button", null, spec.submitLabel || "提交");
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
  if (["executing", "rework", "awaiting_acceptance"].includes(requirement.status)) {
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
  }
  if ((MANAGERS.includes(role) || requirement.requester_id === me)
      && !["cancelled", "completed"].includes(requirement.status)) {
    const form = add(block, "div", "form-row");
    const reason = add(form, "input");
    reason.placeholder = "取消理由（可留空）";
    reason.size = 34;
    // 有在途执行时第一次取消只能落 cancelling；对账做完再按一次，才落 cancelled。
    const cancel = add(form, "button", null,
                       requirement.status === "cancelling" ? "完成取消（已无在途执行）" : "取消需求");
    cancel.type = "button";
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      try {
        const moved = await post(`/projects/${projectId}/requirements/${requirementId}/actions`,
                                 {action: "cancel", expected_version: requirement.version,
                                  reason: reason.value});
        say(moved.status === "cancelled" ? "需求已取消。"
                                        : "需求进入取消流程；停止未确认的执行会生成对账待办。", "ok");
        await load();
      } catch (error) {
        cancel.disabled = false;
        if (error.name !== "AbortError") {
          say(`取消需求失败：${error.message}`, "error");
        }
      }
    });
  }
  if (requirement.current_revision_id) {
    const revision = await get(`/projects/${projectId}/requirements/${requirementId}`
                               + `/revisions/${requirement.current_revision_id}`);
    add(block, "dt", null, "需求原文（完整，只读）");
    add(block, "pre", "original", revision.content);
  }
  await renderWorkItems(block, projectId, requirementId);
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

async function renderWorkItems(block, projectId, requirementId) {
  const plans = await get(`/projects/${projectId}/requirements/${requirementId}/plans`);
  const published = plans.filter(plan => plan.status === "published");
  add(block, "dt", null, "工作项");
  if (!published.length) {
    add(block, "dd", null, "还没有已发布的计划，界面不代为拆解需求。");
    return;
  }
  const roster = await get(`/projects/${projectId}/members`);
  const people = {};
  for (const member of roster) {
    people[member.user_id] = member.display_name;
  }
  const plan = published[published.length - 1];
  const detail = await get(`/projects/${projectId}/requirements/${requirementId}/plans`
                           + `/${plan.plan_id}`);
  const keys = {};
  for (const item of detail.work_items) {
    keys[item.work_item_id] = item.key;
  }
  const needs = {};
  for (const edge of detail.dependencies) {
    (needs[edge.work_item_id] = needs[edge.work_item_id] || []).push(
      keys[edge.depends_on_id] || edge.depends_on_id);
  }
  if (MANAGERS.includes(role)) {
    const form = add(block, "div", "form-row");
    const tick = add(form, "button", null, "推进工作项队列");
    tick.type = "button";
    tick.addEventListener("click", async () => {
      tick.disabled = true;
      try {
        const moved = await post(`/projects/${projectId}/scheduler/tick`, {});
        say(`队列已重算，${moved.work_items_changed} 个工作项状态变化。`, "ok");
        await load();
      } catch (error) {
        tick.disabled = false;
        if (error.name !== "AbortError") {
          say(`队列推进失败：${error.message}`, "error");
        }
      }
    });
  }
  for (const item of detail.work_items) {
    const row = add(block, "dl", null);
    add(row, "dd", null, `${item.key} · ${item.title} · ${item.status} · 版本 ${item.version}`
                          + ` · 估算 ${item.estimated_units}`);
    add(row, "dd", null, `职责 ${item.duty} · 负责人 ${people[item.assignee_user_id] ?? item.assignee_user_id}`
                          + (needs[item.work_item_id] ? ` · 依赖 ${needs[item.work_item_id].join("、")}`
                                                       : ""));
    if (MANAGERS.includes(role) && item.status === "ready") {
      const form = add(row, "div", "form-row");
      const button = add(form, "button", null, "派发一次尝试");
      button.type = "button";
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await post(`/projects/${projectId}/work-items/${item.work_item_id}/dispatch`, {});
          say("已为该工作项派发一次尝试。", "ok");
          await load();
        } catch (error) {
          button.disabled = false;
          if (error.name !== "AbortError") {
            say(`派发失败：${error.message}`, "error");
          }
        }
      });
    }
    const closed = item.status === "accepted" || item.status === "cancelled";
    if (!closed && (MANAGERS.includes(role) || item.assignee_user_id === me)) {
      const form = add(row, "div", "form-row");
      const reason = add(form, "input");
      reason.placeholder = "撤销理由（可留空）";
      reason.size = 24;
      const button = add(form, "button", null, "撤销执行权");
      button.type = "button";
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await post(`/projects/${projectId}/work-items/${item.work_item_id}/cancel`,
                     {expected_version: item.version, reason: reason.value});
          say("执行权已撤销；若停止未被确认，会转为一条对账待办。", "ok");
          await load();
        } catch (error) {
          button.disabled = false;
          if (error.name !== "AbortError") {
            say(`撤销失败：${error.message}`, "error");
          }
        }
      });
    }
  }
}

async function loadProjects() {
  const kept = fields.project.value;
  const projects = await get("/projects");
  projectNames = {};
  fields.project.replaceChildren();
  for (const project of projects) {
    projectNames[project.project_id] = project.name;
    const option = document.createElement("option");
    option.value = project.project_id;
    option.textContent = project.name;
    fields.project.appendChild(option);
  }
  if ([...fields.project.options].some(option => option.value === kept)) {
    fields.project.value = kept;
  }
  await loadRequirements();
}

async function loadRequirements() {
  const kept = fields.requirement.value;
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
  if ([...fields.requirement.options].some(option => option.value === kept)) {
    fields.requirement.value = kept;
  }
  await refresh();
}

async function load() {
  await loadRequirements();
}

function enter(session) {
  csrf = session.csrf_token;
  me = session.user.user_id;
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
  try {
    await post("/auth/logout", {});
  } catch (error) {
    if (error.name === "AbortError") {
      return;
    }
  }
  // Whatever the server said, this browser stops holding anyone's view of the work.
  csrf = null;
  me = null;
  role = null;
  projectNames = {};
  fields.workspace.hidden = true;
  fields.login.hidden = false;
  fields.logout.hidden = true;
  fields.project.replaceChildren();
  fields.requirement.replaceChildren();
  fields.todos.replaceChildren();
  fields.detail.replaceChildren();
  text(fields.who, "");
});

fields.project.addEventListener("change", loadRequirements);
fields.requirement.addEventListener("change", refresh);
document.getElementById("refresh").addEventListener("click", load);
resume();
