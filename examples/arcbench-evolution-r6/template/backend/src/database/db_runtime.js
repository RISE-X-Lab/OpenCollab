const { getDb, initializeDatabase } = require('./init_db');

function normalizeParams(params) {
  if (Array.isArray(params)) {
    return params;
  }
  if (params === undefined) {
    return [];
  }
  return [params];
}

function runOnDatabase(database, sql, params = []) {
  return new Promise((resolve, reject) => {
    database.run(sql, normalizeParams(params), function handleRun(err) {
      if (err) {
        reject(err);
        return;
      }
      resolve({
        lastID: this.lastID,
        changes: this.changes,
      });
    });
  });
}

function getOnDatabase(database, sql, params = []) {
  return new Promise((resolve, reject) => {
    database.get(sql, normalizeParams(params), (err, row) => {
      if (err) {
        reject(err);
        return;
      }
      resolve(row || null);
    });
  });
}

function allOnDatabase(database, sql, params = []) {
  return new Promise((resolve, reject) => {
    database.all(sql, normalizeParams(params), (err, rows) => {
      if (err) {
        reject(err);
        return;
      }
      resolve(rows || []);
    });
  });
}

function execOnDatabase(database, sql) {
  return new Promise((resolve, reject) => {
    database.exec(sql, (err) => {
      if (err) {
        reject(err);
        return;
      }
      resolve();
    });
  });
}

async function run(sql, params = []) {
  const database = await initializeDatabase();
  return runOnDatabase(database, sql, params);
}

async function get(sql, params = []) {
  const database = await initializeDatabase();
  return getOnDatabase(database, sql, params);
}

async function all(sql, params = []) {
  const database = await initializeDatabase();
  return allOnDatabase(database, sql, params);
}

async function exec(sql) {
  const database = await initializeDatabase();
  return execOnDatabase(database, sql);
}

async function withTransaction(work) {
  const database = await initializeDatabase();
  await execOnDatabase(database, 'BEGIN IMMEDIATE TRANSACTION;');

  const tx = {
    run: (sql, params = []) => runOnDatabase(database, sql, params),
    get: (sql, params = []) => getOnDatabase(database, sql, params),
    all: (sql, params = []) => allOnDatabase(database, sql, params),
    exec: (sql) => execOnDatabase(database, sql),
  };

  try {
    const result = await work(tx);
    await execOnDatabase(database, 'COMMIT;');
    return result;
  } catch (error) {
    try {
      await execOnDatabase(database, 'ROLLBACK;');
    } catch (rollbackError) {
      error.rollbackError = rollbackError;
    }
    throw error;
  }
}

module.exports = {
  run,
  get,
  all,
  exec,
  withTransaction,
};
