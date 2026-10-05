"""The fictional company's working calendar: Monday–Saturday within business hours, except company holidays.
Accounts-payable payment runs happen mostly on Tuesdays and Fridays."""

import random
from datetime import date, datetime, time, timedelta

HOLIDAYS = [
    ((1, 26), "Republic Day"),
    ((5, 1), "Labour Day"),
    ((7, 7), "Company Foundation Day"),
    ((8, 15), "Independence Day"),
    ((10, 2), "Gandhi Jayanti"),
    ((12, 25), "Christmas"),
]
PAYMENT_RUN_DAYS = (1, 4)  # Tuesday, Friday
_HOURS = [(10, 2), (11, 4), (12, 3), (14, 3), (15, 4), (16, 3), (17, 2)]


class Calendar:
    def __init__(self, start: date, end: date, business_hours: list[str]) -> None:
        self.start, self.end = start, end
        self.open = time.fromisoformat(business_hours[0])
        self.close = time.fromisoformat(business_hours[1])
        self.holidays: dict[date, str] = {}
        for year in range(start.year, end.year + 1):
            for (m, d), name in HOLIDAYS:
                day = date(year, m, d)
                if start <= day <= end:
                    self.holidays[day] = name

    def is_working(self, d: date) -> bool:
        return d.weekday() != 6 and d not in self.holidays

    def months(self) -> list[date]:
        out, d = [], self.start.replace(day=1)
        while d <= self.end:
            out.append(d)
            d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        return out

    @staticmethod
    def month_end(month: date) -> date:
        return (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    def working_days(self, month: date, weekdays: tuple[int, ...] | None = None) -> list[date]:
        days, d = [], max(month, self.start)
        last = min(self.month_end(month), self.end)
        while d <= last:
            if self.is_working(d) and (weekdays is None or d.weekday() in weekdays):
                days.append(d)
            d += timedelta(days=1)
        return days

    def last_working_day(self, month: date) -> date:
        return self.working_days(month)[-1]

    def next_working(self, d: date, offset: int = 0) -> date:
        while not self.is_working(d):
            d += timedelta(days=1)
        for _ in range(offset):
            d += timedelta(days=1)
            while not self.is_working(d):
                d += timedelta(days=1)
        return d

    def payment_day(self, rng: random.Random, month: date) -> date:
        """A payment-run day (80% Tue/Fri), never a Sunday or holiday."""
        runs = self.working_days(month, PAYMENT_RUN_DAYS)
        if runs and rng.random() < 0.8:
            return rng.choice(runs)
        return rng.choice(self.working_days(month))

    def business_time(self, rng: random.Random, d: date) -> datetime:
        hours, weights = zip(*_HOURS, strict=True)
        h = rng.choices(hours, weights)[0]
        t = datetime.combine(d, time(h, rng.randint(0, 59), rng.randint(0, 59)))
        lo, hi = datetime.combine(d, self.open), datetime.combine(d, self.close)
        return min(max(t, lo), hi)
