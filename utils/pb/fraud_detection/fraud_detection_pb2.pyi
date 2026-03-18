from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class OrderRequest(_message.Message):
    __slots__ = ("order_json",)
    ORDER_JSON_FIELD_NUMBER: _ClassVar[int]
    order_json: str
    def __init__(self, order_json: _Optional[str] = ...) -> None: ...

class FraudResponse(_message.Message):
    __slots__ = ("fraud_detected", "reason")
    FRAUD_DETECTED_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    fraud_detected: bool
    reason: str
    def __init__(self, fraud_detected: bool = ..., reason: _Optional[str] = ...) -> None: ...
