# Display golden frames

These files are hand-authored design contracts for one complete 9x34 matrix frame.
They are transcribed from the approved mockup, never generated from renderer output.

- `.` = off (`0`)
- `-` = dim weather pixel (`64`): dim moon, stars, sun-ray tips, drizzle, fog, hail trail
- `L` = secondary pixel (`128`): equalizer bar body
- `#` = primary widget pixel (`220`): bars, digits, cloud, equalizer peak
- `?` = dim unknown-state pixel (`24`)

Every file has exactly 34 rows of 9 pixels: all 306 LEDs are reviewed. A deliberate
layout change requires updating the mockup, editing the affected golden by hand, and
performing one visual hardware check.

Left panel: rows 0–20 battery (columns 0–1), CPU (3–5) and RAM (7–8) bars, row 21 full-width
base, row 22 dark, rows 23–33 mirrored microphone equalizer.
Right panel: rows 0–9 weather icon, 11–15 outside temperature, 17–21 CPU temperature without
a unit, row 22 dark, rows 23–33 microphone equalizer.

`left_dashboard.txt` is the approved left mockup (78 / 35 / 62 %).
`right_weather_showers_day_0.txt` is the approved right mockup verbatim (WMO 80, 29°, 105°).
`right_local_metrics.txt` covers the dim unknown-weather state, a two-digit CPU value and a
silent microphone.
`right_weather_clear_0.txt` and `right_weather_clear_1.txt` lock the two phases of the
clear-sky animation (frames 0 and 2: the rays turn once a second). `right_weather_partly_cloudy_night_0.txt`,
`right_weather_heavy_rain_day_0.txt` and `right_weather_thunder_hail_night_1.txt` cover the
night palette with the moon in the top-right corner, heavy precipitation and the most severe
documented WMO state. `left_alert_battery_off.txt` and `right_alert_cpu_off.txt` lock the
local blanking boundary for one critical widget while all unrelated pixels stay unchanged.
