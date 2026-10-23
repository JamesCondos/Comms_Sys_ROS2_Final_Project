"""Minimal single-column E1 topology; exports PDF, SVG and PNG.

The figure shows the implemented configuration, not a live network inventory.
See E1_PROTOCOL.md for rates, actual node counts and FIFO/sender settings.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Rectangle


def main():
    out = Path(__file__).resolve().parents[1] / "report" / "overleaf" / "figures"
    out.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "pdf.fonttype": 42,
        "svg.fonttype": "none",
    })
    # Native width is suitable for a typical single column, with 8.5 pt labels.
    fig, ax = plt.subplots(figsize=(3.5, 1.28))
    fig.subplots_adjust(left=0, bottom=0, right=1, top=1)
    ax.set(xlim=(0, 3.5), ylim=(0, 1.28))
    ax.axis("off")

    ax.add_patch(Rectangle((0.035, 0.055), 3.43, 1.115, fill=False,
                           edgecolor="#666666", linewidth=0.7,
                           linestyle=(0, (4, 3))))
    ax.text(1.75, 1.17, "Single host (WSL2)", ha="center", va="center",
            fontsize=8, bbox={"facecolor": "white", "edgecolor": "none", "pad": 2.5})

    def node(x, width, text):
        ax.add_patch(Rectangle((x, 0.35), width, 0.56, facecolor="white",
                               edgecolor="black", linewidth=0.85))
        ax.text(x + width / 2, 0.63, text, ha="center", va="center",
                fontsize=8.5, linespacing=1.35)

    node(0.13, 0.65, "ROS 2\nsource")
    node(1.33, 0.80, "FIFO\ngateway")
    node(2.77, 0.60, "HTTP\nserver")

    ax.add_patch(FancyArrowPatch((0.78, 0.63), (1.33, 0.63),
                                arrowstyle="-|>", mutation_scale=8,
                                linewidth=0.8, color="black", shrinkA=1, shrinkB=1))
    ax.text(1.055, 0.79, "ROS 2", ha="center", va="center", fontsize=7)
    ax.text(1.055, 0.47, "topics", ha="center", va="center", fontsize=7)

    # One bidirectional line denotes both directions of one TCP connection.
    ax.add_patch(FancyArrowPatch((2.13, 0.63), (2.77, 0.63),
                                arrowstyle="<|-|>", mutation_scale=8,
                                linewidth=0.8, color="black", shrinkA=1, shrinkB=1))
    ax.text(2.45, 0.79, "HTTP/1.1", ha="center", va="center", fontsize=7)
    ax.text(2.45, 0.47, "TCP", ha="center", va="center", fontsize=7)
    ax.text(2.45, 0.30, "loopback", ha="center", va="center", fontsize=7)

    for extension in ("pdf", "svg", "png"):
        target = out / f"e1_network_topology_paper.{extension}"
        fig.savefig(target, dpi=400, facecolor="white")
        print(target)
    plt.close(fig)


if __name__ == "__main__":
    main()
