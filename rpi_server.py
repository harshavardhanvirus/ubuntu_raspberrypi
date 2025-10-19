#!/usr/bin/env python3
"""
pi_server.py — Remote-control server for Raspberry Pi / Ubuntu
Streams the screen and accepts mouse + keyboard control.

✅ No arguments needed — configure the constants below.
✅ Works with the controller_client.py you already have.
"""

import socket
import threading
import struct
import json
import time
import io
import logging
import zlib
import os
import subprocess
import re
from PIL import Image
import mss

# ==================== CONFIGURATION ====================

TOKEN = "S3CR3T"          # Must match the client's token
HOST = "0.0.0.0"          # Listen on all interfaces
PORT = 5000               # Port to use
DISPLAY = ":1"            # Your active X display (e.g., ":0" or ":1")
FRAME_SCALE = 0.7         # 0.7 = 70% size, adjust for performance
JPEG_QUALITY = 60         # 1–100 (higher = better quality, slower)
FRAME_DELAY = 0.05        # ~20 FPS (lower = faster stream)
# =======================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# === Utility functions ===
def send_json(sock, obj):
    data = json.dumps(obj).encode("utf8")
    sock.sendall(struct.pack("!I", len(data)))
    sock.sendall(data)

def recv_json_sock(sock):
    header = sock.recv(4)
    if not header:
        return None
    (n,) = struct.unpack("!I", header)
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            break
        data += chunk
    try:
        return json.loads(data.decode("utf8")), True
    except Exception:
        return data, False

def send_frame(sock, jpg_bytes):
    compressed = zlib.compress(jpg_bytes, level=6)
    sock.sendall(struct.pack("!I", len(compressed)))
    sock.sendall(compressed)

# === Detect screen resolution ===
def get_screen_size(display=DISPLAY):
    try:
        env = dict(os.environ)
        env["DISPLAY"] = display
        out = subprocess.check_output(["xdpyinfo"], env=env, text=True, stderr=subprocess.DEVNULL)
        m = re.search(r"dimensions:\s+(\d+)x(\d+)", out)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None, None

# === Xdotool control functions ===
def xdotool(cmd_args, display=DISPLAY):
    env = dict(os.environ)
    env["DISPLAY"] = display
    subprocess.run(["xdotool"] + cmd_args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def mouse_abs(x, y, btn, pressed, display=DISPLAY):
    xdotool(["mousemove", "--sync", str(x), str(y)], display)
    if pressed:
        xdotool(["mousedown", "1" if btn == "left" else "3"], display)
    else:
        xdotool(["mouseup", "1" if btn == "left" else "3"], display)

def key_action(key, pressed, display=DISPLAY):
    action = "keydown" if pressed else "keyup"
    xdotool([action, str(key)], display)

# === Main connection handler ===
def handle_client(conn, addr):
    logging.info(f"Client connected: {addr}")

    try:
        data, is_json = recv_json_sock(conn)
        if not is_json or not isinstance(data, dict) or data.get("auth") != TOKEN:
            logging.warning(f"Auth failed from {addr}")
            conn.close()
            return
        logging.info(f"Authenticated {addr}")

        # Send screen size to client
        w, h = get_screen_size()
        if w and h:
            send_json(conn, {"type": "screen_size", "width": w, "height": h})
            logging.info(f"Sent screen size {w}x{h}")
        else:
            logging.warning("Could not detect screen size")

        # Thread for control messages
        stop_event = threading.Event()

        def reader():
            while not stop_event.is_set():
                res = recv_json_sock(conn)
                if res is None:
                    break
                payload, is_json = res
                if not is_json:
                    continue
                typ = payload.get("type")
                if typ == "mouse_abs":
                    mouse_abs(int(payload["x"]), int(payload["y"]),
                              payload.get("button", "left"),
                              payload.get("pressed", True))
                elif typ == "mouse_button":
                    btn = 1 if payload.get("button") == "left" else 3
                    if payload.get("pressed"):
                        xdotool(["mousedown", str(btn)])
                    else:
                        xdotool(["mouseup", str(btn)])
                elif typ == "key":
                    key_action(payload.get("key"), payload.get("pressed", True))
                elif typ == "mouse_move":
                    dx, dy = payload.get("dx", 0), payload.get("dy", 0)
                    env = dict(os.environ); env["DISPLAY"] = DISPLAY
                    out = subprocess.check_output(["xdotool", "getmouselocation", "--shell"], env=env, text=True)
                    x = int(re.search(r"X=(\d+)", out).group(1))
                    y = int(re.search(r"Y=(\d+)", out).group(1))
                    xdotool(["mousemove", "--sync", str(x + dx), str(y + dy)], display=DISPLAY)

        threading.Thread(target=reader, daemon=True).start()

        # Stream frames
        with mss.mss() as sct:
            mon = sct.monitors[1]
            while True:
                img = sct.grab(mon)
                pil = Image.frombytes("RGB", img.size, img.rgb)
                pil = pil.resize((int(img.width * FRAME_SCALE), int(img.height * FRAME_SCALE)))
                buf = io.BytesIO()
                pil.save(buf, format="JPEG", quality=JPEG_QUALITY)
                send_frame(conn, buf.getvalue())
                time.sleep(FRAME_DELAY)
    except Exception as e:
        logging.exception(f"Client error: {e}")
    finally:
        try:
            conn.close()
        except:
            pass
        logging.info(f"Connection closed: {addr}")

# === Main server loop ===
def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, PORT))
    sock.listen(1)
    logging.info(f"Server started on {HOST}:{PORT}, display={DISPLAY}")
    logging.info(f"Auth token: {TOKEN}")

    try:
        while True:
            conn, addr = sock.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        logging.info("Shutting down.")
    finally:
        sock.close()

if __name__ == "__main__":
    main()
