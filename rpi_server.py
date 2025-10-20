#!/usr/bin/env python3
"""
webrtc_server.py
Simple aiortc-based WebRTC server that captures the desktop and sends it to a browser.
Also listens on a DataChannel for input events (mouse_abs / key) and injects them locally.

Usage:
    DISPLAY=:1 python3 webrtc_server.py
Open http://<PI_IP>:8080 in your browser and click "Start".
"""

import asyncio
import json
import os
import logging
import argparse
from aiohttp import web
import aiohttp_jinja2, jinja2

from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaBlackhole
from aiortc.rtcrtpsender import RTCRtpSender
from av import VideoFrame

import mss
from PIL import Image, ImageDraw

import subprocess
import re
import time

logging.basicConfig(level=logging.INFO)
ROOT = os.path.dirname(__file__)

# ---------------- Config ----------------
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8080
DISPLAY_ENV = os.environ.get("DISPLAY", ":1")   # ensure you run with DISPLAY set
# ----------------------------------------

# aiohttp + jinja setup
app = web.Application()
aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(os.path.join(ROOT, "static")))

pcs = set()

# utility to inject input (xdotool path)
def inject_mouse_abs(x, y, button='left', pressed=True, display=DISPLAY_ENV):
    try:
        env = dict(os.environ)
        env['DISPLAY'] = display
        # move then down/up
        subprocess.run(['xdotool', 'mousemove', '--sync', str(x), str(y)], env=env)
        if pressed:
            subprocess.run(['xdotool', 'mousedown', '1' if button=='left' else '3'], env=env)
        else:
            subprocess.run(['xdotool', 'mouseup', '1' if button=='left' else '3'], env=env)
    except Exception as e:
        logging.exception("inject_mouse_abs failed: %s", e)

def inject_key(key, pressed=True, display=DISPLAY_ENV):
    try:
        env = dict(os.environ)
        env['DISPLAY'] = display
        if pressed:
            subprocess.run(['xdotool', 'keydown', str(key)], env=env)
        else:
            subprocess.run(['xdotool', 'keyup', str(key)], env=env)
    except Exception as e:
        logging.exception("inject_key failed: %s", e)

# ------------- Video Track ----------------
class ScreenTrack(VideoStreamTrack):
    """
    A VideoStreamTrack that captures the desktop using mss and yields VideoFrame objects.
    We overlay a simple cursor for demo purposes using PIL.
    """
    def __init__(self, display=DISPLAY_ENV, fps=15, scale=1.0):
        super().__init__()  # don't forget
        self.sct = mss.mss()
        self.monitor = self.sct.monitors[1]
        self.fps = fps
        self.frame_time = 1.0 / fps
        self.scale = scale
        self._last_ts = None

    async def recv(self):
        # adhere to required timing
        pts, time_base = await self.next_timestamp()
        # grab screen
        img = self.sct.grab(self.monitor)
        pil = Image.frombytes('RGB', img.size, img.rgb)
        # optionally scale down to reduce bandwidth
        if self.scale != 1.0:
            w = int(pil.width * self.scale)
            h = int(pil.height * self.scale)
            pil = pil.resize((w, h), Image.LANCZOS)

        # overlay cursor: get x,y via xdotool
        try:
            env = dict(os.environ); env['DISPLAY'] = DISPLAY_ENV
            out = subprocess.check_output(['xdotool', 'getmouselocation', '--shell'], env=env, text=True)
            m = re.search(r'X=(\d+)', out)
            n = re.search(r'Y=(\d+)', out)
            if m and n:
                cx = int(m.group(1))
                cy = int(n.group(1))
                # adjust for scaling
                if self.scale != 1.0:
                    cx = int(cx * self.scale)
                    cy = int(cy * self.scale)
                draw = ImageDraw.Draw(pil)
                # simple cursor marker (white circle with black border)
                r = max(2, int(pil.width * 0.01))  # radius relative to size
                draw.ellipse((cx-r, cy-r, cx+r, cy+r), fill=(255,255,255))
                draw.ellipse((cx-r-1, cy-r-1, cx+r+1, cy+r+1), outline=(0,0,0))
        except Exception:
            pass

        # convert to VideoFrame
        frame = VideoFrame.from_image(pil)
        frame.pts = pts
        frame.time_base = time_base
        return frame

# ------------- Web handlers ----------------
@aiohttp_jinja2.template('client.html')
async def index(request):
    return {}

async def offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params['sdp'], type=params['type'])

    pc = RTCPeerConnection()
    pcs.add(pc)
    logging.info("Created PC %s", pc)

    # prepare track and advertise width/height to client via DataChannel after negotiation
    screen = ScreenTrack()
    pc.addTrack(screen)

    # DataChannel handling
    @pc.on("datachannel")
    def on_datachannel(channel):
        logging.info("DataChannel created: %s", channel.label)

        @channel.on("message")
        def on_message(message):
            # expect JSON messages like {"type":"mouse_abs","x":100,"y":200,"button":"left","pressed":true}
            try:
                if isinstance(message, str):
                    obj = json.loads(message)
                else:
                    obj = json.loads(message.decode('utf8'))
            except Exception:
                logging.warning("Invalid DC message")
                return
            typ = obj.get('type')
            if typ == 'mouse_abs':
                x = int(obj.get('x',0)); y = int(obj.get('y',0))
                button = obj.get('button','left'); pressed = obj.get('pressed', True)
                logging.debug("Inject mouse_abs %s,%s %s %s", x, y, button, pressed)
                inject_mouse_abs(x, y, button, pressed, display=DISPLAY_ENV)
            elif typ == 'key':
                key = obj.get('key'); pressed = obj.get('pressed', True)
                inject_key(key, pressed, display=DISPLAY_ENV)
            else:
                logging.debug("DC unknown type %s", typ)

    # set remote description
    await pc.setRemoteDescription(offer)
    # create answer
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.json_response({'sdp': pc.localDescription.sdp, 'type': pc.localDescription.type})

async def on_shutdown(app):
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()

# routes
app.router.add_get('/', index)
app.router.add_post('/offer', offer)
app.on_shutdown.append(on_shutdown)
app.router.add_static('/static/', path=os.path.join(ROOT, 'static'), name='static')

# server run
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default=LISTEN_HOST)
    parser.add_argument('--port', type=int, default=LISTEN_PORT)
    parser.add_argument('--display', default=DISPLAY_ENV)
    args = parser.parse_args()
    DISPLAY_ENV = args.display
    logging.info("Starting server on %s:%d (DISPLAY=%s)", args.host, args.port, DISPLAY_ENV)
    web.run_app(app, host=args.host, port=args.port)
