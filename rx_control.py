#!/usr/bin/env python3
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

FIXED_MCS_PATH = Path("/sys/module/morse/parameters/fixed_mcs")
FIXED_RATE_PATH = Path("/sys/module/morse/parameters/enable_fixed_rate")


def write_sysfs(path: Path, value: str) -> None:
    path.write_text(value)


def set_fixed_rate_enabled() -> None:
    write_sysfs(FIXED_RATE_PATH, "Y")


def set_rx_mcs(mcs: int) -> None:
    set_fixed_rate_enabled()
    write_sysfs(FIXED_MCS_PATH, str(mcs))


def read_rx_mcs() -> str:
    try:
        return FIXED_MCS_PATH.read_text().strip()
    except Exception:
        return ""


def start_iperf_server(port: int) -> subprocess.Popen:
    # Keep it simple: run iperf3 server forever
    # Log goes to /tmp so you can inspect later if needed.
    logf = open("/tmp/iperf3_server.log", "a", buffering=1)
    return subprocess.Popen(["iperf3", "-s", "-p", str(port)], stdout=logf, stderr=logf)


def main():
    if len(sys.argv) < 3:
        print("Usage: sudo ./rx_control.py <bind_ip> <ctrl_port> [iperf_port]", file=sys.stderr)
        print("Example: sudo ./rx_control.py 0.0.0.0 9999 5201", file=sys.stderr)
        sys.exit(1)

    bind_ip = sys.argv[1]
    ctrl_port = int(sys.argv[2])
    iperf_port = int(sys.argv[3]) if len(sys.argv) >= 4 else 5201

    if not FIXED_MCS_PATH.exists() or not FIXED_RATE_PATH.exists():
        print("Morse sysfs paths not found. Is the morse module loaded?", file=sys.stderr)
        sys.exit(1)

    print(f"[rx] Starting iperf3 server on port {iperf_port}")
    iperf_proc = start_iperf_server(iperf_port)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((bind_ip, ctrl_port))
    sock.settimeout(None)

    current = read_rx_mcs()
    print(f"[rx] Control listening on {bind_ip}:{ctrl_port}, current_mcs={current}")

    while True:
        data, addr = sock.recvfrom(4096)
        try:
            msg = json.loads(data.decode("utf-8", errors="replace"))
        except Exception:
            continue

        cmd = msg.get("cmd")
        seq = msg.get("seq")

        reply = {"ok": True, "seq": seq, "rx_mcs": read_rx_mcs(), "ts": time.time()}

        try:
            if cmd == "set_rx_mcs":
                mcs = int(msg.get("mcs"))
                set_rx_mcs(mcs)
                reply["rx_mcs"] = read_rx_mcs()

            elif cmd == "set_rx_fixed_rate":
                enabled = msg.get("enabled", True)
                if enabled:
                    set_fixed_rate_enabled()
                else:
                    write_sysfs(FIXED_RATE_PATH, "N")
                reply["fixed_rate"] = FIXED_RATE_PATH.read_text().strip()
                print(f"[rx] fixed_rate set to {'Y' if enabled else 'N'}")

            elif cmd == "ping":
                pass

            elif cmd == "stop":
                reply["stopping"] = True
                sock.sendto(json.dumps(reply).encode("utf-8"), addr)
                break

            else:
                reply = {"ok": False, "seq": seq, "error": "unknown_cmd", "ts": time.time()}

        except Exception as e:
            reply = {"ok": False, "seq": seq, "error": str(e), "ts": time.time()}

        sock.sendto(json.dumps(reply).encode("utf-8"), addr)

    try:
        iperf_proc.terminate()
    except Exception:
        pass


if __name__ == "__main__":
    main()
