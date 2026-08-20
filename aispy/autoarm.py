import datetime
import math

# Fixed reference point for the every-N-days repeat. Anchoring to a real date instead of
# "whenever the process started" is what makes a schedule reproducible across restarts,
# and is what lets previous_occurrence() be computed at all.
DEFAULT_ANCHOR_DATE = datetime.date(1970, 1, 1)


class AutoArm:
	def __init__(self, hour, minute=0, repeat_every_days=1, active_days=None, do_arm=True,
				 anchor_date=None, now=None):
		self.hour = hour
		self.minute = minute
		self.repeat_every_days = max(1, repeat_every_days)
		self.active_days = active_days if active_days is not None else [0, 1, 2, 3, 4, 5, 6]
		self.do_arm = do_arm
		self.anchor_date = anchor_date if anchor_date is not None else self.default_anchor()
		# A day either matches the weekday list or it does not, and the N-day repeat has a
		# fixed phase, so the pattern repeats every lcm(7, N) days. Searching further than
		# that can only find days we have already looked at.
		self.search_window = math.lcm(7, self.repeat_every_days)
		self.next_time = self.next_occurrence(now)

	def default_anchor(self) -> datetime.date:
		"""Start the repeat count on a day the timer is actually allowed to fire.

		Anchoring on a fixed calendar date alone would make 'every 7 days on Wednesday'
		fire never, because 1970-01-01 was a Thursday and the repeat would only ever
		land on Thursdays. Stepping the anchor forward to the first active weekday makes
		the usual 'every N weeks on day X' schedule work while staying restart-stable.
		"""
		for offset in range(7):
			day = DEFAULT_ANCHOR_DATE + datetime.timedelta(days=offset)
			if day.weekday() in self.active_days:
				return day
		return DEFAULT_ANCHOR_DATE

	def matches(self, day: datetime.date) -> bool:
		"""Is this a day the timer fires on?"""
		if day.weekday() not in self.active_days:
			return False
		return (day - self.anchor_date).days % self.repeat_every_days == 0

	def time_on(self, day: datetime.date) -> datetime.datetime:
		return datetime.datetime.combine(day, datetime.time(self.hour, self.minute))

	def next_occurrence(self, now=None) -> datetime.datetime | None:
		"""The first firing strictly after now, or None if the timer never fires."""
		now = now if now is not None else datetime.datetime.now()
		for offset in range(self.search_window + 1):
			day = (now + datetime.timedelta(days=offset)).date()
			if self.matches(day):
				fire_time = self.time_on(day)
				if fire_time > now:
					return fire_time
		return None

	def previous_occurrence(self, now=None) -> datetime.datetime | None:
		"""The most recent firing at or before now, or None if the timer never fires."""
		now = now if now is not None else datetime.datetime.now()
		for offset in range(self.search_window + 1):
			day = (now - datetime.timedelta(days=offset)).date()
			if self.matches(day):
				fire_time = self.time_on(day)
				if fire_time <= now:
					return fire_time
		return None

	def check_action(self, now=None):
		"""Return do_arm if the timer is due, else None. Polled, so it must be cheap."""
		now = now if now is not None else datetime.datetime.now()
		if self.next_time is None or now < self.next_time:
			return None
		self.next_time = self.next_occurrence(now)
		return self.do_arm

	def never_fires(self) -> bool:
		"""True for a config whose weekday list and repeat never intersect."""
		return self.next_occurrence() is None and self.previous_occurrence() is None

	def __str__(self):
		arm_str = 'Arm' if self.do_arm else 'Disarm'
		return (f'{arm_str} at {self.hour:02d}:{self.minute:02d} on days: {self.active_days}, '
				f'repeating every {self.repeat_every_days} days')


def last_scheduled_action(timers, now=None):
	"""The most recent action any of the timers should have taken by now.

	Used at startup to put the system into the state the schedule says it should be in,
	instead of whatever it was in when the power went out. Only the latest action matters:
	replaying every firing of the past week would end at the same place anyway.

	Returns (fire_time, timer) or None if no timer has ever fired.
	"""
	now = now if now is not None else datetime.datetime.now()
	latest = None
	for timer in timers:
		fire_time = timer.previous_occurrence(now)
		if fire_time is None:
			continue
		# On an exact tie, arming wins: leaving the system armed is the safe failure.
		if latest is None or fire_time > latest[0] or (fire_time == latest[0] and timer.do_arm):
			latest = (fire_time, timer)
	return latest
