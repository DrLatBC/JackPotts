# Scoring

Jack Potts fully simulates Balatro's scoring pipeline before picking a hand. Measured accuracy against the game's actual chip totals sits at **99.89%** across our integration test corpus.

## Pipeline

```
base chips + mult from hand type
  + per-card chips  (rank value + enhancement bonuses)
  + joker chip effects
  --> chip total

  × mult accumulators (joker +mult, edition +mult, enhancement +mult)
  × xmult accumulators (joker xMult, edition Polychrome, Glass, …)
  --> final score
```

Joker effects are applied in left-to-right order, which matches the game. The pipeline lives in `joker_effects/` — `registry.py` drives it, `context.py` holds the mutable `ScoreContext` (chips, mult, xmult, scoring cards, held cards), and `simple.py` / `complex.py` hold the individual effect functions.

## Card rules

Handled in `domain/scoring/`:

- **Enhancements**: Bonus (+30 chips), Mult (+4 mult), Wild (any suit), Steel (x1.5 mult when held), Glass (x2 mult, may shatter), Gold (+$3 at round end if held), Lucky (RNG mult/money), Stone (+50 chips, no rank/suit, always scores).
- **Editions**: Foil (+50 chips), Holographic (+10 mult), Polychrome (×1.5 mult), Negative (+1 joker slot).
- **Seals**: Gold (+$3 on score), Red (retrigger), Blue (planet on round end if held), Purple (tarot on discard).
- **Retriggers**: Seltzer, Hanging Chad, Dusk, Sock and Buskin, red seals, Hack.

## Boss blinds

Most boss blinds are handled by mutating `RoundContext` before decisions are made (`context.py`):

| Boss | Handling |
|------|----------|
| The Flint | Halve all hand levels |
| The Arm | Reduce hand levels on play |
| The Eye | Track played hand types; exclude from best-hand search |
| The Needle | Treated as one-shot via `hands_left=1` |
| The Head / Club / Window | Set `scoring_suit` (feeds `CardProtection`) |
| The Mouth | Hand type lock |
| The Psychic | 5-card minimum |
| Verdant Leaf | `VerdantLeafUnlock` rule sells to clear debuff |
| Crimson Heart | Score discount |
| Cerulean Bell | Forced card index |
| The Ox | `most_played` tracking from API |
| The Wall | Milk margin in play policy |

Still on the backlog: The Goad, Serpent, Water, Tooth, Fish, Hook, Pillar, Plant. See the TODOs in [CLAUDE.md](../CLAUDE.md) for specifics.

## The Idol mod patch

Upstream balatrobot does not expose `G.GAME.current_round.idol_card` — The Idol's per-round target rank+suit. Without it, The Idol's ×2 mult couldn't be predicted and scoring diverged from actuals on ~99% of hands with The Idol active.

Jack Potts ships with a patch on the [DrLatBC/balatrobot](https://github.com/DrLatBC/balatrobot) fork (`main` branch). The patch adds `round.idol_card = {rank, suit}` to the gamestate serializer in `src/lua/utils/gamestate.lua`. The bot threads this through `Snapshot -> RoundContext -> ScoreContext -> _idol` in `joker_effects/complex.py`, applying ×2 mult per matching scored card.

When upstream merges changes to `gamestate.lua`, verify the idol_card block survives.

## Hand enumeration

`domain/scoring/search.py` enumerates every 5-card subset (or smaller for hands like Pair / High Card) via `enumerate_hands`. With jokers owned, candidates are ranked by total score; without, by hand-type priority first.

`discard_candidates` returns unranked chase tuples — the policy layer does the Monte Carlo ranking (see [architecture.md](architecture.md#unified-discard-ranking)).

## Strategy layer

`strategy.py` derives a `Strategy` from the current joker set:

- `preferred_hands` — joker hand-type affinity weights
- `preferred_suits` — joker suit affinity
- `preferred_ranks` — joker rank affinity (Triboulet K/Q, Fibonacci, Even Steven, …)
- `active_archetypes` — cross-cutting builds (face card, etc.)

This drives planet buying, discard protection, pack picks, joker valuation, and consumable targeting.
