import { test, expect } from '@playwright/test';

for (const path of ['/budgets', '/providers', '/sessions', '/knowledge']) {
  test(`${path} 根路径和子路径返回后端 JSON`, async ({ request }) => {
    for (const url of [path, `${path}/probe?limit=2`]) {
      const response = await request.get(url, { headers: { 'X-API-Key': 'test-only-key' } });
      await expect(response).toBeOK();
      expect(response.headers()['content-type']).toContain('application/json');
      expect(await response.json()).toEqual({
        source: 'proxy-brain', method: 'GET', url, body: null, apiKey: 'test-only-key',
      });
    }
  });
}

test('会话创建、预算更新及知识删除保留方法与 JSON 请求体', async ({ request }) => {
  for (const [method, url] of [['POST', '/sessions'], ['PUT', '/budgets/probe'], ['DELETE', '/knowledge/probe']]) {
    const data = { title: '代理验证', limit: 2 };
    const response = await request.fetch(url, { method, data });
    await expect(response).toBeOK();
    expect(await response.json()).toMatchObject({ source: 'proxy-brain', method, url, body: data });
  }
});

test('后端错误保留 404，不回退到前端页面', async ({ request }) => {
  const response = await request.get('/sessions/missing');
  expect(response.status()).toBe(404);
  expect(await response.json()).toMatchObject({ source: 'proxy-brain' });
});

test('开发首页仍正常返回 HTML', async ({ request }) => {
  const response = await request.get('/');
  await expect(response).toBeOK();
  expect(response.headers()['content-type']).toContain('text/html');
  expect(await response.text()).toContain('/src/main.tsx');
});
