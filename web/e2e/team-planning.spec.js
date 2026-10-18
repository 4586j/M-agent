import {test, expect} from '@playwright/test';
import {readFileSync} from 'node:fs';

const source = readFileSync(new URL('../../workspace/agent/orchestrator/runtime/collaboration/web/app.js', import.meta.url), 'utf8');
const between = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
// Exercise the shipped rendering/event handlers in Chromium; transport is separately
// covered by the authenticated HTTP tests, without needing a paid model or a server.
const renderer = [between('const PROPOSAL_STATE', '//'),
  between('function named(', 'const ACTIVITY_LIMIT'),
  between('function add(', 'function say('),
  between('function draftButton(', 'function ackButton('),
  between('async function renderPlanning(', 'async function renderWorkItems(')].join('\n');

async function render(page, mode) {
  await page.setContent('<main id="planning"></main>');
  await page.evaluate(async ({renderer, mode}) => {
    window.calls = [];
    window.errors = [];
    const me = mode === 'clarification' ? 'requester' : 'manager';
    const role = mode === 'clarification' ? 'member' : 'manager';
    const roster = [{user_id: 'developer', display_name: '开发成员'}];
    const candidate = {analysis: '完成范围内实现', items: [{key: 'implement', title: '实现需求',
      duty: '开发', objective: '交付代码', scope: '冻结范围', non_goals: '不自动验收',
      assignee_user_id: 'developer', acceptance_ids: ['AC-001'], estimated_units: 50000, depends_on: []}]};
    const proposal = {proposal_id: 'proposal-1', revision_id: 'revision-1', version: 2,
      status: mode === 'invalid' ? 'blocked' : mode === 'clarification' ? 'clarification' : 'pending',
      cost_units: 2000, report: {problems: mode === 'invalid' ? ['invalid_item'] : []},
      acceptance_items: [{item_id: 'AC-001', text: '交付可核对源码'}],
      candidate: mode === 'invalid' ? {items: [null], clarification_questions: 7} :
        mode === 'clarification' ? {clarification_questions: ['接口边界是什么？']} : candidate};
    const requirement = {status: 'approved', requester_id: 'requester', version: 4,
      approved_revision_id: mode === 'stale' ? 'revision-2' : 'revision-1', current_revision_id: 'revision-1'};
    const get = async path => path === '/capabilities' ? {real_planner: true} :
      path.includes('/revisions/') ? {content: '原冻结需求'} : [proposal];
    const post = async (path, body) => {window.calls.push({path, body}); return {};};
    const say = message => window.errors.push(message);
    const report = async () => {};
    const run = new Function('document', 'me', 'role', 'roster', 'get', 'post', 'say', 'report',
      'const MANAGERS = ["owner", "manager"];\n' + renderer + '\nreturn renderPlanning;');
    await run(document, me, role, roster, get, post, say, report)(document.getElementById('planning'),
      'project-1', 'requirement-1', requirement);
  }, {renderer, mode});
}

test('候选展示职责和验收；只有显式确认才发布', async ({page}) => {
  await render(page, 'pending');
  await expect(page.getByText('等待管理者确认', {exact: false})).toBeVisible();
  await expect(page.getByText('验收 AC-001：交付可核对源码')).toBeVisible();
  expect(await page.evaluate(() => window.calls)).toEqual([]);
  await page.getByRole('button', {name: '确认职责与预算并发布', exact: true}).click();
  await expect.poll(() => page.evaluate(() => window.calls.length)).toBe(1);
  expect((await page.evaluate(() => window.calls))[0].body).toMatchObject({decision: 'approve', expected_version: 2});
});

test('无效模型条目显示阻塞原因，不导致页面异常或出现确认按钮', async ({page}) => {
  await render(page, 'invalid');
  await expect(page.getByText('invalid_item', {exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: '确认职责与预算并发布', exact: true})).toHaveCount(0);
  expect(await page.evaluate(() => window.calls)).toEqual([]);
});

test('补充澄清保留验收标准，另存待审核版本', async ({page}) => {
  await render(page, 'clarification');
  await page.getByText('根据澄清补充需求，保存为待审核的新版本').click();
  await page.locator('textarea').first().fill('补充后的完整需求');
  await page.getByRole('button', {name: '另存新版本并重新审核', exact: true}).click();
  await expect.poll(() => page.evaluate(() => window.calls.length)).toBe(1);
  const call = (await page.evaluate(() => window.calls))[0];
  expect(call.path).toContain('/revisions');
  expect(call.body).toEqual({content: '补充后的完整需求', acceptance_items: ['交付可核对源码'], expected_version: 4});
  expect(await page.evaluate(() => window.errors)).toEqual([]);
});

test('旧需求版本候选不能从界面确认发布', async ({page}) => {
  await render(page, 'stale');
  await expect(page.getByRole('button', {name: '确认职责与预算并发布', exact: true})).toHaveCount(0);
});
