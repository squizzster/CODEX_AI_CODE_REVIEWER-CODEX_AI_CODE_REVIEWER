#!/usr/bin/env python3
"""Run a small higher/lower simulation and print its aggregate report."""

from __future__ import annotations

import argparse
import json
import random
import time
from dataclasses import asdict, dataclass

DEFAULT_RUNS = 500_000
LOWEST_NUMBER = 1
HIGHEST_NUMBER = 1_000
MAX_ATTEMPTS = 10


@dataclass(frozen=True, slots=True)
class SimulationReport:
    runs: int
    successes: int
    failures: int
    success_rate_percent: float
    average_attempts: float
    higher_hints: int
    lower_hints: int
    elapsed_seconds: float
    seed: int | None


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def play_game(generator: random.Random) -> tuple[bool, int, int, int]:
    target = generator.randint(LOWEST_NUMBER, HIGHEST_NUMBER)
    lower_bound = LOWEST_NUMBER
    upper_bound = HIGHEST_NUMBER
    higher_hints = 0
    lower_hints = 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        guess = generator.randint(lower_bound, upper_bound)
        if guess == target:
            return True, attempt, higher_hints, lower_hints
        if guess < target:
            higher_hints += 1
            lower_bound = guess + 1
        else:
            lower_hints += 1
            upper_bound = guess - 1

    return False, MAX_ATTEMPTS, higher_hints, lower_hints


def run_simulation(
    runs: int = DEFAULT_RUNS, seed: int | None = None
) -> SimulationReport:
    if type(runs) is not int or runs < 1:
        raise ValueError("runs must be a positive integer")
    generator = random.Random(seed)
    successes = 0
    total_attempts = 0
    higher_hints = 0
    lower_hints = 0
    started = time.perf_counter()

    for _ in range(runs):
        success, attempts, game_higher_hints, game_lower_hints = play_game(generator)
        successes += int(success)
        total_attempts += attempts
        higher_hints += game_higher_hints
        lower_hints += game_lower_hints

    elapsed_seconds = time.perf_counter() - started
    failures = runs - successes
    return SimulationReport(
        runs=runs,
        successes=successes,
        failures=failures,
        success_rate_percent=round(successes / runs * 100, 3),
        average_attempts=round(total_attempts / runs, 3),
        higher_hints=higher_hints,
        lower_hints=lower_hints,
        elapsed_seconds=round(elapsed_seconds, 3),
        seed=seed,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=positive_integer, default=DEFAULT_RUNS)
    parser.add_argument("--seed", type=int)
    arguments = parser.parse_args()
    print(json.dumps(asdict(run_simulation(arguments.runs, arguments.seed)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
