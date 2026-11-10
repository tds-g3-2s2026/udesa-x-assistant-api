"""Tries one request against the model and shows every step, to test a prompt or a
model before running the whole set.

    uv run python -m evals.playground "dale like al tercero"
"""

import argparse
import json

from assistant_api.agent.loop import run_request
from evals.app import SimulatedApp
from evals.run import model_from_environment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("request", help="El pedido, como lo diría el usuario")
    parser.add_argument("--thinking", action="store_true", help="Con el razonamiento prendido")
    args = parser.parse_args()

    app = SimulatedApp()
    transcript = run_request(model_from_environment(thinking=args.thinking), app, args.request)

    turns = iter(transcript.turns)
    for number, step in enumerate(transcript.steps, start=1):
        turn = next(turns)
        outcome = "ok" if step.result.get("ok") else f"error: {step.result.get('error')}"
        print(
            f"{number}. {step.action} {json.dumps(step.arguments, ensure_ascii=False)} -> {outcome}"
            f"  [{turn.input_tokens} in / {turn.output_tokens} out, {turn.latency_ms:.0f} ms]"
        )
    print(f"\nTerminó: {transcript.outcome}")
    print(f"Dice: {transcript.said}")
    print(f"Cambios: {json.dumps(app.changes(), ensure_ascii=False)}")
    print(f"Confirmados por el usuario: {app.confirmed}")


if __name__ == "__main__":
    main()
