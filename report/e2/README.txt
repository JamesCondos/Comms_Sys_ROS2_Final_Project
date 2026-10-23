E2 report package

Upload this directory to Overleaf and choose main.tex as the main document.
Compile twice with pdfLaTeX. e2_section.tex can be included in another report; copy the figures directory and bibliography entries too.

The reusable section requires graphicx, float and amsmath in the receiving document's preamble.
Copy bibliography entries little1961 and mitqueue from main.tex, or supply equivalent entries under those citation keys.
The standalone main.tex also uses url to typeset the source link in its bibliography.

E2A is a discrete-event M/M/1 simulation, not measured ROS/HTTP performance. E2B is measured ROS/HTTP arrival variability.
Every point is the mean of five run-level statistics; bars show min/max across those runs, not confidence intervals.
Every figure contains one plot. tables/ preserves per-run, per-class and per-source values; no robot-equivalent labels are used.

Rebuild from the workspace root:
python -B scripts/report_e2.py --model "results\e2_model_complete" --live "C:\Users\james\Documents\ELEN90061_E2\results\e2_live_complete" --output "report\e2"
Do not run plotting during live performance measurement. Input SHA-256 hashes are recorded in report_evidence.json.
