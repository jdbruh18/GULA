const { Pool } = require('pg');

const databaseUrl = process.env.DATABASE_URL || 'postgresql://postgres:postgrespassword@localhost:5432/gula_auth';

const pool = new Pool({
  connectionString: databaseUrl,
});

async function initDB() {
  const client = await pool.connect();
  try {
    await client.query(`
      CREATE TABLE IF NOT EXISTS users (
        id UUID PRIMARY KEY,
        username VARCHAR(255) UNIQUE NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        role VARCHAR(50) NOT NULL,
        tenant_id VARCHAR(100) NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      );
    `);
    console.log('gula-auth: Database table "users" verified/created successfully.');
  } catch (err) {
    console.error('gula-auth: Database initialization error:', err);
    throw err;
  } finally {
    client.release();
  }
}

module.exports = {
  pool,
  initDB
};
