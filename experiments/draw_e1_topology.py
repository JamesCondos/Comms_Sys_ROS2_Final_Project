"""Draw the implemented E1 topology as a report-ready vector figure.

Sources: E1_PROTOCOL.md, baseline.json, baseline.py, and the ROS2 generator.
This is a configuration diagram, not a live process/network inventory.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "report" / "overleaf" / "figures"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "svg.fonttype": "none",
    })
    fig, ax = plt.subplots(figsize=(16, 9))
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
    ax.set(xlim=(0, 16), ylim=(0, 9))
    ax.axis("off")
    ink, muted = "#142B40", "#526576"
    blue, teal, purple = "#265D93", "#147D7A", "#73569A"
    border = "#C9D5DF"

    def box(x, y, w, h, fill="white", edge=border, lw=1.2, radius=0.12, dash=None):
        p = FancyBboxPatch(
            (x, y), w, h, boxstyle=f"round,pad=0,rounding_size={radius}",
            facecolor=fill, edgecolor=edge, linewidth=lw,
            linestyle=dash or "solid", zorder=1,
        )
        ax.add_patch(p)
        return p

    def label(x, y, s, size=12, color=ink, bold=False, align="center", **kw):
        return ax.text(x, y, s, fontsize=size, color=color, ha=align, va="center",
                       fontweight="bold" if bold else "normal", linespacing=1.45,
                       zorder=4, **kw)

    def arrow(start, end, color=teal, lw=1.7):
        ax.add_patch(FancyArrowPatch(start, end, arrowstyle="-|>",
                                    mutation_scale=15, linewidth=lw,
                                    color=color, shrinkA=0, shrinkB=0, zorder=3))

    label(0.55, 8.55, "E1 historical loopback topology", 25, bold=True, align="left")
    label(0.55, 8.12, "Historical rate sweep: synthetic ROS2 traffic, one FIFO gateway, one HTTP server",
          13, muted, align="left")

    box(0.4, 2.2, 15.2, 5.5, fill="#F8FAFC", lw=1.5)
    label(0.7, 7.37, "SINGLE COMPUTER  |  WSL2 / Ubuntu 22.04", 12, bold=True, align="left")
    label(15.25, 7.37, "All three processes run locally", 11, muted, align="right")

    # Process 1: rate-scaled ROS2 publishers, not N independent network clients.
    box(0.7, 2.85, 3.8, 4.1, fill="#EFF5FC", edge=blue, lw=1.5)
    label(2.6, 6.60, "ROS2 source process", 15, blue, True)
    label(2.6, 6.23, "Synthetic traffic generator", 11.5, muted)
    box(0.95, 5.13, 3.3, 0.72, edge="#A7C1DD")
    label(2.6, 5.49, "Source node 1  |  4 topics", 12, blue, True)
    box(0.95, 4.02, 3.3, 0.83, edge="#A7C1DD", dash=(0, (4, 3)))
    label(2.6, 4.57, "Source node 2  |  4 topics", 12, blue, True)
    label(2.6, 4.23, "Present at total load >= 64 messages/s", 9.5, muted)
    label(2.6, 3.55, "Each node publishes:", 11.3, bold=True)
    label(2.6, 3.17, "command, status, odometry, sensor", 10.1, muted)

    # ROS2 transport is intentionally labelled at the middleware level:
    # DDS shared memory versus UDP was not established by the recorded evidence.
    label(5.1, 6.40, "Local\nROS2", 11.5, blue, True)
    arrow((4.5, 5.82), (5.93, 5.82), blue)
    label(5.1, 5.42, "pub/sub", 10.5, muted)

    # Process 2: one application queue and one serial HTTP sender.
    box(5.7, 2.85, 4.5, 4.1, fill="#EFF9F7", edge=teal, lw=1.5)
    label(7.95, 6.60, "ROS2-REST gateway", 15, teal, True)
    box(5.95, 5.55, 4.0, 0.55, edge="#9DCCC4")
    label(7.95, 5.825, "ROS2 subscriptions (4 or 8)", 12)
    arrow((7.95, 5.55), (7.95, 5.15))
    box(5.95, 4.25, 4.0, 0.90, edge="#9DCCC4")
    label(7.95, 4.91, "Shared FIFO queue", 13, teal, True)
    label(7.95, 4.54, "1,000 waiting messages | drop-tail", 11)
    arrow((7.95, 4.25), (7.95, 3.86))
    box(5.95, 3.08, 4.0, 0.78, edge="#9DCCC4")
    label(7.95, 3.61, "Single HTTP sender", 13, teal, True)
    label(7.95, 3.29, "One outstanding request", 11)
    label(7.95, 2.48, "A full FIFO drops the arriving message", 10.5, muted)

    # Both arrows are directions of the same persistent TCP connection.
    label(11.42, 6.13, "Loopback (lo)", 12, teal, True)
    label(11.42, 5.47, "HTTP/1.1\nover TCP", 13, bold=True)
    label(11.42, 4.67, "One persistent\nconnection", 11, muted)
    label(11.42, 3.99, "POST + JSON", 11, teal, True)
    arrow((9.95, 3.66), (12.65, 3.66), teal)
    arrow((12.65, 3.16), (9.95, 3.16), purple)
    label(11.42, 2.91, "200 + JSON", 11, purple, True)

    # Process 3: one local listener, not an external/cloud host.
    box(12.65, 2.85, 2.65, 4.1, fill="#F5F1FA", edge=purple, lw=1.5)
    label(13.975, 6.60, "HTTP server", 15, purple, True)
    label(13.975, 5.85, "127.0.0.1\nTCP port 18061", 12, purple, True)
    label(13.975, 4.78, "Read request JSON", 11)
    label(13.975, 4.18, "Return message ID\nand receipt timestamp", 10.5, muted)

    # Readable experimental context, outside the topology boundary.
    box(0.4, 0.35, 4.65, 1.53, fill="#F8FAFC")
    label(0.65, 1.57, "LOWEST-LOAD TRAFFIC (ONE SOURCE)", 10.4, blue, True, "left")
    label(0.65, 1.17, "command 2/s  |  status 5/s", 11, align="left")
    label(0.65, 0.82, "odometry 20/s  |  sensor 5/s", 11, align="left")
    label(0.65, 0.49, "Total: 32 messages/s", 10.5, muted, align="left")

    box(5.3, 0.35, 5.3, 1.53, fill="#F8FAFC")
    label(5.55, 1.57, "LOAD SCALING", 10.4, teal, True, "left")
    label(5.55, 1.17, "Total: 32, 64, 128, 256, 512, 1,024, 2,048 messages/s", 9.2, align="left")
    label(5.55, 0.82, "Above 32/s: two source nodes publishing progressively faster.", 9.0, align="left")
    label(5.55, 0.49, "The HTTP client count stays at one throughout.", 10.0, muted, align="left")

    box(10.85, 0.35, 4.75, 1.53, fill="#F8FAFC")
    label(11.10, 1.57, "NETWORK CONDITIONS", 10.4, purple, True, "left")
    label(11.10, 1.10, "No injected delay, loss or bandwidth limit.", 10.5, align="left")
    label(11.10, 0.61, "The HTTP path stays inside this computer.", 10.3, muted, align="left")

    for extension in ("pdf", "svg", "png"):
        target = OUT / f"e1_network_topology.{extension}"
        options = {"dpi": 220} if extension == "png" else {}
        fig.savefig(target, facecolor="white", **options)
        print(target)
    plt.close(fig)


if __name__ == "__main__":
    main()
