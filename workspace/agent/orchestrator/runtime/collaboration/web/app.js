const API = "/collab/v1";
const MANAGERS = ["owner", "manager"];
// 与后端 PAUSABLE_STATUSES 同一条边界：只有还在派发的需求能被暂停，等签字的交付不由界面打断。
const PAUSABLE = ["executing", "rework"];
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
                     choices: [["accepted", "签字验收"], ["changes_requested", "退回整篇交付"]],
                     look: "coverage", lookLabel: "查看覆盖核对", shown: showCoverage},
  integration_check: {title: "集成核对", field: "decision",
                      reason: true, hint: "退回时必填：写下缺了哪一条验收",
                      choices: [["passed", "核对通过，交给提出人"], ["returned", "退回返工"]],
                      look: "coverage", lookLabel: "查看覆盖核对", shown: showCoverage},
  rework_queue: {title: "重新排队", field: null, reason: false, choices: []},
  worker_attention: {title: "执行机失联（租约已过期）", field: null, reason: true, choices: [],
                     hint: "写下你看到的实情（可留空）",
                     look: "instances", lookLabel: "查看执行机", submitLabel: "撤销执行权",
                     shown: renderInstance},
  machine_absent: {title: "执行机整台没消息", field: null, reason: false, choices: [],
                   hint: "禁用只表示不再等这一台：它不再领新的活，重新登记一台才能继续跑",
                   look: "instances", lookLabel: "查看执行机", submitLabel: "禁用这台执行机",
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

// 需求动态读的是审计流水，主语由服务端算好（工作项 A、第 1 次尝试 · A），这里只补谓语那半句。
// 没在这里登记的动作不会被藏起来：它会带着机器名出现，所以漏一个词只是难看，不会少一条事实。
const NEWS = {
  "requirement.created": "被创建",
  "requirement.revision_saved": "存下来了",
  "requirement.message_posted": "补了一条澄清",
  "requirement.review_requested": "已送审",
  "requirement.review_approved": "被点名的审核人批准了这一版",
  "requirement.review_rejected": "被点名的审核人驳回，要改",
  "requirement.executing": "进入执行",
  "requirement.awaiting_acceptance": "整篇交付等提出人签字",
  "requirement.rework": "退回返工",
  "requirement.integrating": "交付齐了，等人核对覆盖",
  "integration.passed": "核对过覆盖，可以签字了",
  "integration.returned": "被核对退回，说清了缺哪一条",
  "requirement.completed": "已签字验收",
  "requirement.cancelling": "进入取消流程",
  "requirement.cancelled": "已取消",
  "requirement.planning_blocked": "拆解被挡住",
  "requirement.paused": "已暂停",
  "requirement.resumed": "已恢复",
  "plan.published": "已发布",
  "plan.blocked": "被挡住",
  "work_item.pending_dependency": "还在等依赖",
  "work_item.ready": "就绪",
  "work_item.waiting_agent": "等可用的执行机",
  "work_item.running": "开始执行",
  "work_item.awaiting_owner_review": "等负责人确认产出",
  "work_item.submitted": "已提交产出",
  "work_item.accepted": "被验收通过",
  "work_item.changes_requested": "被要求改动",
  "work_item.blocked": "被阻塞",
  "work_item.failed": "失败",
  "work_item.requeued": "重新排队",
  "work_item.cancelled": "被取消",
  "attempt.assigned": "已派发",
  "attempt.lease_reclaimed": "租约过期，服务端收回了执行权",
  "attempt.ack": "执行机确认接到",
  "attempt.start": "执行机开始运行",
  "attempt.result": "执行机回报了结果",
  "attempt.cancel": "执行机确认已停止",
  "attempt.confirmed": "负责人确认了这次产出",
  "attempt.changes_requested": "负责人退回了这次产出",
  "attempt.reconciled.cancelled": "对账为已停止且无副作用",
  "attempt.reconciled.failed": "对账为已产生副作用",
};
const ACTIVITY_LIMIT = 20;
const FEED_LIMIT = 20;
const TODO_LIMIT = 20;

let csrf = null;
let me = null;
let role = null;
let roster = [];
let projectNames = {};
let pendingRequirement = null;
let controller = new AbortController();
// 待办一次只摊一屏，这三个数说的是屏幕上这一叠，不是服务端一共欠谁多少条。
let todoShown = 0;
let todoTotal = 0;
let todoUnread = 0;

const fields = {};
for (const id of ["login", "workspace", "who", "logout", "project", "requirement", "todos",
                  "todoCount", "feedList", "feedCount", "createBox", "detail", "status"]) {
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
  const {raw, ...init} = options || {};
  const response = await fetch(API + path, {
    credentials: "same-origin",
    ...init,
    headers: {Accept: "application/json", ...(init.headers || {})},
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
  if (raw) {
    return response;
  }
  return response.status === 204 ? null : response.json();
}

function get(path) {
  return call(path, {method: "GET"});
}

// 待办的总数只能从响应头拿：正文是一个数组，给它套信封会把所有既有读法都改掉。
function getRaw(path) {
  return call(path, {method: "GET", raw: true});
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

function resetTodos() {
  todoShown = 0;
  todoTotal = 0;
  todoUnread = 0;
  fields.todos.replaceChildren();
  text(fields.todoCount, "");
}

async function loadTodos(path) {
  const response = await getRaw(path);
  const rows = await response.json();
  todoTotal = Number(response.headers.get("X-Total-Count") ?? rows.length);
  for (const entry of rows.filter(item => DECISIONS[item.item_type])) {
    renderTodo(entry);
  }
  todoShown += rows.length;
  todoUnread += rows.filter(entry => entry.unread).length;
  renderTodoTail();
}

function renderTodoTail() {
  const old = fields.todos.querySelector(".more");
  if (old) {
    old.remove();
  }
  text(fields.todoCount, todoUnread ? `（未读 ${todoUnread}）` : "");
  if (!todoShown) {
    add(fields.todos, "li", null, "当前没有属于你的待办裁定。");
    return;
  }
  if (todoShown < todoTotal) {
    // 一屏只摊这么多：剩下的条数说清楚，取回下一批的按钮也留在这儿，别让人以为待办就这几条。
    const row = add(fields.todos, "li", "more locator",
                    `已摊开 ${todoShown} 条，还有 ${todoTotal - todoShown} 条没取回来。`);
    const button = add(row, "button", "soft", "再看一批");
    button.type = "button";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await loadTodos(`/inbox?limit=${TODO_LIMIT}&offset=${todoShown}`);
      } catch (error) {
        button.disabled = false;
        if (error.name !== "AbortError") {
          say(`待办翻页失败：${error.message}`, "error");
        }
      }
    });
  }
}

async function refresh() {
  stop();
  const projectId = fields.project.value;
  const requirementId = fields.requirement.value;
  role = null;
  roster = [];
  resetTodos();
  fields.feedList.replaceChildren();
  fields.createBox.replaceChildren();
  fields.detail.replaceChildren();
  text(fields.feedCount, "");
  say("");
  try {
    // 动态跨项目，跟着人走，所以它不等项目选出来才加载；待办与详情才按项目取。
    renderFeed(await get(`/feed?limit=${FEED_LIMIT}`));
    if (!projectId) {
      return true;
    }
    role = (await get(`/projects/${projectId}`)).access_role;
    roster = await get(`/projects/${projectId}/members`);
    await loadTodos(`/inbox?limit=${TODO_LIMIT}`);
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
  if (spec.hint && !spec.reason) {
    // 没有理由输入框时 hint 无处可显：把这条待办的边界摊成一行字，免得"禁用"被读成"这轮作废"。
    add(row, "div", "locator", spec.hint);
  }
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
    ackButton(form, entry, "已记下你看过了这条待办；裁定仍然挂着。");
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

function personName(userId) {
  const found = roster.find(entry => entry.user_id === userId);
  return found ? found.display_name : userId;
}

function showCoverage(parent, view) {
  // 核对的是"原文的每一条验收有没有被这份交付盖住"，所以两边都要摊开：映射，和已经签过的裁定。
  for (const item of view.report.acceptance) {
    add(parent, "dd", null, `${item.item_id} · ${item.text} · `
        + (item.covered_by.length ? `由 ${item.covered_by.join("、")} 交付` : "没有已验收的工作项覆盖"));
  }
  add(parent, "dd", "locator", `本次交付摘要 ${view.report.evidence_hash}`);
  if (!view.verdicts.length) {
    add(parent, "dd", null, "还没有人核对过这一版交付。");
  }
  for (const verdict of view.verdicts) {
    add(parent, "dd", null, `${personName(verdict.actor_user_id)} `
        + `${verdict.decision === "passed" ? "核对通过" : "退回返工"} · `
        + `摘要 ${verdict.evidence_hash} · ${when(verdict.created_at)}`
        + ` · ${verdict.reason || "没有留下理由"}`);
  }
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

function ackButton(form, entry, note) {
  // 读过不等于裁定：标记之后这条待办仍然挂着，直到写下决定。
  const seen = add(form, "button", "soft", "标记已读");
  seen.type = "button";
  seen.addEventListener("click", async () => {
    seen.disabled = true;
    try {
      await post("/inbox/ack", {item_key: entry.key});
      await report(note);
    } catch (error) {
      seen.disabled = false;
      if (error.name !== "AbortError") {
        say(`标记已读失败：${error.message}`, "error");
      }
    }
  });
}

function renderPauseState(block, requirement, pauses) {
  // 一个 "paused" 说明不了为什么没人干活：把按下暂停的人写下的原因原样摊开，等待的才不用猜。
  if (requirement.status === "paused") {
    const latest = pauses.filter(entry => entry.action === "paused").pop();
    const names = {};
    for (const member of roster) {
      names[member.user_id] = member.display_name;
    }
    add(block, "p", "paused", `已暂停：${latest ? latest.reason : "没有留下原因"}`);
    add(block, "dd", "locator", "暂停只停未开始的尝试，已在途的执行不受影响；恢复后界面会重读事实。"
        + (latest ? ` 由 ${names[latest.actor_user_id] || latest.actor_user_id} 于 `
                  + when(latest.created_at) + " 按下。" : ""));
  }
  if (pauses.length) {
    add(block, "dt", null, "暂停与恢复记录");
    for (const entry of pauses) {
      add(block, "dd", null, `${entry.action === "paused" ? "暂停" : "恢复"} · `
                          + `${when(entry.created_at)} · ${entry.reason || "无说明"}`);
    }
  }
}

function renderActivity(block, entries) {
  // 待办说的是「你还欠一个决定」，动态说的是「你不在的时候这里发生了什么」：两件事，两栏。
  add(block, "dt", null, "需求动态（读自审计流水，最新在前）");
  if (!entries.length) {
    add(block, "dd", null, "还没有记到任何动态。");
    return;
  }
  const names = {};
  for (const member of roster) {
    names[member.user_id] = member.display_name;
  }
  for (const entry of entries) {
    const who = entry.actor_id ? names[entry.actor_id] || entry.actor_id : "系统";
    add(block, "dd", null, `${when(entry.created_at)} · ${who} · ${entry.label}：`
                          + `${NEWS[entry.action] || entry.action}`);
  }
  if (entries.length === ACTIVITY_LIMIT) {
    add(block, "dd", "locator", `只列最近 ${ACTIVITY_LIMIT} 条，更早的动态没有消失，界面不自动翻页。`);
  }
}

function jumpButton(form, entry) {
  const button = add(form, "button", "soft", "跳到这条需求");
  button.type = "button";
  button.addEventListener("click", async () => {
    if (![...fields.project.options].some(option => option.value === entry.project_id)) {
      // 宁可说一句「这个项目不在你的列表里」，也不静默把选择器清空，让人以为面板坏了。
      say("这条动态所在的项目已不在你的项目列表里。", "error");
      return;
    }
    button.disabled = true;
    try {
      pendingRequirement = entry.requirement_id;
      fields.project.value = entry.project_id;
      await loadRequirements();
    } catch (error) {
      button.disabled = false;
      if (error.name !== "AbortError") {
        say(`跳转失败：${error.message}`, "error");
      }
    }
  });
}

function renderFeed(entries) {
  // 动态是不请自来的那一半：不代人决定，只把已经发生的事送到眼前，并给一步跳过去的出口。
  fields.feedList.replaceChildren();
  text(fields.feedCount, "");
  if (!entries.length) {
    add(fields.feedList, "li", null, "还没有发给你的动态。");
    return;
  }
  for (const entry of entries) {
    const row = add(fields.feedList, "li");
    const head = add(row, "div", "kind", `${entry.label}：${NEWS[entry.action] || entry.action}`);
    if (entry.unread) {
      add(head, "span", "unread", "未读");
    }
    add(row, "div", "locator", `${when(entry.created_at)} · `
        + `${entry.actor_name || entry.actor_id || "系统"} · `
        + `${projectNames[entry.project_id] ?? entry.project_id} · ${entry.requirement_title}`);
    const form = add(row, "div", "form-row");
    jumpButton(form, entry);
    if (entry.unread) {
      ackButton(form, entry, "已记下你看过了这条动态；它不欠你一个决定。");
    }
  }
  const unread = entries.filter(entry => entry.unread).length;
  text(fields.feedCount, unread ? `（未读 ${unread}）` : "");
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
  if (["executing", "rework", "paused"].includes(requirement.status)) {
    renderPauseState(block, requirement, await get(`${path}/pauses`));
  }
  if (["executing", "rework", "integrating", "awaiting_acceptance"].includes(requirement.status)) {
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
    // 暂停、恢复、取消都只由提出人或项目管理者按下：界面不替别人停掉正在派的活。
    if (PAUSABLE.includes(requirement.status)) {
      const form = add(block, "div", "form-row");
      const why = add(form, "input");
      why.placeholder = "暂停原因（必填：停下来是在等什么）";
      why.size = 34;
      draftButton(form, "暂停需求", null, async () => {
        if (!why.value.trim()) {
          say("暂停需求必须写下原因：等待的人要分清这是有意停手，不是没人干活。", "error");
          return;
        }
        await post(`${path}/actions`, {action: "pause", reason: why.value,
                                       expected_version: requirement.version});
        await report("需求已暂停；未开始的工作项不再派发，已在途的执行不受影响。");
      });
    }
    if (requirement.status === "paused") {
      const form = add(block, "div", "form-row");
      draftButton(form, "恢复需求", null, async () => {
        const back = await post(`${path}/actions`, {action: "resume",
                                                    expected_version: requirement.version});
        await report(`需求已恢复为 ${back.status}；未开始的工作项重新可派发。`);
      });
    }
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
  if (await renderWorkItems(block, projectId, requirementId, requirement.status === "paused")) {
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
    // 签字的人要看得见"谁核对了、签在哪一份交付摘要上"，而不是只知道状态变了。
    try {
      add(block, "dt", null, "覆盖核对（验收项 → 已验收工作项）");
      showCoverage(block, await get(`${path}/integration`));
    } catch (error) {
      if (error.name !== "AbortError") {
        add(block, "p", "locator", `覆盖核对暂不可用：${error.message}`);
      }
    }
  }
  renderActivity(block, await get(`${path}/activity?limit=${ACTIVITY_LIMIT}`));
}

async function renderWorkItems(block, projectId, requirementId, paused) {
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
        if (paused) {
          // 服务端也会拒这一枪，但它只回通用的 Conflict：谁按下的暂停写在横幅里，这里把那句话递到手边。
          say("需求已暂停：先点「恢复需求」才能派发新的尝试。", "error");
          return;
        }
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
    option.textContent = requirement.status === "paused"
      ? `${requirement.title}（paused · 已暂停，不再派发）`
      : `${requirement.title}（${requirement.status}）`;
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
  resetTodos();
  fields.feedList.replaceChildren();
  text(fields.feedCount, "");
  fields.detail.replaceChildren();
  text(fields.who, "");
  say("");
});

fields.project.addEventListener("change", loadRequirements);
fields.requirement.addEventListener("change", refresh);
document.getElementById("refresh").addEventListener("click", load);
resume();
