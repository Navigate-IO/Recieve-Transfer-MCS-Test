#!/usr/bin/env python3
import csv
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

# Morse sysfs params (local TX side)
FIXED_MCS_PATH = Path("/sys/module/morse/parameters/fixed_mcs")
FIXED_RATE_PATH = Path("/sys/module/morse/parameters/enable_fixed_rate")

# MCS sweep order
MCS_LIST = [10, 0, 1, 2, 3, 4, 5, 6, 7]

# One-time wait at the very beginning (seconds)
INITIAL_WAIT = int(os.getenv("INITIAL_WAIT", "0"))

# Optional settle delay between steps (seconds). Set GUARD=0 to disable.
GUARD = int(os.getenv("GUARD", "0"))

# Seconds to wait after disabling fixed rate so the 802.11ah rate controller
# can converge before we start pumping traffic.
AUTO_RATE_SETTLE = int(os.getenv("AUTO_RATE_SETTLE", "5"))

# Seconds to wait after restarting iperf3 server so it can bind.
IPERF_RESTART_WAIT = int(os.getenv("IPERF_RESTART_WAIT", "2"))

# MAC address to block for forced relay topology. Leave empty to disable.
BLOCK_MAC = os.getenv("BLOCK_MAC", "")

# Seconds to wait after re-deleting direct path for BATMAN to reconverge.
REBLOCK_SETTLE = int(os.getenv("REBLOCK_SETTLE", "5"))

# Parse iperf3 receiver summary line like:
# ... 39.1 Mbits/sec  receiver
RX_SUMMARY_RE = re.compile(r"\s(\d+(?:\.\d+)?)\s+([KMG]?bits/sec)\s+receiver\s*$")


def write_sysfs(path: Path, value: str) -> None:
    path.write_text(value)


def set_tx_mcs(mcs: int) -> None:
    write_sysfs(FIXED_RATE_PATH, "Y")
    write_sysfs(FIXED_MCS_PATH, str(mcs))


def read_tx_mcs() -> str:
    try:
        return FIXED_MCS_PATH.read_text().strip()
    except Exception:
        return ""


def reblock_direct_path() -> None:
    """Re-delete direct mesh path/station to maintain forced relay topology."""
    if not BLOCK_MAC:
        return
    subprocess.run(["iw", "dev", "wlan0", "mpath", "del", BLOCK_MAC],
                   capture_output=True)
    subprocess.run(["iw", "dev", "wlan0", "station", "del", BLOCK_MAC],
                   capture_output=True)
    print(f"[tx] Re-blocked direct path to {BLOCK_MAC}, settling {REBLOCK_SETTLE}s")
    time.sleep(REBLOCK_SETTLE)


def udp_call(
    sock: socket.socket,
    rx_addr: Tuple[str, int],
    payload: Dict[str, Any],
    timeout_s: float = 2.0,
    retries: int = 999999,
) -> Optional[Dict[str, Any]]:
    """
    Sends a UDP JSON message and waits for an ACK with matching seq.
    Retries many times by default so field tests can start whenever the link comes up.
    """
    data = json.dumps(payload).encode("utf-8")
    sock.settimeout(timeout_s)

    for _ in range(retries):
        try:
            sock.sendto(data, rx_addr)
            resp, _ = sock.recvfrom(4096)
            msg = json.loads(resp.decode("utf-8", errors="replace"))
            if msg.get("seq") == payload.get("seq"):
                return msg
        except socket.timeout:
            continue
        except Exception:
            continue
    return None


def restart_iperf_server(
    sock: socket.socket,
    rx_addr: Tuple[str, int],
    seq: int,
) -> Tuple[int, bool]:
    """Ask the RX side to restart its iperf3 server. Returns (new_seq, ok)."""
    resp = udp_call(sock, rx_addr, {"cmd": "restart_iperf", "seq": seq}, timeout_s=2.0)
    ok = resp is not None and resp.get("ok", False)
    if ok:
        print(f"[tx] Receiver ACK: iperf3 server restarted")
    else:
        print(f"[tx] WARNING: iperf3 restart request failed: {resp}")
    time.sleep(IPERF_RESTART_WAIT)
    return seq + 1, ok


def run_iperf(server_ip: str, port: int, duration: int, hard_timeout: int) -> Tuple[str, bool]:
    """
    Runs iperf3 with a hard timeout to prevent hangs when the link is bad.
    Returns (stdout+stderr, ok)
    """
    try:
        out = subprocess.check_output(
            ["timeout", str(hard_timeout), "iperf3", "-c", server_ip, "-p", str(port), "-t", str(duration)],
            stderr=subprocess.STDOUT,
            text=True,
        )
        return out, True
    except subprocess.CalledProcessError as e:
        return (e.output or ""), False


def parse_receiver_throughput(iperf_out: str) -> Tuple[str, str]:
    lines = iperf_out.splitlines()
    for line in reversed(lines):
        m = RX_SUMMARY_RE.search(line)
        if m:
            return m.group(1), m.group(2)
    return "", ""


def main() -> None:
    if len(sys.argv) < 5:
        print(
            "Usage: sudo env INITIAL_WAIT=600 GUARD=0 python3 tx_matrix.py <rx_ip> <ctrl_port> <iperf_port> <iperf_duration_s>",
            file=sys.stderr,
        )
        print(
            "Example: sudo env INITIAL_WAIT=600 GUARD=0 python3 tx_matrix.py 192.168.50.2 9999 5201 60",
            file=sys.stderr,
        )
        sys.exit(1)

    rx_ip = sys.argv[1]
    ctrl_port = int(sys.argv[2])
    iperf_port = int(sys.argv[3])
    duration = int(sys.argv[4])

    if not FIXED_MCS_PATH.exists() or not FIXED_RATE_PATH.exists():
        print("Morse sysfs paths not found. Is the morse module loaded on this Pi?", file=sys.stderr)
        sys.exit(1)

    out_dir = f"mcs_matrix_{time.strftime('%Y%m%d_%H%M%S')}"
    csv_path = f"{out_dir}/results.csv"
    raw_path = f"{out_dir}/raw.log"
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    rx_addr = (rx_ip, ctrl_port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    seq = 1

    with open(csv_path, "w", newline="") as fcsv, open(raw_path, "w") as flog:
        writer = csv.writer(fcsv)
        writer.writerow(["rx_mcs", "tx_mcs", "throughput", "unit", "ok", "timestamp"])

        print(f"[tx] Receiver control: {rx_ip}:{ctrl_port}")
        print(f"[tx] iperf target:     {rx_ip}:{iperf_port}")
        print(f"[tx] duration:         {duration}s")
        print(f"[tx] initial_wait:     {INITIAL_WAIT}s")
        print(f"[tx] guard:            {GUARD}s")
        print(f"[tx] auto_rate_settle: {AUTO_RATE_SETTLE}s")
        print(f"[tx] iperf_restart_wait: {IPERF_RESTART_WAIT}s")
        print(f"[tx] block_mac:        {BLOCK_MAC or '(none)'}")
        print(f"[tx] reblock_settle:   {REBLOCK_SETTLE}s")
        print(f"[tx] CSV:              {csv_path}")

        if INITIAL_WAIT > 0:
            print(f"[tx] Initial wait: sleeping {INITIAL_WAIT}s so you can place nodes")
            time.sleep(INITIAL_WAIT)

        # =============================================================
        # Auto-rate baseline test (enable_fixed_rate = N on both sides)
        # =============================================================
        print("\n[tx] === Auto-rate baseline (enable_fixed_rate=N) ===")
        write_sysfs(FIXED_RATE_PATH, "N")
        reblock_direct_path()

        resp = udp_call(sock, rx_addr, {"cmd": "set_rx_fixed_rate", "enabled": False, "seq": seq}, timeout_s=2.0)
        seq += 1
        if resp and resp.get("ok"):
            print(f"[tx] Receiver ACK, fixed_rate disabled (fixed_rate={resp.get('fixed_rate')})")
        else:
            print(f"[tx] Receiver failed to disable fixed_rate: {resp}")

        # Let the 802.11ah rate controller converge before pushing traffic
        print(f"[tx] Waiting {AUTO_RATE_SETTLE}s for rate controller to settle")
        time.sleep(AUTO_RATE_SETTLE)

        if GUARD > 0:
            time.sleep(GUARD)

        # Restart iperf3 server to avoid stale session issues
        seq, _ = restart_iperf_server(sock, rx_addr, seq)

        print(f"[tx] Auto-rate -> iperf ({duration}s)")
        iperf_out, ok = run_iperf(rx_ip, iperf_port, duration, hard_timeout=duration + 30)

        flog.write("\n" + "=" * 70 + "\n")
        flog.write(f"rx_mcs=auto tx_mcs=auto ok={ok} ts={time.time()}\n")
        flog.write(iperf_out + "\n")

        thr, unit = parse_receiver_throughput(iperf_out)
        if thr and unit:
            print(f"[tx] Result: MCS(rx=auto, tx=auto) = {thr} {unit}")
        else:
            print(f"[tx] Result: MCS(rx=auto, tx=auto) = (parse failed)")

        writer.writerow(["auto", "auto", thr, unit, "1" if ok else "0", time.strftime("%Y-%m-%d %H:%M:%S")])
        fcsv.flush()

        if GUARD > 0:
            time.sleep(GUARD)

        # =============================================================
        # Re-enable fixed rate on both sides for the MCS sweep
        # =============================================================
        print("\n[tx] Re-enabling fixed rate for MCS sweep")
        write_sysfs(FIXED_RATE_PATH, "Y")
        reblock_direct_path()

        resp = udp_call(sock, rx_addr, {"cmd": "set_rx_fixed_rate", "enabled": True, "seq": seq}, timeout_s=2.0)
        seq += 1
        if resp and resp.get("ok"):
            print(f"[tx] Receiver ACK, fixed_rate re-enabled (fixed_rate={resp.get('fixed_rate')})")
        else:
            print(f"[tx] Receiver failed to re-enable fixed_rate: {resp}")

        # =============================================================
        # Fixed-MCS matrix sweep
        # =============================================================
        for rx_mcs in MCS_LIST:
            print(f"\n[tx] Setting receiver MCS to {rx_mcs} (retries until ACK)")
            resp = udp_call(sock, rx_addr, {"cmd": "set_rx_mcs", "mcs": rx_mcs, "seq": seq}, timeout_s=2.0)
            seq += 1

            if not resp or not resp.get("ok"):
                print(f"[tx] Receiver failed to set MCS {rx_mcs}: {resp}")
            else:
                print(f"[tx] Receiver ACK, rx_mcs now {resp.get('rx_mcs')}")

            if GUARD > 0:
                time.sleep(GUARD)

            for tx_mcs in MCS_LIST:
                set_tx_mcs(tx_mcs)
                reblock_direct_path()
                tx_readback = read_tx_mcs()
                print(f"[tx] RX {rx_mcs} | TX {tx_mcs} (readback {tx_readback}) -> iperf")

                if GUARD > 0:
                    time.sleep(GUARD)

                # Restart iperf3 server before each test to avoid stale sessions
                seq, _ = restart_iperf_server(sock, rx_addr, seq)

                iperf_out, ok = run_iperf(rx_ip, iperf_port, duration, hard_timeout=duration + 30)

                flog.write("\n" + "=" * 70 + "\n")
                flog.write(f"rx_mcs={rx_mcs} tx_mcs={tx_mcs} ok={ok} ts={time.time()}\n")
                flog.write(iperf_out + "\n")

                thr, unit = parse_receiver_throughput(iperf_out)
                if thr and unit:
                    print(f"[tx] Result: MCS(rx={rx_mcs}, tx={tx_mcs}) = {thr} {unit}")
                else:
                    print(f"[tx] Result: MCS(rx={rx_mcs}, tx={tx_mcs}) = (parse failed)")

                writer.writerow([rx_mcs, tx_mcs, thr, unit, "1" if ok else "0", time.strftime("%Y-%m-%d %H:%M:%S")])
                fcsv.flush()

                if GUARD > 0:
                    time.sleep(GUARD)

    # =============================================================
    # Reset both sides to MCS 10 (max range) after sweep
    # =============================================================
    set_tx_mcs(10)
    resp = udp_call(sock, rx_addr, {"cmd": "set_rx_mcs", "mcs": 10, "seq": seq}, timeout_s=2.0)
    seq += 1
    print("\n[tx] Reset both sides to MCS 10 for max range")

    print(f"\n[tx] Done. Results saved in: {out_dir}/")
    print(f"[tx] Table: {csv_path}")
    print(f"[tx] Raw:   {raw_path}")


if __name__ == "__main__":
    main()
