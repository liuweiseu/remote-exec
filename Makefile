.PHONY: sync proto test install

sync:
	uv sync

proto:
	./scripts/gen_proto.sh

test:
	uv run pytest

install:
	uv tool install --force .
