"""Send home Wi-Fi details to WLED over USB (Improv serial protocol). You type the password; nothing is saved.

    python wled_wifi.py COM7 YourWiFiName          (Windows; on Linux the port is e.g. /dev/ttyACM0)
"""
import getpass, sys, time
import serial  # installed with esptool (pyserial)

if len(sys.argv) < 3:
    sys.exit(__doc__)
port, ssid = sys.argv[1], sys.argv[2]
password = getpass.getpass(f"Wi-Fi password for {ssid}: ")


def packet(ptype, data):
    body = b"IMPROV" + bytes([1, ptype, len(data)]) + data
    return body + bytes([sum(body) & 0xFF])


s, p = ssid.encode(), password.encode()
rpc = bytes([0x01, 2 + len(s) + len(p), len(s)]) + s + bytes([len(p)]) + p   # RPC 0x01 = send Wi-Fi settings
with serial.Serial(port, 115200, timeout=0.5) as ser:
    ser.dtr = False; ser.rts = False
    time.sleep(0.5); ser.reset_input_buffer()
    ser.write(packet(0x03, rpc))
    print("sent, waiting for WLED to join the network (up to 30 s)...")
    buf, end = b"", time.time() + 30
    while time.time() < end:
        buf += ser.read(512)
        i = buf.find(b"IMPROV")
        while i != -1 and len(buf) >= i + 9:
            ptype, ln = buf[i + 7], buf[i + 8]
            data = buf[i + 9:i + 9 + ln]
            if ptype == 0x01 and data:            # current state
                state = {2: "authorized", 3: "connecting / provisioning", 4: "connected"}.get(data[0], data[0])
                print("state:", state)
            elif ptype == 0x02 and data:          # error
                print("error code:", data[0], "(3 = could not connect: check the password)")
            elif ptype == 0x04 and len(data) > 2:  # RPC result: URL(s)
                url = data[3:3 + data[2]].decode(errors="replace")
                print("connected! WLED is at", url)
                sys.exit(0)
            buf = buf[i + 9 + ln:]
            i = buf.find(b"IMPROV")
    print("no confirmation received - check the WLED app or your router for a new device named WLED")
