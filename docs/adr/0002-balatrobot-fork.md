# Run against our fork of the balatrobot mod

Upstream balatrobot doesn't expose some state the scorer needs: edition values, joker ability data, The Ox's locked hand, Ancient Joker's suit and The Idol's round target. It also lacks boss forcing and has endpoint hangs. Without that state, predictions for those jokers and bosses are wrong. So the bot targets the DrLatBC fork, and we upstream fixes by PR rather than working around missing fields in Python. When a prediction looks wrong for one specific joker or boss, check the fork's Lua serializer before the Python scorer.
