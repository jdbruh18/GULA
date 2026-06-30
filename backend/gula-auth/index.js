const express = require('express');
const bcrypt = require('bcryptjs');
const jwt = require('jsonwebtoken');
const crypto = require('crypto');
const amqp = require('amqplib');
const { pool, initDB } = require('./database');

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3001;
const JWT_SECRET = process.env.JWT_SECRET || 'gulasecretkey123';
const RABBITMQ_URL = process.env.RABBITMQ_URL || 'amqp://localhost:5672';

let rabbitChannel = null;

// Connect to RabbitMQ with retry
async function connectRabbitMQ() {
  const maxRetries = 10;
  for (let i = 1; i <= maxRetries; i++) {
    try {
      const connection = await amqp.connect(RABBITMQ_URL);
      rabbitChannel = await connection.createChannel();
      await rabbitChannel.assertExchange('gula.events', 'topic', { durable: true });
      console.log('gula-auth: Connected to RabbitMQ successfully.');
      return;
    } catch (err) {
      console.warn(`gula-auth: RabbitMQ connection failed (attempt ${i}/${maxRetries}). Retrying in 3 seconds...`);
      await new Promise(resolve => setTimeout(resolve, 3000));
    }
  }
  throw new Error('gula-auth: Could not connect to RabbitMQ.');
}

// Publish event utility
function publishEvent(eventType, payload) {
  if (!rabbitChannel) {
    console.error('gula-auth: RabbitMQ channel not initialized. Event lost:', eventType);
    return;
  }
  const eventEnvelope = {
    eventId: crypto.randomUUID(),
    eventType,
    timestamp: new Date().toISOString(),
    source: 'gula-auth',
    payload
  };
  const routingKey = `gula.event.${eventType}`;
  rabbitChannel.publish(
    'gula.events',
    routingKey,
    Buffer.from(JSON.stringify(eventEnvelope)),
    { persistent: true }
  );
  console.log(`gula-auth: Published event "${eventType}" to routing key "${routingKey}"`);
}

// REST Routes
app.get('/health', (req, res) => {
  res.json({ service: 'gula-auth', status: 'UP' });
});

app.post('/api/auth/register', async (req, res) => {
  const { username, password, role, tenantId } = req.body;
  if (!username || !password || !role || !tenantId) {
    return res.status(400).json({ error: 'Missing required fields (username, password, role, tenantId)' });
  }

  const normalizedRole = role.toUpperCase();
  if (!['ADMIN', 'RADIOLOGIST', 'TECHNICIAN'].includes(normalizedRole)) {
    return res.status(400).json({ error: 'Invalid role. Must be ADMIN, RADIOLOGIST, or TECHNICIAN' });
  }

  try {
    const passwordHash = await bcrypt.hash(password, 10);
    const userId = crypto.randomUUID();

    await pool.query(
      'INSERT INTO users (id, username, password_hash, role, tenant_id) VALUES ($1, $2, $3, $4, $5)',
      [userId, username, passwordHash, normalizedRole, tenantId]
    );

    // Publish UserCreated Event
    publishEvent('UserCreated', {
      userId,
      username,
      role: normalizedRole,
      tenantId
    });

    res.status(201).json({
      message: 'User registered successfully',
      user: { id: userId, username, role: normalizedRole, tenantId }
    });
  } catch (err) {
    if (err.code === '23505') { // Unique violation
      return res.status(400).json({ error: 'Username already exists' });
    }
    console.error('Registration error:', err);
    res.status(500).json({ error: 'Internal Server Error' });
  }
});

app.post('/api/auth/login', async (req, res) => {
  const { username, password } = req.body;
  if (!username || !password) {
    return res.status(400).json({ error: 'Username and password required' });
  }

  try {
    const result = await pool.query('SELECT * FROM users WHERE username = $1', [username]);
    if (result.rows.length === 0) {
      return res.status(401).json({ error: 'Invalid username or password' });
    }

    const user = result.rows[0];
    const isMatch = await bcrypt.compare(password, user.password_hash);
    if (!isMatch) {
      return res.status(401).json({ error: 'Invalid username or password' });
    }

    // Sign JWT
    const token = jwt.sign(
      {
        userId: user.id,
        username: user.username,
        role: user.role,
        tenantId: user.tenant_id
      },
      JWT_SECRET,
      { expiresIn: '8h' }
    );

    res.json({
      message: 'Login successful',
      token,
      user: {
        id: user.id,
        username: user.username,
        role: user.role,
        tenantId: user.tenant_id
      }
    });
  } catch (err) {
    console.error('Login error:', err);
    res.status(500).json({ error: 'Internal Server Error' });
  }
});

// App Startup
async function start() {
  await initDB();
  await connectRabbitMQ();
  app.listen(PORT, () => {
    console.log(`gula-auth: Server listening on port ${PORT}`);
  });
}

start().catch(err => {
  console.error('gula-auth: Startup failed:', err);
  process.exit(1);
});
