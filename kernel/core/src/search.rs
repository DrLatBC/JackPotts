//! Every play out of a hand, scored and ranked.
//!
//! Mirrors `balatro_bot.domain.scoring.search`.

use std::cmp::Ordering;
use std::sync::OnceLock;

use crate::card::Card;
use crate::hands::{classify, rank_counts, scoring_cards, HAND_COUNT};
use crate::lineup::{Lineup, Play, MAX_CARDS};

/// What the round allows a play to be.
#[derive(Clone, Debug)]
pub struct Constraints {
    pub min_select: usize,
    pub max_select: usize,
    /// The Mouth: only this hand type.
    pub required_hand: Option<u8>,
    /// The Eye: bit per hand type already used.
    pub excluded_hands: u16,
    /// Cerulean Bell: bit per hand position that must be played.
    pub required_cards: u16,
    /// A tie-break weight per hand type, when the roster has a plan.
    pub affinity: Option<[f64; HAND_COUNT]>,
}

impl Default for Constraints {
    fn default() -> Self {
        Constraints { min_select: 1, max_select: 5, required_hand: None, excluded_hands: 0, required_cards: 0, affinity: None }
    }
}

/// One scored play out of a hand.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Found {
    pub hand_id: u8,
    /// Bit per hand position played.
    pub cards: u16,
    /// Hand positions that score, in scoring order, and how many.
    pub scoring: [u8; 8],
    pub scoring_len: u8,
    pub chips: f64,
    pub mult: f64,
    pub total: f64,
}

type Subset = (u8, [u8; 5]);

/// Every choice of 1 to 5 positions out of `n`, smallest first, in the order
/// Python's `itertools.combinations` gives them.
fn subsets(n: usize) -> &'static [Subset] {
    static CACHE: [OnceLock<Vec<Subset>>; MAX_CARDS + 1] = [const { OnceLock::new() }; MAX_CARDS + 1];
    CACHE[n].get_or_init(|| {
        let mut out = Vec::new();
        for size in 1..=5.min(n) {
            let mut pick: Vec<usize> = (0..size).collect();
            loop {
                let mut entry = [0u8; 5];
                for (k, &p) in pick.iter().enumerate() {
                    entry[k] = p as u8;
                }
                out.push((size as u8, entry));
                let mut k = size;
                while k > 0 && pick[k - 1] == n - size + k - 1 {
                    k -= 1;
                }
                if k == 0 {
                    break;
                }
                pick[k - 1] += 1;
                for j in k..size {
                    pick[j] = pick[j - 1] + 1;
                }
            }
        }
        out
    })
}

/// A hand to search: the cards, and the same cards as they would be with a Gold
/// enhancement and with none (empty unless the lineup has Midas Mask or Vampire).
pub struct Hand<'a> {
    pub cards: &'a [Card],
    pub gold: &'a [Card],
    pub plain: &'a [Card],
}

impl Lineup {
    fn score_subset(&self, hand: &Hand, positions: &[u8], limits: &Constraints) -> Option<Found> {
        let n = positions.len();
        let mut subset = [Card::default(); 5];
        for (k, &p) in positions.iter().enumerate() {
            subset[k] = hand.cards[p as usize];
        }
        let subset = &subset[..n];
        let counts = rank_counts(subset);
        let hand_id = classify(subset, &self.rules, &counts);
        if limits.required_hand.is_some_and(|required| required != hand_id) {
            return None;
        }
        if limits.excluded_hands & (1 << hand_id) != 0 {
            return None;
        }
        let picked = scoring_cards(subset, hand_id, &self.rules, &counts);

        // Without jokers the cards score as listed and held cards are not counted.
        let mut order = [0u8; 5];
        let mut held = [Card::default(); MAX_CARDS];
        let mut held_count = 0;
        let played_bits = positions.iter().fold(0u16, |m, &p| m | 1 << p);
        if self.no_jokers {
            order[..n].copy_from_slice(positions);
        } else {
            self.play_order(hand.cards, positions, &mut order);
            for (i, card) in hand.cards.iter().enumerate() {
                if played_bits & (1 << i) == 0 {
                    held[held_count] = *card;
                    held_count += 1;
                }
            }
        }

        let variants = self.has_before_phase();
        let mut cards = [Card::default(); 5];
        let mut gold = [Card::default(); 5];
        let mut plain = [Card::default(); 5];
        for (k, &p) in order[..n].iter().enumerate() {
            cards[k] = hand.cards[p as usize];
            if variants {
                gold[k] = hand.gold[p as usize];
                plain[k] = hand.plain[p as usize];
            }
        }
        let mut scoring = [0u8; 8];
        let mut scoring_hand = [0u8; 8];
        let mut in_scoring = 0u16;
        for (k, &within) in picked.as_slice().iter().enumerate() {
            let position = positions[within as usize];
            let played_at = order[..n].iter().position(|&p| p == position).unwrap() as u8;
            scoring[k] = played_at;
            scoring_hand[k] = position;
            in_scoring |= 1 << played_at;
        }
        let play = Play {
            hand_id,
            cards: &cards[..n],
            played_count: n,
            scoring: &scoring[..picked.len()],
            in_scoring,
            held: &held[..held_count],
            gold: &gold[..n],
            plain: &plain[..n],
        };
        let (chips, mult) = self.score(&play, None);
        Some(Found {
            hand_id,
            cards: played_bits,
            scoring: scoring_hand,
            scoring_len: picked.len() as u8,
            chips,
            mult,
            total: (chips * mult).floor(),
        })
    }

    /// Every allowed play, in enumeration order.
    pub fn plays<'a>(&'a self, hand: &'a Hand<'a>, limits: &'a Constraints) -> impl Iterator<Item = Found> + 'a {
        subsets(hand.cards.len())
            .iter()
            .filter(move |(size, _)| (limits.min_select..=limits.max_select).contains(&(*size as usize)))
            .filter(move |(size, positions)| {
                let bits = positions[..*size as usize].iter().fold(0u16, |m, &p| m | 1 << p);
                bits & limits.required_cards == limits.required_cards
            })
            .filter_map(move |(size, positions)| self.score_subset(hand, &positions[..*size as usize], limits))
    }

    /// Lower is better. With jokers the total decides, then the hand type; without,
    /// the hand type decides first. The roster's plan, then fewer cards, break ties.
    fn rank(&self, a: &Found, b: &Found, limits: &Constraints) -> Ordering {
        let weight = |f: &Found| limits.affinity.map_or(0.0, |w| -w[f.hand_id as usize]);
        let total = b.total.partial_cmp(&a.total).unwrap_or(Ordering::Equal);
        let hand = a.hand_id.cmp(&b.hand_id);
        let (first, second) = if self.no_jokers { (hand, total) } else { (total, hand) };
        first
            .then(second)
            .then(weight(a).partial_cmp(&weight(b)).unwrap_or(Ordering::Equal))
            .then(a.cards.count_ones().cmp(&b.cards.count_ones()))
    }

    /// Every allowed play, best first; equal plays stay in enumeration order.
    pub fn search(&self, hand: &Hand, limits: &Constraints) -> Vec<Found> {
        let mut all: Vec<Found> = self.plays(hand, limits).collect();
        all.sort_by(|a, b| self.rank(a, b, limits));
        all
    }

    /// The best allowed play: the one `search` would put first.
    pub fn best(&self, hand: &Hand, limits: &Constraints) -> Option<Found> {
        let mut best: Option<Found> = None;
        for found in self.plays(hand, limits) {
            if best.as_ref().map_or(true, |b| self.rank(&found, b, limits) == Ordering::Less) {
                best = Some(found);
            }
        }
        best
    }
}
