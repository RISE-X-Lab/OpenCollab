const { closeDb } = require('./init_db');
const { createRuntimeDatabaseHarness } = require('./test_harness');

async function prepareE2EDatabase(options = {}) {
  const harness = createRuntimeDatabaseHarness({
    label: options.label || process.env.ARC_E2E_DB_LABEL || 'playwright-e2e',
    seedDefault: options.seedDefault !== false,
  });

  const runtime = await harness.setup();
  await closeDb();

  return {
    dbPath: runtime.dbPath,
    seeded: options.seedDefault !== false,
  };
}

if (require.main === module) {
  prepareE2EDatabase()
    .then((result) => {
      console.log(`E2E database prepared at: ${result.dbPath}`);
      console.log(`Seed default: ${result.seeded ? 'yes' : 'no'}`);
    })
    .catch((error) => {
      console.error('E2E database preparation failed:', error);
      process.exitCode = 1;
    });
}

module.exports = prepareE2EDatabase;
module.exports.prepareE2EDatabase = prepareE2EDatabase;
