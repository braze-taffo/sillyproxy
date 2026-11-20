import json
import unittest

from playground import WireStream, parse_frame, upstream_error


def frame(text, finish=None, usage=None):
    """AI Studio frame shape: [ [[content, finish]], null, usage ].

    content = [[[null, text], ...], "model"]
    """
    content = [[[None, text]], "model"]
    return [[[content, finish]], None, usage]


def texts_of(chunks):
    stream = WireStream()
    texts = []
    for chunk in chunks:
        for item in stream.feed(chunk):
            texts.extend(item.texts)
    for item in stream.finish():
        texts.extend(item.texts)
    return "".join(texts)


class TestFrames(unittest.TestCase):
    def test_frame_fields(self):
        parsed = parse_frame(frame("Hello", finish=1, usage=[5, 2]))
        self.assertEqual(parsed.texts, ["Hello"])
        self.assertEqual(parsed.finish, 1)
        self.assertEqual(parsed.usage, [5, 2])

    def test_thought_summary_is_skipped(self):
        parts = [None, "thinking", None, None, None, None, None, None, None, None, None, None, True]
        content = [[parts], "model"]
        parsed = parse_frame([[[content, None]], None, None])
        self.assertEqual(parsed.texts, [])


class TestWireStream(unittest.TestCase):
    def body(self):
        frames = [frame("AAA"), frame("BBB"), frame("CCC", finish=1, usage=[3, 1])]
        return ")]}'\n" + json.dumps([frames])

    def test_whole_body(self):
        self.assertEqual(texts_of([self.body()]), "AAABBBCCC")

    def test_arbitrary_chunking(self):
        body = self.body()
        for size in (5, 7, 13, 97):
            with self.subTest(size=size):
                chunks = [body[i:i + size] for i in range(0, len(body), size)]
                self.assertEqual(texts_of(chunks), "AAABBBCCC")

    def test_split_between_the_two_leading_brackets(self):
        # Regression: a chunk boundary between ")]}'\n[" and "[frame..." used
        # to leave the second bracket unstripped and truncate the answer.
        body = self.body()
        cut = body.index("[[") + 1
        self.assertEqual(texts_of([body[:cut], body[cut:]]), "AAABBBCCC")

    def test_repeated_drain_terminates(self):
        stream = WireStream()
        stream.feed(self.body())
        stream.finish()
        self.assertEqual(stream.feed(""), [])
        self.assertEqual(stream.finish(), [])


class TestUpstreamError(unittest.TestCase):
    def test_error_mapping(self):
        cases = [
            (403, '[,[7,"The caller does not have permission"]]', 403, "google_permission_denied"),
            (429, '[,[8,"You have reached your usage limit"]]', 429, "google_quota"),
            (503, '[,[14,"high demand",[["x"]]]]', 503, "google_unavailable"),
            (500, "boom", 500, "upstream_http_error"),
        ]
        for status, body, expected_status, expected_code in cases:
            with self.subTest(status=status):
                error = upstream_error(status, body)
                self.assertEqual(error.status, expected_status)
                self.assertEqual(error.code, expected_code)

    def test_message_is_passed_through(self):
        error = upstream_error(429, '[,[8,"resets in 16 hours"]]')
        self.assertIn("resets in 16 hours", error.message)


if __name__ == "__main__":
    unittest.main()
