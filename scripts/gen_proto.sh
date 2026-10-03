#!/usr/bin/env bash
# Regenerate Python protobuf / gRPC stubs into src/remote_exec/v1/.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run python -m grpc_tools.protoc \
    -I proto \
    --python_out=src \
    --pyi_out=src \
    --grpc_python_out=src \
    proto/remote_exec/v1/remote_exec.proto

echo "generated: $(ls src/remote_exec/v1/*_pb2*.py*)"
