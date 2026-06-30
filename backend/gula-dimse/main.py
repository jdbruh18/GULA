import os
import io
import requests
from pynetdicom import AE, evt, AllStoragePresentationContexts

STUDY_SERVICE_URL = os.getenv("STUDY_SERVICE_URL", "http://127.0.0.1:3002/dicomweb/studies")
PORT = int(os.getenv("DIMSE_PORT", "11112"))
AE_TITLE = os.getenv("AE_TITLE", "GULA_PACS").encode('utf-8')

def handle_store(event):
    """Handle standard incoming DICOM C-STORE storage requests."""
    try:
        ds = event.dataset
        # Extract metadata from incoming study dataset
        patient_id = getattr(ds, 'PatientID', 'PT-UNKNOWN')
        modality = getattr(ds, 'Modality', 'OT')
        acc_num = getattr(ds, 'AccessionNumber', '')
        
        print(f"gula-dimse: Received C-STORE transmission. PatientID: {patient_id}, Modality: {modality}, Accession: {acc_num}")
        
        # Serialize dataset back to memory buffer in DICOM P10 format
        dcm_buf = io.BytesIO()
        ds.save_as(dcm_buf, write_like_original=False)
        dcm_buf.seek(0)
        
        # Forward binary DICOM file to gula-study via STOW-RS HTTP multipart POST
        files = {
            "file": (f"{patient_id}_dimse.dcm", dcm_buf, "application/dicom")
        }
        form_data = {
            "patientId": patient_id,
            "tenantId": "HOSPITAL-ALPHA",
            "modality": modality,
            "accessionNumber": acc_num
        }
        
        response = requests.post(STUDY_SERVICE_URL, files=files, data=form_data)
        if response.status_code in [200, 201]:
            print(f"gula-dimse: Successfully routed C-STORE object to Study Archive. HTTP {response.status_code}")
            return 0x0000 # Success status code in DICOM protocol standard
        else:
            print(f"gula-dimse: Failed to route object to archive. Study Service returned: {response.status_code} - {response.text}")
            return 0xC000 # Processing failure error code
            
    except Exception as e:
        print(f"gula-dimse: Internal error handling C-STORE request: {e}")
        return 0xC000 # Processing failure error code

def main():
    # Initialize Application Entity (AE)
    ae = AE(ae_title=AE_TITLE)
    
    # Declare support for all standard clinical storage classes
    ae.supported_contexts = AllStoragePresentationContexts
    
    # Register events handlers
    handlers = [(evt.EVT_C_STORE, handle_store)]
    
    print(f"gula-dimse: Starting C-STORE SCP server on port {PORT} with AE Title '{AE_TITLE.decode('utf-8')}'...")
    
    # Run the server. Bind to 0.0.0.0 so external machines can connect in docker configurations
    ae.start_server(('0.0.0.0', PORT), block=True, evt_handlers=handlers)

if __name__ == "__main__":
    main()
