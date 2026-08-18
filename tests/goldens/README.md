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

`right_local_metrics.txt` covers local widgets plus the dim unknown-weather state.
`right_weather_clear_0.txt` and `right_weather_clear_1.txt` independently lock the two
approved clear-weather animation phases and the complete right-panel layout.
`right_weather_partly_cloudy_night_0.txt`, `right_weather_heavy_rain_day_0.txt`, and
`right_weather_thunder_hail_night_1.txt` cover the night palette, heavy precipitation,
and the most severe documented WMO state without deriving expectations from renderer output.
