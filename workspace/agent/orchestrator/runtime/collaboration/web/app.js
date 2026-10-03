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
                     hint: "退回时必填：写下整篇交付缺了什么",
                     choices: [["accepted", "签字验收"], ["changes_requested", "退回整篇交付"]]},
  rework_queue: {title: "重新排队", field: null, reason: false, choices: []},
  worker_attention: {title: "执行机失联（租约已过期）", field: null, reason: true, choices: [],
                     hint: "写下你看到的实情（可留空）",
                     look: "instances", lookLabel: "查看执行机", submitLabel: "撤销执行权",
                     shown: renderInstance},
  requirement_review: {title: "需求评审（人工确认这一版原文）", field: "action", reason: true,
                       hint: "驳回时写下要改哪里",
                       choices: [["approve", "批准这一版"], ["reject", "驳回"]],
                       extra: {revision_id: "review_revision_id"},
                       look: "revision", lookLabel: "查看送审原文", shown: showRevision,
                       messages: true},
  requirement_returned: {title: "需求被打回，改稿后重送", field: null, reason: false, choices: [],
                         look: "revision", lookLabel: "查看当前原文", shown: showRevision,
                         edit: true},
};

let csrf = null;
let me = null;
let role = null;
let roster = [];
let projectNames = {};
let pendingRequirement = null;
let controller = new AbortController();

const fields = {};
for (const id of ["login", "workspace", "who", "logout", "project", "requirement", "todos",
                  "todoCount", "createBox", "detail", "status"]) {
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
  roster = [];
  fields.todos.replaceChildren();
  fields.createBox.replaceChildren();
  fields.detail.replaceChildren();
  text(fields.todoCount, "");
  say("");
  if (!projectId) {
    return true;
  }
  try {
    role = (await get(`/projects/${projectId}`)).access_role;
    roster = await get(`/projects/${projectId}/members`);
    const inbox = await get("/inbox");
    const todos = inbox.filter(item => DECISIONS[item.item_type]);
    for (const entry of todos) {
      renderTodo(entry);
    }
    const unread = todos.filter(entry => entry.unread).length;
    text(fields.todoCount, unread ? `（未读 ${unread}）` : "");
    if (!fields.todos.children.length) {
      add(fields.todos, "li", null, "当前没有属于你的待办裁定。");
    }
    renderCreate(projectId);
    if (requirementId) {
      await renderRequirement(projectId, requirementId);
    }
    return true;
  } catch (error) {
    if (error.name === "AbortError") {
      // 被更晚的一次刷新取代：这一轮不算失败，也不报错误。
      return true;
    }
    say(error.message, "error");
    return false;
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
  const head = add(row, "div", "kind");
  add(head, "span", null, spec.title);
  if (entry.unread) {
    add(head, "span", "unread", "未读");
  }
  add(row, "div", "locator", `${projectNames[entry.project_id] ?? entry.project_id} · `
                             + `${entry.title} · ${entry.item_type} · 版本 ${entry.expected_version}`);
  const facts = add(row, "dl", "evidence");
  facts.hidden = true;
  const form = add(row, "div", "form-row");
  if (spec.look) {
    peekButton(form, entry, facts, spec.look, spec.lookLabel, spec.shown,
               "这个执行身份还没有认领到任何实例。");
    if (spec.messages) {
      // 提出人写在澄清里的边界，正是审核人要照着判的那部分上下文。
      peekButton(form, entry, facts, "messages", "查看澄清对话", showMessage,
                 "提出人还没有补充过澄清说明。");
    }
  }
  if (spec.edit) {
    draftEditor(row, {project_id: entry.project_id, requirement_id: entry.requirement_id,
                      expected_version: entry.expected_version,
                      current_revision_id: entry.current_revision_id,
                      revision_href: entry.links.revision, submit: "重新送审"});
  } else {
    renderChoice(form, entry, spec);
  }
  if (entry.unread) {
    ackButton(form, entry);
  }
}

function peekButton(form, entry, facts, link, label, shown, empty) {
  const button = add(form, "button", "soft", label);
  button.type = "button";
  button.addEventListener("click", async () => {
    try {
      const rows = await get(entry.links[link]);
      const list = Array.isArray(rows) ? rows : [rows];
      facts.replaceChildren();
      if (!list.length) {
        add(facts, "dd", null, empty);
      }
      for (const item of list) {
        shown(facts, item);
      }
      facts.hidden = false;
    } catch (error) {
      if (error.name !== "AbortError") {
        say(`${label}失败：${error.message}`, "error");
      }
    }
  });
}

function showRevision(parent, revision) {
  add(parent, "dd", null, `第 ${revision.number} 版 · 摘要 ${revision.content_hash}`);
  add(parent, "pre", "original", revision.content);
}

function showMessage(parent, message) {
  add(parent, "dd", null, `${message.role === "user" ? "提出人" : "助手"} · `
                          + `${when(message.created_at)} · ${message.content}`);
}

function renderChoice(form, entry, spec) {
  let choice;
  let reason;
  if (spec.choices.length) {
    choice = add(form, "select");
    for (const [value, label] of spec.choices) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      choice.appendChild(option);
    }
  }
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
    for (const [name, field] of Object.entries(spec.extra || {})) {
      body[name] = entry[field];
    }
    try {
      await post(entry.href, body);
      await report(`${spec.title}已记录。`);
    } catch (error) {
      button.disabled = false;
      if (error.name !== "AbortError") {
        say(`${spec.title}失败：${error.message}`, "error");
      }
    }
  });
}

function draftEditor(parent, target) {
  const editor = add(parent, "textarea", "draft");
  editor.rows = 8;
  editor.placeholder = "需求原文：整篇贴回来，服务端存的是完整原文";
  const form = add(parent, "div", "form-row");
  const path = `/projects/${target.project_id}/requirements/${target.requirement_id}`;
  if (target.revision_href) {
    draftButton(form, "把当前原文载入编辑框", "soft", async () => {
      const revision = await get(target.revision_href);
      editor.value = revision.content;
      say("当前原文已载入编辑框。", "ok");
    });
  }
  draftButton(form, "保存为新修订", null, async () => {
    await post(`${path}/revisions`, {content: editor.value,
                                      expected_version: target.expected_version});
    await report(`新修订已保存；再选审核人点「${target.submit}」。`);
  });
  if (target.current_revision_id) {
    // 驳回会把审核人清空，首次送审也还没点过名，所以这一步必须重新选：没有可继承的默认值。
    const reviewer = reviewerSelect(form);
    draftButton(form, target.submit, null, async () => {
      if (!reviewer.value) {
        say("先选一位审核人，再送审。", "error");
        return;
      }
      await post(`${path}/reviews`, {action: "request", revision_id: target.current_revision_id,
                                     reviewer_user_id: reviewer.value,
                                     expected_version: target.expected_version});
      await report("已送审，等审核人裁定这一版。");
    });
  }
}

function reviewerSelect(form) {
  const select = add(form, "select");
  const blank = add(select, "option", null, "选择审核人");
  blank.value = "";
  for (const member of roster.filter(item => item.status === "active")) {
    const option = document.createElement("option");
    option.value = member.user_id;
    option.textContent = `${member.display_name}（${member.access_role}）`;
    select.appendChild(option);
  }
  return select;
}

function renderCreate(projectId) {
  const box = fields.createBox;
  add(box, "p", "locator", "写成一份完整原文再送审：审核人点名的那一位批准之后，需求才会进入拆解与执行。");
  const title = add(box, "input");
  title.placeholder = "需求标题";
  title.size = 34;
  const content = add(box, "textarea", "draft");
  content.rows = 6;
  content.placeholder = "需求原文（整篇贴入）";
  const items = add(box, "textarea", "draft");
  items.rows = 3;
  items.placeholder = "验收标准，每行一条";
  const form = add(box, "div", "form-row");
  draftButton(form, "创建需求", null, async () => {
    if (!content.value.trim()) {
      say("需求原文不能为空：这里存的是整篇原文，不是摘要。", "error");
      return;
    }
    const acceptance = items.value.split("\n").map(line => line.trim()).filter(line => line);
    const created = await post(`/projects/${projectId}/requirements`,
                               {title: title.value, content: content.value,
                                acceptance_items: acceptance});
    pendingRequirement = created.requirement_id;
    await report("需求已创建为草稿；补好澄清对话后在下方选审核人送审。");
  });
}

async function renderClarification(block, projectId, requirementId) {
  const path = `/projects/${projectId}/requirements/${requirementId}`;
  const messages = await get(`${path}/messages`);
  add(block, "dt", null, "澄清对话（本服务未接模型，这里只存人写下的补充）");
  if (!messages.length) {
    add(block, "dd", null, "还没有补充说明。把审核人可能要追问的边界先写在这里。");
  }
  for (const message of messages) {
    add(block, "dd", null, `${message.role === "user" ? "我" : "助手"} · `
                           + `${when(message.created_at)} · ${message.content}`);
  }
  const form = add(block, "div", "form-row");
  const input = add(form, "input");
  input.placeholder = "补充一条说明（不改动原文）";
  input.size = 44;
  draftButton(form, "记进澄清对话", null, async () => {
    await post(`${path}/messages`, {content: input.value});
    await report("补充说明已记下。");
  });
}

function draftButton(form, label, kind, run) {
  const button = add(form, "button", kind, label);
  button.type = "button";
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await run();
    } catch (error) {
      if (error.name !== "AbortError") {
        say(`${label}失败：${error.message}`, "error");
      }
    } finally {
      // 写成功时整行会被重建，这里只是让「没发出去」的那次点击还能再点一次。
      button.disabled = false;
    }
  });
}

function ackButton(form, entry) {
  // 读过不等于裁定：标记之后这条待办仍然挂着，直到写下决定。
  const seen = add(form, "button", "soft", "标记已读");
  seen.type = "button";
  seen.addEventListener("click", async () => {
    seen.disabled = true;
    try {
      await post("/inbox/ack", {item_key: entry.key});
      await report("已记下你看过了这条待办；裁定仍然挂着。");
    } catch (error) {
      seen.disabled = false;
      if (error.name !== "AbortError") {
        say(`标记已读失败：${error.message}`, "error");
      }
    }
  });
}

async function renderRequirement(projectId, requirementId) {
  const requirement = await get(`/projects/${projectId}/requirements/${requirementId}`);
  const block = add(fields.detail, "div");
  const path = `/projects/${projectId}/requirements/${requirementId}`;
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
        await report("需求状态已按工作项事实重算。");
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
        await report(moved.status === "cancelled" ? "需求已取消。"
                                                  : "需求进入取消流程；停止未确认的执行会生成对账待办。");
      } catch (error) {
        cancel.disabled = false;
        if (error.name !== "AbortError") {
          say(`取消需求失败：${error.message}`, "error");
        }
      }
    });
  }
  if (requirement.status === "draft" && requirement.requester_id === me) {
    await renderClarification(block, projectId, requirementId);
    add(block, "dt", null, "改稿与送审");
    draftEditor(block, {project_id: projectId, requirement_id: requirementId,
                        expected_version: requirement.version,
                        current_revision_id: requirement.current_revision_id,
                        revision_href: requirement.current_revision_id
                          ? `${path}/revisions/${requirement.current_revision_id}` : null,
                        submit: "送审"});
  }
  if (requirement.current_revision_id) {
    const revision = await get(`${path}/revisions/${requirement.current_revision_id}`);
    add(block, "dt", null, "需求原文（完整，只读）");
    add(block, "pre", "original", revision.content);
  }
  // 没有已发布计划时不请求交付视图：那份汇总本来就不存在，服务端只会回一个 409。
  if (await renderWorkItems(block, projectId, requirementId)) {
    try {
      const delivery = await get(`${path}/delivery`);
      add(block, "dt", null, "交付证据");
      for (const item of delivery.delivery.items) {
        const row = add(block, "dl", null);
        add(row, "dd", null, `${item.key} · ${item.status} · 已接受尝试 `
                          + `${item.attempt_number ?? "无"} · 摘要 ${item.result_hash ?? "无"}`);
        add(row, "dd", null,
            `验收项：${item.acceptance.map(entry => entry.text).join("、") || "无"}`);
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
}

async function renderWorkItems(block, projectId, requirementId) {
  const plans = await get(`/projects/${projectId}/requirements/${requirementId}/plans`);
  const published = plans.filter(plan => plan.status === "published");
  add(block, "dt", null, "工作项");
  if (!published.length) {
    add(block, "dd", null, "还没有已发布的计划，界面不代为拆解需求。");
    return false;
  }
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
        await report(`队列已重算，${moved.work_items_changed} 个工作项状态变化。`);
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
          await report("已为该工作项派发一次尝试。");
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
          await report("执行权已撤销；若停止未被确认，会转为一条对账待办。");
        } catch (error) {
          button.disabled = false;
          if (error.name !== "AbortError") {
            say(`撤销失败：${error.message}`, "error");
          }
        }
      });
    }
  }
  return true;
}

async function report(message) {
  // 先刷新再报结果：刷新会重写整块面板，反过来的话这句提示一闪就没了。
  // 刷新失败也不能把已经写进去的裁定说成没写，所以两句一起留下。
  let kind = "ok";
  let detail = "";
  try {
    if (!await load()) {
      kind = "";
      detail = "（刷新失败，请按刷新重试）";
    }
  } catch (error) {
    if (error.name === "AbortError") {
      return;
    }
    kind = "";
    detail = `（刷新失败：${error.message}）`;
  }
  say(message + detail, kind);
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
  return loadRequirements();
}

async function loadRequirements() {
  // 刚创建的需求要能被自动选中，否则用户写完原文却找不到它落在哪一行。
  const kept = pendingRequirement || fields.requirement.value;
  pendingRequirement = null;
  fields.requirement.replaceChildren();
  if (!fields.project.value) {
    return true;
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
  return refresh();
}

async function load() {
  return loadRequirements();
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
  roster = [];
  pendingRequirement = null;
  projectNames = {};
  fields.workspace.hidden = true;
  fields.login.hidden = false;
  fields.logout.hidden = true;
  fields.project.replaceChildren();
  fields.requirement.replaceChildren();
  fields.todos.replaceChildren();
  text(fields.todoCount, "");
  fields.detail.replaceChildren();
  text(fields.who, "");
  say("");
});

fields.project.addEventListener("change", loadRequirements);
fields.requirement.addEventListener("change", refresh);
document.getElementById("refresh").addEventListener("click", load);
resume();
