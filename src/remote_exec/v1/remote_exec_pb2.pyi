import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ExecuteRequest(_message.Message):
    __slots__ = ("command", "working_directory", "environment", "timeout_seconds")
    class EnvironmentEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    WORKING_DIRECTORY_FIELD_NUMBER: _ClassVar[int]
    ENVIRONMENT_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_SECONDS_FIELD_NUMBER: _ClassVar[int]
    command: str
    working_directory: str
    environment: _containers.ScalarMap[str, str]
    timeout_seconds: float
    def __init__(self, command: _Optional[str] = ..., working_directory: _Optional[str] = ..., environment: _Optional[_Mapping[str, str]] = ..., timeout_seconds: _Optional[float] = ...) -> None: ...

class ExecuteEvent(_message.Message):
    __slots__ = ("started", "stdout", "stderr", "finished")
    STARTED_FIELD_NUMBER: _ClassVar[int]
    STDOUT_FIELD_NUMBER: _ClassVar[int]
    STDERR_FIELD_NUMBER: _ClassVar[int]
    FINISHED_FIELD_NUMBER: _ClassVar[int]
    started: Started
    stdout: OutputChunk
    stderr: OutputChunk
    finished: Finished
    def __init__(self, started: _Optional[_Union[Started, _Mapping]] = ..., stdout: _Optional[_Union[OutputChunk, _Mapping]] = ..., stderr: _Optional[_Union[OutputChunk, _Mapping]] = ..., finished: _Optional[_Union[Finished, _Mapping]] = ...) -> None: ...

class Started(_message.Message):
    __slots__ = ("execution_id", "pid", "start_time", "hostname")
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    PID_FIELD_NUMBER: _ClassVar[int]
    START_TIME_FIELD_NUMBER: _ClassVar[int]
    HOSTNAME_FIELD_NUMBER: _ClassVar[int]
    execution_id: str
    pid: int
    start_time: _timestamp_pb2.Timestamp
    hostname: str
    def __init__(self, execution_id: _Optional[str] = ..., pid: _Optional[int] = ..., start_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., hostname: _Optional[str] = ...) -> None: ...

class OutputChunk(_message.Message):
    __slots__ = ("data",)
    DATA_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    def __init__(self, data: _Optional[bytes] = ...) -> None: ...

class Finished(_message.Message):
    __slots__ = ("status", "exit_code", "signal", "start_time", "end_time", "message", "execution_id")
    class Status(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        STATUS_UNSPECIFIED: _ClassVar[Finished.Status]
        EXITED: _ClassVar[Finished.Status]
        TIMEOUT: _ClassVar[Finished.Status]
        ERROR: _ClassVar[Finished.Status]
    STATUS_UNSPECIFIED: Finished.Status
    EXITED: Finished.Status
    TIMEOUT: Finished.Status
    ERROR: Finished.Status
    STATUS_FIELD_NUMBER: _ClassVar[int]
    EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    SIGNAL_FIELD_NUMBER: _ClassVar[int]
    START_TIME_FIELD_NUMBER: _ClassVar[int]
    END_TIME_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    status: Finished.Status
    exit_code: int
    signal: int
    start_time: _timestamp_pb2.Timestamp
    end_time: _timestamp_pb2.Timestamp
    message: str
    execution_id: str
    def __init__(self, status: _Optional[_Union[Finished.Status, str]] = ..., exit_code: _Optional[int] = ..., signal: _Optional[int] = ..., start_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., end_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., message: _Optional[str] = ..., execution_id: _Optional[str] = ...) -> None: ...

class GetServerInfoRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ServerInfo(_message.Message):
    __slots__ = ("hostname", "version", "user", "shell")
    HOSTNAME_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    SHELL_FIELD_NUMBER: _ClassVar[int]
    hostname: str
    version: str
    user: str
    shell: str
    def __init__(self, hostname: _Optional[str] = ..., version: _Optional[str] = ..., user: _Optional[str] = ..., shell: _Optional[str] = ...) -> None: ...
