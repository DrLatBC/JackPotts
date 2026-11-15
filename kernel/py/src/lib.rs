//! Python binding for the scoring kernel. Cards cross as fixed-size byte records
//! built once per card by `pack_card`; a hand is those records joined.

use jackpotts_kernel_core as core;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn pack_card<'py>(
    py: Python<'py>,
    rank: u8,
    suits: u8,
    suits_smeared: u8,
    flags: u8,
    chips: f64,
    mult: f64,
    xmult: f64,
    edition_mult: f64,
    edition_xmult: f64,
) -> Bound<'py, PyBytes> {
    let card = core::Card { rank, suits, suits_smeared, flags, chips, mult, xmult, edition_mult, edition_xmult };
    PyBytes::new(py, &card.to_bytes())
}

fn unpack(hand: &[u8]) -> PyResult<Vec<core::Card>> {
    if hand.len() % core::CARD_BYTES != 0 || hand.len() / core::CARD_BYTES > core::MAX_HAND {
        return Err(PyValueError::new_err("hand is not a whole number of card records, or too many cards"));
    }
    Ok(hand.chunks_exact(core::CARD_BYTES).map(core::Card::from_bytes).collect())
}

type PlayTuple = (u8, u16, u16, f64, f64, f64);

fn as_tuple(play: core::Play) -> PlayTuple {
    (play.hand_id, play.cards, play.scoring, play.chips, play.mult, play.total)
}

#[pyclass(frozen)]
struct Lineup {
    inner: core::Lineup,
}

#[pymethods]
impl Lineup {
    /// `levels` is (chips, mult) per hand in HAND_TYPES order; `jokers` is (key, value).
    #[new]
    #[pyo3(signature = (levels, jokers, four_fingers=false, shortcut=false, smeared=false, splash=false))]
    fn new(
        levels: Vec<(f64, f64)>,
        jokers: Vec<(String, f64)>,
        four_fingers: bool,
        shortcut: bool,
        smeared: bool,
        splash: bool,
    ) -> PyResult<Self> {
        if levels.len() != core::HAND_COUNT {
            return Err(PyValueError::new_err("levels needs one entry per hand type"));
        }
        let mut inner = core::Lineup { rules: core::Rules { four_fingers, shortcut, smeared, splash }, ..Default::default() };
        inner.levels.copy_from_slice(&levels);
        for (key, value) in jokers {
            match key.as_str() {
                "j_joker" => inner.effects.push(core::JokerEffect::FlatMult(value)),
                "j_jolly" => inner.effects.push(core::JokerEffect::PairMult(value)),
                "j_cavendish" => inner.effects.push(core::JokerEffect::XMult(value)),
                "j_greedy_joker" => inner.suit_mult.push((core::DIAMONDS, value)),
                "j_hack" => inner.hack = true,
                "j_baron" => inner.baron.push(value),
                other => return Err(PyValueError::new_err(format!("joker not in the spike: {other}"))),
            }
        }
        Ok(Lineup { inner })
    }

    /// The best play as (hand id, played bits, scoring bits, chips, mult, total), or None.
    #[pyo3(signature = (hand, min_select=1, max_select=5))]
    fn best(&self, hand: &[u8], min_select: usize, max_select: usize) -> PyResult<Option<PlayTuple>> {
        let cards = unpack(hand)?;
        Ok(self.inner.best(&cards, min_select, max_select).map(as_tuple))
    }

    /// Every play, in enumeration order.
    #[pyo3(signature = (hand, min_select=1, max_select=5))]
    fn plays(&self, hand: &[u8], min_select: usize, max_select: usize) -> PyResult<Vec<PlayTuple>> {
        let cards = unpack(hand)?;
        Ok(self.inner.plays(&cards, min_select, max_select).map(as_tuple).collect())
    }

    /// The best total for each hand made of `keep` plus one of `draws`.
    fn best_totals(&self, keep: &[u8], draws: Vec<Vec<u8>>) -> PyResult<Vec<f64>> {
        let kept = unpack(keep)?;
        let mut totals = Vec::with_capacity(draws.len());
        let mut hand = Vec::with_capacity(core::MAX_HAND);
        for draw in &draws {
            hand.clear();
            hand.extend_from_slice(&kept);
            hand.extend(unpack(draw)?);
            totals.push(self.inner.best(&hand, 1, 5).map_or(0.0, |p| p.total));
        }
        Ok(totals)
    }
}

#[pymodule]
fn jackpotts_kernel(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(pack_card, m)?)?;
    m.add_class::<Lineup>()?;
    m.add("CARD_BYTES", core::CARD_BYTES)?;
    Ok(())
}
