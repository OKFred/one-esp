import { serve } from '@hono/node-server';
import { WebSocket, WebSocketServer } from 'ws';
import { createBridgeApp } from './app.js';
import { createBridgeController, type BridgeController } from './controller.js';

export interface BridgeServerOptions {
  port?: number;
  controller?: BridgeController;
}

export async function startBridgeServer(options: BridgeServerOptions = {}) {
  const controller = options.controller ?? createBridgeController();
  const app = createBridgeApp(controller);
  const websocketServer = new WebSocketServer({ noServer: true });
  const lastPong = new WeakMap<WebSocket, number>();
  websocketServer.on('connection', (client) => {
    lastPong.set(client, Date.now());
    client.on('pong', () => lastPong.set(client, Date.now()));
  });
  const server = serve({
    fetch: app.fetch,
    hostname: '127.0.0.1',
    port: options.port ?? 8787,
    websocket: { server: websocketServer },
  });
  if (!server.listening) {
    await new Promise<void>((resolve, reject) => {
      server.once('listening', resolve);
      server.once('error', reject);
    });
  }
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Invalid bridge listen address');
  // Independent of device telemetry: even a paused/quiet device must allow a
  // viewer to detect a half-open channel instead of displaying cached data.
  const heartbeat = setInterval(() => {
    const now = Date.now();
    for (const client of websocketServer.clients) {
      if (client.readyState !== WebSocket.OPEN) continue;
      if (now - (lastPong.get(client) ?? now) >= 15000 || client.bufferedAmount > 256 * 1024) {
        client.terminate();
        continue;
      }
      client.send(JSON.stringify({ type: 'heartbeat', data: { ts: now } }));
      client.ping();
    }
  }, 5000);
  heartbeat.unref();
  return {
    app,
    controller,
    server,
    port: address.port,
    async close() {
      clearInterval(heartbeat);
      for (const client of websocketServer.clients) client.terminate();
      let controllerError: unknown;
      try {
        await controller.close();
      } catch (error) {
        controllerError = error;
      }
      await new Promise<void>((resolve) => websocketServer.close(() => resolve()));
      await new Promise<void>((resolve, reject) =>
        server.close((error) => (error ? reject(error) : resolve())),
      );
      if (controllerError) throw controllerError;
    },
  };
}
