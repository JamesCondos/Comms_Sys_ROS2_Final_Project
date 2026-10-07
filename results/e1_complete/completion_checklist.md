# E1 completion against the workbook

Scope: the workbook's E1 clean-network baseline study, plus the relevant two-robot
traffic, logging and stability evidence. This is not a declaration that every
preceding daily-plan milestone or the complete M2 gate has been achieved.

| Requirement | Evidence | Status |
|---|---|---|
| HTTP/1.1, persistent connection, FIFO, one server, CLEAN | Per-run config/server logs; CLEAN qdisc/ping checks | Complete |
| Increase load at 1/2/4/8 robots or equivalent rates | 1/2/4/8/16/32/64 equivalents using scaled rates across at most two real ROS2 namespaces | Complete |
| At least three fixed-duration repetitions | 21 runs, three per condition, 60-second measurement each | Complete |
| p50/p95/p99 and per-class latency | Summaries, summary_table.csv, class_deadlines.csv | Complete |
| Queue wait, mean/max occupancy, goodput, throughput | Raw stage timestamps, queue.csv and run summaries | Complete |
| Server utilisation | Raw cpu.csv, server CPU percentage, server-handler occupied fraction | Complete |
| Deadline misses | Frozen class targets, loss-inclusive denominators, separate tardiness counts | Complete |
| Required scaling plots | Figures 1–4 show latency, deadlines, queue occupancy and throughput; Figures 5–6 show resource usage | Complete |
| Reusable stress configuration | ../../experiments/configs/e1_stress.json | Complete |
| Dedicated ROS2 traffic package and topic separation | ../../ros2_ws/src/rest_traffic; topic manifests; callback identity checks | Complete |
| Two-robot five-minute verification | ../e1_two_robot_validation; 19,200 measured messages, zero losses/errors | Complete |
| Actual topic-rate checks | ../e1_two_robot_validation/topic_hz.txt and topic_verification.json | Complete |
| Packet/protocol and overhead evidence | ../e1_packet_evidence; every stability HTTP exchange captured and byte-accounted | Complete with capture limits documented |
| Raw data and provenance preserved | 175 original final-sweep files; source snapshots; config hashes; matching copied-file checksums | Complete |
| Report text and figure guidance | report_blurbs.md, figure_guide.md, figures in PNG/PDF/SVG | Complete |

## Validation

The final 21-run dataset accounts for 731,504 measured messages: 386,438 successful
completions and 345,066 FIFO admission drops. There were zero missing ROS2 messages,
zero HTTP request errors and zero messages pending after the drain. Dropped
messages were never transmitted as HTTP requests. All runs used one accepted
HTTP/1.1 connection. The three metric boundary/denominator unit tests passed.

All original copies were compared with their Linux SHA-256 manifests: 175 files
for the final sweep, 16 for stability verification, two original capture files and
22 for the new smoke tests. Derived reports and notes are additional files.

## Interpretation limits

High-load results varied substantially across repetitions. CPU time per message
changed across source, gateway and server processes during the sweep. No run was
removed or selectively replaced. There is no proven cause for the execution-speed
change and no justified single stable service-capacity estimate. See individual
run values and variability_note.md. The highest load with zero overflow in every
repeat was 16 equivalents; 64 equivalents overloaded the FIFO in every repeat.

The earlier single-topic `baseline_formal` dataset is preliminary. Do not combine
its values or figures with this namespaced, rate-scaled implementation.

## Remaining outside E1

DELAY/LOSS/BW impairment profiles, Little's Law/controlled arrivals, HTTP/2, priority,
ARQ, HTTP/3, multiple servers and adaptive control have not been implemented here.
Lecturer approval and Git milestone administration are not asserted as completed.
Run metadata stores the configuration hash; it is not repeated in every CSV row.
The server echoes the composite message ID containing the sequence, rather than
a separate JSON sequence-number field. Those are explicit limits against the
literal wording of earlier foundation rows, rather than missing E1 comparisons.
