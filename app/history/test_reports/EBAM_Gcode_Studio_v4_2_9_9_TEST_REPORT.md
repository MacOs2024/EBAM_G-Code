# EBAM G-code Studio v4.2.9.9 - test report

## PDF report review

Read/rendered PDF: `EBAM_Gcode_Studio_Continuation_Report_2026_07_09.pdf`.
Pages: 8. It describes a parallel version with `EXPERIENCE CALIBRATION`: using measured cylinder results, manual override values, Z-offset and target wall to build a next G-code profile.

## Implemented from PDF

- Experience calibration tab.
- JSON/CSV profile export.
- Zone profile calculation: start capture / stable wall / upper Z correction.
- Target-wall guard: do not copy high Wire override when measured wall is too thick.
- Upper Z-step correction from Z-offset.
- Profile application to the no-pause rotary C generator.
- Stats/audit markers for enabled experience profile.

## Important implementation correction found during testing

Initial implementation calculated upper-zone E2 before applying corrected upper Z-step. That made the estimated upper wall thicker when Z-step was reduced. Fixed by recalculating upper E2 using `z_upper`.

Before:

```python
e2_target_upper = _wire_for_target_wall(target_wall, z0, f_upper, d)
```

After:

```python
# Important: calculate upper-zone E2 with the corrected upper Z-step, otherwise the wall becomes too thick when Z-step is reduced.
e2_target_upper = _wire_for_target_wall(target_wall, z_upper, f_upper, d)
```

## Tests

| Test | Result |
|---|---:|
| py_compile | PASS |
| compileall | PASS |
| extended_parameter_smoke.py | 207/207 PASS |
| deep_interaction_test.py | 213/213 PASS |
| experience_profile_test.py | 12/12 PASS |

## No-pause invariants checked

- No `M68` inside continuous movement block.
- No `G4` inside continuous movement block.
- No `W` commands inside continuous movement block.
- C360 count equals generated layer count.
- C+Z transitions use zone Z-step when profile is enabled.
- Cfeed follows zone profile.
- E2 target-wall guard works.
