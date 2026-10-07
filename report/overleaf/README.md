# Revised Overleaf report template

This is an editable working draft based on the supplied tau template. It contains the completed E1 results and a plan for the remaining experiments. It is not a finished submission.

## Use in Overleaf

1. Keep the existing Overleaf project, especially its `tau-class/` folder and any class assets.
2. Replace its main document with `main.tex` from this folder and upload the supplied `figures/` folder alongside it. The ZIP contains those files with the same relative paths.
3. Retain your actual bibliography file and enable the corresponding `\addbibresource{...}` line. This draft supplies no invented references.
4. Compile in the existing project and review float placement, title spacing and any class-specific warnings. Resolve all `\draftnote{...}` markers before submission.

The tau class and its supporting assets were not attached and are not installed locally, so this package is not standalone and a full tau compilation was not verified. The supplied title and abstract mechanisms are preserved. Unused `multicol` and `listings` packages were removed; add `listings` back if you use code listings.

## Structural changes

Seven main sections replace the original thirteen: introduction, background, system design and implementation, methodology, results, discussion and limitations, conclusion. The traffic model is a table, metrics are defined once, and detailed configurations/API material belongs in the appendix.

Results use the workbook's IDs. The working sequence is E1, E4, E2, E3, E5, E6, E7, E8; E4 appears earlier because the daily schedule places queueing validation before the protocol comparison. Future results headings are comments until evidence exists. The experiment-plan table explicitly marks their status.

The abstract and conclusion currently describe E1 only. Rewrite both when the later experiments are finished. The title is shortened; author names and student IDs are preserved. There is no supplied page limit or assessment rubric, so the section lengths still need to be checked against course requirements.

## Figures and evidence

Each PDF contains one plot, with no lettered subfigure titles. The six main E1 plots use separate `figure` environments at `\columnwidth` in the two-column report. The two within-run variability plots appear as separate figures in the one-column appendix. Review their placement after compiling with the tau class. PDF files are copied directly from `results/e1_complete/figures` without alteration.

The supplied `figures/` folder contains all 11 standalone PDFs:

| File | Included in the draft |
|---|---|
| `fig1_latency.pdf` | Per-class p95 latency |
| `fig2_deadlines.pdf` | Per-class deadline failures |
| `fig3_queue.pdf` | Mean FIFO occupancy |
| `fig4_throughput.pdf` | Completed request throughput |
| `fig5_cpu.pdf` | Process CPU consumption |
| `fig6_busy_time.pdf` | Sender and server-handler occupied time |
| `appendix_within_run_cpu.pdf` | Appendix: CPU changes within one run |
| `appendix_within_run_queue.pdf` | Appendix: queue changes within one run |
| `appendix_goodput.pdf` | Optional additional evidence |
| `appendix_drops.pdf` | Optional additional evidence |
| `appendix_queue_trace.pdf` | Optional additional evidence |

Both the latency and deadline figures use robot-equivalent load on the bottom axis and nominal total mixed traffic on the top axis. Each coloured curve measures one class under the same combined workload. For example, at one equivalent, odometry generates 20 messages/s within a total of 32 messages/s. The queue, throughput, CPU and occupied-time figures explicitly label their horizontal axes as total mixed-traffic load.

All numerical E1 statements come from the completed dataset, principally `summary_table.csv`, `report_blurbs.md` and `variability_note.md`. Earlier pilot data are excluded. The draft explicitly distinguishes robot-equivalent loads from independent clients, successful-message latency from loss-inclusive deadline failures, and CPU consumption from occupied time. It reports the large repeat variation rather than claiming a stable capacity limit.
