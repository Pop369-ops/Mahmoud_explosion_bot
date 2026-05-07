"""
Position Sizing Calculator — the most important risk management module.

Philosophy: The entry/SL/TP are useless if your position size is wrong.
A great signal with bad sizing loses money. A mediocre signal with proper
sizing keeps you alive.

Two methods supported:
1. Fixed Fractional: risk X% of capital per trade (recommended for retail)
2. Kelly Criterion (lite): adjust based on win rate and avg R:R (advanced)

Industry standard: 0.5% to 2% risk per trade.
- Conservative: 0.5%   (long-term capital growth, 100+ trades to compound)
- Standard: 1%         (balanced — recommended for most)
- Aggressive: 2%       (high conviction setups only, expert only)
- Reckless: > 2%       (gambling territory)
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PositionSize:
    """Result of position size calculation."""
    capital: float                # user's total capital
    risk_pct: float               # % of capital risked on this trade
    risk_usd: float               # dollar amount at risk
    position_size_usd: float      # actual money to deploy
    position_size_units: float    # quantity of the asset
    leverage_used: float          # 1x, 2x, 5x, etc.
    sl_distance_pct: float        # SL distance as % of entry
    notional_value: float         # position_size_usd × leverage

    # Liquidation safety
    liquidation_price: float      # estimated liq price (if leveraged)
    liq_distance_pct: float       # safety buffer to liquidation
    safe: bool                    # True if liq is far from SL

    # Sanity checks
    warnings: list = field(default_factory=list)


def calculate_position_size(
    capital: float,
    entry_price: float,
    sl_price: float,
    direction: str = "long",
    risk_pct: float = 1.0,
    leverage: float = 1.0,
    maintenance_margin: float = 0.005,  # 0.5% — Binance standard for crypto
) -> PositionSize:
    """Calculate proper position size based on risk %.

    Args:
        capital: total trading capital in USD
        entry_price: planned entry price
        sl_price: stop loss price
        direction: 'long' or 'short'
        risk_pct: % of capital to risk (default 1.0%)
        leverage: leverage multiplier (1.0 = spot/no leverage)
        maintenance_margin: exchange's MM rate (0.5% for crypto perps)

    Returns:
        PositionSize with all calculations done
    """
    warnings = []

    if capital <= 0:
        warnings.append("⚠️ رأس المال غير محدد")
        return _empty_position(capital, warnings)

    if entry_price <= 0 or sl_price <= 0:
        warnings.append("⚠️ سعر الدخول أو SL غير صحيح")
        return _empty_position(capital, warnings)

    # SL distance as % of entry
    sl_dist_pct = abs(entry_price - sl_price) / entry_price * 100
    if sl_dist_pct < 0.1:
        warnings.append("⚠️ مسافة SL ضيقة جداً (< 0.1%) — احتمال spike يضربه")
        return _empty_position(capital, warnings)

    # Sanity check on risk_pct
    if risk_pct > 5.0:
        warnings.append(f"🚨 مخاطرة {risk_pct}% عالية جداً — مخاطر تدمير الحساب")
    elif risk_pct > 2.0:
        warnings.append(f"⚠️ مخاطرة {risk_pct}% فوق الموصى به (2%)")

    # ─── Core calculation ───────────────────────────────
    risk_usd = capital * (risk_pct / 100)

    # Position size = risk_usd / sl_distance%
    # Without leverage: I need $X position to lose $risk_usd if SL hits
    position_size_usd = risk_usd / (sl_dist_pct / 100)
    position_size_units = position_size_usd / entry_price

    # With leverage:
    notional_value = position_size_usd
    margin_required = notional_value / leverage

    # Cap at available capital (can't use more margin than capital)
    if margin_required > capital:
        # Need to scale down
        scale = capital / margin_required
        position_size_usd *= scale
        position_size_units *= scale
        notional_value *= scale
        margin_required = capital
        warnings.append(
            f"⚠️ تم تقليص حجم المركز — رأس المال غير كافٍ للرافعة {leverage}x"
        )

    # ─── Liquidation calculation ────────────────────────
    # Simplified Binance-style isolated margin liq formula
    # liq_dist% ≈ (1/leverage - maintenance_margin) × 100
    if leverage > 1:
        liq_dist_pct = (1 / leverage - maintenance_margin) * 100
        if direction == "long":
            liquidation_price = entry_price * (1 - liq_dist_pct / 100)
        else:
            liquidation_price = entry_price * (1 + liq_dist_pct / 100)

        # Safety: liquidation must be FURTHER than SL (in the loss direction)
        if direction == "long":
            safe = liquidation_price < sl_price
        else:
            safe = liquidation_price > sl_price

        if not safe:
            warnings.append(
                f"🚨 خطر تصفية! سعر التصفية ${liquidation_price:.6g} "
                f"أقرب من SL — قلل الرافعة أو ابعد SL"
            )
        elif abs(liquidation_price - sl_price) / sl_price < 0.005:
            warnings.append(
                f"⚠️ سعر التصفية قريب من SL (< 0.5%) — استخدم رافعة أقل"
            )
    else:
        liquidation_price = 0.0
        liq_dist_pct = 100.0  # no liquidation in spot
        safe = True

    return PositionSize(
        capital=capital,
        risk_pct=risk_pct,
        risk_usd=round(risk_usd, 2),
        position_size_usd=round(position_size_usd, 2),
        position_size_units=round(position_size_units, 6),
        leverage_used=leverage,
        sl_distance_pct=round(sl_dist_pct, 2),
        notional_value=round(notional_value, 2),
        liquidation_price=round(liquidation_price, 8) if liquidation_price else 0,
        liq_distance_pct=round(liq_dist_pct, 2),
        safe=safe,
        warnings=warnings,
    )


def _empty_position(capital: float, warnings: list) -> PositionSize:
    return PositionSize(
        capital=capital, risk_pct=0, risk_usd=0,
        position_size_usd=0, position_size_units=0,
        leverage_used=1, sl_distance_pct=0, notional_value=0,
        liquidation_price=0, liq_distance_pct=0, safe=False,
        warnings=warnings,
    )


def suggest_leverage(sl_distance_pct: float, max_leverage: int = 20) -> int:
    """Suggest a sensible leverage based on SL distance.

    Rule of thumb: liquidation should be at least 3x further than SL.
    With maintenance margin ~0.5%:
      - SL 1%   → safe leverage ≤ 25x  (we cap at 20)
      - SL 2%   → safe leverage ≤ 15x
      - SL 3%   → safe leverage ≤ 10x
      - SL 5%   → safe leverage ≤ 6x
      - SL 7%+  → safe leverage ≤ 4x
    """
    if sl_distance_pct <= 0:
        return 1
    # liq_dist needs to be ≥ 3 × sl_dist for safety
    max_safe_lev = int(100 / (sl_distance_pct * 3 + 0.5))
    return max(1, min(max_leverage, max_safe_lev))
