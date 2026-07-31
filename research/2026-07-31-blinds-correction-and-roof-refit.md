# The blinds were never 85%, and the roof matters more than we thought

*Window Watch research note — 31 July 2026. Supersedes §1 and §2 of the
[18 July note](2026-07-18-roof-insulation-analysis.md), and revises §4. Derived from
1,951 half-hourly samples (22 June – 31 July 2026), including the 25–31 July heatwave.*

---

## 0. What changed and why it propagated

The 18 July analysis assumed `BLIND_FACTOR = 0.15` — blinds passing 15% of solar, i.e.
blocking 85%. That figure described *external* blinds. These block **~41%**.

That single wrong constant sat upstream of almost everything else, because the
calibration used it to attenuate solar before fitting. Feeding the regression a solar
input four times too small on every blinds-down pair made it compensate by inflating the
coefficient: the `closed` regime's `b` came out **3.2× too high**. Two further defects
compounded it:

- **A blank blinds column was read as blinds-up.** ~40% of history predates the logging,
  and blinds-down is the lived default, so the fit was handed full sun on shaded hours.
- **`_blinds.eff` was never used.** The learner had been measuring the real block since
  commit `3eea1b6` and displaying it on the dashboard. Nothing fed it back into the model.

All three are fixed (`0eddc07`, `95b183d`). This note re-derives what they change.

---

## 1. Blinds effectiveness: ~41% blocked

Measured by within-regime contrast — blinds-up vs blinds-down compared only *inside* a
single window regime, so ventilation dilution cancels in the ratio instead of confounding
blinds with windows.

| specification | block |
|---|---|
| joint fit, open+part, shared τ (n=172) | 41% |
| + day fixed effects | 48% |
| + day and 3-hourly time-of-day fixed effects | 44% |
| day-clustered bootstrap | **41%, 90% CI [26%, 54%]** |
| leave-one-day-out range | 35–50% |
| the model's own bucketed learner | 38% (open 41% / part 36%) |

**The cleanest single test is 31 July**, when the blinds went up 07:00–11:00 and down
11:00–16:00 on one sunny day — same room, same fabric, sun on the glass throughout:

| | implied solar coefficient |
|---|---|
| blinds up (07–11h) | 1.20e-3 °C per W/m²·h |
| blinds down (11–16h) | 7.11e-4 |

→ **41% block**, matching the pooled estimate exactly. This is the within-day natural
experiment the July analysis lacked, and it is why the figure is now trustworthy at the
first significant figure.

**Placebos are clean.** Relabelling blinds-down-only pairs at random yields 0% median
(90% top 30%); labelling them morning-vs-afternoon yields 0%. The estimator does not
manufacture a blinds effect from time-of-day or noise.

**~41% is also physically sensible.** Internal blinds typically block 35–50%; external
shading achieves 70–90%. The old 0.15 seed was claiming external-blind performance from
internal-blind hardware.

### 1a. What they bought during the heatwave

Blinds were down every sun hour on 28, 29 and 30 July, so the week offers no
*within-heatwave* contrast (zero sunny blinds-up pairs on the 28th and 29th). Heat
avoided, from the energy integral `b·Σsolar·(1−τ)`:

| day | outdoor peak | heat avoided |
|---|---|---|
| 28 Jul | 30.0 °C | ~1.0 °C |
| 29 Jul | 33.6 °C, 7% cloud | ~1.4 °C |
| 30 Jul | 28.3 °C | ~1.1 °C |

On 29 July indoor peaked **29.8 °C against 33.6 °C outdoors**, rising only 2.4 °C while
outdoor rose 10.7 °C.

### 1b. Effect on the model

| | before | after |
|---|---|---|
| calibration one-step RMSE (weighted) | 0.1418 | 0.1359 |
| `closed` regime alone | 0.091 | 0.066 |
| **sensor-anchored day projection RMSE** | **0.921 °C** | **0.617 °C** (−33%) |
| mean signed error | +0.606 °C | −0.120 °C |

The projection figures are the ones that matter — that is the path that runs when the
Shelly is online. The systematic hot bias is eliminated, not merely narrowed.

---

## 2. Glass vs roof, refitted

The 18 July split gave glass `b ≈ 0.000665` and roof `b ≈ 0.000159`, concluding **~30% of
solar heat-soak arrives through the roof**. Refitting the same two-channel decomposition
with the corrected factor, the blank-state guard, and three extra weeks of data:

| setup | n | a | b_glass | b_roof | RMSE |
|---|---|---|---|---|---|
| old (0.15, no guard) | 579 | 0.01180 | 5.02e-4 | 1.62e-4 | 0.0474 |
| 0.15, with guard | 575 | 0.01163 | 7.66e-5 | 1.95e-4 | 0.0472 |
| **new (0.59, with guard)** | **575** | **0.01158** | **−2.3e-5** | **2.08e-4** | **0.0472** |

Two things moved:

**The roof channel is ~28% stronger** — `b_roof` 1.62e-4 → 2.08e-4. Integrated against
real irradiance that is **1.2–1.4 °C/day** into the living room on a clear day (1.44 °C
on 29 July), against the note's ~1.0 °C. Day-clustered bootstrap: 1.78e-4, 90% CI
[1.27e-4, 2.06e-4], and it never came out ≤ 0 across 400 resamples. Adding the channel
still earns its place (single-channel RMSE 0.0492 → 0.0472).

**The glass channel collapses to nothing** in the closed regime — slightly negative,
i.e. indistinguishable from zero. Read plainly: on hot days with the windows shut and the
blinds down, essentially all remaining solar heat-soak arrives through a path the blinds
cannot touch.

### 2a. The identification caveat, stated plainly

The `closed` regime contains **zero sunny blinds-up pairs** — the blinds have never been
up on a hot day with the windows shut. Consequences:

- The glass regressor is proportional to facade solar whatever factor is used, so the
  factor only *rescales* `b_glass`. This is why the roof estimate is robust to the very
  error this note is correcting: `b_roof` moves only 1.95e-4 → 2.09e-4 across a
  0.15→0.75 sweep.
- But it also means glass and roof are collinear inside `closed` — both are GHI-driven —
  so the split is **weakly identified**. `b_glass ≈ 0` is *consistent* with the blinds
  working well; it is not proof of it. The 41% figure in §1 stands on the open/part
  regimes, where real contrast exists.

The roof share of as-lived solar gain is ~88% (90% CI 62–99%), but note this is measured
against *blinds-down* glass, whereas the July note's "~30%" measured against *unshaded*
glass. Both can be true. The second framing is the decision-relevant one: **once you are
doing the right thing with the blinds, the roof is nearly the whole remaining problem.**

---

## 3. What loft insulation would buy, revised

Assumptions unchanged from the July note: ~300 mm roll, killing ~93% of the roof channel
(modelled ×0.07). Conductance held fixed — still conservative.

Method changed in one important way: instead of synthetic day curves, this repeats the
**real logged 29 July profile** (20.1 → 33.5 °C, peak GHI 836 W/m², clear) back-to-back,
with regime switching so the **overnight flush is modelled** — windows open whenever it
is cooler outside, exactly as the app advises.

| day | uninsulated | insulated | saving |
|---|---|---|---|
| 1 | 28.0 | 27.3 | 0.8 °C |
| 2 | 28.4 | 26.6 | 1.8 °C |
| 3 | 28.6 | 26.2 | 2.4 °C |
| 6 | 28.8 | 25.7 | **3.1 °C** |

Day-clustered bootstrap on the day-6 saving: **3.1 °C, 90% CI [2.2, 3.9]**.
Degree-hours above 26 °C across six days: **237 → 17**.

The compounding the July note identified is real and survives correction — the
uninsulated flat ratchets while the insulated one settles. With a closed-regime half-life
of ~60 h, the overnight flush cannot clear a day's roof input before the next one lands.

### 3a. Three things that shape the decision

**Your night purge is already doing a quarter of the job.** The same scenario with the
windows held shut gives a 4.3 °C saving instead of 3.1 °C — the flush is capturing much
of the available benefit behaviourally, for free. This is not hypothetical: 30 July
restarted 3.6 °C below 29 July's peak.

**The benefit is heatwave-specific.** Repeating the milder 30 July profile (28.1 °C peak)
gives a 2.2 °C day-6 saving, but degree-hours above 26 °C go 1 → 0. Insulation buys
almost nothing in an ordinary summer and a great deal in the weeks that actually hurt.

**It degrades gracefully.** Day-6 saving against how much of the roof channel the work
actually kills:

| roof channel killed | day-6 saving |
|---|---|
| ~93% (300 mm) | 3.1 °C |
| 80% | 2.6 °C |
| 65% | 2.1 °C |
| 50% | 1.6 °C |

Even substantial underperformance retains most of the value.

### 3b. Honest limit on §3

The free-running multi-day simulation **under-predicts the observed 28–31 July peaks by
1.5–2.9 °C**:

| day | observed peak | model | error |
|---|---|---|---|
| 28 Jul | 28.2 °C | 26.1 | −2.1 |
| 29 Jul | 29.8 °C | 26.9 | −2.9 |
| 30 Jul | 28.3 °C | 26.8 | −1.5 |
| 31 Jul | 26.5 °C | 25.9 | −0.6 |

So absolute levels here are not trustworthy; the **differences between scenarios** are
the robust output — the same caveat the July note made, and it still applies. The error
runs in the direction of *understating* the saving, since it is the uninsulated ratchet
the model is failing to reproduce. Note this is the free-running sim; the sensor-anchored
projection used in production is accurate to 0.617 °C RMSE (§1b).

---

## 4. The wall-lag diagnostic is retired

§1 of the July note fitted an *effective* facade bearing of ~139° against a measured 160°,
and read the ~21° gap as the brick wall's thermal lag — proposing it as a
building-performance indicator to track across insulation work.

Fixing the blind factor and the blank-state guard moves that fitted bearing from **146° to
169° on identical data**. It now sits *west* of measured, which flips the sign and makes
"lag" an untenable reading. The gap was substantially an artifact of feeding the scan
shaded hours labelled as exposed.

Consequences:

- The dashboard's `wall lag ≈ Xh` readout (`docs/index.html:959`) will render **negative**
  and should be reworked or removed.
- **Do not baseline pre-insulation lag off the 139°/146° figures.** The bearing estimate
  is more sensitive to blind-state labelling than to wall thermal mass. If it is to track
  anything, it needs re-establishing on clean post-fix data first.
- The measured 160° continues to drive all live projections, unchanged. That decision was
  never contingent on the fitted bearing.

The time constant remains a sound performance indicator: closed-regime half-life ~60 h,
open ~23 h.

---

## 5. Follow-ups

- **Highest-value missing measurement, unchanged in kind but sharper in aim:** a hot clear
  day with **windows shut and blinds up for a few midday hours**. The `closed` regime has
  never seen one, and it is the single observation that would break the glass/roof
  collinearity in §2a and convert the insulation case from strong to settled.
- Still take a **dated calibration snapshot before any insulation work** (a, b_roof, τ) —
  the before/after comparison remains the real measurement. Take it *after* these fixes,
  not from the July figures.
- Winter plus a heating-energy meter still unlocks absolute HTC (W/K) via
  energy-signature regression.
- `simulate_indoor_day()` — the no-sensor fallback — anchors to
  `min(overnight outdoor, INDOOR_BASE)` with `INDOOR_BASE = 19`, starting this flat 6–8 °C
  too cold in summer. Unrelated to the above and untouched; only bites when the Shelly is
  offline.
