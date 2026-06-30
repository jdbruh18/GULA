import os
import sqlite3
import uuid
import json
import time
import threading
import asyncio
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import websockets

app = FastAPI(title="GULA Patient Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "patient.db"
GATEWAY_WS_URL = "ws://127.0.0.1:8000/ws/events"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS patients (
            id TEXT PRIMARY KEY,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            gender TEXT NOT NULL,
            birth_date TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS patient_timeline_events (
            id TEXT PRIMARY KEY,
            patient_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            source TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    print("gula-patient: SQLite patient & timeline tables verified.")

# Pydantic Schema
class PatientCreateSchema(BaseModel):
    first_name: str
    last_name: str
    gender: str
    birth_date: str # YYYY-MM-DD
    tenant_id: str

# Helper to publish events
async def publish_ws_event(event_type: str, payload: dict):
    try:
        async with websockets.connect(GATEWAY_WS_URL) as ws:
            event_envelope = {
                "eventId": str(uuid.uuid4()),
                "eventType": event_type,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "source": "gula-patient",
                "payload": payload
            }
            await ws.send(json.dumps(event_envelope))
            print(f"gula-patient: Published '{event_type}' event to Event Bus.")
    except Exception as e:
        print(f"gula-patient: Event publish failed: {e}")

# Handle incoming event messages from Event Bus
def handle_incoming_event(msg_str: str):
    try:
        event = json.loads(msg_str)
        event_id = event.get("eventId")
        event_type = event.get("eventType")
        source = event.get("source")
        timestamp = event.get("timestamp")
        payload = event.get("payload", {})
        
        # Extract Patient ID
        patient_id = None
        if event_type == "PatientCreated":
            patient_id = payload.get("id")
        elif event_type in ["StudyReceived", "StudyStored", "AIRequested", "AICompleted", "ReportCreated", "ReportSigned"]:
            patient_id = payload.get("patientId")
            
        if not patient_id:
            return
            
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        created_at = datetime.utcnow().isoformat()
        
        try:
            # 1. Save event to patient timeline
            cursor.execute(
                "INSERT OR REPLACE INTO patient_timeline_events (id, patient_id, event_type, source, timestamp, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, patient_id, event_type, source, timestamp, json.dumps(payload), created_at)
            )
            
            # 2. Sync patient demographics locally if received PatientCreated from another service
            if event_type == "PatientCreated":
                first = payload.get("name", [{}])[0].get("given", [""])[0]
                last = payload.get("name", [{}])[0].get("family", "")
                cursor.execute(
                    "INSERT OR REPLACE INTO patients (id, first_name, last_name, gender, birth_date, tenant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (patient_id, first, last, payload.get("gender"), payload.get("birthDate"), payload.get("tenantId"), created_at)
                )
            conn.commit()
            print(f"gula-patient: Recorded event '{event_type}' for patient '{patient_id}' in timeline database.")
        except Exception as db_err:
            conn.rollback()
            print(f"gula-patient: Database error processing event {event_type}: {db_err}")
        finally:
            conn.close()
            
    except Exception as err:
        print(f"gula-patient: Error parsing consumed message: {err}")

# WS Event Listener Thread
def ws_listener_thread():
    async def listen():
        while True:
            try:
                async with websockets.connect(GATEWAY_WS_URL) as ws:
                    print("gula-patient: Connected to Event Bus WebSocket. Active consumer.")
                    while True:
                        msg = await ws.recv()
                        handle_incoming_event(msg)
            except Exception as e:
                print(f"gula-patient: Event Bus link lost ({e}). Reconnecting in 3s...")
                await asyncio.sleep(3)
                
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(listen())

threading.Thread(target=ws_listener_thread, daemon=True).start()

# API Endpoints
@app.get("/api/patients/health")
def health():
    return {"service": "gula-patient", "status": "UP"}

@app.post("/api/patients")
async def create_patient(data: PatientCreateSchema):
    patient_id = "PT-" + str(uuid.uuid4())[:8].upper()
    created_at = datetime.utcnow().isoformat()
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO patients (id, first_name, last_name, gender, birth_date, tenant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (patient_id, data.first_name, data.last_name, data.gender, data.birth_date, data.tenant_id, created_at)
        )
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"Database write failed: {str(e)}")
    finally:
        conn.close()
        
    # Publish PatientCreated Event (FHIR formatted)
    fhir_payload = {
        "resourceType": "Patient",
        "id": patient_id,
        "name": [{
            "given": [data.first_name],
            "family": data.last_name
        }],
        "gender": data.gender,
        "birthDate": data.birth_date,
        "tenantId": data.tenant_id
    }
    await publish_ws_event("PatientCreated", fhir_payload)
    
    return {
        "message": "Patient created successfully",
        "patientId": patient_id,
        "name": f"{data.first_name} {data.last_name}"
    }

@app.get("/api/patients")
def get_patients():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, first_name, last_name, gender, birth_date, tenant_id FROM patients")
    rows = cursor.fetchall()
    conn.close()
    
    result = []
    for r in rows:
        result.append({
            "patientId": r[0],
            "firstName": r[1],
            "lastName": r[2],
            "gender": r[3],
            "birthDate": r[4],
            "tenantId": r[5]
        })
    return result

@app.get("/api/patients/{patient_id}/timeline")
def get_patient_timeline(patient_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Get Patient
    cursor.execute("SELECT id, first_name, last_name, gender, birth_date, tenant_id FROM patients WHERE id = ?", (patient_id,))
    p_row = cursor.fetchone()
    if not p_row:
        conn.close()
        raise HTTPException(status_code=404, detail="Patient not found")
        
    # Get Timeline Events
    cursor.execute("SELECT id, event_type, source, timestamp, payload FROM patient_timeline_events WHERE patient_id = ? ORDER BY timestamp ASC", (patient_id,))
    e_rows = cursor.fetchall()
    conn.close()
    
    timeline = []
    for r in e_rows:
        timeline.append({
            "eventId": r[0],
            "eventType": r[1],
            "source": r[2],
            "timestamp": r[3],
            "payload": json.loads(r[4])
        })
        
    return {
        "patient": {
            "id": p_row[0],
            "name": f"{p_row[1]} {p_row[2]}",
            "gender": p_row[3],
            "birthDate": p_row[4],
            "tenantId": p_row[5]
        },
        "timeline": timeline
    }

init_db()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=3003, log_level="info")
