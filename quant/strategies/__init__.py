from .base import Signal, Strategy
from .sector_reversion import SectorReversion
from .etf_arb import EtfArb
from .opening_range import OpeningRange

ALL = {"sector_reversion": SectorReversion, "etf_arb": EtfArb, "opening_range": OpeningRange}
