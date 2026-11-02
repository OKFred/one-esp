import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';

const screenshotDirectory = path.resolve('test-results/screenshots');

async function assertNoPageOverflow(page: Page) {
  const dimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
  expect(dimensions.document).toBeLessThanOrEqual(dimensions.viewport + 1);
  expect(dimensions.body).toBeLessThanOrEqual(dimensions.viewport + 1);
}

async function selectScenario(page: Page, label: string) {
  const scenario = page.getByRole('combobox', { name: '回放场景', exact: true });
  await scenario.focus();
  await scenario.press('ArrowDown');
  await page.locator('.ant-select-item-option-content').getByText(label, { exact: true }).click();
  await page.getByRole('button', { name: '场景回放', exact: true }).click();
  await expect(page.getByText('REPLAY / 模拟数据', { exact: true })).toBeVisible();
}

async function saveScreenshot(page: Page, width: number, scenario: string) {
  await mkdir(screenshotDirectory, { recursive: true });
  await page.screenshot({
    path: path.join(screenshotDirectory, `console-${width}-${scenario}.png`),
    fullPage: true,
    animations: 'disabled',
  });
}

for (const viewport of [
  { width: 390, height: 844 },
  { width: 768, height: 1024 },
  { width: 1440, height: 1000 },
]) {
  test(`${viewport.width}px console supports normal, Wi-Fi error and offline replay`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(`pageerror: ${error.message}`));
    page.on('console', (message) => {
      if (message.type() === 'error') errors.push(`console: ${message.text()}`);
    });

    await page.goto('/');
    await expect(page.getByText('BRIDGE ONLINE', { exact: true })).toBeVisible();
    await selectScenario(page, '正常启动');
    await expect(page.getByText('Wi-Fi 已连接', { exact: true })).toBeVisible();
    await expect(page.getByText('场景回放模式', { exact: true })).toBeVisible();
    await expect(page.getByLabel('时间校准状态', { exact: true })).toContainText('目标 500 ms');
    const resources = page.getByRole('region', { name: '设备资源指标', exact: true });
    await expect(resources).toContainText('160 MHz');
    await expect(resources).toContainText('非环境温度');
    await expect(resources).toContainText('Wi-Fi 吞吐率：固件未提供');
    const canvas = page.locator('.oled-glass canvas');
    await expect(canvas).toHaveAttribute('width', '128');
    await expect(canvas).toHaveAttribute('height', '64');
    await expect(canvas).toHaveAttribute('aria-label', /WiFi Connected!/);
    await expect
      .poll(() =>
        canvas.evaluate((element) => {
          const context = (element as HTMLCanvasElement).getContext('2d');
          const pixels = context?.getImageData(0, 0, 128, 64).data;
          if (!pixels) return 0;
          let lit = 0;
          for (let index = 0; index < pixels.length; index += 4) {
            if ((pixels[index] ?? 0) > 20) lit++;
          }
          return lit;
        }),
      )
      .toBeGreaterThan(100);
    const canvasBounds = await canvas.boundingBox();
    expect(canvasBounds?.width).toBeGreaterThan(240);
    expect(canvasBounds!.width / canvasBounds!.height).toBeCloseTo(2, 1);

    const gpioButtons = page.getByRole('button', { name: /^GPIO\d+ 设为[低高]电平$/ });
    expect(await gpioButtons.count()).toBeGreaterThan(20);
    expect(
      await gpioButtons.evaluateAll((buttons) =>
        buttons.every((button) => (button as HTMLButtonElement).disabled),
      ),
    ).toBe(true);
    const pinTable = page.locator('.pin-table-wrap');
    if (viewport.width < 1000) {
      expect(await pinTable.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(
        true,
      );
    }
    await assertNoPageOverflow(page);
    await saveScreenshot(page, viewport.width, 'normal');

    const debugSwitch = page.getByRole('switch', { name: '实时调试采集', exact: true });
    await expect(debugSwitch).toBeChecked();
    await debugSwitch.click();
    await expect(debugSwitch).not.toBeChecked();
    await expect(resources).toContainText('采集已暂停');
    await expect(page.getByRole('button', { name: '应用参数 / 续期', exact: true })).toBeDisabled();
    await debugSwitch.click();
    await expect(debugSwitch).toBeChecked();
    await page.getByRole('button', { name: '获取快照', exact: true }).click();
    await expect(page.getByRole('status')).toContainText('设备已确认操作');

    await page.getByRole('button', { name: '设备配置', exact: true }).click();
    const configDialog = page.getByRole('dialog', { name: '设备配置', exact: true });
    await expect(configDialog).toBeVisible();
    await expect(configDialog.getByLabel('Wi-Fi 名称', { exact: true })).toHaveValue(
      'DEMO NETWORK',
    );
    await expect(configDialog.getByPlaceholder('留空保留原密码')).toHaveValue('');
    const dialogBounds = await configDialog.boundingBox();
    expect(dialogBounds!.width).toBeLessThanOrEqual(viewport.width);
    expect(dialogBounds!.x).toBeGreaterThanOrEqual(0);
    expect(dialogBounds!.x + dialogBounds!.width).toBeLessThanOrEqual(viewport.width + 1);
    await assertNoPageOverflow(page);
    await saveScreenshot(page, viewport.width, 'config');
    await configDialog.getByRole('button', { name: /关\s*闭/ }).click();
    await expect(configDialog).not.toBeVisible();

    await selectScenario(page, 'Wi-Fi 密码错误');
    await expect(canvas).toHaveAttribute('aria-label', /Check password/);
    await expect(page.getByText('Wi-Fi 未连接', { exact: true })).toBeVisible();
    await expect(page.getByRole('log')).toContainText('模拟 Wi-Fi 认证失败：请检查密码。');
    await assertNoPageOverflow(page);
    await saveScreenshot(page, viewport.width, 'wifi-error');

    await selectScenario(page, '设备离线');
    await expect(canvas).toHaveAttribute('aria-label', /DEVICE OFFLINE/);
    await expect(page.getByText('最后一帧 · 当前数据已过期', { exact: true })).toBeVisible();
    await expect(debugSwitch).toBeDisabled();
    await assertNoPageOverflow(page);
    await saveScreenshot(page, viewport.width, 'offline');
    expect(errors, 'browser console and uncaught errors').toEqual([]);
  });
}

test('restores a paused LITE session and resumes from the screen action', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await selectScenario(page, '正常启动');
  const profile = page.getByRole('combobox', { name: '采集档位', exact: true });
  await profile.focus();
  await profile.press('ArrowDown');
  await page
    .locator('.ant-select-item-option-content')
    .getByText('LITE · 轻量采集', { exact: true })
    .click();
  await page.getByRole('button', { name: '应用参数 / 续期', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('设备已确认操作');
  const debugSwitch = page.getByRole('switch', { name: '实时调试采集', exact: true });
  await debugSwitch.click();
  await expect(debugSwitch).not.toBeChecked();
  await page.reload();
  await expect(page.getByRole('spinbutton', { name: '屏幕帧率', exact: true })).toHaveAttribute(
    'aria-valuenow',
    '1',
  );
  await expect(
    page.getByRole('spinbutton', { name: 'GPIO 采样频率', exact: true }),
  ).toHaveAttribute('aria-valuenow', '1');
  await expect(page.getByRole('spinbutton', { name: '屏幕帧率', exact: true })).toHaveAttribute(
    'aria-valuemax',
    '1',
  );
  await expect(
    page.getByText('采集已暂停 · 显示最后快照，可开启调试或获取快照', { exact: true }),
  ).toBeVisible();
  const outgoing = page.waitForRequest(
    (request) =>
      request.url().endsWith('/api/command') && request.postDataJSON()?.command === 'debug.set',
  );
  await page.getByRole('button', { name: '开启实时采集', exact: true }).click();
  expect((await outgoing).postDataJSON().args).toMatchObject({
    enabled: true,
    profile: 'lite',
    oledFps: 1,
    gpioHz: 1,
  });
  await expect(debugSwitch).toBeChecked();
  await expect(page.getByRole('region', { name: '设备资源指标' })).toContainText('低频采集中');
  await expect(page.getByRole('button', { name: '开启实时采集', exact: true })).toHaveCount(0);
  await assertNoPageOverflow(page);
});

test('renews an active short session by default and respects a manual pause', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await selectScenario(page, '正常启动');
  const autoRenew = page.getByRole('switch', { name: '自动续期', exact: true });
  await expect(autoRenew).toBeChecked();
  const ttlInput = page.getByRole('spinbutton', { name: '调试会话有效期', exact: true });
  await ttlInput.fill('10');
  await ttlInput.press('Tab');
  const renewal = page.waitForRequest(
    (request) =>
      request.url().endsWith('/api/command') && request.postDataJSON()?.command === 'debug.renew',
  );
  await page.getByRole('button', { name: '应用参数 / 续期', exact: true }).click();
  const request = await renewal;
  expect(request.postDataJSON().args).toEqual({ ttlSeconds: 10 });
  expect(request.postDataJSON().bootId).toBeTruthy();
  await expect(page.getByText(/上次成功续期/)).toBeVisible();
  const debugSwitch = page.getByRole('switch', { name: '实时调试采集', exact: true });
  await expect(debugSwitch).toBeChecked();
  await debugSwitch.click();
  await expect(debugSwitch).not.toBeChecked();
  await expect(page.getByText('开启采集后自动续期', { exact: true })).toBeVisible();
  await expect(autoRenew).toBeChecked();
  await assertNoPageOverflow(page);
});

test('detects OLED-only silence and recovers without restarting sampling', async ({ page }) => {
  let dropOled = false;
  await page.routeWebSocket('**/ws', (viewer) => {
    const server = viewer.connectToServer();
    server.onMessage((message) => {
      const packet = JSON.parse(String(message));
      if (dropOled && packet.type === 'event' && packet.data.type === 'oled') return;
      viewer.send(message);
    });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await selectScenario(page, '正常启动');
  const canvas = page.locator('.oled-glass canvas');
  await expect(canvas).toHaveAttribute('aria-label', /WiFi Connected!/);
  dropOled = true;
  const initial = await canvas.getAttribute('aria-label');
  const snapshotRequest = page.waitForRequest(
    (request) =>
      request.url().endsWith('/api/command') && request.postDataJSON()?.command === 'snapshot.get',
    { timeout: 25000 },
  );
  const writes: string[] = [];
  page.on('request', (request) => {
    if (request.url().endsWith('/api/command')) writes.push(request.postDataJSON().command);
  });
  await expect(page.locator('.display-panel')).toContainText('画面停更', { timeout: 25000 });
  await snapshotRequest;
  expect(await canvas.getAttribute('aria-label')).toBe(initial);
  await saveScreenshot(page, 390, 'oled-stalled');
  dropOled = false;
  await expect(canvas).not.toHaveAttribute('aria-label', initial!);
  await expect(page.locator('.display-panel')).not.toContainText('画面停更');
  expect(writes).not.toContain('debug.set');
  await assertNoPageOverflow(page);
});

test('reconnects a silently stalled websocket and receives a fresh screen', async ({ page }) => {
  let blackhole = false;
  let connections = 0;
  await page.routeWebSocket('**/ws', (viewer) => {
    const number = ++connections;
    const server = viewer.connectToServer();
    server.onMessage((message) => {
      if (blackhole && number === 1) return;
      viewer.send(message);
    });
  });
  await page.goto('/');
  await selectScenario(page, '正常启动');
  const canvas = page.locator('.oled-glass canvas');
  await expect(canvas).toHaveAttribute('aria-label', /WiFi Connected!/);
  const initial = await canvas.getAttribute('aria-label');
  blackhole = true;
  await expect.poll(() => connections, { timeout: 25000 }).toBeGreaterThan(1);
  await expect(canvas).not.toHaveAttribute('aria-label', initial!);
  await expect(page.getByRole('switch', { name: '实时调试采集', exact: true })).toBeChecked();
});
