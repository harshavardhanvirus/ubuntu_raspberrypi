#!/usr/bin/env python3
# pi_server.py
# Run on Raspberry Pi (Ubuntu). Streams screen and accepts JSON control messages to inject input.
# Usage: sudo python3 pi_server.py --host 0.0.0.0 --port 5000 --token S3CR3T

import socket
import threading
import argparse
import struct
import json
import time
import io
import logging
from PIL import Image
import mss
import zlib
import os
import subprocess

# Optional: evdev for uinput injection (preferable)
USE_EVDEV = True
try:
    from evdev import UInput, ecodes as e
except Exception:
    USE_EVDEV = False
    logging.info("evdev not available, will fallback to xdotool for input injection (X11 only)")

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

class InputInjector:
    def __init__(self):
        if USE_EVDEV:
            caps = {
                e.EV_KEY: [i for i in range(1, 0x2ff)],
                e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL],
            }
            try:
                self.ui = UInput(caps, name="py-remote-uinput")
                logging.info("UInput created")
            except Exception as ex:
                logging.exception("Failed to create UInput, falling back to xdotool")
                self.ui = None
        else:
            self.ui = None

    def mouse_move(self, dx, dy):
        if self.ui:
            self.ui.write(e.EV_REL, e.REL_X, int(dx))
            self.ui.write(e.EV_REL, e.REL_Y, int(dy))
            self.ui.syn()
        else:
            # fallback: use xdotool to move relative (requires X11)
            subprocess.call(['xdotool', 'mousemove_relative', '--', str(int(dx)), str(int(dy))])

    def mouse_button(self, button, pressed):
        if button == 'left': b = '1'
        elif button == 'right': b = '3'
        else: b = '2'
        action = 'mousedown' if pressed else 'mouseup'
        if self.ui:
            code = {'left': e.BTN_LEFT, 'right': e.BTN_RIGHT, 'middle': e.BTN_MIDDLE}.get(button, e.BTN_LEFT)
            self.ui.write(e.EV_KEY, code, 1 if pressed else 0)
            self.ui.syn()
        else:
            subprocess.call(['xdotool', action, b])

    def key_event(self, key, pressed):
        # very naive mapping: for letters & basic keys, use xdotool fallback if needed
        if self.ui:
            # map some keys simply; extend as needed
            keyname = key.lower()
            if len(keyname) == 1:
                # a..z
                code = getattr(e, 'KEY_' + keyname.upper(), None)
            else:
                code = None
            if code:
                self.ui.write(e.EV_KEY, code, 1 if pressed else 0)
                self.ui.syn()
                return
        # fallback to xdotool
        if pressed:
            subprocess.call(['xdotool', 'keydown', key])
        else:
            subprocess.call(['xdotool', 'keyup', key])

injector = InputInjector()

# frame sending helpers
def send_frame(sock, jpeg_bytes):
    # send: 4-byte length, then zlib-compressed jpeg
    compressed = zlib.compress(jpeg_bytes, level=6)
    sock.sendall(struct.pack('!I', len(compressed)))
    sock.sendall(compressed)

def recv_json(sock):
    # each JSON message is length-prefixed with 4 bytes
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
    return json.loads(data.decode('utf8'))

def send_json(sock, obj):
    data = json.dumps(obj).encode('utf8')
    sock.sendall(struct.pack('!I', len(data)))
    sock.sendall(data)

def handle_client(conn, addr, token):
    logging.info("Client connected %s", addr)
    try:
        # expect auth JSON first
        auth = recv_json(conn)
        if not auth or auth.get('auth') != token:
            logging.warning("Auth failed from %s", addr)
            conn.close()
            return
        logging.info("Authenticated %s", addr)

        # start a thread to read incoming control messages
        def reader():
            while True:
                try:
                    msg = recv_json(conn)
                    if msg is None:
                        break
                    typ = msg.get('type')
                    if typ == 'mouse_move':
                        injector.mouse_move(msg.get('dx',0), msg.get('dy',0))
                    elif typ == 'mouse_button':
                        injector.mouse_button(msg.get('button','left'), msg.get('pressed', True))
                    elif typ == 'key':
                        injector.key_event(msg.get('key'), msg.get('pressed', True))
                except Exception as ex:
                    logging.exception("Reader error")
                    break
            logging.info("Reader thread ending")
        t = threading.Thread(target=reader, daemon=True)
        t.start()

        # main loop: capture screen and send frames
        with mss.mss() as sct:
            monitor = sct.monitors[1]  # primary
            while True:
                img = sct.grab(monitor)
                pil = Image.frombytes('RGB', img.size, img.rgb)
                # resize to reduce bandwidth (optional)
                pil = pil.resize((int(img.width*0.6), int(img.height*0.6)))
                buf = io.BytesIO()
                pil.save(buf, format='JPEG', quality=60)
                jpg = buf.getvalue()
                send_frame(conn, jpg)
                time.sleep(0.05)
    except Exception as ex:
        logging.exception("Client handler error")
    finally:
        try:
            conn.close()
        except:
            pass
        logging.info("Connection closed %s", addr)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--token', required=True)
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((args.host, args.port))
    sock.listen(1)
    logging.info("Server listening on %s:%d", args.host, args.port)
    while True:
        conn, addr = sock.accept()
        threading.Thread(target=handle_client, args=(conn, addr, args.token), daemon=True).start()

if __name__ == '__main__':
    main()
