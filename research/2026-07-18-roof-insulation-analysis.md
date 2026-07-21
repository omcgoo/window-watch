# Solar geometry, the butterfly roof, and what loft insulation would buy

*Window Watch research note — 18 July 2026. All figures derived from the flat's own
logged data (~1,300 half-hourly samples, June–July 2026) via the calibrated
first-order thermal model.*

## 1. Facade bearing: measured vs effective

The south windows face **160°** (measured off the map, front-to-back). Fitting the
facade bearing as a free parameter against the temperature history instead yields an
*effective* bearing of **~139°** (from 715+ sunny sample pairs).

The ~21° gap ≈ **1.4 hours of sun travel** and is interpreted as the solid brick
wall's thermal lag: the model assumes instantaneous response, so it compensates by
shifting the bearing east (toward earlier sun). Tested alternatives (roof pitch
asymmetry — see §3) do not explain it.

**Decision:** the measured 160° drives all live solar projections and advisory
timing, because blinds/windows act on radiation the instant it strikes the glass —
pure geometry. The fitted bearing gains only ~0.3% prediction RMSE (0.1525 vs
0.1530) and would mistime every sun-on-glass call by ~1.5h. The effective bearing is
retained as a **lag diagnostic**: the measured−effective gap should *shrink* after
insulation work, making it a building-performance indicator alongside the time
constant.

## 2. Splitting solar gain: glass vs roof

The single solar coupling `b` was decomposed into two channels with distinct daily
signatures:

- **South glass** — facade-projected irradiance at 160°, shaded by the external
  blinds when down (×0.15)
- **Butterfly roof** — horizontal irradiance (GHI), never shaded by blinds

The July 1–12 holiday (blinds down + windows shut for 12 days) provides the key
contrast: solar response in that period is predominantly roof, which is what makes
the two channels separable.

**Closed-regime fit (755 pairs — the trustworthy one):**

| channel | coefficient | strong-sun gain |
|---|---|---|
| glass (facade ~500 W/m²) | b ≈ 0.000665 | **+0.33 °C/hr** |
| roof (GHI ~800 W/m²) | b ≈ 0.000159 | **+0.13 °C/hr** |

Adding the roof channel genuinely improves the fit (RMSE 0.0905 → 0.0872), so the
signal is real. **~30% of the flat's solar heat-soak arrives through the roof**
(±10 points — the two signals are correlated, both being GHI-driven). Integrated
over a clear July day the roof feeds **~1 °C/day** into the living room. Critically,
the roof is the one gain path the blinds cannot touch.

## 3. Butterfly roof geometry

The valley channel runs front-to-back (along the 160°↔340° axis), so the two pitches
face **~70° (E)** and **~250° (W)**.

**Symmetry theorem:** for equal pitch areas and tilt θ, the direct-beam terms of the
two faces are equal and opposite in the east–west direction — summed, a balanced
butterfly behaves *exactly* like a horizontal roof scaled by cos θ. The plain-GHI
roof channel in §2 is therefore geometrically correct, not an approximation.

**Asymmetry test:** a fourth fitted channel carrying the east-minus-west difference
signal comes out faintly west-leaning but with negligible fit improvement (RMSE
0.0872 → 0.0871). Conclusion: **the roof is balanced within measurement precision**
(confidence ~±20% — the test is weakest at low sun, where asymmetry lives). This
also rules out roof asymmetry as the cause of the 139° effective-bearing skew,
strengthening the wall-lag interpretation.

## 4. What loft insulation would do

Assume ~300 mm loft roll: roof U-value ~2.3 → ~0.13 W/m²K, killing ~90–95% of the
roof solar channel (modelled below as ×0.07). Conductance `a` held fixed —
conservative, since insulation trims that too.

### 4a. Single hot day (32 °C), consecutive days

| 32 °C day | uninsulated peak | insulated peak | saving |
|---|---|---|---|
| 1 | 24.5 °C | 23.5 °C | 1.0 °C |
| 2 | 25.5 °C | 23.8 °C | 1.7 °C |
| 3 | 25.9 °C | 23.9 °C | 2.0 °C |

The benefit **compounds**: with τ ≈ 26 h the overnight flush never clears the roof
input, so the uninsulated flat ratchets upward while the insulated one plateaus.

### 4b. Ten-day heatwave (2026-style: builds 28→33 °C, holds, breaks)

- Saving saturates at **~2.2–2.3 °C** by mid-wave and holds
- Uninsulated flat spends days 5–8 above 26 °C indoors; insulated never crosses 24.5 °C
- Degree-hours above 26 °C across the wave: **12 vs 0**
- The debt outlives the weather: after the wave breaks, the uninsulated flat is
  still ~2 °C warmer and takes ~2 extra days to recover

### 4c. Climate-crisis extreme (5 days at 38–39 °C, hot nights, pre-warmed flat)

| metric | uninsulated | insulated |
|---|---|---|
| worst indoor peak | 32.7 °C | 30.3 °C |
| worst night low | 30.5 °C | 28.9 °C |
| degree-hours > 26 °C | 458 | 233 |
| degree-hours > 28 °C | **220** | **67** |
| recovery after break | ~2 days slower | — |

The ~2.4 °C peak saving is worth disproportionately more here: **exposure to the
genuinely harmful >28 °C band drops ~70%**. Honest limit: insulation alone does not
make a 39 °C wave comfortable — it converts *dangerous* into *bad* and shortens bad
by days. Active cooling would still be needed for the worst 2–3 days.

**Caveats:** coefficients were fitted on ≤32 °C outdoor data, so §4c is a linear
extrapolation (likely understating the uninsulated case); synthetic day-curves;
absolute indoor values are looser than the differences between scenarios, which are
the robust output.

## 5. Follow-ups

- **Before any insulation work: take a dated calibration snapshot** (a, b_glass,
  b_roof, τ, effective bearing) — the before/after comparison is the measurement.
- Winter + a heating-energy meter unlocks the absolute HTC (W/K) via energy-signature
  regression — the number that can't be extracted from passive summer data.
- Metric of choice for scenario comparison: **degree-hours above threshold** — it
  captures the trend/accumulation behaviour that daily peaks understate.
- Deferred feature ("scenario lab"): interactive what-if simulator with
  blinds/windows/insulation levers over a chosen day profile, incl. counterfactual
  "what if I hadn't followed the advice" curves.
