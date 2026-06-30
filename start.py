import os
import sys
import subprocess
import time
import signal
import shutil

# Ports matrix
SERVICES = [
    {"name": "gula-gateway", "dir": "backend/gula-gateway", "script": "main.py", "port": 8000},
    {"name": "gula-auth", "dir": "backend/gula-auth", "script": "main.py", "port": 3001},
    {"name": "gula-study", "dir": "backend/gula-study", "script": "main.py", "port": 3002},
    {"name": "gula-patient", "dir": "backend/gula-patient", "script": "main.py", "port": 3003},
    {"name": "gula-ai", "dir": "backend/gula-ai", "script": "main.py", "port": 3004}
]

processes = []
log_files = []

def cleanup(sig=None, frame=None):
    print("\n[Start] Shutting down GULA microservices...")
    for p in processes:
        try:
            p.terminate()
            p.wait(timeout=2)
            print(f" - Terminated subprocess {p.pid}")
        except Exception:
            pass
            
    for f in log_files:
        try:
            f.close()
        except Exception:
            pass
            
    print("[Start] Cleanup complete. Goodbye!")
    sys.exit(0)

# Register shutdown handlers
signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

def check_venv():
    venv_dir = os.path.join(os.getcwd(), "venv")
    pip_path = os.path.join(venv_dir, "Scripts", "pip.exe") if sys.platform == "win32" else os.path.join(venv_dir, "bin", "pip")
    python_path = os.path.join(venv_dir, "Scripts", "python.exe") if sys.platform == "win32" else os.path.join(venv_dir, "bin", "python")
    
    if not os.path.exists(venv_dir):
        print("[Start] Creating Python Virtual Environment (venv)...")
        subprocess.run([sys.executable, "-m", "venv", "venv"], check=True)
        print("[Start] Virtual environment created.")
        
    print("[Start] Installing/Verifying dependencies in venv...")
    packages = ["fastapi", "uvicorn", "websockets", "httpx", "pyjwt", "bcrypt", "python-multipart", "requests"]
    subprocess.run([python_path, "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run([python_path, "-m", "pip", "install"] + packages, check=True)
    print("[Start] Dependency verification complete.")
    return python_path

def main():
    print("=" * 60)
    print(" GULA | Microservice Sandbox Bootstrapper")
    print("=" * 60)
    
    # 1. Clean databases and storage from previous runs to ensure clean test
    db_files = ["auth.db", "study.db", "patient.db"]
    for db in db_files:
        if os.path.exists(db):
            try:
                os.remove(db)
                print(f"[Start] Reset database: {db}")
            except Exception as e:
                print(f"[Start] Warning - Could not reset {db}: {e}")
                
    if os.path.exists("storage"):
        try:
            shutil.rmtree("storage")
            print("[Start] Reset storage directory.")
        except Exception as e:
            print(f"[Start] Warning - Could not reset storage directory: {e}")
            
    # 2. Setup Venv
    python_path = check_venv()
    
    # Create logs directory
    if not os.path.exists("logs"):
        os.makedirs("logs")
        
    # 3. Launch Gateway first (WS Broker)
    gateway = SERVICES[0]
    print(f"[Start] Launching {gateway['name']} on port {gateway['port']}...")
    gw_log = open(f"logs/{gateway['name']}.log", "w")
    log_files.append(gw_log)
    
    gw_script_path = os.path.join(gateway['dir'], gateway['script'])
    p = subprocess.Popen(
        [python_path, gw_script_path],
        cwd=os.getcwd(),
        stdout=gw_log,
        stderr=subprocess.STDOUT
    )
    processes.append(p)
    
    # Wait for gateway WS broker to start
    time.sleep(2)
    
    # 4. Launch rest of the microservices
    for svc in SERVICES[1:]:
        print(f"[Start] Launching {svc['name']} on port {svc['port']}...")
        log_f = open(f"logs/{svc['name']}.log", "w")
        log_files.append(log_f)
        
        script_path = os.path.join(svc['dir'], svc['script'])
        sp = subprocess.Popen(
            [python_path, script_path],
            cwd=os.getcwd(),
            stdout=log_f,
            stderr=subprocess.STDOUT
        )
        processes.append(sp)
        time.sleep(1)
        
    print("\n" + "=" * 60)
    print(" GULA RUNNING!")
    print(" Access Developer Dashboard at: http://localhost:8000/dashboard")
    print(" Logs are stored in ./logs/")
    print(" Press Ctrl+C to terminate all services.")
    print("=" * 60 + "\n")
    
    # Keep main script alive, monitor child processes
    while True:
        try:
            for p in processes:
                if p.poll() is not None:
                    print(f"\n[Start] Warning: A subprocess has terminated (PID: {p.pid}). Triggering cleanup...")
                    cleanup()
            time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            cleanup()

if __name__ == "__main__":
    main()
