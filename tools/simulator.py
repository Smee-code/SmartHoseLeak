"""
Stand-in for the ESP32.

Posts the same JSON payload the firmware sends, so the dashboards, the event
log, the notifications and the charts can all be exercised without the rig
powered up. Useful for development and for rehearsing the defence.

    python tools/simulator.py --key <device api key>
    python tools/simulator.py --key <key> --leak 2      # force a leak on M2

Press Ctrl+C to stop.
"""

import argparse
import json
import math
import random
import sys
import time
import urllib.error
import urllib.request

# Mirrors the firmware constants so simulated data behaves like real data.
P_LOW_THRESHOLD_BAR = 1.5
FLOW_ZERO_THRESHOLD_LPM = 0.3
RESIDUAL_LEAK_THRESHOLD = 0.18
REQUIRED_CONSECUTIVE = 15

BASELINE_PRESSURE = {1: 2.6, 2: 2.3, 3: 2.0}
K_CONDUCTANCE = {1: 3.1, 2: 2.8, 3: 2.5}


class Module:
    """Reproduces one module of the firmware: readings, classify, hysteresis."""

    def __init__(self, index):
        self.index = index
        self.k = K_CONDUCTANCE[index]
        self.status = "NORMAL"
        self.candidate = "NORMAL"
        self.consecutive = 0
        self.mode = "normal"  # normal | leak | low_supply

    def sample(self, tick):
        base = BASELINE_PRESSURE[self.index]
        # A slow diurnal swing plus sensor noise, so charts do not look flat.
        wave = 0.15 * math.sin(tick / 90.0 + self.index)
        noise = random.gauss(0, 0.02)

        if self.mode == "leak":
            # A leak bleeds pressure while flow runs higher than the
            # pressure-flow curve predicts - the firmware residual test.
            pressure = max(0.2, base - 0.55 + wave + noise)
            expected = self.k * math.sqrt(pressure)
            flow = expected * random.uniform(1.35, 1.6)
        elif self.mode == "low_supply":
            pressure = max(0.1, 1.1 + wave * 0.5 + noise)
            expected = self.k * math.sqrt(pressure)
            flow = expected * random.uniform(0.95, 1.05)
        else:
            pressure = max(0.1, base + wave + noise)
            expected = self.k * math.sqrt(pressure)
            flow = expected * random.uniform(0.92, 1.08)

        return round(pressure, 3), round(flow, 3)

    def classify(self, pressure, flow):
        expected = self.k * math.sqrt(max(pressure, 0.0))
        residual = (flow - expected) / expected if expected > 0.05 else 0.0

        if pressure < P_LOW_THRESHOLD_BAR:
            if abs(residual) < RESIDUAL_LEAK_THRESHOLD:
                return "LOW_SUPPLY"
            if residual > RESIDUAL_LEAK_THRESHOLD:
                return "POSSIBLE_LEAK"
        elif residual > RESIDUAL_LEAK_THRESHOLD:
            return "POSSIBLE_LEAK"
        return "NORMAL"

    def step(self, tick):
        pressure, flow = self.sample(tick)
        candidate = self.classify(pressure, flow)

        if candidate == self.candidate:
            self.consecutive += 1
        else:
            self.candidate = candidate
            self.consecutive = 1

        if self.consecutive >= REQUIRED_CONSECUTIVE and self.candidate != self.status:
            self.status = self.candidate

        return {
            "module": self.index,
            "pressure_bar": pressure,
            "flow_lpm": flow,
            "k": self.k,
            "status": self.status,
            "calibrating": False,
            "simulated": True,
        }


def post(url, key, payload, timeout=8):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Device-Key": key},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Simulate the 3-module ESP32 rig.")
    parser.add_argument("--url", default="http://127.0.0.1:8000/api/ingest/")
    parser.add_argument("--key", required=True, help="Device API key from `manage.py setup_demo`.")
    parser.add_argument("--device-id", default="esp32-01")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between posts.")
    parser.add_argument("--leak", type=int, choices=[1, 2, 3], help="Hold this module in a leak condition.")
    parser.add_argument("--low", type=int, choices=[1, 2, 3], help="Hold this module in low supply.")
    parser.add_argument(
        "--random-events",
        action="store_true",
        help="Occasionally drift a random module into a leak and back, for a hands-off demo.",
    )
    args = parser.parse_args()

    modules = {i: Module(i) for i in (1, 2, 3)}
    if args.leak:
        modules[args.leak].mode = "leak"
    if args.low:
        modules[args.low].mode = "low_supply"

    print(f"Posting to {args.url} every {args.interval}s. Ctrl+C to stop.")
    tick = 0
    try:
        while True:
            tick += 1

            if args.random_events and tick % 60 == 0:
                target = modules[random.choice([1, 2, 3])]
                target.mode = "leak" if target.mode == "normal" else "normal"
                print(f"  [sim] module {target.index} -> {target.mode}")

            payload = {
                "device_id": args.device_id,
                "readings": [modules[i].step(tick) for i in (1, 2, 3)],
            }
            try:
                result = post(args.url, args.key, payload)
                states = " | ".join(
                    f"M{r['module']} {r['pressure_bar']:.2f}bar {r['flow_lpm']:.2f}L/min {r['status']}"
                    for r in payload["readings"]
                )
                flag = " <-- EVENT" if result.get("events") else ""
                print(f"[{tick:5d}] {states}{flag}")
                for error in result.get("errors", []):
                    print(f"        server: {error}")
            except urllib.error.HTTPError as exc:
                print(f"[{tick:5d}] HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}")
            except urllib.error.URLError as exc:
                print(f"[{tick:5d}] cannot reach server: {exc.reason}")

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
