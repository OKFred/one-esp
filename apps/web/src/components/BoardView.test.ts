import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';
import BoardView from './BoardView.vue';

describe('board inspection', () => {
  it('supports keyboard pin selection without treating power rails as controllable GPIO', async () => {
    const wrapper = mount(BoardView, { props: { pins: [], selected: null } });
    const pin = wrapper.find('[aria-label="GPIO5 · 状态未知"]');
    expect(pin.attributes('role')).toBe('button');
    await pin.trigger('keydown', { key: 'Enter' });
    expect(wrapper.emitted('select')).toEqual([[5]]);
    expect(
      wrapper.find('[aria-label="3V3 · 电源标识，非实时测量"]').attributes('role'),
    ).toBeUndefined();
    expect(wrapper.text()).toContain('当前断开');
    wrapper.unmount();
  });
});
