"""
Fingerprint Attendance System — FastAPI Backend
Communicates with Arduino via USB Serial, serves REST API + SSE.
Run: uvicorn server:app --host 0.0.0.0 --port 8000 --reload
"""

import json
import csv
import os
import threading
import time
import asyncio
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

import serial
import serial.tools.list_ports
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    StreamingResponse,
    JSONResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─── Configuration ───
SERIAL_PORT = None  # Auto-detect, or set e.g. "/dev/ttyACM0" or "COM3"
SERIAL_BAUD = 9600
DATA_DIR = Path(__file__).parent / "data"
USERS_FILE = DATA_DIR / "users.json"
ATTENDANCE_FILE = DATA_DIR / "attendance.csv"
STATIC_DIR = Path(__file__).parent

# ─── Attendance Time Window ───
# Only scans between ATTENDANCE_START and LATE_CUTOFF are accepted.
# Scans after LATE_CUTOFF are declined (not logged) and reported as "late".
ATTENDANCE_START = dtime(2, 0)   # 02:00 AM  ← DEMO MODE (change back to 9,0 after demo)
LATE_CUTOFF      = dtime(2, 38)  # 02:30 AM  ← DEMO MODE (change back to 9,5 after demo)

# ─── Ensure data files exist ───
DATA_DIR.mkdir(exist_ok=True)

if not USERS_FILE.exists():
    USERS_FILE.write_text("[]", encoding="utf-8")

if not ATTENDANCE_FILE.exists():
    with open(ATTENDANCE_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "name", "timestamp"])

# ─── FastAPI App ───
app = FastAPI(title="Fingerprint Attendance System", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Pydantic Models ───
class UserCreate(BaseModel):
    id: int
    name: str

# ─── Global State ───
ser: Optional[serial.Serial] = None
serial_lock = threading.Lock()
sse_clients: list[asyncio.Queue] = []
sse_lock = threading.Lock()
serial_connected = False


# ─── Helper Functions ───
def load_users() -> list[dict]:
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def save_users(users: list[dict]):
    USERS_FILE.write_text(json.dumps(users, indent=2, ensure_ascii=False), encoding="utf-8")


def get_user_name(uid: int) -> str:
    users = load_users()
    for u in users:
        if u["id"] == uid:
            return u["name"]
    return f"ID-{uid}"


def append_attendance(uid: int, name: str, timestamp: str):
    with open(ATTENDANCE_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([uid, name, timestamp])


def load_attendance() -> list[dict]:
    rows = []
    try:
        with open(ATTENDANCE_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except FileNotFoundError:
        pass
    return rows


def send_to_arduino(data: dict):
    global ser
    if ser and ser.is_open:
        try:
            with serial_lock:
                msg = json.dumps(data) + "\n"
                ser.write(msg.encode("utf-8"))
                ser.flush()
        except Exception as e:
            print(f"[Serial TX Error] {e}")


def push_sse_event(event_type: str, data: dict):
    """Send an SSE event to all connected clients."""
    payload = {"type": event_type, **data}
    with sse_lock:
        for q in sse_clients:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass  # Drop if client is slow


# ─── Auto-detect Arduino Serial Port ───
def find_arduino_port() -> Optional[str]:
    ports = serial.tools.list_ports.comports()
    for p in ports:
        desc = (p.description or "").lower()
        mfg = (p.manufacturer or "").lower()
        if any(kw in desc for kw in ["arduino", "ch340", "cp210", "usb serial", "acm"]):
            return p.device
        if any(kw in mfg for kw in ["arduino", "wch", "silicon labs", "ftdi"]):
            return p.device
    # Fallback: try common ports
    for candidate in ["/dev/ttyACM0", "/dev/ttyUSB0", "/dev/ttyACM1", "/dev/ttyUSB1"]:
        if os.path.exists(candidate):
            return candidate
    return None


# ─── Serial Reader Thread ───
def serial_reader_thread():
    global ser, serial_connected

    while True:
        # Connect / reconnect
        if ser is None or not ser.is_open:
            serial_connected = False
            port = SERIAL_PORT or find_arduino_port()
            if port:
                try:
                    ser = serial.Serial(port, SERIAL_BAUD, timeout=1)
                    time.sleep(2)  # Wait for Arduino reset
                    serial_connected = True
                    print(f"[Serial] Connected to {port}")
                    push_sse_event("serial_status", {"connected": True, "port": port})
                except Exception as e:
                    print(f"[Serial] Connection failed on {port}: {e}")
                    time.sleep(3)
                    continue
            else:
                time.sleep(3)
                continue

        # Read lines
        try:
            if ser.in_waiting:
                line = ser.readline().decode("utf-8", errors="ignore").strip()
                if line:
                    process_arduino_message(line)
        except Exception as e:
            print(f"[Serial] Read error: {e}")
            serial_connected = False
            try:
                ser.close()
            except:
                pass
            ser = None
            time.sleep(2)

        time.sleep(0.05)


def process_arduino_message(line: str):
    print(f"[Arduino] {line}")
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return

    event = data.get("event")

    if event == "match":
        uid = data.get("id", -1)
        name = get_user_name(uid)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        now_time = datetime.now().time()

        # ─── Late cutoff check ───
        if now_time > LATE_CUTOFF:
            # Scan is after 9:15 AM — decline it
            send_to_arduino({"cmd": "display", "name": "Late!"})
            push_sse_event("late", {
                "id": uid,
                "name": name,
                "timestamp": timestamp,
                "cutoff": LATE_CUTOFF.strftime("%H:%M"),
            })
            print(f"[LATE] {name} (ID {uid}) scanned after cutoff at {timestamp} — declined")
            return

        # ─── On-time: log attendance ───
        append_attendance(uid, name, timestamp)

        # Send name to Arduino LCD
        send_to_arduino({"cmd": "display", "name": name})

        # Push SSE
        push_sse_event("attendance", {
            "id": uid,
            "name": name,
            "timestamp": timestamp,
            "confidence": data.get("confidence", 0),
        })
        print(f"[Attendance] {name} (ID {uid}) at {timestamp}")

    elif event == "no_match":
        push_sse_event("no_match", {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

    elif event == "enrolled":
        uid = data.get("id", -1)
        push_sse_event("enrolled", {"id": uid})
        print(f"[Enrolled] ID {uid}")

    elif event == "deleted":
        uid = data.get("id", -1)
        push_sse_event("deleted", {"id": uid})

    elif event == "error":
        msg = data.get("msg", "unknown")
        push_sse_event("error", {"msg": msg})
        print(f"[Arduino Error] {msg}")

    elif event == "ready":
        push_sse_event("ready", {})
        print("[Arduino] Sensor ready")


# ─── Start Serial Thread ───
serial_thread = threading.Thread(target=serial_reader_thread, daemon=True)
serial_thread.start()


# ─── API Routes ───

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(404, "index.html not found")
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


@app.get("/users")
async def get_users():
    return load_users()


@app.post("/users")
async def add_user(user: UserCreate):
    users = load_users()

    # Check for duplicate ID
    for u in users:
        if u["id"] == user.id:
            raise HTTPException(400, f"User with ID {user.id} already exists")

    users.append({"id": user.id, "name": user.name})
    save_users(users)

    # Send enroll command to Arduino
    send_to_arduino({"cmd": "enroll", "id": user.id})

    push_sse_event("user_added", {"id": user.id, "name": user.name})
    return {"status": "ok", "message": f"Enrolling {user.name} as ID {user.id}"}


@app.delete("/users/{user_id}")
async def delete_user(user_id: int):
    users = load_users()
    found = False
    new_users = []
    for u in users:
        if u["id"] == user_id:
            found = True
        else:
            new_users.append(u)

    if not found:
        raise HTTPException(404, f"User with ID {user_id} not found")

    save_users(new_users)

    # Send delete command to Arduino
    send_to_arduino({"cmd": "delete", "id": user_id})

    push_sse_event("user_deleted", {"id": user_id})
    return {"status": "ok", "message": f"Deleted user ID {user_id}"}


@app.get("/attendance")
async def get_attendance():
    return load_attendance()


@app.get("/export")
async def export_attendance():
    if not ATTENDANCE_FILE.exists():
        raise HTTPException(404, "No attendance data")
    return FileResponse(
        ATTENDANCE_FILE,
        media_type="text/csv",
        filename=f"attendance_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
    )


@app.get("/status")
async def get_status():
    return {
        "serial_connected": serial_connected,
        "serial_port": ser.port if ser and ser.is_open else None,
        "users_count": len(load_users()),
        "attendance_count": len(load_attendance()),
        "attendance_window": {
            "start": ATTENDANCE_START.strftime("%H:%M"),
            "cutoff": LATE_CUTOFF.strftime("%H:%M"),
        },
    }


@app.get("/events")
async def sse_endpoint(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)

    with sse_lock:
        sse_clients.append(queue)

    async def event_generator():
        try:
            # Send initial connection event
            yield f"data: {json.dumps({'type': 'connected', 'serial': serial_connected})}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield f": keepalive\n\n"
        finally:
            with sse_lock:
                if queue in sse_clients:
                    sse_clients.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
