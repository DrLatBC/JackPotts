//! Hand search and scoring for the JackPotts Balatro bot.
//!
//! - `card`: one playing card as scoring sees it
//! - `hands`: what hand a play makes and which cards score
//! - `jokers`: what each joker does
//! - `lineup`: scoring one play under one joker lineup, and play order
//! - `search`: every play out of a hand, ranked
//!
//! No Python in this crate; `kernel/py` is the binding.

pub mod card;
pub mod hands;
pub mod jokers;
pub mod lineup;
pub mod search;

#[cfg(test)]
mod tests {
    use crate::card::*;
    use crate::hands::*;
    use crate::jokers::*;
    use crate::lineup::*;
    use crate::search::*;

    fn card(rank: u8, suit: u8) -> Card {
        Card {
            rank,
            suits: suit,
            suits_smeared: suit,
            flags: if (11..=13).contains(&rank) { FACE } else { 0 },
            chips: if rank == 14 { 11.0 } else { rank.min(10) as f64 },
            xmult: 1.0,
            edition_xmult: 1.0,
            ..Card::default()
        }
    }

    fn around() -> Surroundings {
        let mut around = Surroundings { hands_left: 1.0, joker_limit: 5.0, ..Surroundings::default() };
        around.levels[PAIR as usize] = Level { chips: 10.0, mult: 2.0, ..Level::default() };
        around.levels[HIGH_CARD as usize] = Level { chips: 5.0, mult: 1.0, ..Level::default() };
        around.levels[FLUSH as usize] = Level { chips: 35.0, mult: 4.0, ..Level::default() };
        around
    }

    fn lineup(keys: &[&str]) -> Lineup {
        let jokers: Vec<JokerSpec> = keys.iter().map(|k| JokerSpec { key: k.to_string(), ..JokerSpec::default() }).collect();
        let steps = (0..jokers.len()).map(|i| Step { runs: Some(i), ..Step::default() }).collect();
        Lineup::new(LineupSpec { resolved: (0..jokers.len()).collect(), jokers, steps, around: around(), ..LineupSpec::default() })
    }

    fn best(lineup: &Lineup, cards: &[Card]) -> Found {
        lineup.best(&Hand { cards, gold: &[], plain: &[] }, &Constraints::default()).unwrap()
    }

    #[test]
    fn card_survives_the_byte_round_trip() {
        let c = Card { mult: 4.0, edition_mult: 10.0, flags2: GOLD | ENHANCED, ..card(13, SPADES) };
        assert_eq!(Card::from_bytes(&c.to_bytes()), c);
    }

    #[test]
    fn a_pair_scores_its_two_cards() {
        let hand = [card(9, SPADES), card(4, HEARTS), card(9, CLUBS)];
        let found = best(&lineup(&[]), &hand);
        assert_eq!(found.hand_id, PAIR);
        assert_eq!(&found.scoring[..found.scoring_len as usize], &[0, 2]);
        assert_eq!((found.chips, found.mult, found.total), (28.0, 2.0, 56.0));
    }

    #[test]
    fn five_of_one_suit_is_a_flush() {
        let hand = [card(2, HEARTS), card(5, HEARTS), card(8, HEARTS), card(9, HEARTS), card(13, HEARTS)];
        let found = best(&lineup(&[]), &hand);
        assert_eq!(found.hand_id, FLUSH);
        assert_eq!(found.total, (35.0 + 2.0 + 5.0 + 8.0 + 9.0 + 10.0) * 4.0);
    }

    #[test]
    fn joker_adds_four_mult() {
        let hand = [card(14, SPADES)];
        let found = best(&lineup(&["j_joker"]), &hand);
        assert_eq!((found.chips, found.mult, found.total), (16.0, 5.0, 80.0));
    }

    #[test]
    fn every_joker_phase_key_is_listed_once() {
        let phases = joker_phases();
        let mut keys: Vec<&str> = phases.iter().map(|&(key, _)| key).collect();
        keys.sort_unstable();
        keys.dedup();
        assert_eq!(keys.len(), phases.len());
    }

    #[test]
    fn each_hack_retriggers_a_two() {
        let hand = [card(2, SPADES)];
        assert_eq!(best(&lineup(&["j_hack"]), &hand).chips, 9.0);
        assert_eq!(best(&lineup(&["j_hack", "j_hack"]), &hand).chips, 11.0);
    }
}
