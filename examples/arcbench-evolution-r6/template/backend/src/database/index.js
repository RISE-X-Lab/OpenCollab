const initDb = require('./init_db');
const runtime = require('./db_runtime');
const seedDatabase = require('./seed_db');
const { createTestDatabaseHarness, createRuntimeDatabaseHarness, DEFAULT_TEST_DB_ROOT } = require('./test_harness');

module.exports = {
  ...initDb,
  ...runtime,
  seedDatabase,
  createTestDatabaseHarness,
  createRuntimeDatabaseHarness,
  DEFAULT_TEST_DB_ROOT,
};
