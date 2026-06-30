import os
import json
from typing import List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import pika
import threading
import asyncio
import time

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
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                pass

manager = ConnectionManager()

# Background RabbitMQ-to-WebSocket Event Bridge
def rabbitmq_bridge_thread(loop):
    rabbitmq_url = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
    print(f"gula-gateway: Production mode active. Connecting to RabbitMQ at {rabbitmq_url}")
    while True:
        try:
            params = pika.URLParameters(rabbitmq_url)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.exchange_declare(exchange='gula.events', exchange_type='topic', durable=True)
            
            # Temporary queue for gateway websocket broadcast
            result = channel.queue_declare(queue='', exclusive=True)
            queue_name = result.method.queue
            channel.queue_bind(exchange='gula.events', queue=queue_name, routing_key='gula.event.#')
            
            def callback(ch, method, properties, body):
                msg = body.decode('utf-8')
                # Route safely back to the FastAPI main thread loop
                asyncio.run_coroutine_threadsafe(manager.broadcast(msg), loop)
                
            channel.basic_consume(queue=queue_name, on_message_callback=callback, auto_ack=True)
            print("gula-gateway: Connected to RabbitMQ. Bridging all events to WebSockets.")
            channel.start_consuming()
        except Exception as e:
            print(f"gula-gateway: RabbitMQ connection error in bridge thread: {e}. Reconnecting in 3s...")
            time.sleep(3)

@app.on_event("startup")
def startup_event():
    # If RabbitMQ url is configured, launch the listener bridge thread
    if os.getenv("RABBITMQ_URL"):
        loop = asyncio.get_event_loop()
        t = threading.Thread(target=rabbitmq_bridge_thread, args=(loop,), daemon=True)
        t.start()

# Dynamic Reverse Proxy Routing
async def proxy_request(service_url: str, path: str, request: Request) -> Response:
    if path.endswith("/"):
        path = path[:-1]
    method = request.method
    req_body = await request.body()
    req_headers = dict(request.headers)
    
    # Clean headers that conflict with forwarding
    req_headers.pop("host", None)
    req_headers.pop("content-length", None)
    
    target_url = f"{service_url}/{path}"
    
    async with httpx.AsyncClient() as client:
        try:
            res = await client.request(
                method,
                target_url,
                content=req_body,
                headers=req_headers,
                params=request.query_params,
                timeout=30.0
            )
            return Response(
                content=res.content,
                status_code=res.status_code,
                headers=dict(res.headers)
            )
        except Exception as e:
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
            data = await websocket.receive_text()
            try:
                event = json.loads(data)
                print(f"gula-gateway: [Event Bus Routing] Routing event '{event.get('eventType')}' from '{event.get('source')}'")
            except Exception:
                pass
                
            # Broadcast to all connected websockets
            await manager.broadcast(data, sender=websocket)
            
            # If in RabbitMQ mode, publish it to RabbitMQ exchange as well!
            rabbitmq_url = os.getenv("RABBITMQ_URL")
            if rabbitmq_url:
                try:
                    event = json.loads(data)
                    event_type = event.get("eventType")
                    routing_key = f"gula.event.{event_type}"
                    params = pika.URLParameters(rabbitmq_url)
                    conn = pika.BlockingConnection(params)
                    ch = conn.channel()
                    ch.basic_publish(
                        exchange='gula.events',
                        routing_key=routing_key,
                        body=data
                    )
                    conn.close()
                except Exception as ex:
                    print(f"gula-gateway: Failed to publish WS event to RabbitMQ: {ex}")
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# Serve Dashboard static files
@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard():
    static_html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(static_html_path, "r") as f:
        return HTMLResponse(content=f.read())

# Mount static files
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, log_level="info")
