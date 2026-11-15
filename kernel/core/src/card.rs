//! One playing card as scoring sees it.
//!
//! Python builds the record from `balatro_bot.cards` (the accessors stay there),
//! so the numbers here are already what the card adds on each trigger: a debuffed
//! card carries zeros and ones.

// `flags`
pub const STONE: u8 = 1;
pub const DEBUFFED: u8 = 2;
pub const FACE: u8 = 4;
pub const RED_SEAL: u8 = 8;
pub const STEEL: u8 = 16;
/// Wild enhancement, debuffed or not.
pub const WILD: u8 = 32;
pub const LUCKY: u8 = 64;
pub const GOLD_SEAL: u8 = 128;

// `flags2`
/// Has any enhancement.
pub const ENHANCED: u8 = 1;
/// Gold enhancement.
pub const GOLD: u8 = 2;

// Suit bits, matching `balatro_bot.cards._SUIT_BIT`.
pub const HEARTS: u8 = 1;
pub const DIAMONDS: u8 = 2;
pub const CLUBS: u8 = 4;
pub const SPADES: u8 = 8;

pub const CARD_BYTES: usize = 48;

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Card {
    /// 2..=14, or 0 for a card with no rank (Stone).
    pub rank: u8,
    /// Bit per suit the card counts as.
    pub suits: u8,
    /// The same with Smeared Joker's merged suits.
    pub suits_smeared: u8,
    pub flags: u8,
    pub flags2: u8,
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
        out[4] = self.flags2;
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
            flags2: b[4],
            chips: number(0),
            mult: number(1),
            xmult: number(2),
            edition_mult: number(3),
            edition_xmult: number(4),
        }
    }

    pub fn is(&self, flag: u8) -> bool {
        self.flags & flag != 0
    }

    pub fn is2(&self, flag: u8) -> bool {
        self.flags2 & flag != 0
    }

    pub fn debuffed(&self) -> bool {
        self.is(DEBUFFED)
    }

    pub fn suits_under(&self, smeared: bool) -> u8 {
        if smeared {
            self.suits_smeared
        } else {
            self.suits
        }
    }

    /// The chips the rank is worth: 2-10 at face value, J/Q/K 10, Ace 11.
    pub fn rank_chips(&self) -> f64 {
        match self.rank {
            14 => 11.0,
            11..=13 => 10.0,
            r => r as f64,
        }
    }
}

pub fn unpack(bytes: &[u8]) -> Vec<Card> {
    bytes.chunks_exact(CARD_BYTES).map(Card::from_bytes).collect()
}
