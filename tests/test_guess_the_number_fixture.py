from __future__ import annotations

import pytest

from test_code_review.auto_mated_guess_the_number import run_simulation


@pytest.mark.parametrize("runs", [0, -1, -10, True])
def test_run_simulation_rejects_invalid_direct_call_counts(runs: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        run_simulation(runs, seed=17)


def test_run_simulation_reports_every_requested_game() -> None:
    report = run_simulation(1_000, seed=17)

    assert report.runs == 1_000
    assert report.successes + report.failures == report.runs
    assert 0 <= report.success_rate_percent <= 100
    assert 1 <= report.average_attempts <= 10
