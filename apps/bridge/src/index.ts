import { startBridgeServer } from './server.js';

const configuredPort = Number(process.env.PORT ?? 8787);
if (!Number.isInteger(configuredPort) || configuredPort < 1 || configuredPort > 65535) {
  throw new Error('PORT must be an integer between 1 and 65535');
}
const bridge = await startBridgeServer({ port: configuredPort });
console.log(`one-esp bridge: http://127.0.0.1:${bridge.port} (no serial port opened)`);
let closing = false;
const close = async () => {
  if (closing) return;
  closing = true;
  try {
    await bridge.close();
    process.exitCode = 0;
  } catch {
    process.exitCode = 1;
  }
};
process.on('SIGINT', () => {
  void close();
});
process.on('SIGTERM', () => {
  void close();
});
