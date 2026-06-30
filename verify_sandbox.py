import os
import json
import time
import requests
import asyncio
import websockets
import threading

GATEWAY_HTTP = "http://127.0.0.1:8000"
GATEWAY_WS = "ws://127.0.0.1:8000/ws/events"

received_events = []
ws_connected = threading.Event()

# WS listener client
def run_ws_listener():
    async def listen():
        try:
            async with websockets.connect(GATEWAY_WS) as ws:
                print("[Test] Connected to WebSocket Event Bus.")
                ws_connected.set()
                while True:
                    msg = await ws.recv()
                    event = json.loads(msg)
                    received_events.append(event)
                    print(f"[Test] Event received on bus: {event.get('eventType')}")
        except Exception as e:
            print(f"[Test] WebSocket listener error: {e}")
            
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(listen())

def main():
    print("=" * 60)
    print(" GULA | Integration Verification Test Suite")
    print("=" * 60)
    
    # 1. Wait for services to be ready
    print("[Test] Verifying API Gateway health...")
    max_checks = 10
    gateway_up = False
    for i in range(max_checks):
        try:
            res = requests.get(f"{GATEWAY_HTTP}/")
            if res.status_code == 200:
                gateway_up = True
                print("[Test] Gateway is UP.")
                break
        except Exception:
            pass
        print(f"[Test] Gateway not ready (check {i+1}/{max_checks}). Waiting 2s...")
        time.sleep(2)
        
    if not gateway_up:
        print("[Test] ERROR: Gateway failed to start. Aborting tests.")
        return
        
    # 2. Start WebSocket Listener Client
    t = threading.Thread(target=run_ws_listener, daemon=True)
    t.start()
    
    # Wait for connection
    if not ws_connected.wait(timeout=5):
        print("[Test] ERROR: Could not connect to WebSocket event bus. Aborting.")
        return
        
    # 3. Trigger User Registration (gula-auth)
    print("\n[Test] Action 1: Registering new Radiologist user...")
    test_username = f"dr_smith_{int(time.time())}"
    user_payload = {
        "username": test_username,
        "password": "securepassword123",
        "role": "RADIOLOGIST",
        "tenant_id": "HOSPITAL-ALPHA"
    }
    res = requests.post(f"{GATEWAY_HTTP}/api/auth/register", json=user_payload)
    assert res.status_code in [200, 201], f"Failed to register user: {res.text}"
    user_data = res.json()
    print(f"[Test] User registered: {user_data['user']['username']} (ID: {user_data['user']['id']})")
    
    # 4. Trigger Patient Creation (gula-patient)
    print("\n[Test] Action 2: Registering new Patient...")
    patient_payload = {
        "first_name": "Alexander",
        "last_name": "Fleming",
        "gender": "male",
        "birth_date": "1945-03-11",
        "tenant_id": "HOSPITAL-ALPHA"
    }
    res = requests.post(f"{GATEWAY_HTTP}/api/patients", json=patient_payload)
    assert res.status_code == 210 or res.status_code == 200, f"Failed to create patient: {res.text}"
    patient_data = res.json()
    patient_id = patient_data['patientId']
    print(f"[Test] Patient created: {patient_data['name']} (ID: {patient_id})")
    
    # 5. Trigger DICOM Upload (gula-study STOW-RS)
    print("\n[Test] Action 3: Generating and uploading real binary CT scan (STOW-RS)...")
    dcm_res = requests.get(f"{GATEWAY_HTTP}/dicomweb/generate-test?patientId={patient_id}&name=Alexander+Fleming&modality=CT")
    assert dcm_res.status_code == 200, "Failed to generate test DICOM"
    files = {"file": ("scan_slice.dcm", dcm_res.content, "application/octet-stream")}
    form_data = {
        "patientId": patient_id,
        "tenantId": "HOSPITAL-ALPHA",
        "modality": "CT",
        "accessionNumber": "ACC-998877"
    }
    res = requests.post(f"{GATEWAY_HTTP}/dicomweb/studies", files=files, data=form_data)
    assert res.status_code in [200, 201], f"DICOM upload failed: {res.text}"
    study_data = res.json()
    study_uid = study_data['studyInstanceUid']
    print(f"[Test] DICOM STOW-RS completed. Study Instance UID: {study_uid}")
    
    # 6. Wait for AI Engine processing and timeline updates
    print("\n[Test] Waiting 4 seconds for AI pipeline and timeline compilations...")
    time.sleep(4)
    
    # 7. Pull the Patient Timeline and assert results
    print("\n[Test] Action 4: Querying Patient Timeline...")
    res = requests.get(f"{GATEWAY_HTTP}/api/patients/{patient_id}/timeline")
    assert res.status_code == 200, f"Failed to retrieve timeline: {res.text}"
    timeline_data = res.json()
    
    timeline = timeline_data['timeline']
    print(f"[Test] Retrieved Digital Patient Timeline: {len(timeline)} events recorded.")
    for idx, event in enumerate(timeline):
        print(f"  [{idx+1}] {event['eventType']} from {event['source']} at {event['timestamp']}")
        if event['eventType'] == 'AICompleted':
            print(f"      AI Findings: {event['payload'].get('findings')}")
            
    # 8. Run Assertions on Event Log Propagation
    print("\n[Test] Verifying Event Log Assertions...")
    expected_events = [
        "UserCreated",
        "PatientCreated",
        "StudyReceived",
        "StudyStored",
        "AIRequested",
        "AICompleted"
    ]
    
    bus_event_types = [e['eventType'] for e in received_events]
    print(f"[Test] Event Bus event log: {bus_event_types}")
    
    for exp in expected_events:
        assert exp in bus_event_types, f"Assertion Error: Missing expected event '{exp}' from the WebSocket Bus!"
        print(f"  [PASS] Event '{exp}' successfully verified on bus.")
        
    timeline_event_types = [e['eventType'] for e in timeline]
    expected_timeline = [
        "PatientCreated",
        "StudyReceived",
        "StudyStored",
        "AIRequested",
        "AICompleted"
    ]
    
    for exp in expected_timeline:
        assert exp in timeline_event_types, f"Assertion Error: Missing expected timeline block '{exp}' in database!"
        print(f"  [PASS] Timeline block '{exp}' successfully verified in patient.db.")
        
    print("\n" + "=" * 60)
    print(" GULA SYSTEM VERIFICATION SUCCESSFUL!")
    print(" All microservices and event pipelines are fully operational.")
    print("=" * 60 + "\n")

if __name__ == "__main__":
    main()
