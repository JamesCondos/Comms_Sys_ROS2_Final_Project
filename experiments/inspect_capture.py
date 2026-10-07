"""Inspect an Ethernet/IPv4 TCP pcap without claiming full TCP reassembly.

Counts visible HTTP start lines and connection tuples in the captured prefix.
Does not estimate retransmissions, complete message counts or wire overhead.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import socket
import struct


def inspect(path):
    streams, requests, responses = Counter(), Counter(), Counter()
    syns, packets, truncated = 0, 0, 0
    first = last = None
    with path.open("rb") as f:
        magic = f.read(4)
        endian = {b"\xd4\xc3\xb2\xa1": "<", b"\xa1\xb2\xc3\xd4": ">"}.get(magic)
        if endian is None:
            raise ValueError("Expected microsecond-resolution classic pcap")
        header = f.read(20)
        linktype = struct.unpack(endian + "HHIIII", header)[-1]
        if linktype != 1:
            raise ValueError(f"Expected Ethernet capture, got linktype {linktype}")
        while raw := f.read(16):
            seconds, micros, captured, original = struct.unpack(endian + "IIII", raw)
            packet = f.read(captured)
            if len(packet) != captured:
                raise ValueError("Incomplete pcap packet")
            timestamp = seconds + micros / 1e6
            first = timestamp if first is None else first
            last = timestamp
            packets += 1
            truncated += captured < original
            if len(packet) < 54 or packet[12:14] != b"\x08\x00":
                continue
            ip = packet[14:]
            if ip[9] != 6:
                continue
            tcp = ip[(ip[0] & 15) * 4:]
            sport, dport = struct.unpack("!HH", tcp[:4])
            src, dst = socket.inet_ntoa(ip[12:16]), socket.inet_ntoa(ip[16:20])
            key = f"{src}:{sport}<->{dst}:{dport}" if dport == 18061 else f"{dst}:{dport}<->{src}:{sport}"
            streams[key] += 1
            syns += bool(tcp[13] & 2) and not bool(tcp[13] & 16)
            body = tcp[(tcp[12] >> 4) * 4:]
            if body.startswith(b"POST "):
                requests[body.split(b"\r\n", 1)[0].decode("ascii", errors="replace")] += 1
            if body.startswith(b"HTTP/1.1 "):
                responses[body.split(b"\r\n", 1)[0].decode("ascii", errors="replace")] += 1
    result = {"capture": str(path), "packets": packets, "truncated_packets": truncated,
              "duration_s": last - first if first is not None else 0,
              "tcp_streams": dict(streams), "initial_syn_packets": syns,
              "visible_request_start_lines": dict(requests),
              "visible_response_start_lines": dict(responses),
              "limitations": "Truncated packet prefixes, no TCP reassembly; not a message counter or retransmission/overhead analysis."}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("pcap", type=Path)
    args = parser.parse_args()
    result = inspect(args.pcap)
    args.pcap.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "visible_request_start_lines"}, indent=2))
