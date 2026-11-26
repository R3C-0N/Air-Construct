# Validation: heated blocks in a channel

Three aluminium blocks (20 × 20 × 10 mm, 1 W each) on the floor of a flat channel
(300 × 100 × 20 mm). Air enters the whole `xmin` face at 0.3 m/s and 20 °C and leaves
through the whole `xmax` face. Channel walls are adiabatic. `block_b` is the mirror
image of `block_a` about the channel mid-plane; `block_c` sits 80 mm downstream of
`block_a`, in its wake.

```
y ^        [B]
  |
  |        [A]              [C]
  +--------------------------------> x (0.3 m/s)
```

Flow: Re = U·H/ν ≈ 400 on the channel height, so the case is run laminar and steady.
Mesh: 2.5 mm cells, 38 400 cells; about 1-2 minutes on one core.

Run it: `airconstruct run validation/heated_blocks/case.yaml`, or
`pytest -m openfoam tests/test_validation_run.py`, which also writes
`runs/heated_blocks/validation.md`. CI runs it on every PR (OpenFOAM job; the
report is in the job summary and in the `simulation-outputs` artifact).

## Checks

| check | reference | tolerance | why |
|---|---|---|---|
| outlet / inlet mass flow | 1 | ± 0.5 % | mass conservation |
| heat carried by the air / power | 1 | ± 2 % | energy conservation (adiabatic walls) |
| outlet mean temperature rise | P / (ṁ·cp) = 4.13 K | ± 2 % | same, as a temperature |
| T(block_b) − T(block_a) | 0 | ± 2 % of the block's rise | symmetry |
| (T_c − T_in)/(T_a − T_in) − 1 | in (0, 1] | | the wake of A pre-heats C |
| h / h_flat_plate per block | 0.7 … 3 | | h = P/(A_wet·ΔT) against laminar flat-plate Nu = 0.664 Re^½ Pr^⅓ |

The first three are exact at steady state, so they catch errors in the solver setup
or the post-processing. The last one is a plausibility band, not a precise
reference: a short block in a channel has a higher h than a flat plate of the same
length (leading edges, flow acceleration), but not lower than about 0.7 times.

## Reference results (Air-Construct, 2.5 mm mesh)

| | block_a | block_b | block_c |
|---|---|---|---|
| T mean [°C] | 80.2 | 79.8 | 88.3 |
| h [W/m²K] | 13.9 | 13.9 | 12.7 |

Outlet 24.12 °C (analytic 24.13 °C), pressure drop 0.25 Pa, max air speed 0.74 m/s.

## Comparing with SimScale

To cross-check against SimScale (Conjugate Heat Transfer v2 analysis): build the
same geometry, laminar, steady, gravity −z, air at 20 °C / 101 325 Pa, velocity inlet
0.3 m/s, pressure outlet 0 Pa gauge, adiabatic walls, aluminium blocks
(k = 200 W/m·K) with a 1 W volumetric heat source each. Compare the block mean
temperatures and the outlet mean temperature with the table above.
