# One shop evaluator ranks whole action plans

The shop is one decision. It lists every candidate plan (buy, sell, reorder, reroll, pack, voucher, consumable), ranks each by its value against the cost of spending the money, and runs the first step of the best plan, checking the rest again each tick. We replaced independent shop rules because they couldn't trade off against each other: a buy rule couldn't weigh itself against a reroll or a sell-down. New shop behaviour is a new kind of plan, not a new rule.
