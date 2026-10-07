# Five-minute baseline packet evidence

`two_robot_full.pcap` is the untruncated, full-payload capture from the separate
two-robot stability run `n002_r01_f1b14375`. The capture filter was loopback TCP
port 18061. It includes every logged HTTP exchange, including five seconds of
warm-up, and the initial TCP handshake. It does **not** include the final TCP
teardown packets; no FIN or RST was recorded before capture stopped.

The capture is supporting evidence for the stability test, not a capture of the
entire 21-run load sweep. `tcpdump.log` reports zero packets dropped by the kernel.

## Checks

- 117,127 captured packets, none truncated.
- One initial SYN and one server-recorded connection.
- 19,520 HTTP/1.1 POST start lines and 19,520 HTTP/1.1 200 response start lines.
- Those counts match all requests in the server log (warm-up plus measurement).
- The 300-second measurement cohort alone contains 19,200 messages, all delivered.
- Sequence-range accounting found 98 overlapping TCP payload bytes. They are
  counted in captured traffic but excluded from unique stream-byte accounting;
  their cause is not classified here.

## Byte accounting

| Component | Bytes |
|---|---:|
| Configured application payload | 64,477,000 |
| JSON request metadata | 4,362,596 |
| HTTP request headers and complete responses | 7,421,810 |
| TCP headers, including observed control packets | 3,748,092 |
| IPv4 headers | 2,342,540 |
| Overlapping TCP payload | 98 |
| **Total captured IPv4 bytes** | **82,352,136** |

Captured IPv4 traffic was **27.72% larger than the configured application payload**.
This comparison includes JSON metadata, HTTP responses and protocol headers in
both directions. It is not a statement that TCP/IP headers alone add 27.72%.

This is logical loopback accounting, affected by host packetisation/offload. It
does not measure a physical Ethernet link, Ethernet preambles/FCS/inter-frame
gaps, or the omitted final teardown packets. No interactive Wireshark inspection
is claimed. Open the pcap in Wireshark and decode TCP port 18061 as HTTP if needed.

Reproduce the figures in this table with:

```text
python experiments/packet_accounting.py results/e1_packet_evidence/two_robot_full.pcap results/e1_two_robot_validation
```

The script reads packet headers and performs TCP sequence-range accounting; it
does not reconstruct message bodies using a general TCP stream reassembler.
The generated numeric results are in `two_robot_full.json`.
