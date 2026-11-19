//! One joker lineup in one round situation, and how a play scores under it.
//!
//! Mirrors `balatro_bot.domain.scoring.estimate.HandScorer` and
//! `balatro_bot.domain.scoring.play_order.PlayOrderer`.

use crate::card::{Card, ENHANCED, FACE, GOLD, GOLD_SEAL, RED_SEAL, STEEL};
use crate::hands::Rules;
use crate::jokers::{card_effect, joker_effect, Blind, CardEffect, JokerSpec, Rolls, Scoring, Surroundings};

/// The most cards one play can hold: the played cards, then scoring cards that were not played.
pub const MAX_CARDS: usize = 16;
/// The most cards a hand to search can hold: one bit per hand position in a `u32`.
pub const MAX_HAND: usize = 32;

/// One active joker's turn in the joker phase.
#[derive(Clone, Debug, Default)]
pub struct Step {
    /// Edition numbers: Foil's chips or Holographic's mult before the effect,
    /// Polychrome's xmult after it.
    pub pre_chips: Option<f64>,
    pub pre_mult: Option<f64>,
    pub post_xmult: Option<f64>,
    /// An Uncommon joker while a Baseball Card is in effect.
    pub baseball: bool,
    /// The joker whose effect runs: this one, or the one Blueprint/Brainstorm copy.
    pub runs: Option<usize>,
}

/// What Python works out once per lineup (`joker_effects.roster`): who is active,
/// who copies whom, and each joker's numbers.
#[derive(Clone, Debug, Default)]
pub struct LineupSpec {
    /// Every joker in order, debuffed ones included.
    pub jokers: Vec<JokerSpec>,
    /// One step per active joker, in order.
    pub steps: Vec<Step>,
    /// The joker in effect for each active joker, copies resolved to their target.
    pub resolved: Vec<usize>,
    /// Active Midas Mask and Vampire jokers, in order.
    pub before: Vec<usize>,
    /// One xmult per Baseball Card in effect.
    pub baseball: Vec<f64>,
    pub around: Surroundings,
    pub rules: Rules,
    /// Active (not copied) Hanging Chad and DNA, which change the play order.
    pub chad_active: bool,
    pub dna_active: bool,
}

#[derive(Clone, Copy, Debug)]
enum Before {
    /// Face cards that score become Gold.
    Midas,
    /// Scoring cards lose their enhancement; (stored xmult, gain per card).
    Vampire(f64, f64),
}

#[derive(Clone, Debug, Default)]
pub struct Lineup {
    pub around: Surroundings,
    pub rules: Rules,
    pub no_jokers: bool,
    jokers: Vec<JokerSpec>,
    steps: Vec<Step>,
    baseball: Vec<f64>,
    before: Vec<Before>,
    card_effects: Vec<CardEffect>,
    photograph: bool,
    chad_active: bool,
    dna_active: bool,
    // How many of each are in effect, copies included.
    hack: u32,
    sock: u32,
    chad: u32,
    every_card: u32,
    mime: u32,
    fist: u32,
    baron: (f64, u32),
    moon: (f64, u32),
    tooth: bool,
}

/// One play to score. `cards` holds the played cards in play order, then any
/// scoring cards that are not among them.
pub struct Play<'a> {
    pub hand_id: u8,
    pub cards: &'a [Card],
    pub played_count: usize,
    /// Positions in `cards` of the scoring cards, in the caller's order.
    pub scoring: &'a [u8],
    /// Bit per played position that scores.
    pub in_scoring: u16,
    pub held: &'a [Card],
    /// `cards` as they would be with a Gold enhancement, and with none; needed
    /// only when the lineup has Midas Mask or Vampire.
    pub gold: &'a [Card],
    pub plain: &'a [Card],
}

/// Chips and mult before the jokers, after them, and each active joker's share as
/// (chips added, mult added, mult ratio), and how many times each scoring card
/// triggered, in `Play::scoring` order.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Breakdown {
    pub pre_chips: f64,
    pub pre_mult: f64,
    pub chips: f64,
    pub mult: f64,
    pub jokers: Vec<(f64, f64, f64)>,
    pub triggers: Vec<u32>,
}

impl Lineup {
    pub fn new(spec: LineupSpec) -> Lineup {
        let LineupSpec { jokers, steps, resolved, before, baseball, around, rules, chad_active, dna_active } = spec;
        let count = |key: &str| resolved.iter().filter(|&&i| jokers[i].key == key).count() as u32;
        let last = |key: &str, default: f64| {
            resolved.iter().filter(|&&i| jokers[i].key == key).last().map_or(0.0, |&i| jokers[i].ability.extra.unwrap_or(default))
        };
        let card_effects: Vec<CardEffect> = resolved.iter().filter_map(|&i| card_effect(&jokers[i], &around)).collect();
        let dusk = if around.hands_left == 1.0 { count("j_dusk") } else { 0 };
        Lineup {
            no_jokers: jokers.is_empty(),
            photograph: card_effects.iter().any(|e| matches!(e, CardEffect::FirstFaceXMult(_))),
            hack: count("j_hack"),
            sock: count("j_sock_and_buskin"),
            chad: count("j_hanging_chad"),
            every_card: dusk + count("j_selzer"),
            mime: count("j_mime"),
            fist: count("j_raised_fist"),
            baron: (last("j_baron", 1.5), count("j_baron")),
            moon: (last("j_shoot_the_moon", 13.0), count("j_shoot_the_moon")),
            tooth: around.blind == Blind::Tooth,
            before: before
                .iter()
                .map(|&i| {
                    let joker = &jokers[i];
                    if joker.key == "j_midas_mask" {
                        Before::Midas
                    } else {
                        Before::Vampire(joker.xmult.unwrap_or(1.0), joker.ability.extra.unwrap_or(0.1))
                    }
                })
                .collect(),
            card_effects,
            chad_active,
            dna_active,
            around,
            rules,
            jokers,
            steps,
            baseball,
        }
    }

    pub fn has_before_phase(&self) -> bool {
        !self.before.is_empty()
    }

    /// Chips and mult for one play.
    pub fn score(&self, play: &Play, rolls: Option<&mut dyn Rolls>) -> (f64, f64) {
        self.score_traced(play, rolls, None)
    }

    /// The same, with each joker's share.
    pub fn breakdown(&self, play: &Play, rolls: Option<&mut dyn Rolls>) -> Breakdown {
        let mut out = Breakdown::default();
        let (chips, mult) = self.score_traced(play, rolls, Some(&mut out));
        out.chips = chips;
        out.mult = mult;
        out
    }

    fn score_traced(&self, play: &Play, rolls: Option<&mut dyn Rolls>, mut trace: Option<&mut Breakdown>) -> (f64, f64) {
        let level = self.around.levels[play.hand_id as usize];
        let (mut chips, mut mult) = (level.chips, level.mult);

        if self.no_jokers {
            for &p in play.scoring {
                let card = &play.cards[p as usize];
                chips += card.chips;
                mult += card.mult;
                mult *= card.xmult;
                mult += card.edition_mult;
                mult *= card.edition_xmult;
            }
            for card in play.held {
                if card.is(STEEL) && !card.debuffed() {
                    mult *= 1.5;
                }
            }
            if let Some(t) = trace.as_deref_mut() {
                (t.pre_chips, t.pre_mult) = (chips, mult);
                t.triggers = vec![1; play.scoring.len()];
            }
            return (chips, mult);
        }

        let around = &self.around;
        let mut money = if around.ox_locked == Some(play.hand_id) { 0.0 } else { around.money };

        // Before the cards score: Midas Mask and Vampire change the scoring cards, in joker order.
        let mut cards = [Card::default(); MAX_CARDS];
        cards[..play.cards.len()].copy_from_slice(play.cards);
        let mut vampire_xmult = None;
        for before in &self.before {
            match *before {
                Before::Midas => {
                    for &p in play.scoring {
                        let card = cards[p as usize];
                        let face = around.pareidolia || (11..=13).contains(&card.rank);
                        if card.rank != 0 && face && !card.is2(GOLD) {
                            cards[p as usize] = play.gold[p as usize];
                        }
                    }
                }
                Before::Vampire(stored, gain) => {
                    let mut stripped = 0.0;
                    for &p in play.scoring {
                        let card = cards[p as usize];
                        if card.is2(ENHANCED) && !card.debuffed() {
                            stripped += 1.0;
                            cards[p as usize] = play.plain[p as usize];
                        }
                    }
                    vampire_xmult = Some(if stripped > 0.0 { stored + gain * stripped } else { stored });
                }
            }
        }
        let played = &cards[..play.played_count];

        // The scoring cards in the order they score: as played, then any that were not played.
        let mut order = [0u8; MAX_CARDS];
        let mut n = 0;
        for p in 0..play.played_count as u8 {
            if play.in_scoring & (1 << p) != 0 {
                order[n] = p;
                n += 1;
            }
        }
        for &p in play.scoring {
            if p as usize >= play.played_count && !order[..n].contains(&p) {
                order[n] = p;
                n += 1;
            }
        }
        // Hanging Chad retriggers the first played card that scores.
        let chad_card = if self.chad > 0 { order[..n].iter().copied().find(|&p| (p as usize) < play.played_count) } else { None };

        let mut rolls = rolls;
        let mut triggers_at = [1u32; MAX_CARDS];
        let mut first_face_found = false;
        for &p in &order[..n] {
            let card = &cards[p as usize];
            let face = around.pareidolia || card.is(FACE);
            let mut triggers = 1;
            let mut first_face = false;
            if !card.debuffed() {
                triggers += self.every_card;
                if (2..=5).contains(&card.rank) {
                    triggers += self.hack;
                }
                if face {
                    triggers += self.sock;
                }
                if chad_card == Some(p) {
                    triggers += 2 * self.chad;
                }
                if card.is(RED_SEAL) {
                    triggers += 1;
                }
                if !first_face_found && face {
                    first_face = true;
                    first_face_found = true;
                }
                if card.is(GOLD_SEAL) {
                    money += 3.0 * triggers as f64;
                }
            }
            triggers_at[p as usize] = triggers;
            let suits = card.suits_under(around.smeared);
            for trigger in 0..triggers {
                chips += card.chips;
                mult += card.mult;
                mult *= card.xmult;
                mult += card.edition_mult;
                mult *= card.edition_xmult;
                if !card.debuffed() {
                    for effect in &self.card_effects {
                        effect.apply(card, suits, face, first_face, trigger, &mut chips, &mut mult, &mut rolls);
                    }
                }
            }
        }
        if self.tooth {
            money -= play.played_count as f64;
        }

        // Cards held in hand: Steel, then Baron, Shoot the Moon and Raised Fist.
        if !play.held.is_empty() {
            let mut fist_card = None;
            let mut fist_mult = 0.0;
            if self.fist > 0 {
                let mut lowest = 15;
                for (i, card) in play.held.iter().enumerate() {
                    if card.rank != 0 && card.rank <= lowest {
                        lowest = card.rank;
                        fist_card = Some(i);
                        fist_mult = if card.debuffed() { 0.0 } else { 2.0 * card.rank_chips() };
                    }
                }
            }
            for (i, card) in play.held.iter().enumerate() {
                if card.debuffed() {
                    continue;
                }
                for _ in 0..1 + self.mime {
                    if card.is(STEEL) {
                        mult *= 1.5;
                    }
                    if self.baron.0 != 0.0 && card.rank == 13 {
                        for _ in 0..self.baron.1 {
                            mult *= self.baron.0;
                        }
                    }
                    if self.moon.0 != 0.0 && card.rank == 12 {
                        for _ in 0..self.moon.1 {
                            mult += self.moon.0;
                        }
                    }
                    if fist_card == Some(i) {
                        for _ in 0..self.fist {
                            mult += fist_mult;
                        }
                    }
                }
            }
        }
        if let Some(t) = trace.as_deref_mut() {
            (t.pre_chips, t.pre_mult) = (chips, mult);
        }

        // The jokers, left to right.
        let mut scoring = [Card::default(); MAX_CARDS];
        let mut triggers = [1u32; MAX_CARDS];
        for (k, &p) in play.scoring.iter().enumerate() {
            scoring[k] = cards[p as usize];
            triggers[k] = triggers_at[p as usize];
        }
        if let Some(t) = trace.as_deref_mut() {
            t.triggers = triggers[..play.scoring.len()].to_vec();
        }
        let mut counts = [0u8; 15];
        for card in played.iter().filter(|c| c.rank != 0) {
            counts[card.rank as usize] += 1;
        }
        let mut s = Scoring {
            chips,
            mult,
            money,
            hand_id: play.hand_id,
            scoring: &scoring[..play.scoring.len()],
            played_count: play.played_count,
            held: play.held,
            top: counts.iter().copied().max().unwrap_or(0),
            pairs: counts.iter().filter(|&&c| c >= 2).count() as u8,
            triggers: &triggers[..play.scoring.len()],
            vampire_xmult,
            around,
            jokers: &self.jokers,
        };
        for step in &self.steps {
            let (chips_before, mult_before) = (s.chips, s.mult);
            if let Some(value) = step.pre_chips {
                s.chips += value;
            } else if let Some(value) = step.pre_mult {
                s.mult += value;
            }
            if let Some(index) = step.runs {
                joker_effect(index, &mut s, &mut rolls);
            }
            if let Some(value) = step.post_xmult {
                s.mult *= value;
            }
            if step.baseball {
                for &xmult in &self.baseball {
                    s.mult *= xmult;
                }
            }
            if let Some(t) = trace.as_deref_mut() {
                let added = s.mult - mult_before;
                let ratio = if mult_before > 0.0 && added != 0.0 { s.mult / mult_before } else { 1.0 };
                t.jokers.push((s.chips - chips_before, added, ratio));
            }
        }
        (s.chips, s.mult)
    }

    // --- Play order -----------------------------------------------------------

    /// What the per-card jokers add to one trigger of the card: (chips, mult, xmult).
    fn contribution(&self, card: &Card, first_face: bool) -> (f64, f64, f64) {
        if card.debuffed() {
            return (0.0, 0.0, 1.0);
        }
        let suits = card.suits_under(self.around.smeared);
        let face = self.around.pareidolia || card.is(FACE);
        let (mut chips, mut mult, mut xmult) = (0.0, 0.0, 1.0);
        for effect in &self.card_effects {
            let (c, m, x) = effect.for_ordering(card, suits, face, first_face);
            chips += c;
            mult += m;
            xmult *= x;
        }
        (chips, mult, xmult)
    }

    fn card_xmult(&self, card: &Card, first_face: bool) -> f64 {
        if card.debuffed() {
            1.0
        } else {
            card.xmult * card.edition_xmult * self.contribution(card, first_face).2
        }
    }

    fn adds_mult(&self, card: &Card) -> bool {
        !card.debuffed() && card.mult + card.edition_mult + self.contribution(card, false).1 > 0.0
    }

    /// The order to play the cards at `positions` (ascending) in: hand order, unless
    /// a multiplying card would score before one that adds mult, then adding cards go
    /// left of multiplying ones; after any card a joker wants first.
    pub fn play_order(&self, hand: &[Card], positions: &[u8], out: &mut [u8]) {
        let n = positions.len();
        out[..n].copy_from_slice(positions);
        if n <= 1 {
            return;
        }
        let face = |card: &Card| self.around.pareidolia || card.is(FACE);
        let first_of = |score: &dyn Fn(&Card) -> f64, among: &dyn Fn(&Card) -> bool| -> Option<u8> {
            let mut best: Option<(u8, f64)> = None;
            for &p in positions {
                let card = &hand[p as usize];
                if !among(card) {
                    continue;
                }
                let value = score(card);
                if best.map_or(true, |(_, top)| value > top) {
                    best = Some((p, value));
                }
            }
            best.map(|(p, _)| p)
        };
        let everyone = |_: &Card| true;
        let photo_face = |card: &Card| !card.debuffed() && face(card);
        let mut first = None;
        let mut photographed = None;
        if self.chad_active {
            // The first card triggers three times: xmult cubed, twice the additions extra.
            first = first_of(
                &|card| {
                    if card.debuffed() {
                        return 0.0;
                    }
                    let (chips, mult, _) = self.contribution(card, false);
                    let adds = card.chips + card.mult + chips + mult;
                    self.card_xmult(card, face(card)).powf(3.0) + 2.0 * adds
                },
                &everyone,
            );
        } else {
            if self.photograph {
                photographed = first_of(&|card| self.card_xmult(card, true), &photo_face);
            }
            if photographed.is_none() && self.dna_active {
                first = first_of(&|card| if card.debuffed() { -1.0 } else { card.chips / 100.0 }, &everyone);
            }
        }
        let mut k = 0;
        if let Some(p) = first {
            out[0] = p;
            k = 1;
        }
        let multiplies = |p: u8| self.card_xmult(&hand[p as usize], Some(p) == photographed) > 1.0;
        let mut multiplied = false;
        let mut clash = false;
        for &p in positions {
            if Some(p) == first {
                continue;
            }
            if multiplied && self.adds_mult(&hand[p as usize]) {
                clash = true;
                break;
            }
            if multiplies(p) {
                multiplied = true;
            }
        }
        for right in [false, true] {
            for &p in positions {
                if Some(p) == first || (clash && multiplies(p) != right) || (!clash && right) {
                    continue;
                }
                out[k] = p;
                k += 1;
            }
        }
        // Photograph's face scores before every other face.
        if let Some(best) = photographed {
            let at = out[..n].iter().position(|&p| photo_face(&hand[p as usize])).unwrap();
            let from = out[..n].iter().position(|&p| p == best).unwrap();
            out[at..=from].rotate_right(1);
        }
    }
}
