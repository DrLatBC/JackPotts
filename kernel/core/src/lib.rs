//! Hand search and scoring for the JackPotts Balatro bot.
//!
//! Spike scope: every hand type and scoring-card rule, per-card scoring with
//! retriggers and held cards, and six jokers of different kinds. Enough to
//! measure what a search costs; the full joker set comes later.

use std::sync::OnceLock;

// Card flags.
pub const STONE: u8 = 1;
pub const DEBUFFED: u8 = 2;
pub const FACE: u8 = 4;
pub const RED_SEAL: u8 = 8;
pub const STEEL: u8 = 16;

// Suit bits, matching `balatro_bot.cards._SUIT_BIT`.
pub const HEARTS: u8 = 1;
pub const DIAMONDS: u8 = 2;
pub const CLUBS: u8 = 4;
pub const SPADES: u8 = 8;
/// The game tries suits in this order and takes the first that enough cards count as.
const FLUSH_SUIT_ORDER: [u8; 4] = [DIAMONDS, CLUBS, HEARTS, SPADES];

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

pub const CARD_BYTES: usize = 48;
pub const MAX_HAND: usize = 16;

/// One playing card, as scoring sees it. The numbers are what the card adds on
/// each trigger: a debuffed card carries zeros and ones.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Card {
    /// 2..=14, or 0 for a card with no rank (Stone).
    pub rank: u8,
    /// Bit per suit the card counts as.
    pub suits: u8,
    /// The same with Smeared Joker's merged suits.
    pub suits_smeared: u8,
    pub flags: u8,
    pub chips: f64,
    pub mult: f64,
    pub xmult: f64,
    pub edition_mult: f64,
    pub edition_xmult: f64,
}

impl Card {
    pub fn to_bytes(&self) -> [u8; CARD_BYTES] {
        let mut out = [0u8; CARD_BYTES];
        out[0] = self.rank;
        out[1] = self.suits;
        out[2] = self.suits_smeared;
        out[3] = self.flags;
        let numbers = [self.chips, self.mult, self.xmult, self.edition_mult, self.edition_xmult];
        for (i, n) in numbers.iter().enumerate() {
            out[8 + i * 8..16 + i * 8].copy_from_slice(&n.to_le_bytes());
        }
        out
    }

    pub fn from_bytes(b: &[u8]) -> Card {
        let number = |i: usize| f64::from_le_bytes(b[8 + i * 8..16 + i * 8].try_into().unwrap());
        Card {
            rank: b[0],
            suits: b[1],
            suits_smeared: b[2],
            flags: b[3],
            chips: number(0),
            mult: number(1),
            xmult: number(2),
            edition_mult: number(3),
            edition_xmult: number(4),
        }
    }

    fn is(&self, flag: u8) -> bool {
        self.flags & flag != 0
    }
}

/// Jokers that change the rules of what counts as a hand.
#[derive(Clone, Copy, Debug, Default)]
pub struct Rules {
    pub four_fingers: bool,
    pub shortcut: bool,
    pub smeared: bool,
    pub splash: bool,
}

/// A joker's turn after the cards have scored.
#[derive(Clone, Copy, Debug)]
pub enum JokerEffect {
    /// Joker: +mult.
    FlatMult(f64),
    /// Jolly Joker: +mult if the hand contains a Pair.
    PairMult(f64),
    /// Cavendish: xmult.
    XMult(f64),
}

#[derive(Clone, Debug, Default)]
pub struct Lineup {
    /// (chips, mult) per hand id.
    pub levels: [(f64, f64); HAND_COUNT],
    pub rules: Rules,
    pub effects: Vec<JokerEffect>,
    /// Per scoring card: +mult when the card counts as the suit (Greedy Joker and kin).
    pub suit_mult: Vec<(u8, f64)>,
    /// Hack: 2, 3, 4 and 5 trigger once more.
    pub hack: bool,
    /// Baron: each King held in hand multiplies.
    pub baron: Vec<f64>,
}

fn suits_of(card: &Card, smeared: bool) -> u8 {
    if smeared {
        card.suits_smeared
    } else {
        card.suits
    }
}

/// How many cards of each rank (index 2..=14) the play holds.
fn rank_counts(hand: &[Card], play: &[u8]) -> [u8; 15] {
    let mut counts = [0u8; 15];
    for &i in play {
        let rank = hand[i as usize].rank;
        if rank != 0 {
            counts[rank as usize] += 1;
        }
    }
    counts
}

fn has_flush(hand: &[Card], play: &[u8], rules: &Rules) -> bool {
    if !rules.four_fingers {
        let mut common = 15u8;
        let mut any = false;
        for &i in play {
            let card = &hand[i as usize];
            if card.is(STONE) {
                continue;
            }
            any = true;
            common &= suits_of(card, rules.smeared);
            if common == 0 {
                return false;
            }
        }
        return any;
    }
    FLUSH_SUIT_ORDER.iter().any(|&bit| {
        play.iter()
            .filter(|&&i| {
                let card = &hand[i as usize];
                !card.is(STONE) && suits_of(card, rules.smeared) & bit != 0
            })
            .count()
            >= 4
    })
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
    let window_at = |ranks: &[u8], start: usize| -> bool {
        (0..min_len - 1).all(|k| ranks[start + k + 1] - ranks[start + k] <= max_gap)
    };
    let mut best: Option<u16> = None;
    if n >= min_len {
        for start in 0..=n - min_len {
            if window_at(&ranks[..n], start) {
                best = Some(ranks[start..start + min_len].iter().fold(0, |m, &r| m | 1 << r));
            }
        }
    }
    if best.is_none() && counts[14] != 0 && n >= min_len {
        let mut low = [0u8; 13];
        low[0] = 1;
        low[1..n].copy_from_slice(&ranks[..n - 1]);
        for start in 0..=n - min_len {
            if window_at(&low[..n], start) {
                let bits = low[start..start + min_len]
                    .iter()
                    .fold(0u16, |m, &r| m | 1 << if r == 1 { 14 } else { r });
                return Some(bits);
            }
        }
    }
    best
}

/// The best Balatro hand the play makes.
pub fn classify(hand: &[Card], play: &[u8], rules: &Rules, counts: &[u8; 15]) -> u8 {
    if play.is_empty() {
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
    let ranked_or_suited = play.iter().filter(|&&i| !hand[i as usize].is(STONE)).count();
    if play.len() >= min_len && ranked_or_suited >= min_len {
        flush = has_flush(hand, play, rules);
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

fn all_of(play: &[u8]) -> u16 {
    play.iter().fold(0, |m, &i| m | 1 << i)
}

fn stones(hand: &[Card], play: &[u8]) -> u16 {
    play.iter().filter(|&&i| hand[i as usize].is(STONE)).fold(0, |m, &i| m | 1 << i)
}

/// The first `limit` cards of `rank` in the play.
fn of_rank(hand: &[Card], play: &[u8], rank: u8, limit: usize) -> u16 {
    play.iter().filter(|&&i| hand[i as usize].rank == rank).take(limit).fold(0, |m, &i| m | 1 << i)
}

/// The highest rank held at least `at_least` times, skipping `except`.
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

fn straight_cards(hand: &[Card], play: &[u8], counts: &[u8; 15], min_len: usize, max_gap: u8) -> Option<u16> {
    let window = straight_window(counts, min_len, max_gap)?;
    let ranked = play
        .iter()
        .filter(|&&i| window & (1 << hand[i as usize].rank) != 0 && hand[i as usize].rank != 0)
        .fold(0u16, |m, &i| m | 1 << i);
    Some(ranked | stones(hand, play))
}

fn four_fingers_flush_cards(hand: &[Card], play: &[u8], smeared: bool) -> u16 {
    for bit in FLUSH_SUIT_ORDER {
        let mut group = 0u16;
        let mut size = 0;
        for &i in play {
            let card = &hand[i as usize];
            if !card.is(STONE) && suits_of(card, smeared) & bit != 0 {
                group |= 1 << i;
                size += 1;
            }
        }
        if size >= 4 {
            return group;
        }
    }
    0
}

/// Which of the played cards score, as a bit per hand position.
pub fn scoring_cards(hand: &[Card], play: &[u8], hand_id: u8, rules: &Rules, counts: &[u8; 15]) -> u16 {
    let everything = all_of(play);
    if rules.splash {
        return everything;
    }
    let max_gap = if rules.shortcut { 2 } else { 1 };
    let picked = match hand_id {
        FLUSH_FIVE | FLUSH_HOUSE => return everything,
        STRAIGHT_FLUSH => {
            if !rules.four_fingers {
                return everything;
            }
            let straight = straight_cards(hand, play, counts, 5, max_gap)
                .or_else(|| straight_cards(hand, play, counts, 4, max_gap))
                .unwrap_or(0);
            let both = straight | four_fingers_flush_cards(hand, play, rules.smeared);
            return if both == 0 { everything } else { both | stones(hand, play) };
        }
        FLUSH => {
            if !rules.four_fingers {
                return everything;
            }
            let flush = four_fingers_flush_cards(hand, play, rules.smeared);
            return if flush == 0 { everything } else { flush | stones(hand, play) };
        }
        STRAIGHT => {
            if !rules.four_fingers {
                return everything;
            }
            return straight_cards(hand, play, counts, 5, max_gap)
                .or_else(|| straight_cards(hand, play, counts, 4, max_gap))
                .unwrap_or(everything);
        }
        FIVE_OF_A_KIND => of_rank(hand, play, rank_with(counts, 1, 0), 5),
        FOUR_OF_A_KIND => of_rank(hand, play, rank_with(counts, 4, 0), 4),
        FULL_HOUSE => {
            let trips = rank_with(counts, 3, 0);
            of_rank(hand, play, trips, 3) | of_rank(hand, play, rank_with(counts, 2, trips), 2)
        }
        THREE_OF_A_KIND => of_rank(hand, play, rank_with(counts, 3, 0), 3),
        TWO_PAIR => {
            let first = rank_with(counts, 2, 0);
            let second = rank_with(counts, 2, first);
            // Cards of the first pair's rank, then the second's, four at most.
            let mut picked = 0u16;
            let mut taken = 0;
            for rank in [first, second] {
                for &i in play {
                    if hand[i as usize].rank == rank && taken < 4 {
                        picked |= 1 << i;
                        taken += 1;
                    }
                }
            }
            picked
        }
        PAIR => of_rank(hand, play, rank_with(counts, 2, 0), 2),
        _ => {
            // High Card: the first card of the highest rank, or the first card when none has one.
            let mut best: Option<u8> = None;
            for &i in play {
                let rank = hand[i as usize].rank;
                if rank != 0 && best.map_or(true, |b| rank > hand[b as usize].rank) {
                    best = Some(i);
                }
            }
            1u16 << best.unwrap_or(play[0])
        }
    };
    picked | stones(hand, play)
}

fn contains_pair(hand_id: u8) -> bool {
    matches!(
        hand_id,
        PAIR | TWO_PAIR | THREE_OF_A_KIND | FULL_HOUSE | FOUR_OF_A_KIND | FIVE_OF_A_KIND | FLUSH_HOUSE | FLUSH_FIVE
    )
}

impl Lineup {
    /// Chips and mult for one play: the scoring cards in play order, then the cards
    /// held in hand, then the jokers.
    pub fn score(&self, hand: &[Card], hand_id: u8, order: &[u8], scoring: u16, held: &[u8]) -> (f64, f64) {
        let (mut chips, mut mult) = self.levels[hand_id as usize];
        for &i in order {
            if scoring & (1 << i) == 0 {
                continue;
            }
            let card = &hand[i as usize];
            let mut triggers = 1;
            if !card.is(DEBUFFED) {
                if self.hack && (2..=5).contains(&card.rank) {
                    triggers += 1;
                }
                if card.is(RED_SEAL) {
                    triggers += 1;
                }
            }
            for _ in 0..triggers {
                chips += card.chips;
                mult += card.mult;
                mult *= card.xmult;
                mult += card.edition_mult;
                mult *= card.edition_xmult;
                if !card.is(DEBUFFED) {
                    for &(bit, value) in &self.suit_mult {
                        if suits_of(card, self.rules.smeared) & bit != 0 {
                            mult += value;
                        }
                    }
                }
            }
        }
        for &i in held {
            let card = &hand[i as usize];
            if card.is(DEBUFFED) {
                continue;
            }
            if card.is(STEEL) {
                mult *= 1.5;
            }
            if card.rank == 13 {
                for &xmult in &self.baron {
                    mult *= xmult;
                }
            }
        }
        for effect in &self.effects {
            match *effect {
                JokerEffect::FlatMult(value) => mult += value,
                JokerEffect::PairMult(value) => {
                    if contains_pair(hand_id) {
                        mult += value;
                    }
                }
                JokerEffect::XMult(value) => mult *= value,
            }
        }
        (chips, mult)
    }
}

/// One scored play.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Play {
    pub hand_id: u8,
    /// Bit per hand position played.
    pub cards: u16,
    /// Bit per hand position that scores.
    pub scoring: u16,
    pub chips: f64,
    pub mult: f64,
    pub total: f64,
}

type Subset = (u8, [u8; 5]);

/// Every choice of `min..=max` positions out of `n`, smallest first, in the order
/// Python's `itertools.combinations` gives them.
fn subsets(n: usize) -> &'static [Subset] {
    static CACHE: [OnceLock<Vec<Subset>>; MAX_HAND + 1] = [const { OnceLock::new() }; MAX_HAND + 1];
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

impl Lineup {
    fn score_subset(&self, hand: &[Card], play: &[u8]) -> Play {
        let counts = rank_counts(hand, play);
        let hand_id = classify(hand, play, &self.rules, &counts);
        let scoring = scoring_cards(hand, play, hand_id, &self.rules, &counts);

        // Glass cards go to the right of cards that only add.
        let mut order = [0u8; 5];
        let mut n = 0;
        for multiplies in [false, true] {
            for &i in play {
                let card = &hand[i as usize];
                if (card.xmult != 1.0) == multiplies {
                    order[n] = i;
                    n += 1;
                }
            }
        }
        let played = all_of(play);
        let mut held = [0u8; MAX_HAND];
        let mut held_count = 0;
        for i in 0..hand.len() as u8 {
            if played & (1 << i) == 0 {
                held[held_count] = i;
                held_count += 1;
            }
        }
        let (chips, mult) = self.score(hand, hand_id, &order[..n], scoring, &held[..held_count]);
        Play { hand_id, cards: played, scoring, chips, mult, total: (chips * mult).floor() }
    }

    /// Every play of `min_select..=max_select` cards, in enumeration order.
    pub fn plays<'a>(&'a self, hand: &'a [Card], min_select: usize, max_select: usize) -> impl Iterator<Item = Play> + 'a {
        subsets(hand.len())
            .iter()
            .filter(move |(size, _)| (min_select..=max_select).contains(&(*size as usize)))
            .map(move |(size, positions)| self.score_subset(hand, &positions[..*size as usize]))
    }

    /// The best play: highest total, then the better hand type, then fewer cards;
    /// the first found wins a full tie.
    pub fn best(&self, hand: &[Card], min_select: usize, max_select: usize) -> Option<Play> {
        let mut best: Option<Play> = None;
        for play in self.plays(hand, min_select, max_select) {
            let better = match &best {
                None => true,
                Some(b) => {
                    let key = |p: &Play| (-p.total, p.hand_id, p.cards.count_ones());
                    key(&play) < key(b)
                }
            };
            if better {
                best = Some(play);
            }
        }
        best
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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

    fn lineup() -> Lineup {
        let mut levels = [(0.0, 0.0); HAND_COUNT];
        levels[PAIR as usize] = (10.0, 2.0);
        levels[HIGH_CARD as usize] = (5.0, 1.0);
        levels[FLUSH as usize] = (35.0, 4.0);
        Lineup { levels, ..Lineup::default() }
    }

    #[test]
    fn card_survives_the_byte_round_trip() {
        let c = Card { mult: 4.0, edition_mult: 10.0, ..card(13, SPADES) };
        assert_eq!(Card::from_bytes(&c.to_bytes()), c);
    }

    #[test]
    fn subsets_come_in_combinations_order() {
        let all = subsets(8);
        assert_eq!(all.len(), 218);
        assert_eq!(all[8], (2, [0, 1, 0, 0, 0]));
        assert_eq!(all[217], (5, [3, 4, 5, 6, 7]));
    }

    #[test]
    fn a_pair_scores_its_two_cards() {
        let hand = [card(9, SPADES), card(4, HEARTS), card(9, CLUBS)];
        let best = lineup().best(&hand, 1, 5).unwrap();
        assert_eq!(best.hand_id, PAIR);
        assert_eq!(best.scoring, 0b101);
        assert_eq!((best.chips, best.mult, best.total), (28.0, 2.0, 56.0));
    }

    #[test]
    fn five_of_one_suit_is_a_flush() {
        let hand = [card(2, HEARTS), card(5, HEARTS), card(8, HEARTS), card(9, HEARTS), card(13, HEARTS)];
        let best = lineup().best(&hand, 1, 5).unwrap();
        assert_eq!(best.hand_id, FLUSH);
        assert_eq!(best.total, (35.0 + 2.0 + 5.0 + 8.0 + 9.0 + 10.0) * 4.0);
    }
}
