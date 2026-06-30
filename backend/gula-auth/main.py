import os
import sqlite3
import uuid
import json
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import bcrypt
import jwt
import asyncio
import websockets

app = FastAPI(title="GULA Auth Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "auth.db"
JWT_SECRET = "gulasecretkey123"
GATEWAY_WS_URL = "ws://127.0.0.1:8000/ws/events"

# Helper to init SQLite DB
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    print("gula-auth: SQLite users table verified.")

# Pydantic Schemas
class RegisterSchema(BaseModel):
    username: str
    password: str
    role: str
    tenant_id: str

class LoginSchema(BaseModel):
    username: str
    password: str

# Helper to publish WS event in background
async def publish_ws_event(event_type: str, payload: dict):
    try:
        async with websockets.connect(GATEWAY_WS_URL) as ws:
            event_envelope = {
                "eventId": str(uuid.uuid4()),
                "eventType": event_type,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "source": "gula-auth",
                "payload": payload
            }
            await ws.send(json.dumps(event_envelope))
            print(f"gula-auth: Published '{event_type}' event to WebSocket broker.")
    except Exception as e:
        print(f"gula-auth: WebSocket event publish failed: {e}")

@app.get("/api/auth/health")
def health():
    return {"service": "gula-auth", "status": "UP"}

@app.post("/api/auth/register")
async def register(data: RegisterSchema):
    role = data.role.upper()
    if role not in ["ADMIN", "RADIOLOGIST", "TECHNICIAN"]:
        raise HTTPException(status_code=400, detail="Invalid role. Must be ADMIN, RADIOLOGIST, or TECHNICIAN")
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check duplicate
    cursor.execute("SELECT id FROM users WHERE username = ?", (data.username,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Username already exists")
        
    user_id = str(uuid.uuid4())
    pw_hash = bcrypt.hashpw(data.password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    created_at = datetime.utcnow().isoformat()
    
    try:
        cursor.execute(
            "INSERT INTO users (id, username, password_hash, role, tenant_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, data.username, pw_hash, role, data.tenant_id, created_at)
        )
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"Database insert failed: {str(e)}")
    finally:
        conn.close()
        
    # Publish UserCreated Event
    payload = {
        "userId": user_id,
        "username": data.username,
        "role": role,
        "tenantId": data.tenant_id
    }
    # Run async publish
    asyncio.create_task(publish_ws_event("UserCreated", payload))
    
    return {
        "message": "User registered successfully",
        "user": {
            "id": user_id,
            "username": data.username,
            "role": role,
            "tenantId": data.tenant_id
        }
    }

@app.post("/api/auth/login")
def login(data: LoginSchema):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, password_hash, role, tenant_id FROM users WHERE username = ?", (data.username,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        raise HTTPException(status_code=401, detail="Invalid username or password")
        
    user_id, username, pw_hash, role, tenant_id = row
    
    if not bcrypt.checkpw(data.password.encode('utf-8'), pw_hash.encode('utf-8')):
        raise HTTPException(status_code=401, detail="Invalid username or password")
        
    # Generate token
    token = jwt.encode({
        "userId": user_id,
        "username": username,
        "role": role,
        "tenantId": tenant_id
    }, JWT_SECRET, algorithm="HS256")
    
    return {
        "message": "Login successful",
        "token": token,
        "user": {
            "id": user_id,
            "username": username,
            "role": role,
            "tenantId": tenant_id
        }
    }

init_db()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=3001, log_level="info")
