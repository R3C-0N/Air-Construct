# Contributing

Work is split into workstreams, each owning a set of files (see the `Owned by:` line at
the top of each module). Change another workstream's file only for a small interface
fix, and say so in the PR.

- Keep the public functions' signatures stable; extend with keyword arguments.
- Unit tests must run without OpenFOAM. Tests needing OpenFOAM are marked
  `@pytest.mark.openfoam` and run in the `openfoam` job of the full CI.
- `ruff check .` and `pytest -q` must pass.

## Branches and CI

- `develop` is the integration branch and the target of every feature branch's PR.
  These PRs (and pushes to `develop`) run the fast CI, **CI rapide**
  (`ci-fast.yml`): `ruff check .` and `pytest -q` on Linux, a few minutes.
- `main` only receives `develop` when a large workstream is finished. PRs into `main`
  and pushes to `main` run the full CI: **CI complète** (`ci-full.yml`: unit tests on
  Linux + Windows, real OpenFOAM simulations, validation report) and
  **Build desktop app** (`build.yml`: Windows installer, Linux bundle).
- Either full workflow can be started by hand on any branch from the Actions tab
  (*Run workflow*) when a change needs it before reaching `main`.
