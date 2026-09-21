"""PIN policy for the second factor gating the Security and Payments areas.

Deliberately modest, and shaped by the lockout rather than by password habits.
Ten guesses inside a 3-hour window against a 4-digit space is a 0.1% chance per
window, which the lockout makes the binding constraint — so a long PIN buys
very little, while a PIN people cannot remember gets written on a monitor.

What the policy does rule out is the handful of PINs an attacker would try
first, since those are exactly the ones that make ten guesses enough: a single
repeated digit, and a run of consecutive digits in either direction.
"""
import re

PIN_MIN_LENGTH = 4
PIN_MAX_LENGTH = 8

# How long one successful PIN entry keeps the gated areas open. Fixed from the
# moment of verification, not sliding: a sliding window would stay open
# indefinitely for anyone who keeps clicking, which is the opposite of what a
# re-authentication prompt is for.
PIN_ELEVATION_MINUTES = 15

PIN_POLICY_MESSAGE = f"Your PIN must be {PIN_MIN_LENGTH} to {PIN_MAX_LENGTH} digits."
PIN_TOO_SIMPLE_MESSAGE = "Choose a less predictable PIN — not one repeated digit, and not a run like 1234."


def _is_run(pin: str) -> bool:
    """True for strictly ascending or descending consecutive digits."""
    steps = {ord(b) - ord(a) for a, b in zip(pin, pin[1:])}
    return steps in ({1}, {-1})


def pin_policy_error(pin: str) -> str | None:
    """None when the PIN is acceptable, else the message to show."""
    candidate = pin or ""
    if not re.fullmatch(rf"\d{{{PIN_MIN_LENGTH},{PIN_MAX_LENGTH}}}", candidate):
        return PIN_POLICY_MESSAGE
    if len(set(candidate)) == 1 or _is_run(candidate):
        return PIN_TOO_SIMPLE_MESSAGE
    return None
