const { defineConfig } = require('@playwright/test');

const baseURL = process.env.PLAYWRIGHT_BASE_URL || process.env.ARC_WEB_BASE_URL || 'http://127.0.0.1:3000';

module.exports = defineConfig({
  testDir: './test-e2e',
  testMatch: /.*\.(js|jsx|ts|tsx)$/,
  timeout: 30000,
  use: {
    baseURL,
    trace: 'retain-on-failure',
  },
});
