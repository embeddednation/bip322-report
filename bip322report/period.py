"""A reporting period, resolved to block heights.

Opening figures are "after every block up to and including ``start_height``";
closing figures likewise for ``end_height``; movements are the transactions
confirmed in between.  A calendar period ``[start, end)`` in UTC maps to the
last block *before* each instant, so a year's report opens on the last block
of the previous year and closes on the last block of the year.  An end that
lies ahead means to date: the statement closes on the last block before
today 00:00 UTC, the night before, which is the block ``bip322 audit
snapshot`` stamps by default, so a snapshot and a statement of the same day
agree on their block without a word.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from bip322audit.rpc import BitcoinCli
from bip322audit.stamp import header_before, parse_when

__all__ = [
    "Block",
    "Period",
    "block",
    "last_block_before",
    "parse_when",
    "period_between",
    "period_for_heights",
    "period_for_year",
    "start_of_today",
]


@dataclass(frozen=True)
class Block:
    height: int
    hash: str
    time: int

    @property
    def iso_time(self) -> str:
        return datetime.fromtimestamp(self.time, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def to_dict(self) -> dict:
        return {"height": self.height, "hash": self.hash, "time": self.time, "time_utc": self.iso_time}

    @classmethod
    def from_header(cls, header: dict) -> Block:
        return cls(int(header["height"]), str(header["hash"]), int(header["time"]))


@dataclass(frozen=True)
class Period:
    label: str
    start: Block  # opening balance is as of this block
    end: Block  # closing balance is as of this block
    start_when: str | None = None  # the instants asked for, when the period came from dates
    end_when: str | None = None
    to_date: bool = False  # the end instant lay ahead (or none was given): closed on the last block before today 00:00 UTC

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "start_when": self.start_when,
            "end_when": self.end_when,
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
            "to_date": self.to_date,
        }


def block(cli: BitcoinCli, height: int) -> Block:
    header = cli.block_header(cli.block_hash(height))
    return Block(int(header["height"]), header["hash"], int(header["time"]))


def last_block_before(cli: BitcoinCli, when: datetime, *, tip_height: int | None = None) -> Block:
    """The highest block whose header time is before ``when`` (bip322-audit's search, the one the snapshot's ``--before`` uses).

    Header times are not strictly monotonic, so this is the natural boundary
    rather than a mathematically unique one; the report prints the height it
    used, which is what makes it reproducible.  ``tip_height``: no higher than
    this, the tip of the history the statement is built from.
    """
    return Block.from_header(header_before(cli, when, tip_height=tip_height))


def _now() -> datetime:
    return datetime.now(UTC)  # in one place, so a test on a fake chain can set the clock to that chain's present


def start_of_today() -> datetime:
    """Today 00:00 UTC by the statement's clock: the instant a statement to date closes before."""
    return _now().replace(hour=0, minute=0, second=0, microsecond=0)


def period_between(cli: BitcoinCli, start: datetime, end: datetime, label: str | None = None, *, tip_height: int | None = None) -> Period:
    """The period ``[start, end)``: from the last block before ``start`` to the last block before ``end``.

    An end that lies ahead means the period runs to date: it closes on the
    last block before today 00:00 UTC (the tip when the node has no block
    after that yet), no higher than ``tip_height`` when given (the tip of the
    history the statement is built from, so a block that arrives after the
    history was read cannot open a gap).  An end in the past needs a block at
    or after it: until the node has one, the last block before the end is not
    settled (the node is behind, or the next block is yet to come), and
    closing on the tip would state a year-end that is not one.
    """
    if end <= start:
        raise ValueError("the period's end must be after its start")
    info = cli.call("getblockchaininfo")
    node_tip = int(info["blocks"])
    start_block = last_block_before(cli, start)
    to_date = end > _now()
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    if to_date:
        end_block = last_block_before(cli, start_of_today(), tip_height=tip_height)
    else:
        if info.get("initialblockdownload"):
            raise ValueError(f"the node is still syncing (at block {node_tip}): the last block before {end.strftime(fmt)} is not known yet")
        end_block = last_block_before(cli, end)
        if end_block.height >= node_tip:
            raise ValueError(
                f"the node's last block ({end_block.height}, {end_block.iso_time}) is before the period's end {end.strftime(fmt)}: "
                "the node is behind, or no later block exists yet, so the closing block is not settled"
            )
    if end_block.height <= start_block.height:
        raise ValueError(
            f"no block lies inside the period (both ends resolve to height {start_block.height})"
            + ("; a period to date closes on the last block before today 00:00 UTC" if to_date else "")
        )
    return Period(
        label or f"{start.strftime('%Y-%m-%d')}..{end.strftime('%Y-%m-%d')}",
        start_block,
        end_block,
        start.strftime(fmt),
        end.strftime(fmt),
        to_date,
    )


def period_for_year(cli: BitcoinCli, year: int, *, tip_height: int | None = None) -> Period:
    start = datetime(year, 1, 1, tzinfo=UTC)
    return period_between(cli, start, datetime(year + 1, 1, 1, tzinfo=UTC), label=str(year), tip_height=tip_height)


def period_for_heights(cli: BitcoinCli, start_height: int, end_height: int, label: str | None = None) -> Period:
    if end_height <= start_height:
        raise ValueError("the end height must be above the start height")
    tip_height, _ = cli.tip()
    if end_height > tip_height:
        raise ValueError(f"end height {end_height} is above the tip {tip_height}")
    return Period(label or f"{start_height}..{end_height}", block(cli, start_height), block(cli, end_height))
