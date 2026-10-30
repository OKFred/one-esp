// Optional real-device acceptance check. The local bridge must be running.
// --wifi-rollback stages a deliberately unavailable SSID, then verifies recovery.
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { decodeOledPixels } from '../packages/protocol/dist/index.js';

const origin = 'http://127.0.0.1:8787';
const port = process.argv.find((arg) => arg.startsWith('--port='))?.slice(7) ?? 'COM3';
const rollback = process.argv.includes('--wifi-rollback');
const minuteProbe = process.argv.includes('--minute-probe');
const clockSync = process.argv.includes('--clock-sync');
const metricsCheck = process.argv.includes('--metrics');
const pending = new Map();
const samples = [];
const frames = [];
const probeSamples = new Map();
const clockSamples = new Map();
const metricsSamples = new Map();
const protocolErrors = [];
let recoverableFrameWarnings = 0;
let bootId;
const socket = new WebSocket(origin.replace('http:', 'ws:') + '/ws');
socket.addEventListener('message', ({ data }) => {
  const packet = JSON.parse(String(data));
  if (packet.type !== 'event') return;
  const event = packet.data;
  if (event.type === 'hello') bootId = event.bootId;
  if (event.type === 'status') {
    samples.push(event.data.memFree);
    const probe = event.data.maintenance;
    if (
      probe?.tcpSamples > 0 &&
      !probe.tcpInFlight &&
      probe.tcpLastTestMs >= probe.tcpLastStartedMs &&
      !probeSamples.has(probe.tcpSamples)
    )
      probeSamples.set(probe.tcpSamples, probe);
    const quality = event.data.clock;
    if (quality?.samples > 0 && !quality.busy && !clockSamples.has(quality.samples))
      clockSamples.set(quality.samples, quality);
  }
  if (event.type === 'oled') frames.push(event.data);
  if (event.type === 'metrics')
    metricsSamples.set(`${event.data.intervalMs}:${event.data.sampledAtMs}`, {
      ...event.data,
      encodedBytes: Buffer.byteLength('@one-esp ' + JSON.stringify(event) + '\n'),
    });
  if (event.type === 'log') {
    if (/格式错误|超过 8 KiB/.test(event.data.message)) recoverableFrameWarnings++;
    if (/Traceback|(?:Memory|Attribute|Type|Syntax)Error/.test(event.data.message))
      protocolErrors.push(event.data.message);
  }
  if (event.type !== 'ack') return;
  const ack = event.data;
  const entry = pending.get(ack.id);
  if (!entry || ack.result?.stage === 'testing') return;
  pending.delete(ack.id);
  clearTimeout(entry.timer);
  entry.resolve(ack);
});

async function request(path, body) {
  const response = await fetch(origin + '/api' + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(10_000),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error ?? `HTTP ${response.status}`);
  return data;
}

async function waitFor(predicate, label, timeout = 20_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const state = await request('/state');
    if (predicate(state)) return state;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error('Timed out: ' + label);
}

async function command(name, args = {}, timeout = 20_000) {
  const id = randomUUID();
  const acknowledgement = new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error('ACK timed out: ' + name));
    }, timeout);
    pending.set(id, { timer, resolve });
  });
  try {
    await request('/command', { v: 1, id, bootId, command: name, args });
    return await acknowledgement;
  } finally {
    const entry = pending.get(id);
    if (entry) clearTimeout(entry.timer);
    pending.delete(id);
  }
}

try {
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });
  await request('/connect', { port });
  let state = await waitFor(
    (value) => value.hello && value.oled?.data.available && value.pins?.data.pins.length === 34,
    'complete initial snapshot',
  );
  bootId = state.hello.bootId;
  assert.equal(state.connection.mode, 'usb');
  assert.equal(state.status.data.sensorEnabled, false);
  if (metricsCheck) assert(state.hello.data.capabilities.includes('metrics'));
  if (state.debug.data.enabled) {
    await command('debug.set', { enabled: false });
    await waitFor((value) => value.debug?.data.enabled === false, 'debug session reset');
  }
  assert(state.pins.data.pins.every((pin) => !pin.writable));
  assert(decodeOledPixels(state.oled.data).some((pixel) => pixel === 1));
  let config = await command('config.get');
  assert.equal(config.ok, true);
  assert(!('PASSWORD' in config.result.values));
  assert.equal(typeof config.result.secrets.PASSWORD, 'boolean');
  console.log(
    'PASS initial snapshot: real OLED, 34 GPIO records, sensor disabled, no outputs, no password',
  );

  assert.equal(
    (await command('debug.set', { enabled: true, profile: 'full', ttlSeconds: 4 })).ok,
    true,
  );
  await waitFor(
    (value) => value.debug?.data.enabled === false && value.debug.ts > state.debug.ts,
    'TTL expiry',
  );
  console.log('PASS short debug lease expires automatically');
  assert.equal(
    (await command('debug.set', { enabled: true, profile: 'full', ttlSeconds: 180 })).ok,
    true,
  );
  await waitFor((value) => value.debug?.data.enabled === true, 'active debug telemetry');

  // Exercise the complete web form, including the environment's string numbers.
  const current = config.result.values;
  const saved = await command('config.set', {
    revision: config.result.revision,
    values: {
      SSID: current.SSID,
      NTP_SERVER: current.NTP_SERVER,
      TIMEZONE_HOURS: Number(current.TIMEZONE_HOURS),
      NETWORK_TEST_HOST: current.NETWORK_TEST_HOST,
      NETWORK_TEST_PORT: Number(current.NETWORK_TEST_PORT),
      POLL_INTERVAL_MS: Number(current.POLL_INTERVAL_MS),
    },
  });
  assert.equal(saved.ok, true);
  assert.equal(saved.result.stage, 'applied');
  assert.equal(saved.result.revision, config.result.revision + 1);
  config = await command('config.get');
  assert.equal(config.result.values.SSID, current.SSID);
  console.log('PASS complete configuration form saved; password retained');

  if (rollback) {
    console.log('Testing Wi-Fi rollback (about 60 seconds); original settings stay saved');
    const ack = await command(
      'config.set',
      {
        revision: config.result.revision,
        values: { SSID: 'one-esp-unavailable-' + randomUUID().slice(0, 8) },
      },
      90_000,
    );
    assert.equal(ack.ok, false);
    assert.equal(ack.result.stage, 'rolled-back');
    state = await waitFor((value) => value.status?.data.wifi.connected === true, 'Wi-Fi recovery');
    const restored = await command('config.get');
    assert.equal(restored.result.revision, config.result.revision);
    assert.equal(restored.result.values.SSID, config.result.values.SSID);
    console.log('PASS failed Wi-Fi trial rolled back; original revision and connection restored');
  } else {
    if (clockSync) {
      console.log(
        'Waiting for a second independent NTP response and pre-correction precision measurement',
      );
      await waitFor(
        (value) =>
          value.status?.data.clock?.samples >= 2 &&
          (!minuteProbe || value.status?.data.maintenance?.tcpSamples >= 2),
        'second clock synchronization',
        120_000,
      );
    } else {
      if (minuteProbe) console.log('Observing clock motion and two minute-spaced TCP samples');
      if (metricsCheck) console.log('Observing low-frequency resource samples over one minute');
      await new Promise((resolve) =>
        setTimeout(resolve, minuteProbe || metricsCheck ? 65_000 : 15_000),
      );
    }
  }

  assert(frames.length >= 2);
  for (const frame of frames) assert.equal(decodeOledPixels(frame).length, 8192);
  const clockSeconds = new Set(frames.map((frame) => frame.text.split('\n')[2]).filter(Boolean));
  assert(clockSeconds.size >= 5, 'RTC and mirrored framebuffer must advance every second');
  const probeTimes = [...probeSamples.values()].map((probe) => probe.tcpLastStartedMs);
  if (minuteProbe) {
    assert(probeTimes.length >= 2, 'TCP must continue sampling while debug is enabled');
    assert(
      [...probeSamples.values()].some((probe) => probe.tcpOk),
      'TCP endpoint must be reachable',
    );
    for (let index = 1; index < probeTimes.length; index++) {
      const interval = probeTimes[index] - probeTimes[index - 1];
      assert(interval >= 60_000 && interval < 62_500, 'TCP start interval must be one minute');
    }
  }
  if (clockSync) {
    const second = [...clockSamples.values()].at(-1);
    assert(second?.samples >= 2, 'need two independently received clock samples');
    assert(
      Math.abs(second.lastOffsetUs) / 1000 + second.uncertaintyMs <= 500,
      'pre-correction clock error plus network uncertainty must fit the 500ms target',
    );
    assert(second.withinTarget, 'reported precision must meet the target');
  }
  assert(samples.length >= 4);
  if (metricsCheck) {
    const full = [...metricsSamples.values()].filter((sample) => sample.intervalMs === 5000);
    assert(full.length >= 10, 'FULL should provide repeated resource samples');
    for (let index = 1; index < full.length; index++)
      assert(full[index].sampledAtMs - full[index - 1].sampledAtMs >= 5000);
    assert(new Set(full.map((sample) => sample.storage?.sampledAtMs)).size >= 2);
    assert(new Set(full.map((sample) => sample.temperature?.sampledAtMs)).size >= 2);
    for (const sample of full) {
      assert(sample.cpuHz > 0);
      assert(sample.memory.freeBytes > 0 && sample.memory.allocatedBytes > 0);
      assert(sample.idfHeap.freeBytes > 0);
      assert(sample.idfHeap.largestBlockBytes <= sample.idfHeap.freeBytes);
      assert(sample.storage.freeBytes <= sample.storage.totalBytes);
      assert(sample.flashBytes >= sample.storage.totalBytes);
      assert(sample.temperature.celsius >= -40 && sample.temperature.celsius <= 150);
      assert(sample.rssiDbm !== null && sample.rssiDbm < 0);
      assert(sample.sampleDurationUs !== null && sample.sampleDurationUs < 500_000);
      assert(sample.encodedBytes < 1200, 'Resource frames must remain small');
    }
    assert.equal(
      (await command('debug.set', { enabled: true, profile: 'lite', ttlSeconds: 60 })).ok,
      true,
    );
    await waitFor(
      () =>
        [...metricsSamples.values()].filter((sample) => sample.intervalMs === 15000).length >= 2,
      'two LITE resource samples',
      25_000,
    );
    const lite = [...metricsSamples.values()].filter((sample) => sample.intervalMs === 15000);
    assert(lite[1].sampledAtMs - lite[0].sampledAtMs >= 15_000);
    console.log(
      'PASS FULL/LITE resources, slow storage/temperature sampling and bounded frame size',
    );
  }
  assert.equal(protocolErrors.length, 0);
  await waitFor(
    (value) => value.pins?.data.pins.length === 34,
    'complete pins after frame recovery',
  );
  assert.equal((await command('debug.set', { enabled: false })).ok, true);
  state = await waitFor((value) => value.debug?.data.enabled === false, 'debug shutdown telemetry');
  if (metricsCheck) {
    const pausedSamples = metricsSamples.size;
    await new Promise((resolve) => setTimeout(resolve, 5500));
    assert.equal(
      metricsSamples.size,
      pausedSamples,
      'Resource sampling must stop when debug is off',
    );
  }
  assert.equal(state.hello.bootId, bootId);
  console.log(
    JSON.stringify({
      result: 'PASS',
      oledFrames: frames.length,
      statusSamples: samples.length,
      minFreeHeapBytes: Math.min(...samples),
      maxFreeHeapBytes: Math.max(...samples),
      recoverableFrameWarnings,
      distinctClockSeconds: clockSeconds.size,
      ...(metricsCheck
        ? {
            resourceSamples: metricsSamples.size,
            maximumResourceReadMs:
              Math.max(...[...metricsSamples.values()].map((sample) => sample.sampleDurationUs)) /
              1000,
            maximumResourceEncodedBytes: Math.max(
              ...[...metricsSamples.values()].map((sample) => sample.encodedBytes),
            ),
            latestResources: [...metricsSamples.values()].at(-1),
          }
        : {}),
      tcpProbeStartsMs: probeTimes,
      clockMeasurements: [...clockSamples.values()].map((quality) => ({
        samples: quality.samples,
        lastOffsetUs: quality.lastOffsetUs,
        uncertaintyMs: quality.uncertaintyMs,
        estimatedErrorMs: quality.estimatedErrorMs,
        intervalMs: quality.intervalMs,
        driftPpm: quality.driftPpm,
      })),
      sensorEnabled: state.status.data.sensorEnabled,
      debugEnabled: state.debug.data.enabled,
    }),
  );
} finally {
  if (bootId && socket.readyState === WebSocket.OPEN)
    await command('debug.set', { enabled: false }, 5000).catch(() => {});
  for (const item of pending.values()) clearTimeout(item.timer);
  socket.close();
}
