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
const canvasWrapper = document.getElementById('canvas-wrapper');
const viewerCanvas = document.getElementById('viewer-canvas');
const viewerOverlay = document.getElementById('viewer-overlay');

// Diagnostic Viewer State
let activeTool = 'wl'; // 'wl', 'zoom', 'pan', 'ruler'
let scale = 1.0;
let offsetX = 0;
let offsetY = 0;
let isDragging = false;
let startDragX = 0;
let startDragY = 0;
let imgObj = null; // Store loaded HTML Image object
let activeAIOverlay = null; // Store positive AI findings if any
let pixelSpacing = [1.0, 1.0]; // Default calibration mm/px

// Custom 16-bit Window/Level
let currentWc = null;
let currentWw = null;

// Ruler Measurement State
let rulerStart = null;
let rulerEnd = null;

// Clinical Reporting DOM Elements
const reportCard = document.getElementById('reporting-card');
const reportTemplate = document.getElementById('report-template');
const reportFindings = document.getElementById('report-findings');
const reportConclusion = document.getElementById('report-conclusion');
const reportSignee = document.getElementById('report-signee');
const btnSignReport = document.getElementById('btn-sign-report');
const btnPrintReport = document.getElementById('btn-print-report');
const reportStatusBadge = document.getElementById('report-status-badge');

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
                case 'ReportSigned':
                    headingText = 'Diagnostic Report Signed';
                    description = `Radiologist final sign-off completed by ${item.payload.radiologist}. Status: FINAL.`;
                    findingsHtml = `
                        <div class="findings-box" style="border-left: 3px solid var(--primary); background: rgba(225, 29, 72, 0.05);">
                            <strong>Conclusion:</strong> ${item.payload.conclusion}
                        </div>
                    `;
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

// Setup Tool Click Listeners
const tools = ['wl', 'zoom', 'pan', 'ruler'];
tools.forEach(t => {
    const btn = document.getElementById(`tool-${t}`);
    if (btn) {
        btn.addEventListener('click', () => {
            tools.forEach(tool => {
                const b = document.getElementById(`tool-${tool}`);
                if (b) b.classList.remove('active');
            });
            btn.classList.add('active');
            activeTool = t;
            // Clear ruler when changing tools
            if (activeTool !== 'ruler') {
                rulerStart = null;
                rulerEnd = null;
                redrawCanvas();
            }
        });
    }
});

const btnReset = document.getElementById('tool-reset');
if (btnReset) {
    btnReset.addEventListener('click', () => {
        scale = 1.0;
        offsetX = 0;
        offsetY = 0;
        rulerStart = null;
        rulerEnd = null;
        if (currentPatient && currentStudy) {
            if (currentStudy.modality === 'CT') {
                currentWc = 1000;
                currentWw = 2000;
            } else {
                currentWc = 800;
                currentWw = 1600;
            }
            loadFrameImage();
        } else {
            redrawCanvas();
        }
    });
}

// Convert screen mouse events to canvas space coordinates
function getCanvasMousePos(e) {
    const rect = viewerCanvas.getBoundingClientRect();
    const x = (e.clientX - rect.left) * (viewerCanvas.width / rect.width);
    const y = (e.clientY - rect.top) * (viewerCanvas.height / rect.height);
    return { x, y };
}

// Mouse interaction event listeners
viewerCanvas.addEventListener('mousedown', (e) => {
    isDragging = true;
    const pos = getCanvasMousePos(e);
    startDragX = e.clientX;
    startDragY = e.clientY;
    
    if (activeTool === 'ruler') {
        rulerStart = pos;
        rulerEnd = pos;
        redrawCanvas();
    }
});

viewerCanvas.addEventListener('mousemove', (e) => {
    if (!isDragging) return;
    const pos = getCanvasMousePos(e);
    
    const deltaX = e.clientX - startDragX;
    const deltaY = e.clientY - startDragY;
    
    startDragX = e.clientX;
    startDragY = e.clientY;
    
    if (activeTool === 'wl') {
        if (currentWc !== null && currentWw !== null) {
            currentWw += deltaX * 4;
            currentWc -= deltaY * 4;
            currentWw = Math.max(10, currentWw);
            throttledLoadFrame();
        }
    } else if (activeTool === 'zoom') {
        scale += deltaY * -0.01;
        scale = Math.min(5.0, Math.max(0.2, scale));
        redrawCanvas();
    } else if (activeTool === 'pan') {
        offsetX += deltaX;
        offsetY += deltaY;
        redrawCanvas();
    } else if (activeTool === 'ruler') {
        rulerEnd = pos;
        redrawCanvas();
    }
});

viewerCanvas.addEventListener('mouseup', () => {
    isDragging = false;
});

viewerCanvas.addEventListener('mouseleave', () => {
    isDragging = false;
});

viewerCanvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const zoomIntensity = 0.1;
    if (e.deltaY < 0) {
        scale += zoomIntensity;
    } else {
        scale -= zoomIntensity;
    }
    scale = Math.min(5.0, Math.max(0.2, scale));
    redrawCanvas();
}, { passive: false });

let wlTimeout = null;
function throttledLoadFrame() {
    document.getElementById('overlay-wl').innerText = `W/L: ${Math.round(currentWc)} / ${Math.round(currentWw)}`;
    if (wlTimeout) clearTimeout(wlTimeout);
    wlTimeout = setTimeout(() => {
        loadFrameImage();
    }, 50);
}

// Fetch frame URL with custom window/level query parameters
function loadFrameImage() {
    if (!currentStudy) return;
    
    let frameUrl = `/dicomweb/studies/${currentStudy.id}/series/1.2/instances/1.2.3/frames/1`;
    if (currentWc !== null && currentWw !== null) {
        frameUrl += `?wc=${Math.round(currentWc)}&ww=${Math.round(currentWw)}`;
        document.getElementById('overlay-wl').innerText = `W/L: ${Math.round(currentWc)} / ${Math.round(currentWw)}`;
    } else {
        document.getElementById('overlay-wl').innerText = `W/L: Auto`;
    }
    
    const tempImg = new Image();
    tempImg.src = frameUrl;
    tempImg.onload = () => {
        imgObj = tempImg;
        redrawCanvas();
    };
}

// Redraw main viewport canvas
function redrawCanvas() {
    if (!imgObj) return;
    const ctx = viewerCanvas.getContext('2d');
    ctx.clearRect(0, 0, viewerCanvas.width, viewerCanvas.height);
    
    ctx.save();
    ctx.translate(viewerCanvas.width / 2 + offsetX, viewerCanvas.height / 2 + offsetY);
    ctx.scale(scale, scale);
    
    // Draw DICOM image
    ctx.drawImage(imgObj, -imgObj.width / 2, -imgObj.height / 2);
    
    // Draw AI annotations overlay if positive finding is present
    if (activeAIOverlay) {
        activeAIOverlay.forEach(o => {
            if (o.value === 'Positive') {
                ctx.strokeStyle = '#ef4444';
                ctx.lineWidth = 3 / scale;
                ctx.shadowBlur = 10;
                ctx.shadowColor = 'red';
                ctx.beginPath();
                
                if (o.code === 'brain-hemorrhage') {
                    ctx.arc(-6, -56, 60, 0, Math.PI * 2);
                    ctx.stroke();
                    ctx.save();
                    ctx.shadowBlur = 0;
                    ctx.fillStyle = 'rgba(239, 68, 68, 0.2)';
                    ctx.fill();
                    ctx.restore();
                    
                    ctx.fillStyle = '#ef4444';
                    ctx.font = `${12 / scale}px Courier`;
                    ctx.fillText(`AI: HEMORRHAGE (${(o.probability*100).toFixed(1)}%)`, -56, -126);
                } else if (o.code === 'chest-pneumonia') {
                    ctx.rect(-126, -76, 80, 120);
                    ctx.rect(44, -76, 80, 120);
                    ctx.stroke();
                    ctx.save();
                    ctx.shadowBlur = 0;
                    ctx.fillStyle = 'rgba(239, 68, 68, 0.15)';
                    ctx.fill();
                    ctx.restore();
                    
                    ctx.fillStyle = '#ef4444';
                    ctx.font = `${12 / scale}px Courier`;
                    ctx.fillText(`AI: PNEUMONIA (${(o.probability*100).toFixed(1)}%)`, -126, -96);
                }
            }
        });
    }
    ctx.restore();
    
    // Draw non-transformed ruler measurement overlay
    if (rulerStart && rulerEnd) {
        ctx.save();
        ctx.strokeStyle = '#f59e0b';
        ctx.lineWidth = 2;
        ctx.shadowBlur = 4;
        ctx.shadowColor = '#000';
        
        ctx.beginPath();
        ctx.moveTo(rulerStart.x, rulerStart.y);
        ctx.lineTo(rulerEnd.x, rulerEnd.y);
        ctx.stroke();
        
        ctx.beginPath();
        drawTick(ctx, rulerStart.x, rulerStart.y, rulerEnd.x, rulerEnd.y);
        drawTick(ctx, rulerEnd.x, rulerEnd.y, rulerStart.x, rulerStart.y);
        ctx.stroke();
        
        const dx = rulerEnd.x - rulerStart.x;
        const dy = rulerEnd.y - rulerStart.y;
        const mmDist = Math.hypot(dx * pixelSpacing[0], dy * pixelSpacing[1]);
        
        ctx.fillStyle = '#f59e0b';
        ctx.font = 'bold 13px Outfit, sans-serif';
        const labelText = `${mmDist.toFixed(1)} mm`;
        const midX = (rulerStart.x + rulerEnd.x) / 2 + 10;
        const midY = (rulerStart.y + rulerEnd.y) / 2 - 10;
        ctx.fillText(labelText, midX, midY);
        ctx.restore();
    }
}

function drawTick(ctx, x1, y1, x2, y2) {
    const angle = Math.atan2(y2 - y1, x2 - x1);
    const tickLen = 6;
    ctx.moveTo(x1 + Math.sin(angle) * tickLen, y1 - Math.cos(angle) * tickLen);
    ctx.lineTo(x1 - Math.sin(angle) * tickLen, y1 + Math.cos(angle) * tickLen);
}

// Render raw frame in viewer
function renderDICOMFrame(study) {
    viewerViewport.querySelector('.viewer-placeholder').style.display = 'none';
    canvasWrapper.style.display = 'block';
    viewerOverlay.style.display = 'flex';
    
    // Set default Window Center / Window Width based on study modality
    if (study.modality === 'CT') {
        currentWc = 1000;
        currentWw = 2000;
    } else {
        currentWc = 800;
        currentWw = 1600;
    }
    
    // Reset view state
    scale = 1.0;
    offsetX = 0;
    offsetY = 0;
    rulerStart = null;
    rulerEnd = null;
    activeAIOverlay = null;
    
    // Load frame image
    loadFrameImage();
    
    // Update overlay metadata
    document.getElementById('overlay-patient').innerText = `Patient: ${currentPatient.name}`;
    document.getElementById('overlay-study').innerText = `Study UID: ${study.id.substring(0, 16)}...`;
    document.getElementById('overlay-modality').innerText = `Modality: ${study.modality}`;
    
    // Open reporting workspace
    reportCard.style.display = 'block';
    reportStatusBadge.innerText = 'Draft';
    reportStatusBadge.style.background = 'rgba(245, 158, 11, 0.15)';
    reportStatusBadge.style.color = '#f59e0b';
    reportStatusBadge.style.border = '1px solid rgba(245, 158, 11, 0.3)';
    
    btnSignReport.disabled = false;
    btnSignReport.classList.remove('disabled');
    btnPrintReport.disabled = true;
    btnPrintReport.classList.add('disabled');
    
    reportTemplate.value = 'none';
    reportFindings.value = '';
    reportConclusion.value = '';
}

// Render AI findings overlay on the viewport
function renderAIOverlay(aiEvent) {
    activeAIOverlay = aiEvent.findings;
    redrawCanvas();
    
    // Re-enable patient registration simulator
    btnCreatePatient.classList.remove('disabled');
    btnCreatePatient.disabled = false;
}

// Reporting template populate listener
reportTemplate.addEventListener('change', () => {
    const val = reportTemplate.value;
    if (val === 'ct-brain') {
        let aiText = "AI Analytics evaluates brain slice: [Pending results]";
        let conclusion = "Clinical correlation is advised.";
        if (activeAIOverlay) {
            const hem = activeAIOverlay.find(o => o.code === 'brain-hemorrhage');
            if (hem) {
                if (hem.value === 'Positive') {
                    aiText = `ALERT: High-density focal collection identified. AI inference calculates a brain hemorrhage probability of ${(hem.probability * 100).toFixed(1)}%.`;
                    conclusion = `ACUTE INTRACRANIAL HEMORRHAGE DETECTED.\nAI probability score: ${(hem.probability * 100).toFixed(1)}%. Urgent clinical review required.`;
                } else {
                    aiText = `No abnormal high-density focal collections. AI brain hemorrhage probability is low (${(hem.probability * 100).toFixed(1)}%).`;
                    conclusion = `No acute intracranial hemorrhage.`;
                }
            }
        }
        reportFindings.value = `INDICATION: Acute head trauma. Evaluate for intracranial pathology.\n\nFINDINGS:\n- Bone: No acute calvarial fracture identified.\n- Brain: Grey-white matter differentiation is preserved. No mass effect or midline shift.\n- Ventricles: Symmetrical and normal size.\n- AI Diagnostics: ${aiText}`;
        reportConclusion.value = conclusion;
    } else if (val === 'cxr') {
        let aiText = "AI Analytics evaluates lung fields: [Pending results]";
        let conclusion = "Clinical correlation is advised.";
        if (activeAIOverlay) {
            const pne = activeAIOverlay.find(o => o.code === 'chest-pneumonia');
            if (pne) {
                if (pne.value === 'Positive') {
                    aiText = `ALERT: Focal airspace opacities identified. AI inference calculates pneumonia consolidation probability of ${(pne.probability * 100).toFixed(1)}%.`;
                    conclusion = `BILATERAL LUNG CONSOLIDATION CONSISTENT WITH PNEUMONIA.\nAI probability score: ${(pne.probability * 100).toFixed(1)}%.`;
                } else {
                    aiText = `Lung fields clear. No consolidation or airspace opacities. AI pneumonia probability is low (${(pne.probability * 100).toFixed(1)}%).`;
                    conclusion = `Clear lungs. No radiological evidence of active pneumonia.`;
                }
            }
        }
        reportFindings.value = `INDICATION: Cough and fever. Evaluate for pneumonia.\n\nFINDINGS:\n- Lungs: ${aiText}\n- Heart: Cardiomediastinal silhouette is normal.\n- Pleura: No pleural effusion or pneumothorax.\n- Bones: Thoracic skeleton is intact.`;
        reportConclusion.value = conclusion;
    } else {
        reportFindings.value = '';
        reportConclusion.value = '';
    }
});

// Digital signature submit trigger
btnSignReport.addEventListener('click', () => {
    if (!currentPatient || !currentStudy) return;
    
    const findings = reportFindings.value;
    const conclusion = reportConclusion.value;
    const signee = reportSignee.value;
    
    if (!findings.trim() || !conclusion.trim() || !signee.trim()) {
        alert('Please populate findings, conclusion, and signee name before final signing.');
        return;
    }
    
    const eventEnvelope = {
        eventId: crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).substring(2, 15),
        eventType: 'ReportSigned',
        timestamp: new Date().toISOString(),
        source: 'gula-gateway-client',
        payload: {
            resourceType: 'DiagnosticReport',
            id: 'REP-' + Math.random().toString(36).substring(2, 8).toUpperCase(),
            status: 'final',
            patientId: currentPatient.id,
            studyInstanceUid: currentStudy.id,
            findings: findings,
            conclusion: conclusion,
            radiologist: signee,
            tenantId: 'HOSPITAL-ALPHA'
        }
    };
    
    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(eventEnvelope));
        
        reportStatusBadge.innerText = 'Finalized';
        reportStatusBadge.style.background = 'rgba(16, 185, 129, 0.15)';
        reportStatusBadge.style.color = '#10b981';
        reportStatusBadge.style.border = '1px solid rgba(16, 185, 129, 0.3)';
        
        btnSignReport.disabled = true;
        btnSignReport.classList.add('disabled');
        
        btnPrintReport.disabled = false;
        btnPrintReport.classList.remove('disabled');
        
        addSystemLog(`Report signed electronically by ${signee}.`);
        
        setTimeout(() => {
            fetchTimeline(currentPatient.id);
        }, 800);
    } else {
        alert('WebSocket connection lost. Action aborted.');
    }
});

// Print report listener
btnPrintReport.addEventListener('click', () => {
    if (!currentPatient || !currentStudy) return;
    
    document.getElementById('print-patient-name').innerText = currentPatient.name;
    document.getElementById('print-patient-id').innerText = currentPatient.id;
    document.getElementById('print-accession').innerText = currentStudy.accessionNumber || currentStudy.accession || 'ACC-9923';
    document.getElementById('print-modality').innerText = currentStudy.modality;
    document.getElementById('print-study-uid').innerText = currentStudy.id;
    document.getElementById('print-date').innerText = new Date().toLocaleString();
    
    document.getElementById('print-findings-text').innerText = reportFindings.value;
    document.getElementById('print-conclusion-text').innerText = reportConclusion.value;
    document.getElementById('print-radiologist').innerText = reportSignee.value;
    
    const randomHash = Array.from({length: 64}, () => Math.floor(Math.random()*16).toString(16)).join('');
    document.getElementById('print-report-hash').innerText = `GULA-SECURE-SHA256: ${randomHash.substring(0, 32)}...`;
    
    window.print();
});

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
    
    healthCheckInterval = setInterval(() => {
        checkHealth();
    }, 5000);
}

init();
