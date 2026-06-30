import os
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
import pika

app = FastAPI(title="GULA Patient Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_URL = os.getenv("DATABASE_URL")
RABBITMQ_URL = os.getenv("RABBITMQ_URL")
GATEWAY_WS_URL = "ws://127.0.0.1:8000/ws/events"

# Database Connection Wrapper
def get_connection():
    if DB_URL:
        import psycopg2
        return psycopg2.connect(DB_URL)
    else:
        import sqlite3
        return sqlite3.connect("patient.db")

def translate_query(query: str) -> str:
    if DB_URL:
        # SQLite uses '?', Postgres uses '%s'
        return query.replace("?", "%s")
    return query

def execute_write(query: str, params=()):
    q = translate_query(query)
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(q, params)
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def execute_read_all(query: str, params=()):
    q = translate_query(query)
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(q, params)
        return cursor.fetchall()
    finally:
        conn.close()

def execute_read_one(query: str, params=()):
    q = translate_query(query)
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(q, params)
        return cursor.fetchone()
    finally:
        conn.close()

def init_db():
    # Verify/create tables
    execute_write("""
        CREATE TABLE IF NOT EXISTS patients (
            id VARCHAR(255) PRIMARY KEY,
            first_name VARCHAR(255) NOT NULL,
            last_name VARCHAR(255) NOT NULL,
            gender VARCHAR(50) NOT NULL,
            birth_date VARCHAR(50) NOT NULL,
            tenant_id VARCHAR(100) NOT NULL,
            created_at VARCHAR(100) NOT NULL
        );
    """)
    execute_write("""
        CREATE TABLE IF NOT EXISTS patient_timeline_events (
            id VARCHAR(255) PRIMARY KEY,
            patient_id VARCHAR(255) NOT NULL,
            event_type VARCHAR(255) NOT NULL,
            source VARCHAR(255) NOT NULL,
            timestamp VARCHAR(100) NOT NULL,
            payload TEXT NOT NULL,
            created_at VARCHAR(100) NOT NULL
        );
    """)
    print("gula-patient: Database connection and tables verified.")

class PatientCreateSchema(BaseModel):
    first_name: str
    last_name: str
    gender: str
    birth_date: str
    tenant_id: str

# Dynamic event publisher (RabbitMQ or WS)
async def publish_event(event_type: str, payload: dict):
    event_envelope = {
        "eventId": str(uuid.uuid4()),
        "eventType": event_type,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "source": "gula-patient",
        "payload": payload
    }
    
    if RABBITMQ_URL:
        # Publish to RabbitMQ
        try:
            params = pika.URLParameters(RABBITMQ_URL)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.exchange_declare(exchange='gula.events', exchange_type='topic', durable=True)
            routing_key = f"gula.event.{event_type}"
            channel.basic_publish(
                exchange='gula.events',
                routing_key=routing_key,
                body=json.dumps(event_envelope),
                properties=pika.BasicProperties(delivery_mode=2)
            )
            connection.close()
            print(f"gula-patient: Published '{event_type}' event to RabbitMQ.")
        except Exception as e:
            print(f"gula-patient: RabbitMQ publish failed: {e}")
    else:
        # Fallback to WebSocket Event Bus
        try:
            async with websockets.connect(GATEWAY_WS_URL) as ws:
                await ws.send(json.dumps(event_envelope))
                print(f"gula-patient: Published '{event_type}' event to WebSocket Event Bus.")
        except Exception as e:
            print(f"gula-patient: WebSocket publish failed: {e}")

# Core processing logic for timeline events
def process_timeline_event(event_id, event_type, source, timestamp, payload):
    patient_id = None
    if event_type == "PatientCreated":
        patient_id = payload.get("id")
    elif event_type in ["StudyReceived", "StudyStored", "AIRequested", "AICompleted", "ReportCreated", "ReportSigned"]:
        patient_id = payload.get("patientId")
        
    if not patient_id:
        return
        
    created_at = datetime.utcnow().isoformat()
    
    try:
        # 1. Save event to patient timeline
        execute_write(
            "INSERT OR REPLACE INTO patient_timeline_events (id, patient_id, event_type, source, timestamp, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)" if not DB_URL else
            "INSERT INTO patient_timeline_events (id, patient_id, event_type, source, timestamp, payload, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload",
            (event_id, patient_id, event_type, source, timestamp, json.dumps(payload), created_at)
        )
        
        # 2. Sync patient details if received from external systems
        if event_type == "PatientCreated":
            first = payload.get("name", [{}])[0].get("given", [""])[0]
            last = payload.get("name", [{}])[0].get("family", "")
            execute_write(
                "INSERT OR REPLACE INTO patients (id, first_name, last_name, gender, birth_date, tenant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)" if not DB_URL else
                "INSERT INTO patients (id, first_name, last_name, gender, birth_date, tenant_id, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (patient_id, first, last, payload.get("gender"), payload.get("birthDate"), payload.get("tenantId"), created_at)
            )
        print(f"gula-patient: Recorded event '{event_type}' for patient '{patient_id}' in timeline.")
    except Exception as db_err:
        print(f"gula-patient: Database error processing event {event_type}: {db_err}")

# Event bus consumer loop
def consumer_listener_thread():
    if RABBITMQ_URL:
        # 1. RabbitMQ Consumer
        print(f"gula-patient: Production mode active. Connecting to RabbitMQ consumer at {RABBITMQ_URL}")
        while True:
            try:
                params = pika.URLParameters(RABBITMQ_URL)
                connection = pika.BlockingConnection(params)
                channel = connection.channel()
                channel.exchange_declare(exchange='gula.events', exchange_type='topic', durable=True)
                
                # Bind queue to exchange
                queue_name = 'gula.patient.timeline'
                channel.queue_declare(queue=queue_name, durable=True)
                channel.queue_bind(exchange='gula.events', queue=queue_name, routing_key='gula.event.#')
                
                def callback(ch, method, properties, body):
                    try:
                        event = json.loads(body.decode('utf-8'))
                        process_timeline_event(
                            event.get("eventId"),
                            event.get("eventType"),
                            event.get("source"),
                            event.get("timestamp"),
                            event.get("payload", {})
                        )
                    except Exception as parse_err:
                        print(f"gula-patient: Message parsing error: {parse_err}")
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                    
                channel.basic_consume(queue=queue_name, on_message_callback=callback)
                print("gula-patient: Listening for RabbitMQ events...")
                channel.start_consuming()
            except Exception as e:
                print(f"gula-patient: RabbitMQ consumer connection error: {e}. Retrying in 5s...")
                time.sleep(5)
    else:
        # 2. Local WebSocket Client Consumer
        async def listen():
            while True:
                try:
                    async with websockets.connect(GATEWAY_WS_URL) as ws:
                        print("gula-patient: Connected to WebSocket Event Bus. Listening...")
                        while True:
                            msg = await ws.recv()
                            event = json.loads(msg)
                            process_timeline_event(
                                event.get("eventId"),
                                event.get("eventType"),
                                event.get("source"),
                                event.get("timestamp"),
                                event.get("payload", {})
                            )
                except Exception as e:
                    print(f"gula-patient: WS Event Bus link lost ({e}). Reconnecting in 3s...")
                    await asyncio.sleep(3)
                    
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(listen())

init_db()
threading.Thread(target=consumer_listener_thread, daemon=True).start()

# API Routes
@app.get("/api/patients/health")
def health():
    return {"service": "gula-patient", "status": "UP"}

@app.post("/api/patients")
async def create_patient(data: PatientCreateSchema):
    patient_id = "PT-" + str(uuid.uuid4())[:8].upper()
    created_at = datetime.utcnow().isoformat()
    
    try:
        execute_write(
            "INSERT INTO patients (id, first_name, last_name, gender, birth_date, tenant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (patient_id, data.first_name, data.last_name, data.gender, data.birth_date, data.tenant_id, created_at)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database write failed: {str(e)}")
        
    # Publish PatientCreated Event
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
    await publish_event("PatientCreated", fhir_payload)
    
    return {
        "message": "Patient created successfully",
        "patientId": patient_id,
        "name": f"{data.first_name} {data.last_name}"
    }

@app.get("/api/patients")
def get_patients():
    rows = execute_read_all("SELECT id, first_name, last_name, gender, birth_date, tenant_id FROM patients")
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
    p_row = execute_read_one("SELECT id, first_name, last_name, gender, birth_date, tenant_id FROM patients WHERE id = ?", (patient_id,))
    if not p_row:
        raise HTTPException(status_code=404, detail="Patient not found")
        
    e_rows = execute_read_all("SELECT id, event_type, source, timestamp, payload FROM patient_timeline_events WHERE patient_id = ? ORDER BY timestamp ASC", (patient_id,))
    
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=3003, log_level="info")
