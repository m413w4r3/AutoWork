#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from openai import APIConnectionError, APIStatusError, OpenAI


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KEYS_FILE = ROOT / ".llms_key"


def load_env_file(path: Path) -> None:
    """Load a simple KEY=VALUE file without requiring python-dotenv."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]

        # Explicit environment variables win over .llm_keys.
        os.environ.setdefault(key, value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test native OpenAI-compatible tool calling."
    )

    parser.add_argument(
        "model",
        help="Exact model ID exposed by the provider.",
    )

    parser.add_argument(
        "--keys-file",
        type=Path,
        default=DEFAULT_KEYS_FILE,
        help="Path to local credentials file.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env_file(args.keys_file)

    prefix = "CHAPSVISION"

    base_url = os.getenv(f"{prefix}_BASE_URL", "").strip()
    api_key = os.getenv(f"{prefix}_API_KEY", "").strip()

    if not base_url:
        print(
            f"ERROR: {prefix}_BASE_URL is missing.",
            file=sys.stderr,
        )
        return 2

    if not api_key:
        print(
            f"ERROR: {prefix}_API_KEY is missing.",
            file=sys.stderr,
        )
        return 2

    client = OpenAI(
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        timeout=120.0,
        max_retries=0,
    )

    try:
        response = client.chat.completions.create(
            model=args.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "This is a native function-calling capability test. "
                        "Use the provided function when requested."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Call the probe function exactly once with "
                        'value="ok". Do not answer in plain text.'
                    ),
                },
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "probe",
                        "description": (
                            "Tests native OpenAI-compatible function calling."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "value": {
                                    "type": "string",
                                    "description": "Probe value.",
                                }
                            },
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            tool_choice="auto",
            temperature=0,
        )

    except APIStatusError as exc:
        print(
            f"FAIL: provider returned HTTP {exc.status_code}",
            file=sys.stderr,
        )
        print(exc.response.text, file=sys.stderr)
        return 1

    except APIConnectionError as exc:
        print(
            f"FAIL: connection error: {exc}",
            file=sys.stderr,
        )
        return 1

    except Exception as exc:
        print(
            f"FAIL: unexpected error: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    if not response.choices:
        print("FAIL: response contains no choices.")
        return 1

    choice = response.choices[0]
    message = choice.message
    tool_calls = message.tool_calls or []

    print(f"Provider      : {prefix}")
    print(f"Base URL      : {base_url}")
    print(f"Model         : {args.model}")
    print(f"Finish reason : {choice.finish_reason}")
    print()

    if not tool_calls:
        print("FAIL: no native tool_calls returned.")
        print()
        print(f"Text response: {message.content!r}")
        return 1

    print(f"Native tool calls returned: {len(tool_calls)}")

    valid_probe = False

    for index, call in enumerate(tool_calls, start=1):
        function = call.function

        print()
        print(f"Tool call #{index}")
        print(f"  type      : {call.type}")
        print(f"  name      : {function.name}")
        print(f"  arguments : {function.arguments}")

        if function.name != "probe":
            continue

        try:
            arguments = json.loads(function.arguments)
        except json.JSONDecodeError:
            continue

        if arguments == {"value": "ok"}:
            valid_probe = True

    print()

    if not valid_probe:
        print(
            "FAIL: tool_calls exist, but the expected "
            'probe({"value":"ok"}) call was not produced.'
        )
        return 1

    print("PASS: native OpenAI-compatible tool calling works.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
