//! Python binding for the scoring kernel.
//!
//! Cards cross as fixed-size byte records built once per card by `pack_card`; a
//! hand is those records joined. Everything Python decides once per lineup (who
//! is active, who copies whom, each joker's numbers) arrives in `Lineup(...)`.

use std::collections::HashMap;

use jackpotts_kernel_core as core;
use jackpotts_kernel_core::card::{unpack, Card, CARD_BYTES};
use jackpotts_kernel_core::hands::{Rules, HAND_COUNT};
use jackpotts_kernel_core::jokers::{Ability, Blind, JokerSpec, Level, Rolls, Surroundings};
use jackpotts_kernel_core::lineup::{LineupSpec, Play, Step, MAX_CARDS, MAX_HAND};
use jackpotts_kernel_core::search::{Constraints, Found, Hand};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedBytes;
use pyo3::types::PyBytes;

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn pack_card<'py>(
    py: Python<'py>,
    rank: u8,
    suits: u8,
    suits_smeared: u8,
    flags: u8,
    flags2: u8,
    chips: f64,
    mult: f64,
    xmult: f64,
    edition_mult: f64,
    edition_xmult: f64,
) -> Bound<'py, PyBytes> {
    let card = Card { rank, suits, suits_smeared, flags, flags2, chips, mult, xmult, edition_mult, edition_xmult };
    PyBytes::new(py, &card.to_bytes())
}

/// Card records, `limit` at most.
fn cards_within(bytes: &[u8], limit: usize) -> PyResult<Vec<Card>> {
    if bytes.len() % CARD_BYTES != 0 || bytes.len() / CARD_BYTES > limit {
        return Err(PyValueError::new_err("not a whole number of card records, or too many cards"));
    }
    Ok(unpack(bytes))
}

/// A hand, or cards taken from one: its held cards, its Gold and plain variants.
fn cards(bytes: &[u8]) -> PyResult<Vec<Card>> {
    cards_within(bytes, MAX_HAND)
}

/// Dice drawn from the Python random generator the caller passed.
struct PyRolls<'py> {
    rng: Bound<'py, PyAny>,
    error: Option<PyErr>,
}

impl PyRolls<'_> {
    fn draw(&mut self, result: PyResult<f64>) -> f64 {
        result.unwrap_or_else(|e| {
            self.error.get_or_insert(e);
            0.0
        })
    }
}

impl Rolls for PyRolls<'_> {
    fn random(&mut self) -> f64 {
        let drawn = self.rng.call_method0("random").and_then(|v| v.extract());
        self.draw(drawn)
    }

    fn uniform(&mut self, low: f64, high: f64) -> f64 {
        let drawn = self.rng.call_method1("uniform", (low, high)).and_then(|v| v.extract());
        self.draw(drawn)
    }
}

type JokerTuple = (String, f64, Option<f64>, Option<f64>, Option<f64>, Option<f64>, HashMap<String, f64>);
type StepTuple = (Option<f64>, Option<f64>, Option<f64>, bool, Option<usize>);
type SettingsTuple = (f64, f64, f64, f64, f64, u8, Option<u8>, u8, u8, u8);
type FlagsTuple = (bool, bool, bool, bool, bool, bool, bool, bool);
/// One play as `Lineup.score` takes it: (hand id, cards, played count, scoring
/// positions, scoring bits, held, gold, plain).
type PlayTuple = (u8, PyBackedBytes, usize, PyBackedBytes, u16, PyBackedBytes, PyBackedBytes, PyBackedBytes);
/// (hand id, played bits, scoring positions, chips, mult, total)
type FoundTuple<'py> = (u8, u32, Bound<'py, PyBytes>, f64, f64, f64);

fn found_tuple<'py>(py: Python<'py>, found: &Found) -> FoundTuple<'py> {
    let scoring = PyBytes::new(py, &found.scoring[..found.scoring_len as usize]);
    (found.hand_id, found.cards, scoring, found.chips, found.mult, found.total)
}

#[pyclass(frozen)]
struct Lineup {
    inner: core::lineup::Lineup,
}

impl Lineup {
    #[allow(clippy::too_many_arguments)]
    fn with_play<T>(
        &self,
        hand_id: u8,
        played_and_extra: &[u8],
        played_count: usize,
        scoring: &[u8],
        in_scoring: u16,
        held: &[u8],
        gold: &[u8],
        plain: &[u8],
        run: impl FnOnce(&Play) -> T,
    ) -> PyResult<T> {
        let all = cards_within(played_and_extra, MAX_CARDS)?;
        let (held, gold, plain) = (cards(held)?, cards_within(gold, MAX_CARDS)?, cards_within(plain, MAX_CARDS)?);
        let bad_position = scoring.iter().any(|&p| p as usize >= all.len());
        let bad_variants = self.inner.has_before_phase() && (gold.len() != all.len() || plain.len() != all.len());
        if hand_id as usize >= HAND_COUNT || played_count > all.len() || bad_position || bad_variants {
            return Err(PyValueError::new_err("play does not fit its cards"));
        }
        let play = Play { hand_id, cards: &all, played_count, scoring, in_scoring, held: &held, gold: &gold, plain: &plain };
        Ok(run(&play))
    }
}

fn limits(
    min_select: usize,
    max_select: usize,
    required_hand: Option<u8>,
    excluded_hands: u16,
    required_cards: u32,
    affinity: Option<Vec<f64>>,
) -> PyResult<Constraints> {
    let affinity = match affinity {
        None => None,
        Some(weights) => Some(
            <[f64; HAND_COUNT]>::try_from(weights).map_err(|_| PyValueError::new_err("affinity needs one weight per hand type"))?,
        ),
    };
    Ok(Constraints { min_select, max_select, required_hand, excluded_hands, required_cards, affinity })
}

#[pymethods]
impl Lineup {
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(
        jokers: Vec<JokerTuple>,
        steps: Vec<StepTuple>,
        resolved: Vec<usize>,
        before: Vec<usize>,
        baseball: Vec<f64>,
        levels: Vec<(f64, f64, f64, f64)>,
        settings: SettingsTuple,
        flags: FlagsTuple,
    ) -> PyResult<Self> {
        let count = jokers.len();
        let in_range = |i: &usize| *i < count;
        if levels.len() != HAND_COUNT
            || !resolved.iter().all(in_range)
            || !before.iter().all(in_range)
            || !steps.iter().all(|s| s.4.map_or(true, |i| i < count))
        {
            return Err(PyValueError::new_err("lineup does not fit its jokers or hand levels"));
        }
        let (money, discards_left, hands_left, joker_limit, deck_count, blind, ox_locked, ancient_suit, idol_rank, idol_suit) = settings;
        let (pareidolia, smeared, oops, four_fingers, shortcut, splash, chad_active, dna_active) = flags;
        let mut around = Surroundings {
            money,
            discards_left,
            hands_left,
            joker_limit,
            deck_count,
            blind: match blind {
                1 => Blind::Hook,
                2 => Blind::Tooth,
                _ => Blind::Other,
            },
            ox_locked,
            ancient_suit,
            idol_rank,
            idol_suit,
            pareidolia,
            smeared,
            oops,
            ..Surroundings::default()
        };
        for (slot, (chips, mult, played, played_this_round)) in around.levels.iter_mut().zip(levels) {
            *slot = Level { chips, mult, played, played_this_round };
        }
        let spec = LineupSpec {
            jokers: jokers
                .into_iter()
                .map(|(key, sell, chips, mult, xmult, counter, fields)| {
                    let mut ability = Ability::default();
                    for (name, value) in &fields {
                        ability.set(name, *value);
                    }
                    JokerSpec { key, sell, chips, mult, xmult, counter, ability }
                })
                .collect(),
            steps: steps
                .into_iter()
                .map(|(pre_chips, pre_mult, post_xmult, baseball, runs)| Step { pre_chips, pre_mult, post_xmult, baseball, runs })
                .collect(),
            resolved,
            before,
            baseball,
            around,
            rules: Rules { four_fingers, shortcut, smeared, splash },
            chad_active,
            dna_active,
        };
        Ok(Lineup { inner: core::lineup::Lineup::new(spec) })
    }

    /// (chips, mult) for one play. `cards` is the played cards in play order, then
    /// scoring cards that were not played; `scoring` their positions in the caller's order.
    #[pyo3(signature = (hand_id, cards, played_count, scoring, in_scoring, held, gold, plain, rng=None))]
    #[allow(clippy::too_many_arguments)]
    fn score(
        &self,
        hand_id: u8,
        cards: &[u8],
        played_count: usize,
        scoring: &[u8],
        in_scoring: u16,
        held: &[u8],
        gold: &[u8],
        plain: &[u8],
        rng: Option<Bound<'_, PyAny>>,
    ) -> PyResult<(f64, f64)> {
        let mut rolls = rng.map(|rng| PyRolls { rng, error: None });
        let scored = self.with_play(hand_id, cards, played_count, scoring, in_scoring, held, gold, plain, |play| {
            self.inner.score(play, rolls.as_mut().map(|r| r as &mut dyn Rolls))
        })?;
        match rolls.and_then(|r| r.error) {
            Some(error) => Err(error),
            None => Ok(scored),
        }
    }

    /// `score` for many plays in one call. `rngs`, when given, holds one random
    /// generator per play.
    #[pyo3(signature = (plays, rngs=None))]
    fn score_many(&self, plays: Vec<PlayTuple>, rngs: Option<Vec<Bound<'_, PyAny>>>) -> PyResult<Vec<(f64, f64)>> {
        if rngs.as_ref().is_some_and(|r| r.len() != plays.len()) {
            return Err(PyValueError::new_err("one random generator per play"));
        }
        let mut rngs = rngs.map(|r| r.into_iter());
        let mut out = Vec::with_capacity(plays.len());
        for (hand_id, cards, played_count, scoring, in_scoring, held, gold, plain) in &plays {
            let mut rolls = rngs.as_mut().and_then(|r| r.next()).map(|rng| PyRolls { rng, error: None });
            let scored = self.with_play(*hand_id, cards, *played_count, scoring, *in_scoring, held, gold, plain, |play| {
                self.inner.score(play, rolls.as_mut().map(|r| r as &mut dyn Rolls))
            })?;
            if let Some(error) = rolls.and_then(|r| r.error) {
                return Err(error);
            }
            out.push(scored);
        }
        Ok(out)
    }

    /// (chips before jokers, mult before jokers, chips, mult, [(chips added, mult
    /// added, mult ratio) per active joker]).
    #[allow(clippy::too_many_arguments, clippy::type_complexity)]
    fn breakdown(
        &self,
        hand_id: u8,
        cards: &[u8],
        played_count: usize,
        scoring: &[u8],
        in_scoring: u16,
        held: &[u8],
        gold: &[u8],
        plain: &[u8],
    ) -> PyResult<(f64, f64, f64, f64, Vec<(f64, f64, f64)>)> {
        let b = self.with_play(hand_id, cards, played_count, scoring, in_scoring, held, gold, plain, |play| {
            self.inner.breakdown(play, None)
        })?;
        Ok((b.pre_chips, b.pre_mult, b.chips, b.mult, b.jokers))
    }

    /// Every allowed play out of the hand, best first.
    #[pyo3(signature = (hand, gold, plain, min_select=1, max_select=5, required_hand=None, excluded_hands=0, required_cards=0, affinity=None))]
    #[allow(clippy::too_many_arguments)]
    fn search<'py>(
        &self,
        py: Python<'py>,
        hand: &[u8],
        gold: &[u8],
        plain: &[u8],
        min_select: usize,
        max_select: usize,
        required_hand: Option<u8>,
        excluded_hands: u16,
        required_cards: u32,
        affinity: Option<Vec<f64>>,
    ) -> PyResult<Vec<FoundTuple<'py>>> {
        let (hand, gold, plain) = (cards(hand)?, cards(gold)?, cards(plain)?);
        self.check_variants(&hand, &gold, &plain)?;
        let limits = limits(min_select, max_select, required_hand, excluded_hands, required_cards, affinity)?;
        let found = self.inner.search(&Hand { cards: &hand, gold: &gold, plain: &plain }, &limits);
        Ok(found.iter().map(|f| found_tuple(py, f)).collect())
    }

    /// The best allowed play, or None.
    #[pyo3(signature = (hand, gold, plain, min_select=1, max_select=5, required_hand=None, excluded_hands=0, required_cards=0, affinity=None))]
    #[allow(clippy::too_many_arguments)]
    fn best<'py>(
        &self,
        py: Python<'py>,
        hand: &[u8],
        gold: &[u8],
        plain: &[u8],
        min_select: usize,
        max_select: usize,
        required_hand: Option<u8>,
        excluded_hands: u16,
        required_cards: u32,
        affinity: Option<Vec<f64>>,
    ) -> PyResult<Option<FoundTuple<'py>>> {
        let (hand, gold, plain) = (cards(hand)?, cards(gold)?, cards(plain)?);
        self.check_variants(&hand, &gold, &plain)?;
        let limits = limits(min_select, max_select, required_hand, excluded_hands, required_cards, affinity)?;
        let found = self.inner.best(&Hand { cards: &hand, gold: &gold, plain: &plain }, &limits);
        Ok(found.as_ref().map(|f| found_tuple(py, f)))
    }

    /// The best allowed play's total for each hand, or None where a hand allows no
    /// play. Each hand is (cards, gold, plain).
    #[pyo3(signature = (hands, min_select=1, max_select=5, required_hand=None, excluded_hands=0, required_cards=0, affinity=None))]
    #[allow(clippy::too_many_arguments)]
    fn best_totals(
        &self,
        hands: Vec<(PyBackedBytes, PyBackedBytes, PyBackedBytes)>,
        min_select: usize,
        max_select: usize,
        required_hand: Option<u8>,
        excluded_hands: u16,
        required_cards: u32,
        affinity: Option<Vec<f64>>,
    ) -> PyResult<Vec<Option<f64>>> {
        let limits = limits(min_select, max_select, required_hand, excluded_hands, required_cards, affinity)?;
        let mut totals = Vec::with_capacity(hands.len());
        for (hand, gold, plain) in &hands {
            let (hand, gold, plain) = (cards(hand)?, cards(gold)?, cards(plain)?);
            self.check_variants(&hand, &gold, &plain)?;
            let found = self.inner.best(&Hand { cards: &hand, gold: &gold, plain: &plain }, &limits);
            totals.push(found.map(|f| f.total));
        }
        Ok(totals)
    }

    /// The order to play the hand cards at `positions` (ascending) in.
    fn play_order<'py>(&self, py: Python<'py>, hand: &[u8], positions: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
        let hand = cards(hand)?;
        if positions.len() > MAX_CARDS || positions.iter().any(|&p| p as usize >= hand.len()) {
            return Err(PyValueError::new_err("positions do not fit the hand"));
        }
        let mut out = [0u8; MAX_CARDS];
        self.inner.play_order(&hand, positions, &mut out);
        Ok(PyBytes::new(py, &out[..positions.len()]))
    }
}

impl Lineup {
    fn check_variants(&self, hand: &[Card], gold: &[Card], plain: &[Card]) -> PyResult<()> {
        if self.inner.has_before_phase() && (gold.len() != hand.len() || plain.len() != hand.len()) {
            return Err(PyValueError::new_err("this lineup needs the hand's Gold and plain variants"));
        }
        Ok(())
    }
}

#[pymodule]
fn jackpotts_kernel(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(pack_card, m)?)?;
    m.add_class::<Lineup>()?;
    m.add("CARD_BYTES", CARD_BYTES)?;
    Ok(())
}
