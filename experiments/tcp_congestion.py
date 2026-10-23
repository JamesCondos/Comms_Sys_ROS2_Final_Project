"""Small HTTP/1.1 connection helper for Linux TCP congestion-control tests."""

import http.client
import socket


SUPPORTED_ALGORITHMS = ("reno", "cubic", "bbr")


def available_algorithms(path="/proc/sys/net/ipv4/tcp_available_congestion_control"):
    """Return the congestion-control algorithms registered by this Linux kernel."""
    try:
        with open(path, encoding="utf-8") as available:
            return tuple(available.read().split())
    except OSError:
        return ()


class CongestionControlledHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that selects and verifies a Linux TCP algorithm per socket."""

    def __init__(self, host, port, congestion_control, **kwargs):
        if congestion_control not in SUPPORTED_ALGORITHMS:
            choices = ", ".join(SUPPORTED_ALGORITHMS)
            raise ValueError(f"congestion_control must be one of: {choices}")

        super().__init__(host, port, **kwargs)
        self.requested_congestion_control = congestion_control
        self.actual_congestion_control = None

    def connect(self):
        """Open the socket, select its algorithm, and confirm what Linux applied."""
        super().connect()
        self.sock.setsockopt(
            socket.IPPROTO_TCP,
            socket.TCP_CONGESTION,
            self.requested_congestion_control.encode("ascii"),
        )
        actual = self.sock.getsockopt(
            socket.IPPROTO_TCP, socket.TCP_CONGESTION, 32
        )
        self.actual_congestion_control = actual.split(b"\0", 1)[0].decode("ascii")

        if self.actual_congestion_control != self.requested_congestion_control:
            raise RuntimeError(
                f"Requested TCP {self.requested_congestion_control}, "
                f"but Linux selected {self.actual_congestion_control}"
            )
