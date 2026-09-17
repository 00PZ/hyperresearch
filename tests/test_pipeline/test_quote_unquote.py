"""Host unquote of unmatched quote-integrity spans."""

from hyperresearch.cli.lint import unquote_unmatched_spans


class _Conn:
    def __init__(self, hits: set[str] | None = None) -> None:
        self.hits = hits or set()

    def execute(self, sql: str, params: tuple) -> "_Conn":
        self._params = params
        return self

    def fetchone(self):
        phrase = self._params[0]
        for hit in self.hits:
            if hit in phrase:
                return (1,)
        return None


def test_unquote_drops_unmatched_five_word_span() -> None:
    text = (
        "not the same as \u201cofficial materials do not specify\u201d "
        "in this corpus."
    )
    out, n = unquote_unmatched_spans(_Conn(), text)
    assert n == 1
    assert "\u201cofficial materials do not specify\u201d" not in out
    assert "official materials do not specify" in out


def test_unquote_keeps_matched_span() -> None:
    text = "They are the \u201cheartbeat of the chain\u201d [[n1]]."
    out, n = unquote_unmatched_spans(_Conn(hits={"heartbeat of the chain"}), text)
    assert n == 0
    assert out == text


def test_unquote_skips_short_quotes() -> None:
    text = "call it \u201cthe core\u201d and move on."
    out, n = unquote_unmatched_spans(_Conn(), text)
    assert n == 0
    assert out == text
