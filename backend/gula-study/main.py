import os
import sqlite3
import uuid
import json
import random
import io
from datetime import datetime
from fastapi import FastAPI, HTTPException, UploadFile, Form, File
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import websockets

import pydicom
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import ExplicitVRLittleEndian
import pydicom.uid
import numpy as np
from PIL import Image

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
    
    if not os.path.exists(STORAGE_ROOT):
        os.makedirs(STORAGE_ROOT)
    print("gula-study: SQLite study table and local storage verified.")

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
            print(f"gula-study: Published '{event_type}' event to Event Bus.")
    except Exception as e:
        print(f"gula-study: Event publish failed: {e}")

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

# QIDO-RS: Mock Series
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

# STOW-RS: Store DICOM (Parses binary DICOM file natively)
@app.post("/dicomweb/studies")
async def store_dicom(
    file: UploadFile = File(...),
    patientId: str = Form(None),
    tenantId: str = Form("HOSPITAL-ALPHA"),
    modality: str = Form(None),
    accessionNumber: str = Form(None)
):
    # Read binary bytes
    content = await file.read()
    file_size = len(content)
    
    # Defaults
    pid = patientId
    mod = modality
    acc = accessionNumber
    study_uid = None
    started = datetime.utcnow().isoformat() + "Z"
    
    # Attempt to parse binary bytes using pydicom
    try:
        dicom_stream = io.BytesIO(content)
        ds = pydicom.dcmread(dicom_stream)
        
        # Extract native tags
        if hasattr(ds, "PatientID") and ds.PatientID:
            pid = ds.PatientID
        if hasattr(ds, "Modality") and ds.Modality:
            mod = ds.Modality
        if hasattr(ds, "AccessionNumber") and ds.AccessionNumber:
            acc = ds.AccessionNumber
        if hasattr(ds, "StudyInstanceUID") and ds.StudyInstanceUID:
            study_uid = ds.StudyInstanceUID
            
        print(f"gula-study: Successfully parsed binary DICOM file. PatientID: {pid}, Modality: {mod}")
    except Exception as dcm_err:
        print(f"gula-study: Warning - Pydicom parsing failed, using form values/defaults: {dcm_err}")
        
    # Generate fallback values if missing
    pid = pid or f"PT-{random.randint(10000, 99999)}"
    mod = mod or random.choice(["CT", "MR", "XR"])
    acc = acc or f"ACC-{random.randint(100000, 999999)}"
    study_uid = study_uid or ("1.2.826.0.1.3680043.8.498." + f"{random.randint(100000, 999999)}.{random.randint(10000, 99999)}")
    
    # 1. Publish StudyReceived Event
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
    
    # 2. Save DICOM file binary locally
    tenant_dir = os.path.join(STORAGE_ROOT, tenantId, pid)
    if not os.path.exists(tenant_dir):
        os.makedirs(tenant_dir)
    storage_path = os.path.join(tenant_dir, f"{study_uid}.dcm")
    
    with open(storage_path, "wb") as f:
        f.write(content)
        
    # 3. Save study metadata to DB
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    created_at = datetime.utcnow().isoformat()
    try:
        cursor.execute(
            "INSERT OR REPLACE INTO studies (study_instance_uid, patient_id, accession_number, modality, started, storage_path, file_size, tenant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (study_uid, pid, acc, mod, started, storage_path, file_size, tenantId, created_at)
        )
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"Database write failed: {str(e)}")
    finally:
        conn.close()
        
    # 4. Publish StudyStored Event
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

# WADO-RS: Decodes DICOM pixel matrix and converts to browser PNG on-the-fly
@app.get("/dicomweb/studies/{studyUID}/series/{seriesUID}/instances/{instanceUID}/frames/{frameNumber}")
def get_frame(studyUID: str, seriesUID: str, instanceUID: str, frameNumber: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT storage_path FROM studies WHERE study_instance_uid = ?", (studyUID,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or not os.path.exists(row[0]):
        raise HTTPException(status_code=404, detail="DICOM study file not found")
        
    try:
        # Read the local DICOM file
        ds = pydicom.dcmread(row[0])
        
        # Get raw pixel array
        pixels = ds.pixel_array.astype(float)
        
        # Normalize voxel values to 0-255 grayscale
        p_min = pixels.min()
        p_max = pixels.max()
        if p_max > p_min:
            normalized = (pixels - p_min) * 255.0 / (p_max - p_min)
        else:
            normalized = pixels * 0
            
        img_data = normalized.astype(np.uint8)
        
        # Create PNG image using PIL
        img = Image.fromarray(img_data)
        
        # Save image to byte buffer
        img_buf = io.BytesIO()
        img.save(img_buf, format="PNG")
        img_buf.seek(0)
        
        return StreamingResponse(img_buf, media_type="image/png")
    except Exception as err:
        print(f"gula-study: Error rendering frame: {err}")
        raise HTTPException(status_code=500, detail=f"Failed to render DICOM frame: {str(err)}")

# TEST UTILITY: Dynamically generates valid binary DICOM P10 files with synthetic clinical findings
@app.get("/dicomweb/generate-test")
def generate_test_dicom(
    patientId: str = "PT-UNKNOWN",
    name: str = "Test Patient",
    modality: str = "CT"
):
    mod = modality.upper()
    
    # 1. Create file meta info
    file_meta = Dataset()
    file_meta.MediaStorageSOPClassUID = pydicom.uid.CTImageStorage if mod == "CT" else pydicom.uid.SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = pydicom.uid.generate_uid()
    file_meta.ImplementationClassUID = pydicom.uid.generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    
    # 2. Setup dataset P10 format
    ds = FileDataset("temp.dcm", {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.PatientName = name
    ds.PatientID = patientId
    ds.Modality = mod
    ds.StudyInstanceUID = pydicom.uid.generate_uid()
    ds.SeriesInstanceUID = pydicom.uid.generate_uid()
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = file_meta.MediaStorageSOPClassUID
    ds.AccessionNumber = f"ACC-{random.randint(100000, 999999)}"
    
    # 3. Create simulated anatomy pixel arrays
    arr = np.zeros((512, 512), dtype=np.uint16)
    
    # We randomly decide if there is an anomaly (pathology)
    has_anomaly = random.choice([True, False])
    
    if mod == "CT":
        # Draw skull boundary (intensity 2000 Hounsfield)
        for r in range(160, 166):
            for theta in np.linspace(0, 2*np.pi, 800):
                x = int(256 + r * np.cos(theta))
                y = int(256 + r * np.sin(theta))
                arr[y, x] = 2000
                
        # Draw brain matter background (intensity 100)
        for x in range(512):
            for y in range(512):
                if (x-256)**2 + (y-256)**2 < 155**2 and arr[y, x] != 2000:
                    arr[y, x] = 120
                    
        if has_anomaly:
            # Draw brain hemorrhage blob (density 850) at offset coordinate (220, 200)
            for x in range(512):
                for y in range(512):
                    if (x-220)**2 + (y-200)**2 < 28**2:
                        arr[y, x] = 850
    else:
        # Modality is XR (Chest X-ray style)
        # Draw lungs (lower intensity dark regions) and mediastinum/ribs (high intensity)
        # Initialize background body density
        arr.fill(1400)
        
        # Left Lung contour (density 180)
        for x in range(512):
            for y in range(512):
                if ((x-170)/65)**2 + ((y-260)/140)**2 < 1:
                    arr[y, x] = 180
                    # If has anomaly, draw pneumonia patch (opacity/density 920) inside left lung
                    if has_anomaly and (x-170)**2 + (y-280)**2 < 35**2:
                        arr[y, x] = 920
                        
        # Right Lung contour (density 180)
        for x in range(512):
            for y in range(512):
                if ((x-342)/65)**2 + ((y-260)/140)**2 < 1:
                    arr[y, x] = 180
                    
    # 4. Fill standard image tags
    ds.Rows = 512
    ds.Columns = 512
    ds.PixelSpacing = [1.0, 1.0]
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = arr.tobytes()
    
    # Write to memory buffer
    dcm_buf = io.BytesIO()
    ds.save_as(dcm_buf, write_like_original=False)
    dcm_buf.seek(0)
    
    return StreamingResponse(
        dcm_buf,
        media_type="application/dicom",
        headers={"Content-Disposition": f"attachment; filename={patientId}_study.dcm"}
    )

init_db()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=3002, log_level="info")
