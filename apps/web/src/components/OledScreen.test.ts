import { mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';
import OledScreen from './OledScreen.vue';

afterEach(() => vi.restoreAllMocks());

describe('OLED display', () => {
  it('shows a clearly unavailable state before the first device frame', () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
    const wrapper = mount(OledScreen);
    expect(wrapper.text()).toContain('NO SIGNAL');
    expect(wrapper.find('canvas').attributes('aria-label')).toBe('等待设备画面');
    wrapper.unmount();
  });

  it('renders received pixels at native resolution and marks stale data', () => {
    let output: ImageData | undefined;
    const context = {
      createImageData: (width: number, height: number) => ({
        width,
        height,
        data: new Uint8ClampedArray(width * height * 4),
      }),
      putImageData: (data: ImageData) => {
        output = data;
      },
      imageSmoothingEnabled: true,
    };
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
      context as unknown as CanvasRenderingContext2D,
    );
    const buffer = new Uint8Array(1024);
    buffer[0] = 1;
    const wrapper = mount(OledScreen, {
      props: {
        frame: {
          width: 128,
          height: 64,
          format: 'MONO_VLSB',
          buffer: btoa(String.fromCharCode(...buffer)),
          available: true,
          text: 'Check password',
        },
        stale: true,
      },
    });
    expect(context.imageSmoothingEnabled).toBe(false);
    expect(wrapper.find('canvas').attributes('width')).toBe('128');
    expect(wrapper.find('canvas').attributes('height')).toBe('64');
    expect(output?.data.slice(0, 4)).toEqual(new Uint8ClampedArray([235, 224, 73, 255]));
    expect(output?.data.slice(4, 8)).toEqual(new Uint8ClampedArray([3, 8, 10, 255]));
    expect(wrapper.text()).toContain('当前数据已过期');
    expect(wrapper.find('canvas').attributes('aria-label')).toBe('Check password');
    wrapper.unmount();
  });
});
