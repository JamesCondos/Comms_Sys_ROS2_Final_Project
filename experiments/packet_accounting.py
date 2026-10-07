"""Count captured IPv4/TCP overhead and HTTP start lines for a complete run.

This reports logical loopback bytes, not Ethernet physical-wire utilisation.
No TCP reassembly is claimed. Directional TCP sequence intervals detect overlap.
"""
import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
import struct


def inspect(pcap, sweep):
    totals = defaultdict(int)
    spans = defaultdict(list)
    initial_sequence = {}
    first = last = None
    with pcap.open("rb") as f:
        magic = f.read(4)
        endian = {b"\xd4\xc3\xb2\xa1": "<", b"\xa1\xb2\xc3\xd4": ">"}[magic]
        header = f.read(20)
        assert struct.unpack(endian + "HHIIII", header)[-1] == 1, "Expected Ethernet-style loopback pcap"
        while record := f.read(16):
            sec, usec, captured, original = struct.unpack(endian + "IIII", record)
            packet = f.read(captured)
            assert len(packet) == captured
            first = sec + usec / 1e6 if first is None else first
            last = sec + usec / 1e6
            totals["captured_packets"] += 1
            totals["truncated_packets"] += captured < original
            if packet[12:14] != b"\x08\x00":
                continue
            ip = packet[14:]
            ihl, ip_bytes = (ip[0] & 15) * 4, struct.unpack("!H", ip[2:4])[0]
            assert ip[9] == 6 and len(ip) >= ip_bytes
            tcp = ip[ihl:ip_bytes]
            thl = (tcp[12] >> 4) * 4
            sport, dport, seq = struct.unpack("!HHI", tcp[:8])
            if tcp[13] & 2:
                initial_sequence.setdefault((sport, dport), seq)
            payload = tcp[thl:]
            totals["ipv4_bytes"] += ip_bytes
            totals["ipv4_header_bytes"] += ihl
            totals["tcp_header_bytes"] += thl
            totals["tcp_payload_bytes"] += len(payload)
            totals["initial_syns"] += bool(tcp[13] & 2) and not bool(tcp[13] & 16)
            totals["fin_packets"] += bool(tcp[13] & 1)
            totals["rst_packets"] += bool(tcp[13] & 4)
            totals["visible_http11_posts"] += payload.startswith(b"POST ") and b" HTTP/1.1\r\n" in payload
            totals["visible_http11_200s"] += payload.startswith(b"HTTP/1.1 200 OK\r\n")
            if payload:
                offset = (seq - initial_sequence[(sport, dport)]) % (2**32)
                spans[(sport, dport)].append((offset, offset + len(payload)))
    unique_bytes = 0
    for intervals in spans.values():
        end = -1
        for a, b in sorted(intervals):
            unique_bytes += max(0, b - max(a, end))
            end = max(end, b)
    totals["unique_tcp_payload_bytes"] = unique_bytes
    totals["overlapping_tcp_payload_bytes"] = totals["tcp_payload_bytes"] - unique_bytes
    folders = list(sweep.glob("*/server.csv"))
    assert len(folders) == 1, "Capture must correspond to exactly one stability run"
    with folders[0].open() as f:
        server = list(csv.DictReader(f))
    totals["server_requests"] = len(server)
    totals["application_payload_bytes"] = sum(int(r["payload_bytes"]) for r in server)
    totals["json_request_body_bytes"] = sum(int(r["request_body_bytes"]) for r in server)
    assert totals["truncated_packets"] == 0
    assert totals["initial_syns"] == 1
    assert totals["visible_http11_posts"] == len(server)
    assert totals["visible_http11_200s"] == len(server)
    assert totals["ipv4_bytes"] == totals["ipv4_header_bytes"] + totals["tcp_header_bytes"] + totals["tcp_payload_bytes"]
    result = dict(totals, run=folders[0].parent.name, capture_duration_s=last-first,
                  close_observed=totals["fin_packets"] >= 2 or totals["rst_packets"] > 0,
                  all_logged_http_exchanges_captured=True,
                  json_metadata_bytes=totals["json_request_body_bytes"] - totals["application_payload_bytes"],
                  http_headers_and_response_bytes=unique_bytes - totals["json_request_body_bytes"],
                  ipv4_overhead_relative_to_application_percent=100 * (totals["ipv4_bytes"] / totals["application_payload_bytes"] - 1),
                  scope="All logged stability HTTP exchanges, including warm-up and observed TCP control packets; loopback IPv4/TCP accounting, not physical Ethernet wire overhead")
    pcap.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("pcap", type=Path)
    parser.add_argument("sweep", type=Path)
    args = parser.parse_args()
    inspect(args.pcap, args.sweep)
