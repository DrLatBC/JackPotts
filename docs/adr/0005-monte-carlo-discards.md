# Rank discards by sampled draws with full joker scoring

Each candidate keep-set is valued by drawing random replacements from the remaining deck and scoring the best hand on each draw with the full joker simulation, then averaging the results. A cheaper first pass that ignored jokers was rejected: it threw out the keeps that only score well with the current roster (heart-heavy keeps under Bloodstone, kings and queens under Triboulet) before scoring ever saw them. This costs CPU per decision, which we accept.
