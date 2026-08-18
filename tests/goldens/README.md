# Display golden frames

These files are hand-authored design contracts for one complete 9x34 matrix frame.
They must never be generated from renderer output.

- `.` = off (`0`)
- `L` = Game of Life (`128`)
- `#` = primary widget pixel (`220`)
- `?` = dim unknown-state pixel (`24`)

Every file has exactly 34 rows of 9 pixels: all 306 LEDs are reviewed. A deliberate
layout change requires updating the mockup, editing the affected golden by hand, and
performing one visual hardware check.

`right_local_metrics.txt` covers only the local widgets already implemented. Its weather
and outside-temperature rows stay dark until the weather design slice adds its own goldens.
