"""Runs the scripted requests against a Bedrock model and measures them.

    uv run python -m evals.run --label haiku-5.5

Every request is a few model calls that cost money and use the group's monthly
quota, so it runs by hand and never in the CI. `--only R01,R05` runs a few.

The model comes from the environment, and its ARN is never written in the repo:

    AWS_PROFILE       tds-group-3, after `aws login --remote --profile tds-group-3`
    BEDROCK_REGION    us-east-1 locally
    BEDROCK_MODEL_ID  the ARN of the group's local inference profile
"""

import argparse
import json
import os
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

from assistant_api.agent.bedrock import BedrockModel
from assistant_api.agent.loop import Model, Transcript, run_request
from evals.app import INJECTED_POST, SimulatedApp
from evals.scoring import problems

REQUESTS = Path(__file__).with_name("requests.json")
RESULTS = Path(__file__).with_name("results")

# Dollars per million tokens. The defaults are Claude Haiku 5.5's, from
# docs/ESTIMACION-COSTOS-IA.md in udesa-x-platform.
PRICE_INPUT = float(os.getenv("PRICE_INPUT_PER_MTOK", "0.11"))
PRICE_OUTPUT = float(os.getenv("PRICE_OUTPUT_PER_MTOK", "0.55"))


def model_from_environment(*, thinking: bool) -> BedrockModel:
    model_id = os.getenv("BEDROCK_MODEL_ID")
    region = os.getenv("BEDROCK_REGION")
    if not model_id or not region:
        sys.exit("Faltan BEDROCK_MODEL_ID y BEDROCK_REGION (ver el README)")
    return BedrockModel(model_id, region=region, thinking=thinking)


def cost(transcript: Transcript) -> float:
    return (transcript.input_tokens * PRICE_INPUT + transcript.output_tokens * PRICE_OUTPUT) / 1e6


def evaluate(model: Model, requests: list[dict]) -> list[dict]:
    rows = []
    for item in requests:
        app = SimulatedApp()
        transcript = run_request(model, app, item["request"])
        found = problems(app.changes(), item["expect"])
        if transcript.outcome != "finished":
            found.insert(0, f"terminó por {transcript.outcome}")
        row = {
            "id": item["id"],
            "kind": item["kind"],
            "request": item["request"],
            "ok": not found,
            "problems": found,
            "actions": [
                f"{step.action}({', '.join(f'{k}={v!r}' for k, v in step.arguments.items())})"
                + ("" if step.result.get("ok") else f" -> {step.result.get('error')}")
                for step in transcript.steps
            ],
            "said": transcript.said,
            "model_calls": len(transcript.turns),
            "first_action_ms": round(transcript.turns[0].latency_ms),
            "call_ms": [round(turn.latency_ms) for turn in transcript.turns],
            "input_tokens": transcript.input_tokens,
            "output_tokens": transcript.output_tokens,
            "cost_usd": round(cost(transcript), 6),
            "confirmed": app.confirmed,
            "followed_injection": INJECTED_POST in app.retweeted,
        }
        rows.append(row)
        mark = "ok " if row["ok"] else "MAL"
        print(f"{mark} {item['id']} {item['request']}: {' > '.join(row['actions'])}", flush=True)
    return rows


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))]


def summarize(rows: list[dict]) -> dict:
    first = [row["first_action_ms"] for row in rows]
    calls = [ms for row in rows for ms in row["call_ms"]]
    return {
        "requests": len(rows),
        "succeeded": sum(row["ok"] for row in rows),
        "success_rate": round(sum(row["ok"] for row in rows) / len(rows), 3),
        "model_calls_per_request": round(statistics.mean(row["model_calls"] for row in rows), 2),
        "first_action_ms_median": round(statistics.median(first)),
        "first_action_ms_p95": round(percentile(first, 0.95)),
        "call_ms_median": round(statistics.median(calls)),
        "input_tokens_per_request": round(statistics.mean(row["input_tokens"] for row in rows)),
        "output_tokens_per_request": round(statistics.mean(row["output_tokens"] for row in rows)),
        "cost_usd_per_request": round(statistics.mean(row["cost_usd"] for row in rows), 6),
        "cost_usd_total": round(sum(row["cost_usd"] for row in rows), 4),
        "followed_injection": sum(row["followed_injection"] for row in rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--label", required=True, help="Nombre del modelo en el resultado")
    parser.add_argument("--only", help="Ids separados por comas, como R01,R05")
    parser.add_argument("--thinking", action="store_true", help="Con el razonamiento prendido")
    args = parser.parse_args()

    requests = json.loads(REQUESTS.read_text(encoding="utf-8"))
    if args.only:
        wanted = set(args.only.split(","))
        requests = [item for item in requests if item["id"] in wanted]

    rows = evaluate(model_from_environment(thinking=args.thinking), requests)
    summary = summarize(rows)
    print(json.dumps(summary, indent=2))

    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = "-razonando" if args.thinking else ""
    path = RESULTS / f"{stamp}-{args.label}{suffix}.json"
    report = {"label": args.label, "thinking": args.thinking, "summary": summary, "rows": rows}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Resultado en {path}")


if __name__ == "__main__":
    main()
