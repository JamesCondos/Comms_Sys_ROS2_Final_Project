# E2: queue-model calibration and measured arrival variability

This protocol separates simulated model calibration (E2A) from live ROS 2/HTTP
measurement (E2B). Neither dataset is pooled with E1. E9 has not yet selected a
gateway arrangement: at the user's request E2 proceeds with E1's T0_SHARED
arrangement held fixed. This is a documented ordering deviation, not E9 evidence.

## E2A: controlled M/M/1 model calibration

One FIFO queue and one worker are simulated by an exact discrete-event schedule.
Independent seeded exponential streams generate arrivals and service durations.
Configured service rate is 100/s. Utilisation targets 0.2, 0.5, 0.8 and 0.9 use
warm-up/measurement durations of 200/600, 300/1200, 500/2400 and 1000/4800
simulated seconds respectively, with five repetitions each. These are simulated
durations and timings, not elapsed execution time or measured REST performance.
The waiting queue capacity is 10,000; no-drop behaviour must be verified.

Compare run-level mean W, Wq, L, Lq and busy fraction with configured-rate M/M/1
predictions. Retain realised rate, interarrival/service CV, exponential quantile
and empirical-CDF diagnostics, serial correlations and arrival/service
correlations. Do not choose seeds or alter observations to obtain a theory fit.
Raw job and event records are preserved as named-array compressed NPZ files,
with readable per-second samples, configuration, provenance and summaries.

## E2B: live arrival-pattern comparison

Four actual ROS 2 nodes in one source process each publish command 2/s at 100
useful bytes, status 5/s at 100 bytes, odometry 20/s at 250 bytes, and sensor 5/s
at 20,000 bytes. Expected total load is 128 original messages/s. JSON/protocol
overhead is recorded separately from useful payload. All cases use one shared
FIFO of 1,000 waiting messages, one worker, one persistent HTTP/1.1 connection,
one central server, no retries, and the same unshaped isolated veth path in WSL.
The source schedule does not wait for responses. A fixed 4 ms server processing
hold is applied in every case; this is controlled service time, not CPU load.

The only intended change is arrival timing:

1. Periodic streams with deterministic staggered phases.
2. Periodic streams with synchronised initial phases.
3. Independent Poisson streams per source and message class.
4. Globally synchronised ON/OFF Poisson traffic: each 1 s cycle has a 0.2 s ON
   interval at five times each stream's base rate and a 0.8 s OFF interval.
   This deterministic duty-cycle model is not a Markov-modulated Poisson process.

All cases have the same expected mean class rates and payload-size distribution.
Finite Poisson runs have random realised counts and class proportions; show
these explicitly, rather than describing the finite counts as exactly matched.
Warm-up and measurement cover complete burst cycles. Five repetitions of each
condition use 20 s warm-up, 120 s measurement and up to 60 s drain. The 20-run
order is shuffled reproducibly. Separate feasibility pilots use 5/20/30 s and
are excluded from formal results. The pilot checks stable spare capacity, source
scheduling, message accounting and measurement integrity; it does not select a
preferred outcome ordering. Freeze final parameters after this check. Do not run
other experiment or report-generation workloads during formal timing collection.

## Boundaries, statistics and validation

Gateway system residence W runs from successful FIFO admission to response
handling completion: the sender has read the complete HTTP response body,
parsed the acknowledgement, and recorded completion while holding the queue
lock. A separate timestamp records the earlier response-body read completion.
Waiting Wq runs from admission to dequeue; service S runs from dequeue to the
same response handling completion. These differ from application latency,
which starts at the source timestamp and ends at response handling completion.
Atomic event timestamps and per-message timestamps share the same boundary.
Time-integrate system L, waiting Lq and worker busy state.

For a measurement interval of length T, use the exact finite-window identity:

    L = lambda_admitted * W_admission_cohort
        + (carry_in_area - post_end_tail_area) / T

The analogous identity applies to Lq and Wq. Preserve cohort drain observations,
window initial/final populations and arrival/departure conservation. The
boundary correction and residual are reported; a finite-window discrepancy is
not automatically a failure of Little's Law. Record mean, p95, p99, service
variation, source schedule lag, class deadline outcomes and all loss denominators.
Realistic mixed service is not assumed exponential; do not apply M/M/1 prediction
to E2B merely because one arrival pattern is Poisson.

Figures use means of five per-run statistics and observed minimum-to-maximum
bars. Percentiles are computed within each run, not pooled across repetitions.
These bars are not confidence intervals. First-repeat traces are selected by
repeat number, not favourable appearance. Each figure is a separate PDF without
lettered subpanels or equivalent-source labels.

Validate raw logs independently of the runners, including actual ROS node and
publisher counts, FIFO/concurrency, IDs, timestamp ordering, byte accounting,
clock identity, queue-area identities and all requested parameter settings.
Capture every live run, reconcile HTTP requests/responses with logs, preserve
capture-drop metadata, and verify owned namespace/interface/process cleanup.
Retain failed pilots with their reason. Preserve code snapshots, frozen configs,
raw records and validation outputs; hash the final copied archive.
