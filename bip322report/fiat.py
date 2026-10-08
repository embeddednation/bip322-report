"""Optional valuation of movements in a fiat currency.

Rates come from the user: a constant, or a CSV of ``date,rate`` rows (rate per
whole BTC).  For a transaction the rate in force is the latest dated row on or
before its day.  Nothing is fetched from anywhere.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

SATOSHI = Decimal(100_000_000)


def _rate(text: str, where: str) -> Decimal:
    """A rate per BTC: a positive, finite number, or a ValueError that says where it stood."""
    try:
        rate = Decimal(str(text).strip())
    except InvalidOperation:
        rate = None
    if rate is None or not rate.is_finite() or rate <= 0:
        raise ValueError(f"{where}: {str(text).strip()!r} is not a rate (a positive number per BTC)")
    return rate


@dataclass
class Rates:
    currency: str
    constant: Decimal | None = None
    table: list[tuple[date, Decimal]] | None = None  # sorted by date
    source: str = ""

    @classmethod
    def constant_rate(cls, currency: str, rate: str) -> Rates:
        return cls(currency, _rate(rate, "the rate"), None, f"constant {rate} {currency}/BTC")

    @classmethod
    def from_csv(cls, currency: str, path: Path) -> Rates:
        rows = []
        with Path(path).open(newline="") as fh:
            reader = csv.reader(fh)
            for row in reader:
                if not any(cell.strip() for cell in row) or row[0].strip().lower() == "date" or row[0].lstrip().startswith("#"):
                    continue
                where = f"{path}:{reader.line_num}"
                if len(row) < 2:
                    raise ValueError(f"{where}: expected date,rate")
                try:
                    day = date.fromisoformat(row[0].strip())
                except ValueError:
                    raise ValueError(f"{where}: {row[0].strip()!r} is not a date (YYYY-MM-DD)") from None
                rows.append((day, _rate(row[1], where)))
        if not rows:
            raise ValueError(f"{path}: no date,rate rows")
        rows.sort()
        return cls(currency, None, rows, f"{path} ({len(rows)} daily rates)")

    def rate_on(self, when: int) -> Decimal | None:
        """The rate for a unix time, or None when the table starts later."""
        if self.constant is not None:
            return self.constant
        day = datetime.fromtimestamp(when, UTC).date()
        rate = None
        for d, r in self.table or []:
            if d <= day:
                rate = r
            else:
                break
        return rate

    def value(self, sat: int, when: int) -> str | None:
        rate = self.rate_on(when)
        if rate is None:
            return None
        return str((Decimal(sat) / SATOSHI * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def to_dict(self) -> dict:
        return {"currency": self.currency, "source": self.source}
