"""Packet-checker tests for segmentation, overlap, ordering and HTTP framing."""

import json
import unittest

from e1_capture_check import http_messages, reassemble


class CaptureCheckTests(unittest.TestCase):
    def test_reassembles_out_of_order_and_partial_overlap(self):
        data, overlap = reassemble(100, [(104, b"def"), (101, b"abcde"), (101, b"ab")])
        self.assertEqual(data, b"abcdef")
        self.assertEqual(overlap, 4)

    def test_reassembles_sequence_wrap(self):
        data, overlap = reassemble(2**32 - 3, [(0, b"cd"), (2**32 - 2, b"ab")])
        self.assertEqual(data, b"abcd")
        self.assertEqual(overlap, 0)

    def test_rejects_gap(self):
        with self.assertRaisesRegex(ValueError, "gap"):
            reassemble(100, [(101, b"abc"), (105, b"ef")])

    def test_rejects_conflicting_retransmitted_bytes(self):
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            reassemble(100, [(101, b"abc"), (102, b"Xc")])

    def test_content_length_not_body_delimiter_controls_framing(self):
        records = [dict(id="one", robot_id=1, message_type="sensor", payload="POST HTTP/1.1"),
                   dict(id="two", robot_id=1, message_type="sensor", payload="hello")]
        wire = b""
        for record in records:
            body = json.dumps(record).encode()
            wire += b"POST /robots/1/sensor HTTP/1.1\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        result = http_messages(wire, request=True)
        self.assertEqual([row["id"] for row in result], ["one", "two"])

    def test_rejects_truncated_body(self):
        with self.assertRaisesRegex(ValueError, "Incomplete HTTP message body"):
            http_messages(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{}", request=False)


if __name__ == "__main__":
    unittest.main()
