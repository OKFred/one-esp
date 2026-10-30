import { serveStatic } from '@hono/node-server/serve-static';
import { upgradeWebSocket } from '@hono/node-server';
import { Hono } from 'hono';
import { bodyLimit } from 'hono/body-limit';
import { fileURLToPath } from 'node:url';
import type { BridgeMessage } from '@one-esp/protocol';
import { BridgeController, BridgeError } from './controller.js';
import type { ReplayScenario } from './replay.js';

function isLoopback(hostname: string): boolean {
  return ['localhost', '127.0.0.1', '[::1]', '::1'].includes(hostname.toLowerCase());
}

export function createBridgeApp(controller: BridgeController): Hono {
  const app = new Hono();

  app.use('*', async (context, next) => {
    const requestUrl = new URL(context.req.url);
    if (!isLoopback(requestUrl.hostname)) {
      return context.json({ error: '此服务仅允许本机访问。' }, 403);
    }
    const origin = context.req.header('origin');
    if (origin) {
      try {
        const originUrl = new URL(origin);
        if (!['http:', 'https:'].includes(originUrl.protocol) || !isLoopback(originUrl.hostname)) {
          return context.json({ error: '不允许此网页来源访问调试服务。' }, 403);
        }
      } catch {
        return context.json({ error: '网页来源无效。' }, 403);
      }
    }
    context.header('X-Content-Type-Options', 'nosniff');
    context.header('Cache-Control', 'no-store');
    await next();
  });
  app.use(
    '/api/*',
    bodyLimit({ maxSize: 8192, onError: (c) => c.json({ error: '请求过大。' }, 413) }),
  );

  app.get('/api/ports', async (c) => c.json({ ports: await controller.listPorts() }));
  app.get('/api/state', (c) => c.json(controller.getState()));
  app.get('/api/health', (c) => c.json({ ok: true, service: 'one-esp-bridge' }));
  app.post('/api/connect', async (c) => {
    const body = await c.req.json<{ port?: unknown }>();
    if (typeof body?.port !== 'string') throw new BridgeError('请选择串口。');
    await controller.connect(body.port);
    return c.json(controller.getState());
  });
  app.post('/api/disconnect', async (c) => {
    await controller.disconnect();
    return c.json(controller.getState());
  });
  app.post('/api/replay', async (c) => {
    const body = await c.req.json<{ scenario?: unknown }>();
    if (typeof body?.scenario !== 'string') throw new BridgeError('请选择演示场景。');
    await controller.startReplay(body.scenario as ReplayScenario);
    return c.json(controller.getState());
  });
  app.post('/api/command', async (c) => c.json(await controller.command(await c.req.json())));

  app.get(
    '/ws',
    upgradeWebSocket(() => {
      let unsubscribe: (() => void) | undefined;
      const send = (
        ws: { send: (data: string) => void; raw?: unknown },
        message: BridgeMessage,
      ) => {
        const raw = ws.raw as
          | { bufferedAmount?: number; close?: (code: number, reason: string) => void }
          | undefined;
        if ((raw?.bufferedAmount ?? 0) > 256 * 1024) {
          raw?.close?.(1013, 'Viewer is too slow; reconnect for a fresh snapshot');
          return;
        }
        ws.send(JSON.stringify(message));
      };
      return {
        onOpen(_event, ws) {
          send(ws, { type: 'snapshot', data: controller.getState() });
          unsubscribe = controller.subscribe((message) => send(ws, message));
        },
        onClose() {
          unsubscribe?.();
        },
        onError() {
          unsubscribe?.();
        },
      };
    }),
  );

  const webRoot = fileURLToPath(new URL('../../web/dist/', import.meta.url));
  app.use('*', serveStatic({ root: webRoot }));
  app.notFound((c) => c.json({ error: '未找到资源；开发模式请打开 Vite 页面。' }, 404));
  app.onError((error, c) => {
    if (error instanceof BridgeError) return c.json({ error: error.message }, error.status);
    if (error instanceof SyntaxError) return c.json({ error: '请求不是有效的 JSON。' }, 400);
    // Never echo a request body or device command: it can contain Wi-Fi credentials.
    return c.json({ error: '调试服务遇到异常，请检查连接后重试。' }, 500);
  });
  return app;
}
