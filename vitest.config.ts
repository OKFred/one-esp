import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    projects: [
      {
        extends: true,
        test: {
          name: 'core',
          include: ['packages/**/*.test.ts', 'apps/bridge/**/*.test.ts'],
          environment: 'node',
        },
      },
      'apps/web/vitest.config.ts',
    ],
  },
});
