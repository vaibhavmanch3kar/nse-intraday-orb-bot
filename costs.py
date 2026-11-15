"""Zerodha equity-intraday charges for one round trip (rates in config.py)."""

import config as C


def order_charges(price: float, qty: int, side: str) -> float:
    """Charges for ONE executed order. side = 'BUY' or 'SELL'."""
    turnover = price * qty
    brokerage = min(C.BROKERAGE_CAP, turnover * C.BROKERAGE_PCT / 100)
    exch = turnover * C.EXCH_TXN_PCT / 100
    sebi = turnover * C.SEBI_PCT / 100
    gst = (brokerage + exch + sebi) * C.GST_PCT / 100
    stt = turnover * C.STT_SELL_PCT / 100 if side == "SELL" else 0.0
    stamp = turnover * C.STAMP_BUY_PCT / 100 if side == "BUY" else 0.0
    return brokerage + exch + sebi + gst + stt + stamp


def round_trip(entry: float, exit_: float, qty: int, direction: str) -> float:
    """Total charges for entry + exit. direction = 'LONG' or 'SHORT'."""
    if direction == "LONG":
        return order_charges(entry, qty, "BUY") + order_charges(exit_, qty, "SELL")
    return order_charges(entry, qty, "SELL") + order_charges(exit_, qty, "BUY")


def gross_pnl(entry: float, exit_: float, qty: int, direction: str) -> float:
    return (exit_ - entry) * qty if direction == "LONG" else (entry - exit_) * qty
