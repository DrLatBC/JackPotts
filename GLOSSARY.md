# Jack Potts

A rule-based bot that plays Balatro on its own. These are the words the project uses for its own decisions. Balatro's own terms (ante, blind, joker, tarot, planet, seal, edition, enhancement) keep their in-game meaning and aren't repeated here.

## Playing a round

**Round plan**:
The bot's intended sequence of hands for the current round, with each hand marked as a milk, setup, finisher or scoring play. It is advisory: when a step can't run, the normal play rules decide instead.
_Avoid_: Hand sequence, schedule

**Milk**:
A play or discard made mainly to grow a scaling joker rather than to score, spent only when the bot is far enough ahead to afford it.
_Avoid_: Farm, feed, grind

**Setup play**:
A hand played to put a joker into the state the finisher needs, such as playing a hand type early so Card Sharp fires later in the round.

**Finisher**:
The round's strongest hand, held back for the final hand so jokers that pay out only on the last hand (Acrobat, Dusk) apply to it.
_Avoid_: Closer, last play

**Chase**:
Discarding toward a better hand type that isn't held yet (for example, keeping four hearts to draw a flush), taken only when its expected score beats playing now by a margin.
_Avoid_: Draw, fish

**Chase margin**:
How much better a chase's expected score must be than the best play available now. It rises as discards run out and relaxes for hand types the build favours.

**Play EV**:
The expected score of a keep-set once the discarded cards are replaced by random draws from the remaining deck.
_Avoid_: Hand value, expected hand

**Junk padding**:
Cards added to a play that don't change its hand type, so they leave the hand without spending a discard.
_Avoid_: Filler, free discard

**Card protection**:
How strongly a card should be kept rather than discarded or destroyed, combining build affinity, boss constraints and The Idol's target card.
_Avoid_: Keep priority, card value

## Building a run

**Build**:
The run's current direction (which hand types, ranks, suits and enhancements it leans on), inferred from owned jokers, deck makeup and hand levels.
_Avoid_: Strategy (in conversation), deck plan

**Affinity**:
How strongly the build favours a particular hand type, rank, suit or enhancement. Every keep, buy and tarot decision reads it.
_Avoid_: Preference, bias

**Archetype**:
A named build theme that cuts across hand types, such as face cards, with member jokers, amplifier jokers and anti-jokers.
_Avoid_: Deck type, synergy group

**Roster**:
The jokers the bot currently owns, in slot order.
_Avoid_: Lineup, joker hand

**Scaling joker**:
A joker whose value grows from a repeatable trigger (playing, discarding, selling, rerolling, using a planet). The trigger defines how the bot can milk it.
_Avoid_: Growth joker, stacking joker

**Anchor**:
A scaling joker's current value, read from its "Currently X" text, which values project forward from.

**Fodder**:
A joker bought or kept so another joker can consume it, chiefly Ceremonial Dagger eating the joker to its right.
_Avoid_: Sacrifice, food

**Anti-synergy**:
A pair of jokers whose effects work against each other, so owning one lowers the other's value.

## The shop

**Economy phase**:
The shop's spending posture for the current ante: BUILD (antes 1 to 3), FLEX (antes 4 to 5) or SPEND (ante 6 onward). Each sets a cash reserve and the two aggression levels.
_Avoid_: Budget mode, stage

**Scoring aggression**:
How eagerly the shop buys scoring power, such as jokers and planets.

**Speculative aggression**:
How eagerly the shop makes variance spends, such as rerolls and packs.

**Action plan**:
An ordered set of shop steps (buy, sell, reorder, reroll, open pack, use consumable) ranked as one unit. The bot runs one step per tick and checks the rest again before each one.
_Avoid_: Shop move, combo

**Joker value**:
What one joker is worth to this run right now: its scoring gain against the real deck and roster, adjusted for synergy, ante urgency and likely bosses. It is the single price used to buy, sell, pick from packs and pick Hex targets.
_Avoid_: Joker score, EV delta (in conversation)

**Value map**:
A table of joker values computed across standard scenarios (archetype by ante), published to the dashboard so valuation changes can be compared between batches.

## Running and measuring

**Instance**:
One Balatro process plus the bot driving it, identified by its port.
_Avoid_: Worker, slot

**Batch**:
A numbered set of games played across all instances in one supervisor session. It is the unit for stats and comparisons.
_Avoid_: Run (a run is one game), session

**Run**:
One game of Balatro from the first ante until a win or a loss.
_Avoid_: Episode

**Mismatch**:
A hand where the bot's predicted score differs from the score the game reported.
_Avoid_: Scoring bug, divergence
