//! What hand a play makes and which of its cards score.
//!
//! Mirrors `balatro_bot.domain.scoring.classify`.

use crate::card::{Card, CLUBS, DIAMONDS, HEARTS, SPADES, STONE};

// Hand ids are positions in `balatro_bot.constants.HAND_TYPES`, best hand first.
pub const FLUSH_FIVE: u8 = 0;
pub const FLUSH_HOUSE: u8 = 1;
pub const FIVE_OF_A_KIND: u8 = 2;
pub const STRAIGHT_FLUSH: u8 = 3;
pub const FOUR_OF_A_KIND: u8 = 4;
pub const FULL_HOUSE: u8 = 5;
pub const FLUSH: u8 = 6;
pub const STRAIGHT: u8 = 7;
pub const THREE_OF_A_KIND: u8 = 8;
pub const TWO_PAIR: u8 = 9;
pub const PAIR: u8 = 10;
pub const HIGH_CARD: u8 = 11;
pub const HAND_COUNT: usize = 12;

/// The game tries suits in this order and takes the first that enough cards count
/// as (`get_flush` over Steamodded's suit list).
const FLUSH_SUIT_ORDER: [u8; 4] = [DIAMONDS, CLUBS, HEARTS, SPADES];

/// Jokers that change what counts as a hand.
#[derive(Clone, Copy, Debug, Default)]
pub struct Rules {
    pub four_fingers: bool,
    pub shortcut: bool,
    pub smeared: bool,
    pub splash: bool,
}

/// A short list of card positions, in the order they were picked.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Picked {
    len: usize,
    at: [u8; 8],
}

impl Picked {
    pub fn as_slice(&self) -> &[u8] {
        &self.at[..self.len]
    }

    pub fn len(&self) -> usize {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    fn push(&mut self, position: u8) {
        self.at[self.len] = position;
        self.len += 1;
    }

    fn holds(&self, position: u8) -> bool {
        self.as_slice().contains(&position)
    }

    fn push_new(&mut self, position: u8) {
        if !self.holds(position) {
            self.push(position);
        }
    }

    fn truncate(&mut self, len: usize) {
        self.len = self.len.min(len);
    }

    pub fn from_slice(positions: &[u8]) -> Picked {
        let mut picked = Picked::default();
        for &p in positions {
            picked.push(p);
        }
        picked
    }
}

/// How many cards of each rank (index 2..=14) the play holds.
pub fn rank_counts(cards: &[Card]) -> [u8; 15] {
    let mut counts = [0u8; 15];
    for card in cards {
        if card.rank != 0 {
            counts[card.rank as usize] += 1;
        }
    }
    counts
}

fn has_flush(cards: &[Card], rules: &Rules) -> bool {
    if !rules.four_fingers {
        let mut common = 15u8;
        let mut any = false;
        for card in cards.iter().filter(|c| !c.is(STONE)) {
            any = true;
            common &= card.suits_under(rules.smeared);
            if common == 0 {
                return false;
            }
        }
        return any;
    }
    FLUSH_SUIT_ORDER
        .iter()
        .any(|&bit| cards.iter().filter(|c| !c.is(STONE) && c.suits_under(rules.smeared) & bit != 0).count() >= 4)
}

/// The highest run of `min_len` different ranks, each within `max_gap` of the next,
/// as a bit per rank. An Ace-low run is used only when there is no other.
fn straight_window(counts: &[u8; 15], min_len: usize, max_gap: u8) -> Option<u16> {
    let mut ranks = [0u8; 13];
    let mut n = 0;
    for rank in 2..=14u8 {
        if counts[rank as usize] != 0 {
            ranks[n] = rank;
            n += 1;
        }
    }
    if n < min_len {
        return None;
    }
    let runs = |ranks: &[u8], start: usize| (0..min_len - 1).all(|k| ranks[start + k + 1] - ranks[start + k] <= max_gap);
    let mut best = None;
    for start in 0..=n - min_len {
        if runs(&ranks[..n], start) {
            best = Some(ranks[start..start + min_len].iter().fold(0u16, |m, &r| m | 1 << r));
        }
    }
    if best.is_none() && counts[14] != 0 {
        let mut low = [0u8; 13];
        low[0] = 1;
        low[1..n].copy_from_slice(&ranks[..n - 1]);
        for start in 0..=n - min_len {
            if runs(&low[..n], start) {
                return Some(low[start..start + min_len].iter().fold(0u16, |m, &r| m | 1 << if r == 1 { 14 } else { r }));
            }
        }
    }
    best
}

/// The best Balatro hand the cards make.
pub fn classify(cards: &[Card], rules: &Rules, counts: &[u8; 15]) -> u8 {
    if cards.is_empty() {
        return HIGH_CARD;
    }
    let (mut top, mut second) = (0u8, 0u8);
    for &c in &counts[2..] {
        if c > top {
            second = top;
            top = c;
        } else if c > second {
            second = c;
        }
    }
    let min_len = if rules.four_fingers { 4 } else { 5 };
    let (mut flush, mut straight) = (false, false);
    if cards.len() >= min_len && cards.iter().filter(|c| !c.is(STONE)).count() >= min_len {
        flush = has_flush(cards, rules);
        straight = straight_window(counts, min_len, if rules.shortcut { 2 } else { 1 }).is_some();
    }
    if top >= 5 {
        return if flush { FLUSH_FIVE } else { FIVE_OF_A_KIND };
    }
    if top == 4 {
        return FOUR_OF_A_KIND;
    }
    if top >= 3 && second >= 2 {
        return if flush { FLUSH_HOUSE } else { FULL_HOUSE };
    }
    if straight && flush {
        return STRAIGHT_FLUSH;
    }
    if flush {
        return FLUSH;
    }
    if straight {
        return STRAIGHT;
    }
    if top == 3 {
        return THREE_OF_A_KIND;
    }
    if second >= 2 {
        return TWO_PAIR;
    }
    if top == 2 {
        return PAIR;
    }
    HIGH_CARD
}

fn everything(cards: &[Card]) -> Picked {
    let mut picked = Picked::default();
    for i in 0..cards.len() as u8 {
        picked.push(i);
    }
    picked
}

/// Stone cards always score: add the ones not already picked.
fn add_stones(picked: &mut Picked, cards: &[Card]) {
    for (i, card) in cards.iter().enumerate() {
        if card.is(STONE) {
            picked.push_new(i as u8);
        }
    }
}

/// The first `limit` cards of `rank`.
fn add_of_rank(picked: &mut Picked, cards: &[Card], rank: u8, limit: usize) {
    for (i, _) in cards.iter().enumerate().filter(|(_, c)| c.rank == rank).take(limit) {
        picked.push(i as u8);
    }
}

/// The rank held most often among those held at least `at_least` times, the
/// higher rank on a tie, skipping `except`.
fn rank_with(counts: &[u8; 15], at_least: u8, except: u8) -> u8 {
    let mut best = 0u8;
    for rank in (2..=14u8).rev() {
        let count = counts[rank as usize];
        if rank != except && count >= at_least && (best == 0 || count > counts[best as usize]) {
            best = rank;
        }
    }
    best
}

/// The ranked cards inside the straight's window, then the Stone cards.
fn straight_cards(cards: &[Card], counts: &[u8; 15], min_len: usize, max_gap: u8) -> Option<Picked> {
    let window = straight_window(counts, min_len, max_gap)?;
    let mut picked = Picked::default();
    for (i, card) in cards.iter().enumerate() {
        if card.rank != 0 && window & (1 << card.rank) != 0 {
            picked.push(i as u8);
        }
    }
    add_stones(&mut picked, cards);
    Some(picked)
}

/// The cards of the flush Four Fingers finds: every non-Stone card of the first
/// suit, in the game's suit order, that at least four of them count as.
fn four_fingers_flush_cards(cards: &[Card], smeared: bool) -> Picked {
    for bit in FLUSH_SUIT_ORDER {
        let mut group = Picked::default();
        for (i, card) in cards.iter().enumerate() {
            if !card.is(STONE) && card.suits_under(smeared) & bit != 0 {
                group.push(i as u8);
            }
        }
        if group.len() >= 4 {
            return group;
        }
    }
    Picked::default()
}

/// Which of the played cards score, in the order scoring reads them.
pub fn scoring_cards(cards: &[Card], hand_id: u8, rules: &Rules, counts: &[u8; 15]) -> Picked {
    if rules.splash {
        return everything(cards);
    }
    let max_gap = if rules.shortcut { 2 } else { 1 };
    let mut picked = Picked::default();
    match hand_id {
        FLUSH_FIVE | FLUSH_HOUSE => return everything(cards),
        STRAIGHT_FLUSH => {
            if !rules.four_fingers {
                return everything(cards);
            }
            // A card scores if it is part of the straight or of the flush.
            let mut both = straight_cards(cards, counts, 5, max_gap)
                .or_else(|| straight_cards(cards, counts, 4, max_gap))
                .unwrap_or_default();
            for &i in four_fingers_flush_cards(cards, rules.smeared).as_slice() {
                both.push_new(i);
            }
            add_stones(&mut both, cards);
            return if both.is_empty() { everything(cards) } else { both };
        }
        FLUSH => {
            if !rules.four_fingers {
                return everything(cards);
            }
            let mut flush = four_fingers_flush_cards(cards, rules.smeared);
            if flush.is_empty() {
                return everything(cards);
            }
            for (i, card) in cards.iter().enumerate() {
                if card.is(STONE) {
                    flush.push(i as u8);
                }
            }
            return flush;
        }
        STRAIGHT => {
            if !rules.four_fingers {
                return everything(cards);
            }
            // All five score when they run; the four-card window only when the fifth breaks it.
            return straight_cards(cards, counts, 5, max_gap)
                .or_else(|| straight_cards(cards, counts, 4, max_gap))
                .unwrap_or_else(|| everything(cards));
        }
        FIVE_OF_A_KIND => add_of_rank(&mut picked, cards, rank_with(counts, 1, 0), 5),
        FOUR_OF_A_KIND => add_of_rank(&mut picked, cards, rank_with(counts, 4, 0), 4),
        FULL_HOUSE => {
            let trips = rank_with(counts, 3, 0);
            add_of_rank(&mut picked, cards, trips, 3);
            add_of_rank(&mut picked, cards, rank_with(counts, 2, trips), 2);
        }
        THREE_OF_A_KIND => add_of_rank(&mut picked, cards, rank_with(counts, 3, 0), 3),
        TWO_PAIR => {
            let first = rank_with(counts, 2, 0);
            add_of_rank(&mut picked, cards, first, 8);
            add_of_rank(&mut picked, cards, rank_with(counts, 2, first), 8);
            picked.truncate(4);
        }
        PAIR => add_of_rank(&mut picked, cards, rank_with(counts, 2, 0), 2),
        _ => {
            // High Card: the first card of the highest rank, or the first card when none has one.
            let mut best: Option<usize> = None;
            for (i, card) in cards.iter().enumerate() {
                if card.rank != 0 && best.map_or(true, |b| card.rank > cards[b].rank) {
                    best = Some(i);
                }
            }
            picked.push(best.unwrap_or(0) as u8);
        }
    }
    add_stones(&mut picked, cards);
    picked
}

/// A sub-hand that hand-type jokers ask about.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum SubHand {
    Pair,
    TwoPair,
    ThreeOfAKind,
    FourOfAKind,
    Straight,
    Flush,
}

/// Whether the play "contains" the sub-hand: its hand type implies it, or the
/// played cards hold the rank pattern (a Flush with two Kings contains a Pair).
/// `top` is the most cards of one rank among the played cards, `pairs` how many
/// ranks appear twice or more.
pub fn contains(hand_id: u8, sub: SubHand, top: u8, pairs: u8) -> bool {
    match sub {
        SubHand::Pair => {
            matches!(
                hand_id,
                PAIR | TWO_PAIR | THREE_OF_A_KIND | FULL_HOUSE | FOUR_OF_A_KIND | FIVE_OF_A_KIND | FLUSH_HOUSE | FLUSH_FIVE
            ) || top >= 2
        }
        SubHand::ThreeOfAKind => {
            matches!(hand_id, THREE_OF_A_KIND | FULL_HOUSE | FOUR_OF_A_KIND | FIVE_OF_A_KIND | FLUSH_HOUSE | FLUSH_FIVE)
                || top >= 3
        }
        SubHand::FourOfAKind => matches!(hand_id, FOUR_OF_A_KIND | FIVE_OF_A_KIND | FLUSH_FIVE) || top >= 4,
        SubHand::TwoPair => matches!(hand_id, TWO_PAIR | FULL_HOUSE | FLUSH_HOUSE) || pairs >= 2,
        SubHand::Straight => matches!(hand_id, STRAIGHT | STRAIGHT_FLUSH),
        SubHand::Flush => matches!(hand_id, FLUSH | STRAIGHT_FLUSH | FLUSH_HOUSE | FLUSH_FIVE),
    }
}
