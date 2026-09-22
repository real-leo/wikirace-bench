"""Smoke-test pinned SemIf, materializing weights on CPU before GPU scoring.

This works around an observed Metal timeout during local weight loading.
Model weights, precision, sanitizer, prompts and scorer are unchanged.
Use .venv-semif; run sequentially with other GPU benchmarks.
"""
import argparse
import json
import time
from pathlib import Path

import mlx.core as mx
import mlx_lm
from semif_phase1 import mlx_backend

ROOT = Path(__file__).resolve().parents[1]
REVISION = "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(args.out)
    original_load = mlx_lm.load

    def load_on_cpu(*args, **kwargs):
        print("Materializing source weights on CPU", flush=True)
        with mx.stream(mx.cpu):
            result = original_load(*args, **kwargs)
            mx.eval(result[0].parameters())
        print("Weights ready; switching to GPU scoring", flush=True)
        return result

    mlx_lm.load = load_on_cpu
    started = time.perf_counter()
    try:
        print("Hashing pinned source artifacts", flush=True)
        model, tokenizer, metadata = mlx_backend.load_model(str(ROOT / "models/Qwen3.5-4B"), REVISION)
    finally:
        mlx_lm.load = original_load
    load_seconds = time.perf_counter() - started
    metadata["load_strategy"] = "CPU materialization, native MLX GPU scoring"
    results = []
    rows = [json.loads(line) for line in (ROOT / "third_party/SemIf/examples/decisions.jsonl").read_text().splitlines()]
    for row in rows:
        result = mlx_backend.score(model, tokenizer, row, metadata)
        result["selected"] = result["option_ids"][max(range(len(result["probabilities"])), key=result["probabilities"].__getitem__)]
        results.append(result)
        print(f"{row['id']}: {result['selected']} ({result.get('total_seconds', 0):.3f}s)", flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as handle:
        json.dump({"load_seconds": load_seconds, "peak_mlx_allocation_mib": mx.get_peak_memory()/1024**2,
                   "device": mx.device_info(), "results": results}, handle, indent=2)


if __name__ == "__main__":
    main()
