"""`python -m app data --config <yaml> [--out data]`: generate the dataset and print counts."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from app.config import load_config
from app.generator.generate import generate
from app.vocab import SOURCES


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app data", description=__doc__)
    parser.add_argument("--config", required=True, help="YAML file, e.g. config/small.yaml")
    parser.add_argument("--out", default="data", help="output directory (default: data)")
    args = parser.parse_args(argv[1:] if argv and argv[0] == "data" else argv)

    cfg = load_config(args.config)
    started = time.perf_counter()  # elapsed time only; never feeds the data
    manifest = generate(cfg, Path(args.out))
    elapsed = time.perf_counter() - started

    print(f"config {cfg.name} (seed {cfg.seed}, sha256 {cfg.sha256[:12]}) -> {args.out}/")
    print(f"{'tenant':<8}{'accounts':>9}{'csms':>6}{'renewals':>9}{'churned':>8}", end="")
    print("".join(f"{s:>12}" for s in SOURCES) + f"{'events':>10}")
    totals = dict.fromkeys(SOURCES, 0)
    total_accounts = 0
    for slug, per_source in manifest["counts"].items():
        s = manifest["stats"][slug]
        total_accounts += s["accounts"]
        print(
            f"{slug:<8}{s['accounts']:>9}{s['csms']:>6}{s['renewals']:>9}{s['churned']:>8}", end=""
        )
        for source in SOURCES:
            totals[source] += per_source.get(source, 0)
            print(f"{per_source.get(source, 0):>12}", end="")
        print(f"{sum(per_source.values()):>10}")
    print(f"{'total':<8}{total_accounts:>9}{'':>6}{'':>9}{'':>8}", end="")
    print("".join(f"{totals[s]:>12}" for s in SOURCES) + f"{sum(totals.values()):>10}")
    print(f"done in {elapsed:.1f}s")
    return 0
