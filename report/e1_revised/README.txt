E1 revised report: actual source counts and controlled restrictions

Upload main.tex, e1_section.tex, e1_supplement.tex and figures/ to Overleaf.
Compile main.tex. The optional supplement explains all five extra historical
plots. Omit its input line (and the preceding appendix/clearpage lines) when
space requires the core report alone.
The standalone document uses the standard article class, graphicx and float:
no tau class, booktabs, external bibliography or custom fonts are required.
Overleaf supports both packages. Figures and tables use [H] placement so
production methods, plots and interpretations remain in narrative order.
To reuse it in another report, copy figures/ and input e1_section.tex in a
one-column section or appendix. The tables and inline topology are designed
for a full text width, not a narrow two-column paper column. A two-column
paper needs full-width tables/topology or a deliberate condensed adaptation.
Review section numbering and clear-page boundaries.

Every PDF contains one plot. Eight new figures also have PNG and SVG copies.
All 11 historical PDFs carry a historical_ prefix and unchanged measurements:
six in the core narrative and five in the optional historical appendix.
Historical load axes mean total mixed messages/s. Revised source-count axes
mean actual ROS 2 nodes. Both architectures retain one shared HTTP client.

For a space-limited paper, start with five figures: source_throughput,
source_latency, diagnostic_throughput, diagnostic_queue and diagnostic_stages.
Move CPU figures and historical comparisons/supporting plots to an appendix
as space permits. The complete document is a comprehensive E1 chapter with
all 19 plots, their production methods and interpretations; its full structure
is retained so that you can select material for the final paper.

tables/ contains run results, cell summaries, class sample counts and p95/p99,
all source/class generation rates, and additive mean latency components.
New error bars show the observed min-max of five run-level statistics;
historical figures retain three repetitions. They are not confidence intervals.
report_evidence.json records input hashes, counts and validation warnings.

Regenerate only after all 35 final runs pass independent validation:
python experiments/report_e1_revised.py results/e1_revised_complete --output report/e1_revised

The generator refuses incomplete/unvalidated data. It does not launch traffic
or modify raw results. Generation alone does not assert successful LaTeX
compilation; compile and inspect the rendered pages as a separate check.
