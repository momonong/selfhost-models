import base64
import io

import pytest
from PIL import Image
from pydantic import ValidationError

from selfhost_models.schema import Chat


def image_uri(size=(32, 32)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "red").save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def vision(url):
    return {"model": "org/model", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "What color?"}, {"type": "image_url", "image_url": {"url": url}}]}]}


def test_inline_image_validated():
    assert Chat.model_validate(vision(image_uri()))


@pytest.mark.parametrize("uri", ["https://example.org/p.png", "file:///secret", "data:image/png;base64,garbage", image_uri((1025, 4))])
def test_remote_malformed_oversized_images_rejected(uri):
    with pytest.raises(ValidationError):
        Chat.model_validate(vision(uri))


def test_multiple_images_rejected():
    payload = vision(image_uri())
    payload["messages"][0]["content"].append(payload["messages"][0]["content"][1])
    with pytest.raises(ValidationError):
        Chat.model_validate(payload)


def test_tool_history_requires_matching_results():
    payload = {"model": "org/model", "messages": [
        {"role": "assistant", "tool_calls": [{"id": "call1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call1", "content": "result"}]}
    assert Chat.model_validate(payload)
    payload["messages"][1]["tool_call_id"] = "unknown"
    with pytest.raises(ValidationError):
        Chat.model_validate(payload)
