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

    const debugSwitch = page.getByRole('switch', { name: '开启调试采集', exact: true });
    await expect(debugSwitch).toBeChecked();
    await debugSwitch.click();
    await expect(debugSwitch).not.toBeChecked();
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
