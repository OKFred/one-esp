/** Temporary workaround for bindings-cpp#243: native callbacks omit a Node
 * callback scope, so Node >=26.4 can leave Promise/nextTick work asleep. */
export function createSerialCallbackWakeup(
  runtime = { platform: process.platform as string, nodeVersion: process.versions.node },
): { start(): void; stop(): void } {
  const version = /^v?(\d+)\.(\d+)/.exec(runtime.nodeVersion);
  const affected =
    runtime.platform === 'win32' &&
    version !== null &&
    (Number(version[1]) > 26 || (Number(version[1]) === 26 && Number(version[2]) >= 4));
  let timer: ReturnType<typeof setInterval> | undefined;
  return {
    start() {
      if (!affected || timer !== undefined) return;
      timer = setInterval(() => {}, 20);
      timer.unref();
    },
    stop() {
      if (timer !== undefined) clearInterval(timer);
      timer = undefined;
    },
  };
}
