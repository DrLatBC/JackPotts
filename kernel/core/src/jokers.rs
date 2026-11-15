//! What each joker does to a hand's chips and mult.
//!
//! Three shapes, as in the Python this replaces:
//! - `TABLE`: jokers that are one line each (flat, stored, or hand-type bonuses);
//! - `per_card_effects`: jokers that act on each scoring card as it scores;
//! - `joker_effect`: one match arm per joker that needs its own logic.
//!
//! A fix to one joker is an edit to its row or its arm.

use crate::card::{Card, CLUBS, DIAMONDS, HEARTS, LUCKY, SPADES, STONE, WILD};
use crate::hands::{contains, SubHand, HAND_COUNT};

/// A joker's numbers, resolved in Python (`joker_effects.parsers`): the value the
/// game currently shows, else the stored ability value. `None` means the joker's
/// own default applies.
#[derive(Clone, Debug, Default)]
pub struct JokerSpec {
    pub key: String,
    pub sell: f64,
    pub chips: Option<f64>,
    pub mult: Option<f64>,
    pub xmult: Option<f64>,
    pub counter: Option<f64>,
    pub ability: Ability,
}

/// The ability fields scoring reads, by the game's own names.
#[derive(Clone, Debug, Default)]
pub struct Ability {
    pub extra: Option<f64>,
    pub mult: Option<f64>,
    pub t_mult: Option<f64>,
    pub t_chips: Option<f64>,
    pub s_mult: Option<f64>,
    pub chips: Option<f64>,
    pub chip_mod: Option<f64>,
    pub xmult: Option<f64>,
    pub loyalty_remaining: Option<f64>,
    pub size: Option<f64>,
    pub odds: Option<f64>,
    pub min: Option<f64>,
    pub max: Option<f64>,
    pub hand_add: Option<f64>,
    pub driver_tally: Option<f64>,
    pub dollars: Option<f64>,
    pub d_remaining: Option<f64>,
}

impl Ability {
    /// Set one field by the name the game gives it; unknown names are ignored.
    pub fn set(&mut self, name: &str, value: f64) {
        let slot = match name {
            "extra" => &mut self.extra,
            "mult" => &mut self.mult,
            "t_mult" => &mut self.t_mult,
            "t_chips" => &mut self.t_chips,
            "s_mult" => &mut self.s_mult,
            "chips" => &mut self.chips,
            "chip_mod" => &mut self.chip_mod,
            "Xmult" => &mut self.xmult,
            "loyalty_remaining" => &mut self.loyalty_remaining,
            "size" => &mut self.size,
            "odds" => &mut self.odds,
            "min" => &mut self.min,
            "max" => &mut self.max,
            "hand_add" => &mut self.hand_add,
            "driver_tally" => &mut self.driver_tally,
            "dollars" => &mut self.dollars,
            "d_remaining" => &mut self.d_remaining,
            _ => return,
        };
        *slot = Some(value);
    }
}

/// The source of the dice: the Python random generator the caller passed, or none,
/// in which case chance effects score their expected value.
pub trait Rolls {
    fn random(&mut self) -> f64;
    fn uniform(&mut self, low: f64, high: f64) -> f64;
}

/// (chips, mult, times played, times played this round) for one hand type.
#[derive(Clone, Copy, Debug, Default)]
pub struct Level {
    pub chips: f64,
    pub mult: f64,
    pub played: f64,
    pub played_this_round: f64,
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub enum Blind {
    #[default]
    Other,
    Hook,
    Tooth,
}

/// Everything about the round a joker can read besides the cards.
#[derive(Clone, Debug, Default)]
pub struct Surroundings {
    pub levels: [Level; HAND_COUNT],
    pub money: f64,
    pub discards_left: f64,
    pub hands_left: f64,
    pub joker_limit: f64,
    pub deck_count: f64,
    pub blind: Blind,
    /// The Ox: playing this hand sets money to 0.
    pub ox_locked: Option<u8>,
    /// Suit bit, 0 when unknown.
    pub ancient_suit: u8,
    /// Rank and suit bit, 0 when unknown.
    pub idol_rank: u8,
    pub idol_suit: u8,
    pub pareidolia: bool,
    pub smeared: bool,
    pub oops: bool,
}

/// The state one play is scored in: what the joker phase reads and changes.
pub struct Scoring<'a> {
    pub chips: f64,
    pub mult: f64,
    pub money: f64,
    pub hand_id: u8,
    /// The scoring cards, in the order the caller listed them.
    pub scoring: &'a [Card],
    pub played_count: usize,
    pub held: &'a [Card],
    /// Most played cards of one rank, and how many ranks are played twice or more.
    pub top: u8,
    pub pairs: u8,
    /// Triggers of each scoring card (parallel to `scoring`), for Lucky Cat.
    pub triggers: &'a [u32],
    pub vampire_xmult: Option<f64>,
    pub around: &'a Surroundings,
    pub jokers: &'a [JokerSpec],
}

// --- One-line jokers ---------------------------------------------------------

#[derive(Clone, Copy, Debug)]
pub enum Row {
    /// +mult from the `mult` ability field.
    FlatMult(f64),
    /// +mult, +chips or xmult the joker has built up (its resolved value).
    StoredMult(f64),
    StoredChips(f64),
    StoredXMult(f64),
    /// A bonus when the hand contains a sub-hand: +mult (`t_mult`), +chips (`t_chips`), xmult.
    HandMult(SubHand, f64),
    HandChips(SubHand, f64),
    HandXMult(SubHand, f64),
}

/// The second number in each row is the joker's default when the game gives none.
pub const TABLE: &[(&str, Row)] = &[
    ("j_joker", Row::FlatMult(4.0)),
    ("j_gros_michel", Row::FlatMult(15.0)),
    ("j_popcorn", Row::StoredMult(12.0)),
    ("j_ice_cream", Row::StoredChips(60.0)),
    ("j_cavendish", Row::StoredXMult(3.0)),
    ("j_jolly", Row::HandMult(SubHand::Pair, 8.0)),
    ("j_zany", Row::HandMult(SubHand::ThreeOfAKind, 12.0)),
    ("j_mad", Row::HandMult(SubHand::TwoPair, 10.0)),
    ("j_crazy", Row::HandMult(SubHand::Straight, 12.0)),
    ("j_droll", Row::HandMult(SubHand::Flush, 10.0)),
    ("j_sly", Row::HandChips(SubHand::Pair, 50.0)),
    ("j_wily", Row::HandChips(SubHand::ThreeOfAKind, 100.0)),
    ("j_clever", Row::HandChips(SubHand::TwoPair, 80.0)),
    ("j_devious", Row::HandChips(SubHand::Straight, 100.0)),
    ("j_crafty", Row::HandChips(SubHand::Flush, 80.0)),
    ("j_duo", Row::HandXMult(SubHand::Pair, 2.0)),
    ("j_trio", Row::HandXMult(SubHand::ThreeOfAKind, 3.0)),
    ("j_family", Row::HandXMult(SubHand::FourOfAKind, 4.0)),
    ("j_order", Row::HandXMult(SubHand::Straight, 3.0)),
    ("j_tribe", Row::HandXMult(SubHand::Flush, 2.0)),
    ("j_ceremonial", Row::StoredMult(10.0)),
    ("j_red_card", Row::StoredMult(6.0)),
    ("j_fortune_teller", Row::StoredMult(3.0)),
    ("j_flash", Row::StoredMult(6.0)),
    ("j_erosion", Row::StoredMult(8.0)),
    ("j_stone", Row::StoredChips(25.0)),
    ("j_castle", Row::StoredChips(15.0)),
    ("j_constellation", Row::StoredXMult(1.3)),
    ("j_madness", Row::StoredXMult(2.0)),
    ("j_hologram", Row::StoredXMult(1.5)),
    ("j_campfire", Row::StoredXMult(1.5)),
    ("j_hit_the_road", Row::StoredXMult(1.5)),
    ("j_glass", Row::StoredXMult(1.5)),
    ("j_throwback", Row::StoredXMult(1.5)),
    ("j_caino", Row::StoredXMult(2.0)),
];

fn apply_row(row: Row, joker: &JokerSpec, s: &mut Scoring) {
    let ab = &joker.ability;
    let (hand_id, top, pairs) = (s.hand_id, s.top, s.pairs);
    let has = move |sub| contains(hand_id, sub, top, pairs);
    match row {
        Row::FlatMult(default) => s.mult += ab.mult.unwrap_or(default),
        Row::StoredMult(default) => s.mult += joker.mult.unwrap_or(default),
        Row::StoredChips(default) => s.chips += joker.chips.unwrap_or(default),
        Row::StoredXMult(default) => s.mult *= joker.xmult.unwrap_or(default),
        Row::HandMult(sub, default) => {
            if has(sub) {
                s.mult += ab.t_mult.unwrap_or(default);
            }
        }
        Row::HandChips(sub, default) => {
            if has(sub) {
                s.chips += ab.t_chips.unwrap_or(default);
            }
        }
        Row::HandXMult(sub, default) => {
            if has(sub) {
                s.mult *= joker.xmult.unwrap_or(default);
            }
        }
    }
}

// --- Jokers that act on each scoring card -------------------------------------

const FIBONACCI: u16 = 1 << 14 | 1 << 2 | 1 << 3 | 1 << 5 | 1 << 8;
const EVEN: u16 = 1 << 2 | 1 << 4 | 1 << 6 | 1 << 8 | 1 << 10;
const ODD: u16 = 1 << 14 | 1 << 3 | 1 << 5 | 1 << 7 | 1 << 9;

#[derive(Clone, Copy, Debug)]
pub enum CardEffect {
    /// Ranks as a bit per rank value.
    RanksMult(u16, f64),
    RanksChips(u16, f64),
    RanksChipsMult(u16, f64, f64),
    RanksXMult(u16, f64),
    FaceMult(f64),
    FaceChips(f64),
    /// Suit as a suit bit.
    SuitMult(u8, f64),
    SuitChips(u8, f64),
    SuitXMult(u8, f64),
    /// Bloodstone: (suit, xmult, chance per trigger, xmult's expected value, the
    /// value play ordering uses).
    SuitChanceXMult(u8, f64, f64, f64, f64),
    /// Idol: (rank, suit, xmult).
    RankSuitXMult(u8, u8, f64),
    /// Photograph: xmult on the first face card that scores.
    FirstFaceXMult(f64),
    /// Hiker: chips per earlier trigger of the same card this hand.
    ChipsPerRetrigger(f64),
}

/// The per-card effect of one joker in effect (copies by Blueprint and Brainstorm
/// arrive here as the joker they copy), or None when it has none.
pub fn card_effect(joker: &JokerSpec, around: &Surroundings) -> Option<CardEffect> {
    let ab = &joker.ability;
    let extra = |default: f64| ab.extra.unwrap_or(default);
    Some(match joker.key.as_str() {
        "j_greedy_joker" => CardEffect::SuitMult(DIAMONDS, ab.s_mult.unwrap_or(3.0)),
        "j_lusty_joker" => CardEffect::SuitMult(HEARTS, ab.s_mult.unwrap_or(3.0)),
        "j_wrathful_joker" => CardEffect::SuitMult(SPADES, ab.s_mult.unwrap_or(3.0)),
        "j_gluttenous_joker" => CardEffect::SuitMult(CLUBS, ab.s_mult.unwrap_or(3.0)),
        "j_fibonacci" => CardEffect::RanksMult(FIBONACCI, extra(8.0)),
        "j_even_steven" => CardEffect::RanksMult(EVEN, extra(4.0)),
        "j_odd_todd" => CardEffect::RanksChips(ODD, extra(31.0)),
        "j_scholar" => CardEffect::RanksChipsMult(1 << 14, ab.chips.unwrap_or(20.0), ab.mult.unwrap_or(4.0)),
        "j_smiley" => CardEffect::FaceMult(extra(5.0)),
        "j_scary_face" => CardEffect::FaceChips(extra(30.0)),
        "j_walkie_talkie" => {
            CardEffect::RanksChipsMult(1 << 10 | 1 << 4, ab.chips.unwrap_or(10.0), ab.mult.unwrap_or(4.0))
        }
        "j_photograph" => CardEffect::FirstFaceXMult(extra(2.0)),
        "j_triboulet" => CardEffect::RanksXMult(1 << 13 | 1 << 12, extra(2.0)),
        "j_ancient" => CardEffect::SuitXMult(around.ancient_suit, 1.5),
        "j_arrowhead" => CardEffect::SuitChips(SPADES, extra(50.0)),
        "j_onyx_agate" => CardEffect::SuitMult(CLUBS, extra(7.0)),
        "j_bloodstone" => {
            let xmult = ab.xmult.unwrap_or(1.5);
            let odds = ab.odds.unwrap_or(2.0);
            let chance = if around.oops { 2.0 } else { 1.0 } / odds;
            CardEffect::SuitChanceXMult(HEARTS, xmult, chance, xmult.powf(chance), xmult.powf(1.0 / odds))
        }
        "j_idol" => {
            if around.idol_rank == 0 || around.idol_suit == 0 {
                return None;
            }
            CardEffect::RankSuitXMult(around.idol_rank, around.idol_suit, extra(2.0))
        }
        "j_hiker" => CardEffect::ChipsPerRetrigger(extra(5.0)),
        _ => return None,
    })
}

fn in_ranks(card: &Card, ranks: u16) -> bool {
    card.rank != 0 && ranks & (1 << card.rank) != 0
}

impl CardEffect {
    /// One trigger of a scoring card. `trigger` counts the card's earlier triggers.
    #[allow(clippy::too_many_arguments)]
    pub fn apply(
        &self,
        card: &Card,
        suits: u8,
        face: bool,
        first_face: bool,
        trigger: u32,
        chips: &mut f64,
        mult: &mut f64,
        rolls: &mut Option<&mut dyn Rolls>,
    ) {
        match *self {
            CardEffect::RanksMult(ranks, value) => {
                if in_ranks(card, ranks) {
                    *mult += value;
                }
            }
            CardEffect::RanksChips(ranks, value) => {
                if in_ranks(card, ranks) {
                    *chips += value;
                }
            }
            CardEffect::RanksChipsMult(ranks, c, m) => {
                if in_ranks(card, ranks) {
                    *chips += c;
                    *mult += m;
                }
            }
            CardEffect::RanksXMult(ranks, value) => {
                if in_ranks(card, ranks) {
                    *mult *= value;
                }
            }
            CardEffect::FaceMult(value) => {
                if face {
                    *mult += value;
                }
            }
            CardEffect::FaceChips(value) => {
                if face {
                    *chips += value;
                }
            }
            CardEffect::SuitMult(suit, value) => {
                if suits & suit != 0 {
                    *mult += value;
                }
            }
            CardEffect::SuitChips(suit, value) => {
                if suits & suit != 0 {
                    *chips += value;
                }
            }
            CardEffect::SuitXMult(suit, value) => {
                if suits & suit != 0 {
                    *mult *= value;
                }
            }
            CardEffect::SuitChanceXMult(suit, xmult, chance, expected, _) => {
                if suits & suit != 0 {
                    match rolls {
                        Some(r) => {
                            if r.random() < chance {
                                *mult *= xmult;
                            }
                        }
                        None => *mult *= expected,
                    }
                }
            }
            CardEffect::RankSuitXMult(rank, suit, value) => {
                if card.rank == rank && suits & suit != 0 {
                    *mult *= value;
                }
            }
            CardEffect::FirstFaceXMult(value) => {
                if first_face {
                    *mult *= value;
                }
            }
            CardEffect::ChipsPerRetrigger(value) => *chips += value * trigger as f64,
        }
    }

    /// What one trigger adds, for ordering a play: (chips, mult, xmult). Hiker is
    /// left out; a chance xmult counts at its ordering value.
    pub fn for_ordering(&self, card: &Card, suits: u8, face: bool, first_face: bool) -> (f64, f64, f64) {
        match *self {
            CardEffect::RanksMult(ranks, value) if in_ranks(card, ranks) => (0.0, value, 1.0),
            CardEffect::RanksChips(ranks, value) if in_ranks(card, ranks) => (value, 0.0, 1.0),
            CardEffect::RanksChipsMult(ranks, c, m) if in_ranks(card, ranks) => (c, m, 1.0),
            CardEffect::RanksXMult(ranks, value) if in_ranks(card, ranks) => (0.0, 0.0, value),
            CardEffect::FaceMult(value) if face => (0.0, value, 1.0),
            CardEffect::FaceChips(value) if face => (value, 0.0, 1.0),
            CardEffect::SuitMult(suit, value) if suits & suit != 0 => (0.0, value, 1.0),
            CardEffect::SuitChips(suit, value) if suits & suit != 0 => (value, 0.0, 1.0),
            CardEffect::SuitXMult(suit, value) if suits & suit != 0 => (0.0, 0.0, value),
            CardEffect::SuitChanceXMult(suit, _, _, _, ordering) if suits & suit != 0 => (0.0, 0.0, ordering),
            CardEffect::RankSuitXMult(rank, suit, value) if card.rank == rank && suits & suit != 0 => (0.0, 0.0, value),
            CardEffect::FirstFaceXMult(value) if first_face => (0.0, 0.0, value),
            _ => (0.0, 0.0, 1.0),
        }
    }
}

// --- Jokers with their own logic ------------------------------------------------

/// Python's floor division for the numbers scoring meets.
fn floor_div(a: f64, b: f64) -> f64 {
    (a / b).floor()
}

/// One joker's turn in the joker phase. `index` is the joker's place in the lineup.
pub fn joker_effect(index: usize, s: &mut Scoring, rolls: &mut Option<&mut dyn Rolls>) {
    let jokers = s.jokers;
    let joker = &jokers[index];
    if let Some(&(_, row)) = TABLE.iter().find(|(key, _)| *key == joker.key) {
        apply_row(row, joker, s);
        return;
    }
    let ab = &joker.ability;
    let around = s.around;
    let level = around.levels[s.hand_id as usize];
    let (hand_id, top, pairs) = (s.hand_id, s.top, s.pairs);
    let has = move |sub| contains(hand_id, sub, top, pairs);
    match joker.key.as_str() {
        "j_half" => {
            if s.played_count as f64 <= ab.size.unwrap_or(3.0) {
                s.mult += ab.mult.unwrap_or(20.0);
            }
        }
        "j_stencil" => {
            let stencils = jokers.iter().filter(|j| j.key == "j_stencil").count() as f64;
            let empty = around.joker_limit - jokers.len() as f64;
            s.mult *= (empty + stencils).max(1.0);
        }
        "j_blue_joker" => s.chips += joker.chips.unwrap_or(2.0 * around.deck_count),
        "j_banner" => s.chips += ab.extra.unwrap_or(30.0) * around.discards_left,
        "j_mystic_summit" => {
            if around.discards_left <= ab.d_remaining.unwrap_or(0.0) {
                s.mult += ab.mult.unwrap_or(15.0);
            }
        }
        "j_loyalty_card" => match ab.loyalty_remaining {
            Some(remaining) => {
                if remaining == 0.0 {
                    s.mult *= ab.xmult.unwrap_or(4.0);
                }
            }
            None => s.mult *= 1.5,
        },
        "j_misprint" => {
            let (low, high) = (ab.min.unwrap_or(0.0), ab.max.unwrap_or(23.0));
            match rolls {
                Some(r) => s.mult += r.uniform(low, high),
                None => {
                    let share = (around.hands_left / 4.0).min(1.0) * 0.5;
                    s.mult += low + (high - low) * share;
                }
            }
        }
        "j_abstract" => s.mult += ab.extra.unwrap_or(3.0) * jokers.len() as f64,
        "j_supernova" => s.mult += level.played + 1.0,
        "j_runner" => {
            // Stored chips are from before this hand: a Straight adds its increment first.
            let mut chips = joker.chips.unwrap_or(30.0);
            if has(SubHand::Straight) {
                chips += ab.chip_mod.unwrap_or(15.0);
            }
            s.chips += chips;
        }
        "j_square" => {
            let mut chips = joker.chips.unwrap_or(20.0);
            if s.played_count == 4 {
                chips += ab.chip_mod.unwrap_or(4.0);
            }
            s.chips += chips;
        }
        "j_trousers" => {
            let mut mult = joker.mult.unwrap_or(6.0);
            if has(SubHand::TwoPair) {
                mult += ab.extra.unwrap_or(2.0);
            }
            s.mult += mult;
        }
        "j_green_joker" => {
            // The Hook's discard takes back this hand's +1, unless the stored mult is already 0.
            let stored = joker.mult.unwrap_or(0.0);
            let mut gain = ab.hand_add.unwrap_or(1.0);
            if around.blind == Blind::Hook && stored > 0.0 {
                gain = 0.0;
            }
            s.mult += stored + gain;
        }
        "j_ride_the_bus" => {
            // A scoring face card resets it; debuffed faces do not count.
            let face = around.pareidolia || s.scoring.iter().any(|c| !c.debuffed() && (11..=13).contains(&c.rank));
            if !face {
                s.mult += joker.mult.unwrap_or(5.0) + ab.extra.unwrap_or(1.0);
            }
        }
        "j_blackboard" => {
            if s.held.iter().all(|c| c.suits_under(around.smeared) & (SPADES | CLUBS) != 0) {
                s.mult *= ab.extra.unwrap_or(3.0);
            }
        }
        "j_acrobat" => {
            if around.hands_left == 1.0 {
                s.mult *= ab.extra.unwrap_or(3.0);
            }
        }
        "j_card_sharp" => {
            if level.played_this_round > 0.0 {
                s.mult *= joker.xmult.unwrap_or(3.0);
            }
        }
        "j_seeing_double" => {
            let live = || s.scoring.iter().filter(|c| !c.debuffed());
            let club = live().any(|c| c.suits_under(around.smeared) & CLUBS != 0);
            let other = live().any(|c| c.suits_under(around.smeared) & !CLUBS != 0);
            if club && other {
                s.mult *= ab.extra.unwrap_or(2.0);
            }
        }
        "j_flower_pot" => flower_pot(joker, s),
        "j_drivers_license" => {
            if ab.driver_tally.is_some_and(|tally| tally >= 16.0) {
                s.mult *= ab.extra.unwrap_or(3.0);
            }
        }
        "j_bootstraps" => s.mult += ab.mult.unwrap_or(2.0) * floor_div(s.money, ab.dollars.unwrap_or(5.0)),
        "j_swashbuckler" => {
            let others: f64 = jokers.iter().enumerate().filter(|(i, _)| *i != index).map(|(_, j)| j.sell).sum();
            s.mult += others;
        }
        "j_bull" => s.chips += ab.extra.unwrap_or(2.0) * s.money,
        "j_stuntman" => s.chips += ab.chip_mod.unwrap_or(250.0),
        "j_wee" => {
            let twos = s.scoring.iter().filter(|c| !c.debuffed() && c.rank == 2).count() as f64;
            s.chips += joker.chips.unwrap_or(16.0) + ab.chip_mod.unwrap_or(8.0) * twos;
        }
        "j_lucky_cat" => {
            // Gains `extra` each time a Lucky card's mult hits: 1 in 5, or 2 in 5 with Oops.
            let stored = joker.xmult.unwrap_or(1.5);
            let extra = ab.extra.unwrap_or(0.25);
            let chance = if around.oops { 2.0 / 5.0 } else { 1.0 / 5.0 };
            let lucky: u32 = s
                .scoring
                .iter()
                .zip(s.triggers)
                .filter(|(c, _)| !c.debuffed() && c.is(LUCKY))
                .map(|(_, &t)| t)
                .sum();
            match rolls {
                Some(r) => {
                    let hits = (0..lucky).filter(|_| r.random() < chance).count() as f64;
                    s.mult *= stored + extra * hits;
                }
                None => s.mult *= stored + extra * chance * lucky as f64,
            }
        }
        "j_vampire" => s.mult *= s.vampire_xmult.unwrap_or_else(|| joker.xmult.unwrap_or(1.0)),
        "j_obelisk" => {
            // Grows when the hand played is not the most played one; a tie counts as most played.
            let most = around.levels.iter().fold(0.0f64, |m, l| if l.played > m { l.played } else { m });
            if level.played < most {
                s.mult *= joker.xmult.unwrap_or(1.0) + ab.extra.unwrap_or(0.2);
            }
        }
        "j_steel_joker" => {
            let xmult = joker.xmult.unwrap_or(1.0);
            if xmult > 1.0 {
                s.mult *= xmult;
            }
        }
        "j_ramen" => {
            // The Hook discards two cards before scoring, so the stored value is 0.02 stale.
            let mut xmult = joker.xmult.unwrap_or(1.5);
            if around.blind == Blind::Hook {
                xmult -= 0.02;
            }
            if xmult > 1.0 {
                s.mult *= xmult;
            }
        }
        "j_yorick" => {
            // The Hook's two discards can cross Yorick's threshold before scoring.
            let mut xmult = joker.xmult.unwrap_or(2.0);
            if around.blind == Blind::Hook && joker.counter.is_some_and(|left| left.trunc() <= 2.0) {
                xmult += 1.0;
            }
            s.mult *= xmult;
        }
        _ => {}
    }
}

/// Flower Pot: the scoring cards cover all four suits. Each Wild fills one missing
/// suit. With Smeared Joker: two red and two black cards instead.
fn flower_pot(joker: &JokerSpec, s: &mut Scoring) {
    if s.scoring.len() < 4 {
        return;
    }
    let fires = if s.around.smeared {
        let (mut red, mut black) = (0, 0);
        for card in s.scoring {
            if card.is(WILD) {
                if !card.debuffed() {
                    if red <= black {
                        red += 1;
                    } else {
                        black += 1;
                    }
                }
            } else if !card.is(STONE) {
                if card.suits & (HEARTS | DIAMONDS) != 0 {
                    red += 1;
                } else if card.suits & (CLUBS | SPADES) != 0 {
                    black += 1;
                }
            }
        }
        red >= 2 && black >= 2
    } else {
        let mut natural = 0u8;
        let mut wilds = 0;
        for card in s.scoring {
            if card.is(WILD) {
                if !card.debuffed() {
                    wilds += 1;
                }
            } else {
                natural |= card.suits;
            }
        }
        natural.count_ones() + wilds >= 4
    };
    if fires {
        s.mult *= joker.ability.extra.unwrap_or(3.0);
    }
}
