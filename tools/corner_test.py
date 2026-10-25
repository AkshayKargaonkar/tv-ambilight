"""Light the corner LEDs in WLED to check / measure your layout. Pauses the live TV feed while it runs.

    python3 corner_test.py wled-a1b2c3.local 33,21,33,21     # show corner markers for this layout
    python3 corner_test.py wled-a1b2c3.local --off            # back to the live TV feed

Markers (layout = top,right,bottom,left, clockwise from the TOP-LEFT corner):
    red      LED 1                 -> must sit exactly at the top-left corner
    green    last top / first right -> must straddle the top-right corner
    blue     last right / first bottom -> bottom-right corner
    yellow   last bottom / first left  -> bottom-left corner
    magenta  last LED              -> just below the top-left corner, on the left side

If a marker sits N LEDs past its corner, that side is N LEDs shorter; N LEDs before, N longer.
If red is off, change "Skip first LEDs" in WLED (Settings > LED Preferences) instead.
"""
import json, sys, urllib.request


def post(host, state):
    req = urllib.request.Request(f"http://{host}/json/state", data=json.dumps(state).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


if len(sys.argv) < 3:
    sys.exit(__doc__)
host = sys.argv[1]
if sys.argv[2] == "--off":
    print(post(host, {"lor": 0}))
    print("live TV feed resumed")
    sys.exit()

top, right, bottom, left = (int(v) for v in sys.argv[2].split(","))
n = top + right + bottom + left
c1, c2, c3 = top, top + right, top + right + bottom      # first LED after each corner (0-based)
leds = [0, n, "000000",
        0, 1, "FF0000",
        c1 - 1, c1 + 1, "00FF00",
        c2 - 1, c2 + 1, "0000FF",
        c3 - 1, c3 + 1, "FFC000",
        n - 1, n, "FF00FF"]
print(post(host, {"on": True, "bri": 160, "lor": 2, "seg": [{"id": 0, "fx": 0, "col": [[0, 0, 0]], "i": leds}]}))
print(f"corner markers on for {n} LEDs (top {top}, right {right}, bottom {bottom}, left {left})")
print(f"when done:  python3 {sys.argv[0]} {host} --off")
