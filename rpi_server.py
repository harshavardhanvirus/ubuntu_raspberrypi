#!/usr/bin/env python3
"""
Simple remote-control server for Raspberry Pi or Ubuntu
Streams screen and accepts mouse + keyboard inputs.
"""

import socket, threading, struct, json, time, io, zlib, subprocess, os, re, logging
from PIL import Image
import mss

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# === CONFIG ===
TOKEN   = "S3CR3T"       # must match client
HOST    = "0.0.0.0"
PORT    = 5000
DISPLAY = ":1"            # your X display (":0" or ":1")
# ===============

def send_json(sock, obj):
    data = json.dumps(obj).encode('utf8')
    sock.sendall(struct.pack('!I', len(data)))
    sock.sendall(data)

def recv_json(sock):
    header = sock.recv(4)
    if not header:
        return None
    (n,) = struct.unpack('!I', header)
    data = b''
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            break
        data += chunk
    try:
        return json.loads(data.decode('utf8'))
    except:
        return None

def send_frame(sock, jpg):
    compressed = zlib.compress(jpg, 6)
    sock.sendall(struct.pack('!I', len(compressed)))
    sock.sendall(compressed)

def get_screen_size():
    try:
        env = dict(os.environ)
        env["DISPLAY"] = DISPLAY
        out = subprocess.check_output(['xdpyinfo'], env=env, text=True)
        m = re.search(r'dimensions:\s+(\d+)x(\d+)', out)
        if m:
            return int(m.group(1)), int(m.group(2))
    except:
        pass
    return None, None

def xdotool(args):
    env = dict(os.environ)
    env['DISPLAY'] = DISPLAY
    subprocess.run(['xdotool'] + args, env=env)

def handle_client(conn, addr):
    logging.info(f"Client connected: {addr}")
    data = recv_json(conn)
    if not data or data.get('auth') != TOKEN:
        logging.warning("Auth failed")
        conn.close()
        return
    logging.info("Client authenticated")

    w, h = get_screen_size()
    if w and h:
        send_json(conn, {'type': 'screen_size', 'width': w, 'height': h})
        logging.info(f"Sent screen size {w}x{h}")

    stop = False

    def reader():
        nonlocal stop
        while not stop:
            try:
                header = conn.recv(4)
                if not header:
                    break
                (n,) = struct.unpack('!I', header)
                data = conn.recv(n)
                msg = json.loads(data.decode())
                typ = msg.get('type')
                if typ == 'mouse_abs':
                    x, y = int(msg['x']), int(msg['y'])
                    btn = msg.get('button', 'left')
                    pressed = msg.get('pressed', True)
                    b = '1' if btn == 'left' else '3'
                    xdotool(['mousemove', '--sync', str(x), str(y)])
                    xdotool(['mousedown' if pressed else 'mouseup', b])
                elif typ == 'key':
                    key = msg.get('key')
                    pressed = msg.get('pressed', True)
                    xdotool(['keydown' if pressed else 'keyup', str(key)])
            except Exception as e:
                logging.warning(f"Reader error: {e}")
                break
        stop = True

    threading.Thread(target=reader, daemon=True).start()

    with mss.mss() as sct:
        mon = sct.monitors[1]
        while not stop:
            img = sct.grab(mon)
            pil = Image.frombytes('RGB', img.size, img.rgb)
            pil = pil.resize((int(img.width * 0.7), int(img.height * 0.7)))
            buf = io.BytesIO()
            pil.save(buf, format='JPEG', quality=60)
            jpg = buf.getvalue()
            try:
                send_frame(conn, jpg)
            except:
                break
            time.sleep(0.05)
    conn.close()
    logging.info("Connection closed")

def main():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HOST, PORT))
    s.listen(1)
    logging.info(f"Server listening on {HOST}:{PORT}")
    while True:
        c, a = s.accept()
        threading.Thread(target=handle_client, args=(c,a), daemon=True).start()

if __name__ == '__main__':
    main()
