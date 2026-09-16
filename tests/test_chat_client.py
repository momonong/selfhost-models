import pytest

from scripts.chat import stream_text


def test_stream_text_requires_done_and_delivers_content():
    assert list(stream_text(['data: {"choices":[{"delta":{"content":"hello"}}]}',
                             'data: [DONE]'])) == ["hello"]


def test_truncated_stream_is_failure_after_partial_output():
    result = stream_text(['data: {"choices":[{"delta":{"content":"partial"}}]}'])
    assert next(result) == "partial"
    with pytest.raises(ValueError, match="without"):
        next(result)


def test_error_frame_is_failure_even_if_done_follows():
    with pytest.raises(ValueError, match="deadline"):
        list(stream_text(['data: {"error":{"code":"deadline"}}', 'data: [DONE]']))
