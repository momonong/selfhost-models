import base64
import io
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class TextPart(StrictModel):
    type: Literal["text"]
    text: Annotated[str, Field(min_length=1, max_length=32768)]


class ImageURL(StrictModel):
    url: str

    @field_validator("url")
    @classmethod
    def bounded_png(cls, value):
        from PIL import Image
        prefix = "data:image/png;base64,"
        if not value.startswith(prefix) or len(value) > 1400000:
            raise ValueError("only inline PNG up to 1 MiB is supported")
        try:
            raw = base64.b64decode(value[len(prefix):], validate=True)
            if len(raw) > 1048576:
                raise ValueError("image too large")
            with Image.open(io.BytesIO(raw)) as image:
                if image.format != "PNG" or image.width > 1024 or image.height > 1024:
                    raise ValueError("PNG dimensions must be <=1024x1024")
                image.verify()
        except Exception as exc:
            raise ValueError("invalid or oversized PNG") from exc
        return value


class ImagePart(StrictModel):
    type: Literal["image_url"]
    image_url: ImageURL


class FunctionCall(StrictModel):
    name: Annotated[str, Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_-]{0,63}$")]
    arguments: Annotated[str, Field(max_length=32768)]


class ToolCall(StrictModel):
    id: Annotated[str, Field(min_length=1, max_length=200)]
    type: Literal["function"]
    function: FunctionCall


class Message(StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: Annotated[str, Field(min_length=1, max_length=32768)] | Annotated[list[Annotated[TextPart | ImagePart, Field(discriminator="type")]], Field(min_length=1, max_length=8)] | None = None
    tool_calls: Annotated[list[ToolCall], Field(min_length=1, max_length=8)] | None = None
    tool_call_id: Annotated[str, Field(min_length=1, max_length=200)] | None = None

    @model_validator(mode="after")
    def valid_role(self):
        if self.tool_calls and self.role != "assistant":
            raise ValueError("tool_calls requires assistant role")
        if (self.role == "tool") != (self.tool_call_id is not None):
            raise ValueError("tool role requires tool_call_id exclusively")
        if self.content is None and not self.tool_calls:
            raise ValueError("content required except assistant tool calls")
        if isinstance(self.content, list) and self.role != "user":
            raise ValueError("content parts only supported for user role")
        return self


class Function(StrictModel):
    name: Annotated[str, Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_-]{0,63}$")]
    description: Annotated[str, Field(max_length=4096)] = ""
    parameters: dict


class Tool(StrictModel):
    type: Literal["function"]
    function: Function


class TemplateOptions(StrictModel):
    enable_thinking: bool


class StreamOptions(StrictModel):
    include_usage: bool = False


class Chat(StrictModel):
    model: Annotated[str, Field(min_length=3, max_length=200)]
    messages: Annotated[list[Message], Field(min_length=1, max_length=64)]
    stream: bool = False
    max_tokens: Annotated[int, Field(ge=1, le=1024)] = 128
    temperature: Annotated[float, Field(ge=0, le=2)] = 1.0
    top_p: Annotated[float, Field(gt=0, le=1)] = 1.0
    seed: Annotated[int, Field(ge=0, le=2**32 - 1)] | None = None
    stop: Annotated[str, Field(min_length=1, max_length=200)] | Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(min_length=1, max_length=4)] | None = None
    presence_penalty: Annotated[float, Field(ge=-2, le=2)] = 0.0
    frequency_penalty: Annotated[float, Field(ge=-2, le=2)] = 0.0
    stream_options: StreamOptions | None = None
    tools: Annotated[list[Tool], Field(min_length=1, max_length=8)] | None = None
    tool_choice: Literal["auto", "none"] | None = None
    chat_template_kwargs: TemplateOptions | None = None

    @model_validator(mode="after")
    def options_require_stream(self):
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options requires stream=true")
        if self.tool_choice is not None and not self.tools:
            raise ValueError("tool_choice requires tools")
        images = sum(isinstance(p, ImagePart) for m in self.messages if isinstance(m.content, list) for p in m.content)
        if images > 1:
            raise ValueError("only one image per request")
        pending = set()
        for message in self.messages:
            if message.tool_call_id:
                if message.tool_call_id not in pending:
                    raise ValueError("tool result has no preceding call")
                pending.remove(message.tool_call_id)
            elif pending:
                raise ValueError("all tool calls require results before continuing")
            if message.tool_calls:
                ids = [c.id for c in message.tool_calls]
                if len(ids) != len(set(ids)):
                    raise ValueError("duplicate tool call IDs")
                pending.update(ids)
        if pending:
            raise ValueError("missing tool results")
        return self
