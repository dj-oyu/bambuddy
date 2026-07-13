"""Watch the printer's raw MQTT report for hw_switch_state (toolhead filament switch).

Diagnostic for filament-load failures (see 2026-07-13 runout incident): prints a
timestamped line whenever hw_switch_state, ams.tray_now, ams_status or the nozzle
temperatures change, so you can see exactly how far a load attempt gets
(switch=1 for a few seconds then back to 0 = filament reached the toolhead
sensor but was retracted and the load abandoned).

Usage:
    python3 scripts/watch_hw_switch.py [printer_id] [duration_seconds]

Reads the printer's connection info (IP / access code / serial) from the
bambuddy DB — credentials are never printed. DB path from $BAMBUDDY_DB or the
production default below.
"""
import json
import os
import sqlite3
import ssl
import sys
import time

import paho.mqtt.client as mqtt

DB = os.environ.get("BAMBUDDY_DB", "/mnt/petcam-data/bambuddy/data/bambuddy.db")
PRINTER_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 1
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 600.0

row = sqlite3.connect(DB).execute(
    "SELECT ip_address, access_code, serial_number FROM printers WHERE id=?",
    (PRINTER_ID,),
).fetchone()
if row is None:
    sys.exit(f"printer id {PRINTER_ID} not found in {DB}")
ip, access_code, serial = row

seen = {}


def on_connect(client, userdata, flags, rc, props=None):
    client.subscribe(f"device/{serial}/report")
    client.publish(
        f"device/{serial}/request",
        json.dumps({"pushing": {"sequence_id": "1", "command": "pushall"}}),
    )
    print("connected, watching hw_switch_state / tray_now ...", flush=True)


def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload)
    except Exception:
        return
    p = data.get("print", {})
    interesting = {}
    for tk in ("nozzle_temper", "nozzle_target_temper"):
        if tk in p:
            interesting[tk] = p[tk]
    if "hw_switch_state" in p:
        interesting["hw_switch_state"] = p["hw_switch_state"]
    ams = p.get("ams")
    if isinstance(ams, dict) and "tray_now" in ams:
        interesting["tray_now"] = ams["tray_now"]
    if "ams_status" in p:
        interesting["ams_status"] = p["ams_status"]
    for k, v in interesting.items():
        if seen.get(k) != v:
            seen[k] = v
            print(f"{time.strftime('%H:%M:%S')} {k} = {v}", flush=True)


c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
c.username_pw_set("bblp", access_code)
c.tls_set(cert_reqs=ssl.CERT_NONE)
c.tls_insecure_set(True)
c.on_connect = on_connect
c.on_message = on_message
c.connect(ip, 8883, 60)
c.loop_start()
try:
    time.sleep(DURATION)
finally:
    c.loop_stop()
print("done", flush=True)
