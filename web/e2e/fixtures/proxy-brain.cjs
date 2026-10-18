// 回环测试后端：仅回显请求，不加载 Brain、模型或运行时配置。
const http = require('node:http');

http.createServer(async (req, res) => {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const body = Buffer.concat(chunks).toString('utf8');
  res.writeHead(req.url.includes('/missing') ? 404 : 200, {
    'Content-Type': 'application/json; charset=utf-8',
  });
  res.end(JSON.stringify({
    source: 'proxy-brain', method: req.method, url: req.url,
    body: body ? JSON.parse(body) : null,
    apiKey: req.headers['x-api-key'] || null,
  }));
}).listen(8129, '127.0.0.1');
