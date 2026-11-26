"""Ultra simple desktop window.

Owned by: workstream "ui". Depends only on case.Case, runner.run_case,
results.summarize/load_regions and estimate.estimate_runtime_s.

Layout: left = form (enclosure size, components table, openings table, precision
slider with estimated time, ambient temperature), centre = 3D view (geometry before
the run, temperature/velocity after), bottom = log + Run/Stop buttons. Runs in a
QThread so the window stays responsive.
"""


def main() -> int:
    raise NotImplementedError
