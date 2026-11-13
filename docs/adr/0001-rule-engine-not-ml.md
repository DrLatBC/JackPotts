# Hand-tuned rule engine instead of machine learning

The bot decides with a priority-ordered list of rules for each game state; the first rule that matches acts. The rules lean on exhaustive hand enumeration, Monte Carlo draws and a full simulation of Balatro's scoring. We chose this over a learned policy so every decision can be traced to a rule and a number, debugged against the game's own scores and tuned by hand, without a training pipeline or a large game-play dataset. As a result, getting better means improving the simulation and the valuation, not collecting more data.
