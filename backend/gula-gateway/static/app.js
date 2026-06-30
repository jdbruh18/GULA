// GULA Dashboard App Logic
let socket = null;
let currentPatient = null;
let currentStudy = null;
let healthCheckInterval = null;

// DOM Elements
const wsStatus = document.getElementById('ws-status');
const eventLogs = document.getElementById('event-logs');
const pluginContainer = document.getElementById('plugin-container');
const btnCreatePatient = document.getElementById('btn-create-patient');
const btnUploadScan = document.getElementById('btn-upload-scan');
const btnClearLogs = document.getElementById('btn-clear-logs');
const activePatientId = document.getElementById('active-patient-id');
const timelineContainer = document.getElementById('timeline-container');
const viewerViewport = document.getElementById('viewer-viewport');
const viewerImage = document.getElementById('viewer-image');
const viewerOverlay = document.getElementById('viewer-overlay');

const serviceHealthIds = {
    'gateway': 'status-gateway',
    'auth': 'status-auth',
    'study': 'status-study',
    'patient': 'status-patient',
    'ai': 'status-ai'
};

// Start WebSockets Connection
function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/events`;
    
    socket = new WebSocket(wsUrl);
    
    socket.onopen = () => {
        wsStatus.querySelector('.status-indicator').className = 'status-indicator online';
        wsStatus.querySelector('span').innerText = 'Event Bus: Connected';
        addSystemLog('WebSocket Event Bus listener connected.');
    };
    
    socket.onmessage = (event) => {
        try {
            const eventEnvelope = JSON.parse(event.data);
            handleEventMessage(eventEnvelope);
        } catch (err) {
            console.error('Error parsing WebSocket message:', err);
        }
    };
    
    socket.onclose = () => {
        wsStatus.querySelector('.status-indicator').className = 'status-indicator offline';
        wsStatus.querySelector('span').innerText = 'Event Bus: Reconnecting...';
        addSystemLog('WebSocket connection lost. Reconnecting in 3s...');
        setTimeout(connectWebSocket, 3000);
    };
}

// Event Handler
function handleEventMessage(envelope) {
    const { eventType, source, timestamp, payload } = envelope;
    
    // Add to scrolling event logs
    addEventLog(eventType, source, envelope);
    
    // Update active patient timeline if it is the current patient
    if (currentPatient && payload.patientId === currentPatient.id) {
        fetchTimeline(currentPatient.id);
    } else if (currentPatient && eventType === "PatientCreated" && payload.id === currentPatient.id) {
        fetchTimeline(currentPatient.id);
    }
    
    // If study stored and it is our current patient, mock retrieve DICOM frame
    if (eventType === "StudyStored" && currentPatient && payload.patientId === currentPatient.id) {
        currentStudy = payload;
        renderDICOMFrame(payload);
    }
    
    // If AI completed and it is our active study, trigger annotation overlay
    if (eventType === "AICompleted" && currentStudy && payload.studyId === currentStudy.id) {
        renderAIOverlay(payload);
    }
}

// Log UI updates
function addSystemLog(msg) {
    const entry = document.createElement('div');
    entry.className = 'log-entry system';
    entry.innerHTML = `<span class="log-time">[System]</span> ${msg}`;
    eventLogs.insertBefore(entry, eventLogs.firstChild);
}

function addEventLog(type, source, envelope) {
    const entry = document.createElement('div');
    entry.className = `log-entry ${type}`;
    
    const timeStr = new Date(envelope.timestamp).toLocaleTimeString();
    entry.innerHTML = `
        <span class="log-time">[${timeStr}]</span> 
        <strong>${type}</strong> from <em>${source}</em>
        <pre class="log-json">${JSON.stringify(envelope.payload, null, 2)}</pre>
    `;
    
    eventLogs.insertBefore(entry, eventLogs.firstChild);
}

// Poll microservices health
async function checkHealth() {
    // Gateway is online if dashboard is loaded
    updateHealthUI('gateway', 'online');
    
    const services = [
        { key: 'auth', url: '/api/auth/health', portFallback: 3001 },
        { key: 'study', url: '/dicomweb/health', portFallback: 3002 }, // mapped through gateway or fallback
        { key: 'patient', url: '/api/patients/health', portFallback: 3003 },
        { key: 'ai', url: '/api/ai/health', portFallback: 3004 }
    ];
    
    for (const s of services) {
        try {
            // Check health via gateway routes
            const path = s.key === 'study' ? '/dicomweb/health' : `/api/${s.key}/health`;
            const res = await fetch(path);
            if (res.ok) {
                updateHealthUI(s.key, 'online');
            } else {
                updateHealthUI(s.key, 'offline');
            }
        } catch (err) {
            updateHealthUI(s.key, 'offline');
        }
    }
}

function updateHealthUI(service, status) {
    const el = document.getElementById(serviceHealthIds[service]);
    if (el) {
        el.className = `indicator ${status}`;
    }
}

// Fetch AI Plugins
async function fetchPlugins() {
    try {
        const res = await fetch('/api/ai/plugins');
        if (!res.ok) throw new Error('Failed to fetch plugins');
        const plugins = await res.json();
        
        pluginContainer.innerHTML = '';
        plugins.forEach(p => {
            const item = document.createElement('div');
            item.className = 'plugin-item';
            item.innerHTML = `
                <div class="plugin-info">
                    <h4>${p.name}</h4>
                    <p>${p.description} (v${p.version})</p>
                </div>
                <label class="switch">
                    <input type="checkbox" id="toggle-${p.name}" ${p.enabled ? 'checked' : ''}>
                    <span class="slider"></span>
                </label>
            `;
            
            pluginContainer.appendChild(item);
            
            // Add toggle handler
            document.getElementById(`toggle-${p.name}`).addEventListener('change', async (e) => {
                const enabled = e.target.checked;
                try {
                    await fetch(`/api/ai/plugins/${p.name}/toggle`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ enabled })
                    });
                    addSystemLog(`AI Plugin '${p.name}' status updated to: ${enabled ? 'Enabled' : 'Disabled'}`);
                } catch (err) {
                    console.error('Failed to toggle plugin:', err);
                    e.target.checked = !enabled; // revert
                }
            });
        });
    } catch (err) {
        pluginContainer.innerHTML = `<div class="log-entry" style="color:var(--danger)">Failed to load plugins.</div>`;
    }
}

// Create Patient Simulator
btnCreatePatient.addEventListener('click', async () => {
    btnCreatePatient.classList.add('disabled');
    btnCreatePatient.disabled = true;
    
    const firstNames = ["James", "Emma", "John", "Sophia", "Robert", "Olivia", "Michael", "Isabella"];
    const lastNames = ["Smith", "Jones", "Miller", "Davis", "Garcia", "Rodriguez", "Wilson", "Thomas"];
    const genders = ["male", "female"];
    
    const first = firstNames[Math.floor(Math.random() * firstNames.length)];
    const last = lastNames[Math.floor(Math.random() * lastNames.length)];
    const gender = genders[Math.floor(Math.random() * genders.length)];
    const year = Math.floor(Math.random() * 40) + 1960;
    const month = String(Math.floor(Math.random() * 12) + 1).padStart(2, '0');
    const day = String(Math.floor(Math.random() * 28) + 1).padStart(2, '0');
    const birthDate = `${year}-${month}-${day}`;
    
    try {
        const res = await fetch('/api/patients', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                first_name: first,
                last_name: last,
                gender,
                birth_date: birthDate,
                tenant_id: "HOSPITAL-ALPHA"
            })
        });
        
        if (!res.ok) throw new Error('Create patient failed');
        const data = await res.json();
        
        currentPatient = { id: data.patientId, name: data.name, gender, birthDate };
        activePatientId.innerText = `${data.name} (${data.patientId})`;
        
        // Reset timeline container
        timelineContainer.innerHTML = '<div class="timeline-placeholder">Building timeline...</div>';
        
        // Enable scan button
        btnUploadScan.classList.remove('disabled');
        btnUploadScan.disabled = false;
        
        addSystemLog(`Simulated Registration: Created patient ${data.name} (${data.patientId})`);
    } catch (err) {
        addSystemLog(`Error creating patient: ${err.message}`);
        btnCreatePatient.classList.remove('disabled');
        btnCreatePatient.disabled = false;
    }
});

// Upload Simulated Scan (STOW-RS)
btnUploadScan.addEventListener('click', async () => {
    if (!currentPatient) return;
    
    btnUploadScan.classList.add('disabled');
    btnUploadScan.disabled = true;
    
    // Pick between CT (Brain) and XR (Chest) scans
    const modalities = ["CT", "XR"];
    const mod = modalities[Math.floor(Math.random() * modalities.length)];
    
    addSystemLog(`Simulating DICOM Generation (Modality: ${mod}) for Patient ${currentPatient.name}...`);
    
    try {
        // Step 1: Request a real binary DICOM file from the study generator
        const dcmUrl = `/dicomweb/generate-test?patientId=${currentPatient.id}&name=${encodeURIComponent(currentPatient.name)}&modality=${mod}`;
        const dcmRes = await fetch(dcmUrl);
        if (!dcmRes.ok) throw new Error('DICOM generation failed');
        const dcmBlob = await dcmRes.blob();
        
        addSystemLog(`Generated binary DICOM file (${(dcmBlob.size/1024).toFixed(1)} KB). Pushing to GULA Archive via STOW-RS...`);
        
        // Step 2: Post this real binary file to the STOW-RS study receiver
        const formData = new FormData();
        formData.append('file', dcmBlob, `${currentPatient.id}_study.dcm`);
        formData.append('patientId', currentPatient.id);
        formData.append('tenantId', 'HOSPITAL-ALPHA');
        formData.append('modality', mod);
        formData.append('accessionNumber', 'ACC-' + Math.floor(Math.random() * 900000 + 100000));
        
        const uploadRes = await fetch('/dicomweb/studies', {
            method: 'POST',
            body: formData
        });
        
        if (!uploadRes.ok) throw new Error('STOW-RS upload failed');
        const data = await uploadRes.json();
        
        addSystemLog(`STOW-RS Success: DICOM stored in archive. Study Instance UID: ${data.studyInstanceUid}`);
    } catch (err) {
        addSystemLog(`STOW-RS Error: ${err.message}`);
        btnUploadScan.classList.remove('disabled');
        btnUploadScan.disabled = false;
    }
});


// Fetch Timeline
async function fetchTimeline(patientId) {
    try {
        const res = await fetch(`/api/patients/${patientId}/timeline`);
        if (!res.ok) return;
        const data = await res.json();
        
        timelineContainer.innerHTML = '';
        if (data.timeline.length === 0) {
            timelineContainer.innerHTML = '<div class="timeline-placeholder">No events recorded.</div>';
            return;
        }
        
        data.timeline.forEach(item => {
            const entry = document.createElement('div');
            entry.className = 'timeline-item';
            
            let iconClass = item.eventType.toLowerCase();
            let headingText = item.eventType;
            let description = '';
            let findingsHtml = '';
            
            switch (item.eventType) {
                case 'PatientCreated':
                    headingText = 'Patient Registered';
                    description = `Demographics logged: ${data.patient.name} (${data.patient.gender}, born ${data.patient.birthDate})`;
                    break;
                case 'StudyReceived':
                    headingText = 'Study Ordered & Received';
                    description = `Modality: ${item.payload.modality}, Accession: ${item.payload.accessionNumber}`;
                    break;
                case 'StudyStored':
                    headingText = 'DICOM Archive Complete';
                    description = `DICOM objects uploaded to MinIO. Storage path: ${item.payload.storagePath} (${(item.payload.fileSize/1024).toFixed(1)} KB)`;
                    break;
                case 'AIRequested':
                    headingText = 'AI Diagnostics Scheduled';
                    description = `Triggered analysis models: ${item.payload.requestedPlugins.join(', ')}`;
                    break;
                case 'AICompleted':
                    headingText = 'AI Insights Generated';
                    description = `Pipeline executed. Findings evaluated.`;
                    
                    const obsList = item.payload.findings;
                    if (obsList && obsList.length > 0) {
                        findingsHtml = obsList.map(o => `
                            <div class="findings-box">
                                Finding: ${o.code.toUpperCase()}<br>
                                Value: <span style="color:${o.value === 'Positive' ? 'var(--danger)' : 'var(--success)'}">${o.value}</span><br>
                                Confidence: ${(o.probability * 100).toFixed(1)}%
                            </div>
                        `).join('');
                    } else {
                        findingsHtml = '<div class="findings-box">No clinical abnormalities identified.</div>';
                    }
                    break;
            }
            
            const timeStr = new Date(item.timestamp).toLocaleTimeString();
            entry.innerHTML = `
                <div class="timeline-dot ${iconClass}"></div>
                <div class="timeline-content">
                    <h4>${headingText} <span class="time">${timeStr}</span></h4>
                    <p>${description}</p>
                    ${findingsHtml}
                </div>
            `;
            timelineContainer.appendChild(entry);
        });
        
        // Auto scroll to bottom
        timelineContainer.scrollTop = timelineContainer.scrollHeight;
    } catch (err) {
        console.error('Failed to load timeline:', err);
    }
}

// Render raw frame in viewer
function renderDICOMFrame(study) {
    viewerViewport.querySelector('.viewer-placeholder').style.display = 'none';
    viewerImage.style.display = 'block';
    viewerOverlay.style.display = 'flex';
    
    // Fetch frame from WADO-RS route
    const frameUrl = `/dicomweb/studies/${study.id}/series/1.2/instances/1.2.3/frames/1`;
    viewerImage.src = frameUrl;
    
    // Update overlay metadata
    document.getElementById('overlay-patient').innerText = `Patient: ${currentPatient.name}`;
    document.getElementById('overlay-study').innerText = `Study UID: ${study.id.substring(0, 16)}...`;
    document.getElementById('overlay-modality').innerText = `Modality: ${study.modality}`;
}

// Render AI Findings overlay on the viewport
function renderAIOverlay(aiEvent) {
    const obsList = aiEvent.findings;
    
    // Canvas overlay on top of viewport to draw green or red circles depending on AI findings
    // Check if canvas already exists, clean up
    const oldCanvas = viewerViewport.querySelector('canvas');
    if (oldCanvas) oldCanvas.remove();
    
    const canvas = document.createElement('canvas');
    canvas.style.position = 'absolute';
    canvas.style.top = '0';
    canvas.style.left = '0';
    canvas.style.width = '100%';
    canvas.style.height = '100%';
    canvas.style.pointerEvents = 'none';
    canvas.width = viewerViewport.clientWidth;
    canvas.height = viewerViewport.clientHeight;
    
    const ctx = canvas.getContext('2d');
    
    // Draw bounding boxes for any positive findings
    obsList.forEach(o => {
        if (o.value === 'Positive') {
            ctx.strokeStyle = '#ef4444';
            ctx.lineWidth = 3;
            // Draw glowing outline
            ctx.shadowBlur = 10;
            ctx.shadowColor = 'red';
            ctx.beginPath();
            
            if (o.code === 'brain-hemorrhage') {
                // Hemorrhage circle
                ctx.arc(250, 200, 60, 0, Math.PI*2);
                ctx.stroke();
                ctx.fillStyle = 'rgba(239, 68, 68, 0.2)';
                ctx.fill();
                ctx.fillStyle = '#ef4444';
                ctx.font = '12px Courier';
                ctx.fillText(`AI: HEMORRHAGE (${(o.probability*100).toFixed(1)}%)`, 200, 130);
            } else if (o.code === 'chest-pneumonia') {
                // Pneumonia bounding boxes in lungs
                ctx.rect(130, 180, 80, 120);
                ctx.rect(300, 180, 80, 120);
                ctx.stroke();
                ctx.fillStyle = 'rgba(239, 68, 68, 0.15)';
                ctx.fill();
                ctx.fillStyle = '#ef4444';
                ctx.font = '12px Courier';
                ctx.fillText(`AI: PNEUMONIA CONSOLIDATION (${(o.probability*100).toFixed(1)}%)`, 130, 160);
            }
        }
    });
    
    viewerViewport.appendChild(canvas);
    
    // Re-enable patient registration for a new cycle
    btnCreatePatient.classList.remove('disabled');
    btnCreatePatient.disabled = false;
}

// Clear Logs
btnClearLogs.addEventListener('click', () => {
    eventLogs.innerHTML = '';
    addSystemLog('Event logs cleared.');
});

// Initialize Dashboard
function init() {
    connectWebSocket();
    checkHealth();
    fetchPlugins();
    
    // Periodically check health and fetch plugins
    healthCheckInterval = setInterval(() => {
        checkHealth();
    }, 5000);
}

init();
