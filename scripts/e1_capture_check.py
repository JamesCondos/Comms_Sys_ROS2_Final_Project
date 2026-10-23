"""Reassemble one E1 HTTP/1.1 capture and compare requests with application logs.

Reports captured IPv4/TCP bytes on a virtual Ethernet interface. These are not
physical wire bytes. Retransmitted/overlapping sequence ranges are counted, not
treated as an authoritative packet-loss measurement.
"""

import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
import socket
import struct


def reassemble(initial_sequence, segments):
    """Join one directional TCP byte stream, rejecting gaps or conflicting data."""
    ordered = sorted((((sequence - initial_sequence - 1) % (2**32), payload)
                      for sequence, payload in segments), key=lambda item: item[0])
    result = bytearray()
    overlap = 0
    for offset, payload in ordered:
        if offset > len(result):
            raise ValueError(f"TCP capture gap: expected byte {len(result)}, next segment at {offset}")
        shared = min(len(payload), len(result) - offset)
        if shared:
            if result[offset:offset + shared] != payload[:shared]:
                raise ValueError("Conflicting overlapping TCP payload")
            overlap += shared
        result.extend(payload[shared:])
    return bytes(result), overlap


def http_messages(stream, request):
    """Parse Content-Length HTTP/1.1 messages (the E1 sender's actual framing)."""
    messages = []
    offset = 0
    while offset < len(stream):
        end = stream.find(b"\r\n\r\n", offset)
        if end < 0:
            raise ValueError("Incomplete HTTP header at end of stream")
        lines = stream[offset:end].decode("ascii").split("\r\n")
        if request:
            method, path, version = lines[0].split(" ")
            if method != "POST" or version != "HTTP/1.1":
                raise ValueError("Unexpected HTTP request: " + lines[0])
        else:
            if not lines[0].startswith("HTTP/1.1 200 "):
                raise ValueError("Unexpected HTTP response: " + lines[0])
            path = None
        headers = dict(line.split(":", 1) for line in lines[1:])
        headers = {key.lower(): value.strip() for key, value in headers.items()}
        if "transfer-encoding" in headers:
            raise ValueError("This E1 parser expects Content-Length, not transfer encoding")
        length = int(headers["content-length"])
        body = stream[end + 4:end + 4 + length]
        if len(body) != length:
            raise ValueError(f"Incomplete HTTP message body after {len(messages)} complete messages: "
                             f"expected {length} bytes, captured {len(body)}; request_direction={request}")
        document = json.loads(body)
        if request and path != f"/robots/{document['robot_id']}/{document['message_type']}":
            raise ValueError("Captured request URI does not match body identity")
        messages.append({"id": document["id"], "body_bytes": length,
                         "payload_bytes": len(document["payload"]) if request else None,
                         "header_bytes": end + 4 - offset})
        offset = end + 4 + length
    return messages


def inspect(folder, output=None):
    folder = Path(folder)
    config = json.loads((folder / "config.json").read_text())
    capture_metadata = json.loads((folder / "capture_metadata.json").read_text())
    pcap = folder / capture_metadata["capture_file"]
    streams = defaultdict(lambda: {"initial_sequence": None, "segments": []})
    totals = defaultdict(int)
    first = last = None
    with pcap.open("rb") as handle:
        magic = handle.read(4)
        formats = {b"\xd4\xc3\xb2\xa1": ("<", 1e6), b"\xa1\xb2\xc3\xd4": (">", 1e6),
                   b"\x4d\x3c\xb2\xa1": ("<", 1e9), b"\xa1\xb2\x3c\x4d": (">", 1e9)}
        endian, scale = formats[magic]
        global_header = struct.unpack(endian + "HHIIII", handle.read(20))
        if global_header[-1] != 1:
            raise ValueError("Expected Ethernet-link-type pcap from the dedicated veth")
        while True:
            record = handle.read(16)
            if not record:
                break
            sec, subsec, captured, original = struct.unpack(endian + "IIII", record)
            packet = handle.read(captured)
            if len(packet) != captured or captured < original:
                raise ValueError("Truncated packet capture")
            timestamp = sec + subsec / scale
            first = timestamp if first is None else first
            last = timestamp
            totals["captured_frames"] += 1
            totals["captured_frame_bytes"] += captured
            totals["truncated_frames"] += captured < original
            if packet[12:14] != b"\x08\x00":
                totals["non_ipv4_frames"] += 1
                continue
            ip = packet[14:]
            ihl = (ip[0] & 15) * 4
            length = struct.unpack("!H", ip[2:4])[0]
            if ip[9] != 6 or len(ip) < length:
                raise ValueError("Unexpected non-TCP or truncated IPv4 packet")
            if struct.unpack("!H", ip[6:8])[0] & 0x3FFF:
                raise ValueError("IPv4 fragments are not supported by this E1 checker")
            tcp = ip[ihl:length]
            sport, dport, sequence = struct.unpack("!HHI", tcp[:8])
            if config["server_port"] not in (sport, dport):
                raise ValueError("Capture contains unrelated TCP traffic")
            thl = (tcp[12] >> 4) * 4
            payload = tcp[thl:]
            key = (socket.inet_ntoa(ip[12:16]), sport, socket.inet_ntoa(ip[16:20]), dport)
            stream = streams[key]
            flags = tcp[13]
            if flags & 2:
                if stream["initial_sequence"] not in (None, sequence):
                    raise ValueError("Multiple TCP connections reuse the same tuple")
                stream["initial_sequence"] = sequence
                totals["syn_packets"] += 1
                if not flags & 16:
                    totals["initial_syn_packets"] += 1
            totals["fin_packets"] += bool(flags & 1)
            totals["rst_packets"] += bool(flags & 4)
            totals["ipv4_packets"] += 1
            totals["ipv4_bytes"] += length
            totals["ipv4_header_bytes"] += ihl
            totals["tcp_header_bytes"] += thl
            totals["tcp_payload_bytes"] += len(payload)
            direction = "upload" if dport == config["server_port"] else "response"
            totals[direction + "_ipv4_bytes"] += length
            if payload:
                stream["segments"].append((sequence, payload))
    requests, responses = [], []
    for key, stream in streams.items():
        if stream["initial_sequence"] is None:
            raise ValueError("Missing TCP handshake; cannot verify complete byte stream")
        contents, overlap = reassemble(stream["initial_sequence"], stream["segments"])
        totals["unique_tcp_payload_bytes"] += len(contents)
        totals["overlapping_tcp_payload_bytes"] += overlap
        if key[3] == config["server_port"]:
            requests.extend(http_messages(contents, request=True))
        else:
            responses.extend(http_messages(contents, request=False))
    with (folder / "server.csv").open(newline="") as handle:
        server = list(csv.DictReader(handle))
    with (folder / "messages.csv").open(newline="") as handle:
        messages = list(csv.DictReader(handle))
    expected_ids = [row["id"] for row in server]
    requests_match = [row["id"] for row in requests] == expected_ids
    responses_match = [row["id"] for row in responses] == expected_ids
    successful_ids = [row["id"] for row in messages if row["outcome"] == "ok"]
    application_matches = sorted(successful_ids) == sorted(expected_ids)
    body_bytes_match = sum(row["body_bytes"] for row in requests) == sum(int(row["request_body_bytes"]) for row in server)
    totals["application_payload_bytes"] = sum(row["payload_bytes"] for row in requests)
    totals["json_request_body_bytes"] = sum(row["body_bytes"] for row in requests)
    checks = {"requests_match_server_order": requests_match, "responses_match_server_order": responses_match,
              "successful_application_ids_match": application_matches, "request_body_bytes_match": body_bytes_match,
              "one_tcp_connection": len(streams) == 2,
              "capture_reports_zero_drops": capture_metadata.get("capture_drops") == 0,
              "capture_exited_cleanly": capture_metadata.get("exit_code") == 0,
              "ipv4_accounting_consistent": totals["ipv4_bytes"] == totals["ipv4_header_bytes"] + totals["tcp_header_bytes"] + totals["tcp_payload_bytes"]}
    result = {"passed": all(checks.values()), "checks": checks, "totals": dict(totals),
              "capture_link_type": global_header[-1], "capture_link_type_name": "Ethernet (DLT_EN10MB)",
              "capture_snap_length": global_header[-2], "capture_drops": capture_metadata.get("capture_drops"),
              "http_versions": ["HTTP/1.1"],
              "flows": [{"source_ip": key[0], "source_port": key[1], "destination_ip": key[2],
                         "destination_port": key[3], "initial_sequence": stream["initial_sequence"],
                         "payload_segments": len(stream["segments"])} for key, stream in streams.items()],
              "captured_http_requests": len(requests), "captured_http_responses": len(responses),
              "logged_server_requests": len(server), "capture_duration_s": None if first is None else last - first,
              "capture_close_observed": totals["fin_packets"] >= 2 or totals["rst_packets"] > 0,
              "scope": "All warm-up, measurement and drain HTTP exchanges; each packet captured once on gateway veth; captured IPv4 and virtual Ethernet bytes, not physical wire bytes",
              "overlap_note": "Overlapping TCP sequence bytes are not classified as network loss or retransmission cause",
              "capture_drop_note": "Kernel capture drops concern observation; zero capture drops does not prove zero network drops"}
    if output is not None:
        Path(output).write_text(json.dumps(result, indent=2) + "\n")
    if not result["passed"]:
        raise ValueError("Capture verification failed: " + str(checks))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_folder", type=Path)
    parser.add_argument("--output", type=Path, help="Optional explicit JSON output file; otherwise print only")
    args = parser.parse_args()
    print(json.dumps(inspect(args.run_folder, args.output), indent=2))


if __name__ == "__main__":
    main()
