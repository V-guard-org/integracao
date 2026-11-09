"""
Testes do canal da visão para o principal (vision/protocol.py, Issue #10):
só a biblioteca padrão, sem câmera nem processo.

    cd vision && python3 test_protocol.py
"""

import json
import os
import unittest

import protocol
from protocol import MAX_JPEG, MAX_LINE, Reader, Writer

JPEG = b"\xff\xd8" + b"x" * 100 + b"\xff\xd9"


def feed_all(reader, data, step=None):
    """Entrega os bytes de uma vez ou em pedaços de `step`; junta as mensagens e os problemas."""
    messages, problems = [], []
    chunks = [data] if step is None else [data[i:i + step] for i in range(0, len(data), step)]
    for chunk in chunks:
        got, bad = reader.feed(chunk)
        messages += got
        problems += bad
    return messages, problems


class EncodeTests(unittest.TestCase):

    def test_lines_are_compact_json(self):
        self.assertEqual(protocol.ready(), b'{"msg":"pronto","versao":1}\n')
        self.assertEqual(protocol.fps(13.94), b'{"msg":"fps","fps":13.9}\n')
        data = protocol.event(1793980812.4374, "fadiga", 0.82, JPEG)
        line, _, payload = data.partition(b"\n")
        self.assertEqual(json.loads(line), {"msg": "evento", "ts": 1793980812.437, "type": "fadiga",
                                            "score": 0.82, "jpeg": len(JPEG)})
        self.assertEqual(payload, JPEG)

    def test_line_over_one_kilobyte_is_refused(self):
        with self.assertRaises(ValueError):
            protocol.encode({"msg": "fps", "x": "y" * MAX_LINE})

    def test_writer_sends_everything_through_the_descriptor(self):
        read_fd, write_fd = os.pipe()
        try:
            writer = Writer(write_fd)
            writer.ready()
            writer.fps(14.5)
            writer.event(1793980812.0, "distracao", 0.7, JPEG)
            os.close(write_fd)
            data = b""
            while chunk := os.read(read_fd, 65536):
                data += chunk
        finally:
            os.close(read_fd)
        messages, problems = feed_all(Reader(), data)
        self.assertEqual(problems, [])
        self.assertEqual([m.kind for m in messages], ["pronto", "fps", "evento"])
        self.assertEqual(messages[2].jpeg, JPEG)


class ReaderTests(unittest.TestCase):

    def stream(self):
        return (protocol.ready() + protocol.fps(13.9)
                + protocol.event(1793980812.437, "fadiga", 0.82, JPEG)
                + protocol.fps(14.1))

    def test_round_trip_whole_and_byte_by_byte(self):
        for step in (None, 1, 7, 64):
            with self.subTest(step=step):
                messages, problems = feed_all(Reader(), self.stream(), step)
                self.assertEqual(problems, [])
                self.assertEqual([m.kind for m in messages], ["pronto", "fps", "evento", "fps"])
                self.assertEqual(messages[0].data["versao"], 1)
                self.assertEqual(messages[1].data["fps"], 13.9)
                event = messages[2]
                self.assertEqual((event.data["type"], event.data["score"], event.data["ts"]),
                                 ("fadiga", 0.82, 1793980812.437))
                self.assertEqual(event.jpeg, JPEG)

    def test_bad_lines_are_dropped_and_the_stream_goes_on(self):
        bad = (b"isto nao e json\n"
               b'[1, 2]\n'
               b'{"sem":"msg"}\n'
               b'{"msg":"pronto"}\n'
               b'{"msg":"fps","fps":"rapido"}\n'
               b'{"msg":"fps","fps":-1}\n'
               b'{"msg":"evento","ts":1,"type":"fadiga","score":0.8}\n')
        messages, problems = feed_all(Reader(), bad + protocol.fps(12.0))
        self.assertEqual([m.kind for m in messages], ["fps"])
        self.assertEqual(len(problems), 7)

    def test_unknown_message_and_extra_fields_do_not_break(self):
        data = b'{"msg":"novidade","x":1}\n{"msg":"fps","fps":12.5,"extra":true}\n'
        messages, problems = feed_all(Reader(), data)
        self.assertEqual(problems, [])
        self.assertEqual([(m.kind, m.data["fps"]) for m in messages], [("fps", 12.5)])

    def test_line_over_one_kilobyte_is_dropped(self):
        long_line = b'{"msg":"fps","fps":1,"x":"' + b"y" * 2000 + b'"}\n'
        for step in (None, 100):
            with self.subTest(step=step):
                messages, problems = feed_all(Reader(), long_line + protocol.fps(11.0), step)
                self.assertEqual([m.data["fps"] for m in messages], [11.0])
                self.assertEqual(problems, ["mensagem acima de 1 KB descartada"])

    def test_jpeg_without_start_marker_is_dropped(self):
        data = protocol.event(1793980812.0, "fadiga", 0.8, b"PNG nao e JPEG") + protocol.fps(10.0)
        messages, problems = feed_all(Reader(), data)
        self.assertEqual([m.kind for m in messages], ["fps"])
        self.assertEqual(problems, ["evento descartado: o JPEG não começa com FF D8"])

    def test_jpeg_over_five_megabytes_is_skipped_in_step(self):
        big = MAX_JPEG + 1
        header = json.dumps({"msg": "evento", "ts": 1793980812.0, "type": "fadiga",
                             "score": 0.8, "jpeg": big}).encode() + b"\n"
        data = header + b"\xff\xd8" + b"z" * (big - 2) + protocol.fps(9.5)
        messages, problems = feed_all(Reader(), data, step=1 << 20)
        self.assertEqual([(m.kind, m.data["fps"]) for m in messages], [("fps", 9.5)])
        self.assertEqual(problems, [f"evento descartado: JPEG de {big} bytes, acima de 5 MB"])

    def test_event_with_bad_field_skips_its_jpeg(self):
        header = json.dumps({"msg": "evento", "ts": 1793980812.0, "type": "",
                             "score": 0.8, "jpeg": len(JPEG)}).encode() + b"\n"
        messages, problems = feed_all(Reader(), header + JPEG + protocol.fps(8.0))
        self.assertEqual([m.kind for m in messages], ["fps"])
        self.assertEqual(problems, ["evento com campo faltando ou inválido descartado"])

    def test_message_cut_at_the_end_is_dropped(self):
        whole = protocol.event(1793980812.0, "fadiga", 0.8, JPEG)
        for cut in (whole[:10], whole[:len(whole) - 5]):
            with self.subTest(size=len(cut)):
                reader = Reader()
                messages, problems = feed_all(reader, cut)
                self.assertEqual((messages, problems), ([], []))
                self.assertEqual(reader.close(), ["mensagem cortada no fim do canal (a visão saiu): descartada"])
                self.assertEqual(reader.close(), [])
        self.assertEqual(Reader().close(), [])

    def test_booleans_are_not_numbers(self):
        messages, problems = feed_all(Reader(), b'{"msg":"pronto","versao":true}\n{"msg":"fps","fps":true}\n')
        self.assertEqual(messages, [])
        self.assertEqual(len(problems), 2)


if __name__ == "__main__":
    unittest.main()
