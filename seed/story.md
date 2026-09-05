# The scripted show

`show.yaml` declares a production and two ordinary perturbations. Everything
below is *measured from the generated history*, not asserted in the config — if
you change the show, re-measure before quoting these numbers.

Dates are relative to whenever you seed, so the show is always mid-flight.

## Setting

*Nightfall*, a limited series. 200 shots across 12 sequences, ~21 weeks in,
**81% delivered**, delivery in 19 days. 67 artists in 10 pools. On paper the
show is fine: median crew load across pools is **35.6 h/week**.

## What actually happened

**T-25 days — a cache regression.** A lighting cache invalidation starts
failing frame 118 of every SEQ0420 comp render. Nobody connects it to the
schedule, because it is a farm problem and the farm has no idea what a
delivery date is.

**T-18 days — a director note.** Act three gets recut; the grade is colder, so
every comp in SEQ0420 needs revisiting. Ordinary. It lands on a pipeline that
is already quietly wasting farm time on exactly those shots.

## What the data shows

| Signal | SEQ0420 | Rest of show | |
|---|---|---|---|
| Render hours per comp iteration | **9.0 h** | 2.2 h | **4.1×** |
| Wasted render core-hours | **67 h** | 5–6 h | ~11× concentration |
| Comp iterations since the note | 1.73 | 1.46 | 1.2× — *only just started* |
| comp-pool-2 weekly hours | 38.6 → 31.9 → 46.1 → **89.8** | — | this week |

The ordering matters, and it is the argument for the product:

- **The rework signal is still weak.** Eighteen days after a note, with a comp
  pass taking about six days, barely two iterations have had time to happen.
  A producer watching iteration counts sees almost nothing yet.
- **The crew-load signal is loud but late.** By the time the pool hits 89.8
  hours, the overtime has already been worked.
- **The render-waste signal was loud from day one** — and lives in a system no
  producer opens, keyed by a job name no scheduling tool parses.

Joining the two planes turns "comp is behind and everyone is exhausted" into
*every comp iteration on this sequence costs four times what it should, and has
since before the note landed*. That is a fixable cause, and fixing it is worth
more than renegotiating the date.

## Why nothing here is hard-coded

The simulation applies the two perturbations and lets the numbers fall out.
Iteration counts, hours and waste are consequences of one modelling decision:
**a retake does not buy calendar.** The next pass overlaps the work already in
the artist's window, so concurrent load rises. That is what studio crunch is,
and it is why a burndown alone never predicts it.

Remove either beat from `show.yaml` and the numbers move on their own. A
forecast over a constant would prove nothing.
