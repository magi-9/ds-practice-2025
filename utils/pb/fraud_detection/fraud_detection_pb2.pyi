from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class OrderRequest(_message.Message):
    __slots__ = ("order_json",)
    ORDER_JSON_FIELD_NUMBER: _ClassVar[int]
    order_json: str
    def __init__(self, order_json: _Optional[str] = ...) -> None: ...

class OrderInitializationRequest(_message.Message):
    __slots__ = ("order_id", "order_json", "vector_clock")
    class VectorClockEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    ORDER_ID_FIELD_NUMBER: _ClassVar[int]
    ORDER_JSON_FIELD_NUMBER: _ClassVar[int]
    VECTOR_CLOCK_FIELD_NUMBER: _ClassVar[int]
    order_id: str
    order_json: str
    vector_clock: _containers.ScalarMap[str, int]
    def __init__(self, order_id: _Optional[str] = ..., order_json: _Optional[str] = ..., vector_clock: _Optional[_Mapping[str, int]] = ...) -> None: ...

class OrderInitializationResponse(_message.Message):
    __slots__ = ("accepted", "reason", "vector_clock")
    class VectorClockEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    VECTOR_CLOCK_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    reason: str
    vector_clock: _containers.ScalarMap[str, int]
    def __init__(self, accepted: bool = ..., reason: _Optional[str] = ..., vector_clock: _Optional[_Mapping[str, int]] = ...) -> None: ...

class OrderEventRequest(_message.Message):
    __slots__ = ("order_id", "vector_clock")
    class VectorClockEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    ORDER_ID_FIELD_NUMBER: _ClassVar[int]
    VECTOR_CLOCK_FIELD_NUMBER: _ClassVar[int]
    order_id: str
    vector_clock: _containers.ScalarMap[str, int]
    def __init__(self, order_id: _Optional[str] = ..., vector_clock: _Optional[_Mapping[str, int]] = ...) -> None: ...

class OrderEventResponse(_message.Message):
    __slots__ = ("success", "reason", "event_name", "vector_clock")
    class VectorClockEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    EVENT_NAME_FIELD_NUMBER: _ClassVar[int]
    VECTOR_CLOCK_FIELD_NUMBER: _ClassVar[int]
    success: bool
    reason: str
    event_name: str
    vector_clock: _containers.ScalarMap[str, int]
    def __init__(self, success: bool = ..., reason: _Optional[str] = ..., event_name: _Optional[str] = ..., vector_clock: _Optional[_Mapping[str, int]] = ...) -> None: ...

class OrderClearRequest(_message.Message):
    __slots__ = ("order_id", "final_vector_clock")
    class FinalVectorClockEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    ORDER_ID_FIELD_NUMBER: _ClassVar[int]
    FINAL_VECTOR_CLOCK_FIELD_NUMBER: _ClassVar[int]
    order_id: str
    final_vector_clock: _containers.ScalarMap[str, int]
    def __init__(self, order_id: _Optional[str] = ..., final_vector_clock: _Optional[_Mapping[str, int]] = ...) -> None: ...

class OrderClearResponse(_message.Message):
    __slots__ = ("cleared", "reason", "vector_clock")
    class VectorClockEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    CLEARED_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    VECTOR_CLOCK_FIELD_NUMBER: _ClassVar[int]
    cleared: bool
    reason: str
    vector_clock: _containers.ScalarMap[str, int]
    def __init__(self, cleared: bool = ..., reason: _Optional[str] = ..., vector_clock: _Optional[_Mapping[str, int]] = ...) -> None: ...

class FraudResponse(_message.Message):
    __slots__ = ("fraud_detected", "reason")
    FRAUD_DETECTED_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    fraud_detected: bool
    reason: str
    def __init__(self, fraud_detected: bool = ..., reason: _Optional[str] = ...) -> None: ...
