# Representative packet evidence

`representative.pcap` contains the first 20,000 packets of the formal sweep's
16-robot, repeat-1 run (`n016_r01_9aa78a6c`). Capture was restricted to loopback TCP
port 18061, with a 256-byte snap length. It covers approximately 6.51 seconds,
including the connection handshake, warm-up and start of measurement. It is not a
capture of the entire sweep or entire first run.

`tcpdump.log` reports zero packets dropped by the capture kernel. The phrase
"packets received by filter" in that log is a capture-system counter and should not
be interpreted as an application-message count.

`representative.json` was produced by:

```text
python experiments/inspect_capture.py results/evidence/representative.pcap
```

The independent packet inspection found:

- One TCP connection tuple and one initial SYN.
- 3,333 visible POST request start lines using HTTP/1.1.
- 3,332 visible HTTP/1.1 200 OK response start lines.

The capture ends mid-conversation, so these counts are not a delivery-ratio
calculation. Many request bodies are truncated. The inspection script checks packet
prefixes, without TCP stream reassembly. The raw pcap can be opened in Wireshark
with TCP port 18061 decoded as HTTP. No interactive Wireshark inspection is claimed.

Full-run connection reuse is independently checked in every run's server log:
one accepted connection, with all requests recorded as HTTP/1.1.
