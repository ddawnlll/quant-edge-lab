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
}
