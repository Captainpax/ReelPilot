# Statistics and Estimated Values

SQLite at `%LOCALAPPDATA%\ReelPilot\stats\reelpilot.db` is ReelPilot's canonical history.
CSV and JSON are compatibility exports written atomically during safe shutdown.

## F5 refresh

F5 is deliberately restricted to READY and PAUSED. A refresh:

1. upserts the bundled factual catalog;
2. crosses a bounded writer barrier, guaranteeing that earlier catch events committed;
3. reconciles and queries lifetime history; and
4. opens the paged history dashboard.

Press F5 again to return. Page Up and Page Down navigate recognized species. During active
automation F5 is ignored with a reminder to pause; it does not change input or state.

## Schema version 5

- `sessions` records settings, runtime, version context, stop reason, food consumed,
  minimum reliable energy, and inventory-full stops.
- `encounters` records casts, bites, minigames, timing, controller profile, outcome,
  MAX verification, Perfect status, containment breaks, minimum margin, and treasure
  seen/attempted/secured/looted state.
- `catches` contains one unique latched result per encounter plus catalog enrichment.
- `catalog_items` contains vanilla identifiers, English canonical names, base prices,
  difficulty, motion type, and catalog version.

Migration is transactional and idempotent. Recognized historical names are backfilled;
unknown results remain unknown. Active encounters belonging to closed sessions become
`aborted` so they cannot silently distort history.

## MAX and Perfect rates

MAX verification is `verified` only when bounded English OCR sees Stardew's post-cast
`MAX` label. Visual-meter release without that label is `estimated`; unavailable evidence
is `unknown`.

Perfect status is `confirmed` only when OCR sees Stardew's `Perfect!` indicator. A
reliable fish-edge escape marks `missed`; incomplete edge evidence remains `unknown`.
The displayed Perfect rate is confirmed divided by confirmed plus missed, with unknown
encounters reported separately instead of silently treating them as success or failure.

## Fishing treasure

Treasure totals intentionally separate four facts: the chest appeared, ReelPilot spent a
targeting attempt, the in-minigame chest was secured, and every visible post-catch loot
stack was transferred. A collected chest is not reported as looted until the item menu is
observed empty after at least one transfer. If no inventory slot accepts an item, ReelPilot
leaves the menu open and stops so the player can decide what to keep.

## Energy and inventory safety

Lifetime history includes verified food consumption and inventory-full stop totals. The
minimum energy field is the lowest confident screen observation, not save-file data. A food
item is counted only after the meter rises by at least three percentage points; clicking an
eating prompt alone is insufficient. An inventory stop is counted only when a visible
item-grab menu retains source items after an attempted transfer sweep.

## Rarity and difficulty

Observed share is personal history, calculated as:

```text
species recognized quantity / all recognized fish quantity
```

Items and unknown results are excluded. ReelPilot also reports Stardew's minigame
difficulty tiers: easy 0–33, medium 34–66, hard 67–100, and extremely hard 101+.
Difficulty is not spawn rarity. Exact spawn odds also depend on location, season, time,
weather, depth, fishing level, luck, bait, and tackle, so ReelPilot does not present a
misleading universal percentage.

## Sell-value estimates

For a recognized fish, ReelPilot reports:

- base: normal-quality base price × quantity;
- possible maximum: iridium quality (2×) with Angler (1.5×), or 3× base; and
- no exact value because the result card does not expose quality or professions.

Non-fish items use their catalog base price without fish multipliers. Unknown results have
no estimate. Values exclude profit-margin settings, mods, processing such as the Fish
Smoker, special shop rules, and future balance changes.

```python
from reelpilot.stats import find_catalog_entry

bullhead = find_catalog_entry("Bullhead")
assert bullhead is not None
assert bullhead.estimate_value(quantity=2) == (150, 450)
```

The bundled catalog contains factual interoperability metadata for vanilla English
Stardew Valley 1.6.15.24356. It contains no descriptions, fonts, images, sprites, or XNB
content and is never updated over the network.
