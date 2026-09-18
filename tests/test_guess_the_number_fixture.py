from __future__ import annotations

from guess_the_number_game.auto_mated_guess_the_number import run_simulation


def test_run_simulation_reports_every_requested_game() -> None:
    report = run_simulation(1_000, seed=17)

    assert report.runs == 1_000
    assert report.successes + report.failures == report.runs
    assert 0 <= report.success_rate_percent <= 100
    assert 1 <= report.average_attempts <= 10
