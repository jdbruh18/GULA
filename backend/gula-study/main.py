import os
import sqlite3
import uuid
import json
import random
from datetime import datetime
from fastapi import FastAPI, HTTPException, UploadFile, Form, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import websockets

app = FastAPI(title="GULA Study Archive Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "study.db"
STORAGE_ROOT = "./storage"
GATEWAY_WS_URL = "ws://127.0.0.1:8000/ws/events"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS studies (
            study_instance_uid TEXT PRIMARY KEY,
            patient_id TEXT NOT NULL,
            accession_number TEXT NOT NULL,
            modality TEXT NOT NULL,
            started TEXT NOT NULL,
            storage_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            tenant_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    
    # Ensure storage folder exists
    if not os.path.exists(STORAGE_ROOT):
        os.makedirs(STORAGE_ROOT)
    print("gula-study: SQLite database and local storage directory verified.")

async def publish_ws_event(event_type: str, payload: dict):
    try:
        async with websockets.connect(GATEWAY_WS_URL) as ws:
            event_envelope = {
                "eventId": str(uuid.uuid4()),
                "eventType": event_type,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "source": "gula-study",
                "payload": payload
            }
            await ws.send(json.dumps(event_envelope))
            print(f"gula-study: Published '{event_type}' event to WebSocket broker.")
    except Exception as e:
        print(f"gula-study: WebSocket event publish failed: {e}")

@app.get("/dicomweb/health")
def health():
    return {"service": "gula-study", "status": "UP"}

# QIDO-RS: Search Studies
@app.get("/dicomweb/studies")
def search_studies():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT study_instance_uid, patient_id, accession_number, modality, started, storage_path, file_size, tenant_id FROM studies")
    rows = cursor.fetchall()
    conn.close()
    
    studies = []
    for r in rows:
        studies.append({
            "resourceType": "ImagingStudy",
            "id": r[0],
            "status": "available",
            "patientId": r[1],
            "accessionNumber": r[2],
            "modality": r[3],
            "started": r[4],
            "storagePath": r[5],
            "fileSize": r[6],
            "tenantId": r[7]
        })
    return studies

# QIDO-RS: Mock Series details
@app.get("/dicomweb/studies/{studyUID}/series")
def get_series(studyUID: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT patient_id, modality, tenant_id FROM studies WHERE study_instance_uid = ?", (studyUID,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        raise HTTPException(status_code=404, detail="Study not found")
        
    return [{
        "resourceType": "ImagingStudySeries",
        "uid": "1.2.826.0.1.3680043.8.498." + str(uuid.uuid4())[:8],
        "number": 1,
        "modality": row[1],
        "description": f"{row[1]} Slice",
        "instances": 1
    }]

# STOW-RS: Store DICOM
@app.post("/dicomweb/studies")
async def store_dicom(
    file: UploadFile = File(...),
    patientId: str = Form(None),
    tenantId: str = Form("HOSPITAL-ALPHA"),
    modality: str = Form(None),
    accessionNumber: str = Form(None)
):
    pid = patientId or f"PT-{random.randint(10000, 99999)}"
    mod = modality or random.choice(["CT", "MR", "XR"])
    acc = accessionNumber or f"ACC-{random.randint(100000, 999999)}"
    
    study_uid = "1.2.826.0.1.3680043.8.498." + f"{random.randint(100000, 999999)}.{random.randint(10000, 99999)}"
    started = datetime.utcnow().isoformat() + "Z"
    
    # Trigger event 1: StudyReceived
    received_payload = {
        "resourceType": "ImagingStudy",
        "id": study_uid,
        "status": "registered",
        "patientId": pid,
        "started": started,
        "accessionNumber": acc,
        "modality": mod,
        "tenantId": tenantId
    }
    await publish_ws_event("StudyReceived", received_payload)
    
    # Save file locally
    tenant_dir = os.path.join(STORAGE_ROOT, tenantId, pid)
    if not os.path.exists(tenant_dir):
        os.makedirs(tenant_dir)
        
    storage_path = os.path.join(tenant_dir, f"{study_uid}.dcm")
    
    # Read file buffer
    content = await file.read()
    file_size = len(content)
    
    with open(storage_path, "wb") as f:
        f.write(content)
        
    # Write metadata to SQLite
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    created_at = datetime.utcnow().isoformat()
    try:
        cursor.execute(
            "INSERT INTO studies (study_instance_uid, patient_id, accession_number, modality, started, storage_path, file_size, tenant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (study_uid, pid, acc, mod, started, storage_path, file_size, tenantId, created_at)
        )
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"Database write failed: {str(e)}")
    finally:
        conn.close()
        
    # Trigger event 2: StudyStored
    stored_payload = {
        "resourceType": "ImagingStudy",
        "id": study_uid,
        "status": "available",
        "patientId": pid,
        "started": started,
        "accessionNumber": acc,
        "modality": mod,
        "storagePath": storage_path,
        "fileSize": file_size,
        "tenantId": tenantId
    }
    await publish_ws_event("StudyStored", stored_payload)
    
    return {
        "message": "DICOM study processed successfully",
        "studyInstanceUid": study_uid,
        "patientId": pid,
        "accessionNumber": acc,
        "storagePath": storage_path
    }

# WADO-RS: Retrieve Instance frame
@app.get("/dicomweb/studies/{studyUID}/series/{seriesUID}/instances/{instanceUID}/frames/{frameNumber}")
def get_frame(studyUID: str, seriesUID: str, instanceUID: str, frameNumber: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT storage_path FROM studies WHERE study_instance_uid = ?", (studyUID,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or not os.path.exists(row[0]):
        raise HTTPException(status_code=404, detail="DICOM frame not found")
        
    return FileResponse(row[0], media_type="application/octet-stream")

init_db()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=3002, log_level="info")
