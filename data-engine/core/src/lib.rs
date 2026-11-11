//! QEL Data Engine v2 core: exact fixed-point rescale without float.
//!
//! The archive Parquet schema v2 stores coefficients at scale 1e8 (int64).
//! The pinned Nautilus runner consumes Price at 2dp and Quantity at 3dp via
//! `Decimal::round_dp` (half-even midpoint) then `Price::from(str)`. This
//! module replicates that rounding with checked integer arithmetic so an
//! Arrow batch can be converted to typed `Vec<Data>` without JSON and
//! without floating point. Zero-copy Arrow ingestion is NOT claimed here;
//! see the nautilus-adapter crate for the typed conversion boundary.

/// Errors for checked rescale.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RescaleError {
    /// Value would overflow i64 after rescale.
    Overflow,
    /// Exponent delta is out of the supported range (source scale must be >= target).
    BadScale,
}

pub type RescaleResult<T> = Result<T, RescaleError>;

/// `10^exp` for small exp (0..=18), checked.
fn pow10(exp: u32) -> RescaleResult<i64> {
    let mut out: i64 = 1;
    for _ in 0..exp {
        out = out.checked_mul(10).ok_or(RescaleError::Overflow)?;
    }
    Ok(out)
}

/// Rescale a coefficient from `src_scale` to `dst_scale` (both decimal
/// places), rounding half-even on the dropped digits. Replicates
/// `Decimal(value).scaleb(-src).round_dp(dst)` for exact inputs whose
/// string form has no more digits than `src_scale`.
///
/// Returns (coeff_at_dst_scale, exact) is not provided: rounding is always
/// applied and the caller accepts the same rounding the legacy runner did.
/// For values exactly representable at the target scale this is exact.
pub fn rescale_half_even(coeff: i64, src_scale: u32, dst_scale: u32) -> RescaleResult<i64> {
    if src_scale < dst_scale {
        // Upscaling: multiply, check overflow.
        let factor = pow10(dst_scale - src_scale)?;
        return coeff.checked_mul(factor).ok_or(RescaleError::Overflow);
    }
    if src_scale == dst_scale {
        return Ok(coeff);
    }
    let drop = src_scale - dst_scale;
    let divisor = pow10(drop)?;
    // Half-even on magnitude; sign handled symmetrically (round_dp is
    // sign-symmetric for negative values).
    let sign: i64 = if coeff < 0 { -1 } else { 1 };
    let mag = coeff.checked_abs().ok_or(RescaleError::Overflow)? as i64;
    let q = mag / divisor;
    let r = mag % divisor;
    let half = divisor / 2;
    let round_up = r > half || (r == half && q % 2 == 1);
    let rounded = if round_up { q + 1 } else { q };
    Ok(sign * rounded)
}

/// Price coeff at 1e8 -> price coeff at 1e2 (Nautilus Price precision 2).
pub fn price_coeff_1e8_to_2dp(coeff_1e8: i64) -> RescaleResult<i64> {
    rescale_half_even(coeff_1e8, 8, 2)
}

/// Qty coeff at 1e8 -> qty coeff at 1e3 (Nautilus Quantity precision 3).
pub fn qty_coeff_1e8_to_3dp(coeff_1e8: i64) -> RescaleResult<i64> {
    rescale_half_even(coeff_1e8, 8, 3)
}

/// Errors for checked decimal-string to fixed-point conversion.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DecimalError {
    /// Not a finite decimal literal (empty, malformed, NaN/inf, bad exponent).
    Invalid,
    /// Value needs more than 8 decimal places exactly (trailing zeros allowed).
    Precision,
    /// Value would overflow i64 at scale 1e8 (or the mantissa overflows i128).
    Overflow,
}

/// `10^exp` as i128 for small exp, checked.
fn pow10_i128(exp: u32) -> Result<i128, DecimalError> {
    let mut out: i128 = 1;
    for _ in 0..exp {
        out = out.checked_mul(10).ok_or(DecimalError::Overflow)?;
    }
    Ok(out)
}

/// Parsed decimal as `mantissa * 10^-scale`. `scale` may be negative
/// (positive exponent, e.g. `1E+2` => mantissa 1, scale -2).
struct ParsedDecimal {
    mantissa: i128,
    scale: i32,
}

fn parse_decimal_parts(text: &str) -> Result<ParsedDecimal, DecimalError> {
    let s = text.trim();
    if s.is_empty() {
        return Err(DecimalError::Invalid);
    }
    let lower = s.to_ascii_lowercase();
    if matches!(
        lower.as_str(),
        "nan" | "inf" | "+inf" | "-inf" | "infinity" | "+infinity" | "-infinity"
    ) {
        return Err(DecimalError::Invalid);
    }
    let (mantissa_part, exponent): (&str, i32) = match s.find(['e', 'E']) {
        Some(pos) => {
            let (m, e) = s.split_at(pos);
            let exp_str = &e[1..];
            if exp_str.is_empty() || exp_str.matches(['e', 'E']).count() > 0 {
                return Err(DecimalError::Invalid);
            }
            let exp: i32 = exp_str.parse().map_err(|_| DecimalError::Invalid)?;
            if exp.abs() > 1000 {
                return Err(DecimalError::Overflow);
            }
            (m, exp)
        }
        None => (s, 0),
    };
    let (sign, body) = match mantissa_part.strip_prefix('-') {
        Some(r) => (-1i128, r),
        None => match mantissa_part.strip_prefix('+') {
            Some(r) => (1i128, r),
            None => (1i128, mantissa_part),
        },
    };
    if body.is_empty() || body.matches('.').count() > 1 {
        return Err(DecimalError::Invalid);
    }
    let mut digits = String::with_capacity(body.len());
    let mut frac_digits: i32 = 0;
    let mut seen_dot = false;
    let mut seen_digit = false;
    for ch in body.chars() {
        match ch {
            '.' => seen_dot = true,
            '0'..='9' => {
                seen_digit = true;
                digits.push(ch);
                if seen_dot {
                    frac_digits += 1;
                }
            }
            _ => return Err(DecimalError::Invalid),
        }
    }
    if !seen_digit {
        return Err(DecimalError::Invalid);
    }
    let mut mantissa: i128 = 0;
    for ch in digits.chars() {
        let d = ch as i128 - '0' as i128;
        mantissa = mantissa
            .checked_mul(10)
            .and_then(|v| v.checked_add(d))
            .ok_or(DecimalError::Overflow)?;
    }
    Ok(ParsedDecimal {
        mantissa: sign * mantissa,
        scale: frac_digits - exponent,
    })
}

fn i128_to_i64(v: i128) -> Result<i64, DecimalError> {
    i64::try_from(v).map_err(|_| DecimalError::Overflow)
}

/// Parse a decimal string to an exact coefficient at scale 1e8.
///
/// Mirrors the Python reference (`Decimal(text)` then multiply by 1e8 and
/// require an integral result): values needing more than 8 decimal places
/// are rejected unless the extra digits are trailing zeros. Scientific
/// notation (`-1E-8`, as in the monthly fundingRate archive) is supported.
/// No floating point is involved.
pub fn parse_decimal_coeff_1e8(text: &str) -> Result<i64, DecimalError> {
    let p = parse_decimal_parts(text)?;
    if p.mantissa == 0 {
        return Ok(0);
    }
    if p.scale <= 8 {
        let factor = pow10_i128((8 - p.scale) as u32)?;
        let coeff = p
            .mantissa
            .checked_mul(factor)
            .ok_or(DecimalError::Overflow)?;
        i128_to_i64(coeff)
    } else {
        let drop = (p.scale - 8) as u32;
        if drop > 38 {
            return Err(DecimalError::Precision);
        }
        let divisor = pow10_i128(drop)?;
        if p.mantissa % divisor != 0 {
            return Err(DecimalError::Precision);
        }
        i128_to_i64(p.mantissa / divisor)
    }
}

/// Parse a quantity decimal string to a coefficient at scale 1e8 after
/// quantizing to 3 decimal places (half-even, sign-symmetric).
///
/// Mirrors the canonical builders: `Decimal(qty).quantize(Decimal("0.001"))`
/// (default context, ROUND_HALF_EVEN) before scaling by 1e8. Quantization
/// happens before any 8dp precision limit, so quantities with more digits
/// round instead of failing.
pub fn parse_decimal_qty_coeff_1e8(text: &str) -> Result<i64, DecimalError> {
    let p = parse_decimal_parts(text)?;
    if p.mantissa == 0 {
        return Ok(0);
    }
    let coeff_3dp: i128 = if p.scale <= 3 {
        let factor = pow10_i128((3 - p.scale) as u32)?;
        p.mantissa
            .checked_mul(factor)
            .ok_or(DecimalError::Overflow)?
    } else {
        let drop = (p.scale - 3) as u32;
        if drop > 38 {
            // Far below half of the smallest 3dp step: rounds to zero.
            return Ok(0);
        }
        let divisor = pow10_i128(drop)?;
        let sign: i128 = if p.mantissa < 0 { -1 } else { 1 };
        let mag = p.mantissa.checked_abs().ok_or(DecimalError::Overflow)?;
        let q = mag / divisor;
        let r = mag % divisor;
        let half = divisor / 2;
        let round_up = r > half || (r == half && q % 2 == 1);
        sign * if round_up { q + 1 } else { q }
    };
    let coeff_1e8 = coeff_3dp
        .checked_mul(100_000)
        .ok_or(DecimalError::Overflow)?;
    i128_to_i64(coeff_1e8)
}

/// Causal streaming EMA state in exact 1e-9 units.
///
/// Semantics mirror `qel.research.control._ema` (alpha = 2/(window+1),
/// state rounded to a 1e-9 grid each step, half-even) and the Python
/// `arrow_features.EmaState`, but with checked integer arithmetic. The
/// state is carried by the caller across RecordBatch boundaries, so a
/// split input yields identical outputs to a single batch. The recurrence
/// is inherently sequential; this integer kernel replaces the Python
/// scalar loop in the production path.
#[derive(Clone, Debug)]
pub struct EmaState {
    pub window: u32,
    pub state_units: Option<i64>,
}

impl EmaState {
    pub fn new(window: u32) -> Self {
        Self {
            window: window.max(1),
            state_units: None,
        }
    }

    /// One causal step. `mid_units_1e9` is the mid price in 1e-9 units
    /// (see [`mid_units_1e9`]). Returns the new state.
    pub fn update(&mut self, mid_units_1e9: i64) -> i64 {
        match self.state_units {
            None => {
                self.state_units = Some(mid_units_1e9);
                mid_units_1e9
            }
            Some(state) => {
                let window = self.window as i128;
                let numer = 2i128 * mid_units_1e9 as i128 + (window - 1) * state as i128;
                let denom = window + 1;
                let rounded = round_half_even_div_i128(numer, denom);
                // Prices within the certified instrument range stay far
                // inside i64 at 1e-9 units; saturate rather than wrap.
                let clamped = rounded.clamp(i64::MIN as i128, i64::MAX as i128) as i64;
                self.state_units = Some(clamped);
                clamped
            }
        }
    }
}

/// Round `numer / denom` (denom > 0) to nearest integer, half-even,
/// sign-symmetric. Mirrors Python `round(Fraction)` on the 1e-9 grid.
pub fn round_half_even_div_i128(numer: i128, denom: i128) -> i128 {
    debug_assert!(denom > 0);
    let sign: i128 = if numer < 0 { -1 } else { 1 };
    let mag = numer.abs();
    let q = mag / denom;
    let r = mag % denom;
    let round_up = 2 * r > denom || (2 * r == denom && q % 2 == 1);
    sign * if round_up { q + 1 } else { q }
}

/// Exact mid price in 1e-9 units from bid/ask coefficients at 1e8:
/// mid = (bid + ask) / 2, scaled by 1e9 => (bid + ask) * 5. Checked.
pub fn mid_units_1e9(bid_i: i64, ask_i: i64) -> Option<i64> {
    let sum = bid_i as i128 + ask_i as i128;
    i64::try_from(sum * 5).ok()
}

/// Causal EMA over a slice of mid prices in 1e-9 units. Use [`EmaState`]
/// directly when streaming across batch boundaries.
pub fn ema_stream(mids_units_1e9: &[i64], window: u32) -> Vec<i64> {
    let mut state = EmaState::new(window);
    mids_units_1e9.iter().map(|&m| state.update(m)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exact_price_passthrough() {
        // 100.00 at 1e8 = 10_000_000_000 -> 10000 at 1e2.
        assert_eq!(price_coeff_1e8_to_2dp(10_000_000_000), Ok(10_000));
    }

    #[test]
    fn half_even_rounding_matches_decimal() {
        // 1.005 -> 1.00 (2 is even) ; 1.015 -> 1.02 (1 is odd, up).
        // Coeffs at 1e8: 100500000, 101500000.
        assert_eq!(price_coeff_1e8_to_2dp(100_500_000), Ok(100));
        assert_eq!(price_coeff_1e8_to_2dp(101_500_000), Ok(102));
        // Negative funding-like value: -0.005 -> 0 boundary, half-even to even.
        // -1.005 -> -1.00 symmetric.
        assert_eq!(price_coeff_1e8_to_2dp(-100_500_000), Ok(-100));
    }

    #[test]
    fn qty_three_dp_exact_and_round() {
        assert_eq!(qty_coeff_1e8_to_3dp(25_000_000), Ok(250)); // 0.250
        // 0.2505 -> tie at 3dp: 250 is even, stays 250.
        assert_eq!(qty_coeff_1e8_to_3dp(25_050_000), Ok(250));
    }

    #[test]
    fn overflow_rejected() {
        assert_eq!(rescale_half_even(i64::MAX, 0, 8), Err(RescaleError::Overflow));
        // 9223372036854775807 / 1e6 = 9223372036854 rem 775807 > half -> rounds up.
        assert_eq!(price_coeff_1e8_to_2dp(i64::MAX), Ok(9_223_372_036_855));
    }

    #[test]
    fn decimal_parser_exact_values() {
        assert_eq!(parse_decimal_coeff_1e8("58941.9"), Ok(5_894_190_000_000));
        assert_eq!(parse_decimal_coeff_1e8("58941.90"), Ok(5_894_190_000_000));
        assert_eq!(parse_decimal_coeff_1e8("0.00004113"), Ok(4_113));
        assert_eq!(parse_decimal_coeff_1e8("-1E-8"), Ok(-1));
        assert_eq!(parse_decimal_coeff_1e8("-1e-8"), Ok(-1));
        assert_eq!(parse_decimal_coeff_1e8("1E+2"), Ok(10_000_000_000));
        assert_eq!(parse_decimal_coeff_1e8("1.100000000"), Ok(110_000_000));
        assert_eq!(parse_decimal_coeff_1e8("0"), Ok(0));
        assert_eq!(parse_decimal_coeff_1e8("-0.00000001"), Ok(-1));
        assert_eq!(parse_decimal_coeff_1e8("+12.5"), Ok(1_250_000_000));
    }

    #[test]
    fn decimal_parser_rejects() {
        assert_eq!(parse_decimal_coeff_1e8(""), Err(DecimalError::Invalid));
        assert_eq!(parse_decimal_coeff_1e8("abc"), Err(DecimalError::Invalid));
        assert_eq!(parse_decimal_coeff_1e8("1.2.3"), Err(DecimalError::Invalid));
        assert_eq!(parse_decimal_coeff_1e8("NaN"), Err(DecimalError::Invalid));
        assert_eq!(parse_decimal_coeff_1e8("inf"), Err(DecimalError::Invalid));
        assert_eq!(parse_decimal_coeff_1e8("1e"), Err(DecimalError::Invalid));
        assert_eq!(
            parse_decimal_coeff_1e8("1.000000001"),
            Err(DecimalError::Precision)
        );
        assert_eq!(
            parse_decimal_coeff_1e8("99999999999999999999"),
            Err(DecimalError::Overflow)
        );
        assert_eq!(
            parse_decimal_coeff_1e8("92233720368.54775808"),
            Err(DecimalError::Overflow)
        );
    }

    #[test]
    fn qty_parser_quantizes_half_even() {
        assert_eq!(parse_decimal_qty_coeff_1e8("5.366"), Ok(536_600_000));
        assert_eq!(parse_decimal_qty_coeff_1e8("1"), Ok(100_000_000));
        // Ties at 3dp: 0.0005 -> 0.000 (0 even), 0.0015 -> 0.002 (1 odd, up),
        // 0.0025 -> 0.002 (2 even), 0.0035 -> 0.004 (3 odd, up).
        assert_eq!(parse_decimal_qty_coeff_1e8("0.0005"), Ok(0));
        assert_eq!(parse_decimal_qty_coeff_1e8("0.0015"), Ok(200_000));
        assert_eq!(parse_decimal_qty_coeff_1e8("0.0025"), Ok(200_000));
        assert_eq!(parse_decimal_qty_coeff_1e8("0.0035"), Ok(400_000));
        assert_eq!(parse_decimal_qty_coeff_1e8("-0.0015"), Ok(-200_000));
        // More than 8dp rounds instead of failing (quantize happens first).
        assert_eq!(parse_decimal_qty_coeff_1e8("0.00149999999"), Ok(100_000));
    }

    #[test]
    fn ema_matches_fraction_reference() {
        // Mids at 1e8: [100, 100.5, 101, 100, 99]. Expected values are the
        // exact control._ema Fractions scaled by 1e9 (generated with the
        // Python reference, window=20):
        // [100/1, 12505952381/125000000, 25034580499/250000000,
        //  100125148473/1000000000, 25004497869/250000000] * 1e9.
        let mids_coeff = [100_00000000i64, 100_50000000, 101_00000000, 100_00000000, 99_00000000];
        let mids_units: Vec<i64> = mids_coeff.iter().map(|&c| c * 10).collect();
        assert_eq!(
            ema_stream(&mids_units, 20),
            vec![100_000_000_000, 100_047_619_048, 100_138_321_996, 100_125_148_473, 100_017_991_476]
        );
    }

    #[test]
    fn ema_batch_boundary_carry_and_causality() {
        let mids: Vec<i64> = (0..7).map(|i| 100_000_000_000 + i * 10).collect();
        let whole = ema_stream(&mids, 20);
        // Split across a batch boundary: carried state must match.
        let mut state = EmaState::new(20);
        let mut split: Vec<i64> = mids[..3].iter().map(|&m| state.update(m)).collect();
        split.extend(mids[3..].iter().map(|&m| state.update(m)));
        assert_eq!(whole, split);
        // Causality: changing the future must not change earlier outputs.
        let mut a = mids.clone();
        a.extend([200_000_000_000; 5]);
        let mut b = mids.clone();
        b.extend([0; 5]);
        let ea = ema_stream(&a, 20);
        let eb = ema_stream(&b, 20);
        assert_eq!(ea[..7], eb[..7]);
        assert_ne!(ea[7..], eb[7..]);
    }

    #[test]
    fn mid_units_exact() {
        // bid 58941.89 / ask 58941.91 at 1e8 -> mid 58941.90 at 1e9.
        assert_eq!(
            mid_units_1e9(5_894_189_000_000, 5_894_191_000_000),
            Some(58_941_900_000_000)
        );
        assert_eq!(mid_units_1e9(i64::MAX, i64::MAX), None);
    }
}
