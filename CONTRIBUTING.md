# Contributing

Work is split into workstreams, each owning a set of files (see the `Owned by:` line at
the top of each module). Change another workstream's file only for a small interface
fix, and say so in the PR.

- Keep the public functions' signatures stable; extend with keyword arguments.
- Unit tests must run without OpenFOAM. Tests needing OpenFOAM are marked
  `@pytest.mark.openfoam` and run in the `openfoam` CI job.
- `ruff check .` and `pytest -q` must pass.
