import os
import json
from typing import List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx

app = FastAPI(title="GULA API Gateway")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Local microservice ports
AUTH_PORT = 3001
STUDY_PORT = 3002
PATIENT_PORT = 3003
AI_PORT = 3004

AUTH_SERVICE = f"http://127.0.0.1:{AUTH_PORT}"
STUDY_SERVICE = f"http://127.0.0.1:{STUDY_PORT}"
PATIENT_SERVICE = f"http://127.0.0.1:{PATIENT_PORT}"
AI_SERVICE = f"http://127.0.0.1:{AI_PORT}"

# WebSocket connections registry (Acts as Event Bus Broker)
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        print(f"gula-gateway: Client connected to Event Bus. Active connections: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        print(f"gula-gateway: Client disconnected from Event Bus. Active connections: {len(self.active_connections)}")

    async def broadcast(self, message: str, sender: WebSocket = None):
        # Broadcast message to all connected clients
        for connection in self.active_connections:
            # We broadcast to everyone including sender (so dashboard and services see all events)
            try:
                await connection.send_text(message)
            except Exception:
                pass

manager = ConnectionManager()

# Dynamic Reverse Proxy Routing
async def proxy_request(service_url: str, path: str, request: Request) -> Response:
    if path.endswith("/"):
        path = path[:-1]
    method = request.method
    req_body = await request.body()
    req_headers = dict(request.headers)
    
    req_headers.pop("host", None)
    req_headers.pop("content-length", None)
    
    async with httpx.AsyncClient() as client:
        try:
            res = await client.request(
                method=method,
                url=f"{service_url}/{path}",
                headers=req_headers,
                content=req_body,
                params=dict(request.query_params),
                timeout=60.0
            )
            content_type = res.headers.get("content-type", "application/json")
            return Response(
                content=res.content,
                status_code=res.status_code,
                headers={"Content-Type": content_type}
            )
        except httpx.RequestError as e:
            return Response(
                content=json.dumps({"error": f"Gateway proxy failure to {service_url}: {str(e)}"}),
                status_code=502,
                headers={"Content-Type": "application/json"}
            )

# Routes definitions
@app.api_route("/api/auth/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def auth_proxy(path: str, request: Request):
    return await proxy_request(AUTH_SERVICE, f"api/auth/{path}", request)

@app.api_route("/api/patients/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def patient_proxy(path: str, request: Request):
    return await proxy_request(PATIENT_SERVICE, f"api/patients/{path}", request)

@app.api_route("/dicomweb/{path:path}", methods=["GET", "POST"])
async def study_proxy(path: str, request: Request):
    return await proxy_request(STUDY_SERVICE, f"dicomweb/{path}", request)

@app.api_route("/api/ai/{path:path}", methods=["GET", "POST"])
async def ai_proxy(path: str, request: Request):
    return await proxy_request(AI_SERVICE, f"api/ai/{path}", request)

# WebSocket Endpoint (Event Bus Broker Hub)
@app.websocket("/ws/events")
async def websocket_events(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Listen for events published by microservices or client UI
            data = await websocket.receive_text()
            try:
                # Log event routing in gateway stdout
                event = json.loads(data)
                print(f"gula-gateway: [Event Bus Routing] Routing event '{event.get('eventType')}' from '{event.get('source')}'")
            except Exception:
                pass
            # Broadcast the event payload to all subscribers
            await manager.broadcast(data, sender=websocket)
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# Serve Dashboard static files
@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard():
    static_html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(static_html_path, "r") as f:
        return HTMLResponse(content=f.read())

# Mount static files (dashboard assets)
static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
def read_root():
    return {"message": "Welcome to GULA API Gateway. Navigate to /dashboard for developer portal."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, log_level="info")
