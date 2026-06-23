.PHONY: install playground

install:
	uv sync

playground:
	uv run adk web app

run-ambient:
	uv run uvicorn main:app --host 0.0.0.0 --port 8081

generate-traces:
	uv run python tests/eval/generate_traces.py

grade:
	uv run agents-cli eval grade --traces artifacts/traces/generated_traces.json --config tests/eval/eval_config.yaml
