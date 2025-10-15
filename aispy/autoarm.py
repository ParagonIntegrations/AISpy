import datetime


class AutoArm:
	def __init__(self, hour, minute=0, repeat_every_days=1, active_days=None, do_arm=True):
		self.hour = hour
		self.minute = minute
		self.repeat_every_days = repeat_every_days
		self.active_days = active_days if active_days is not None else [0, 1, 2, 3, 4, 5, 6]
		self.do_arm = do_arm
		self.next_time = datetime.datetime.now().replace(hour=hour,minute=minute ,second=0, microsecond=0)
		self.check_action()

	def check_action(self):
		if self.next_time < datetime.datetime.now():
			self.next_time = self.next_time + datetime.timedelta(days=self.repeat_every_days)
			return self.do_arm
		if self.next_time.weekday() not in self.active_days:
			self.next_time = self.next_time + datetime.timedelta(days=self.repeat_every_days)
		return None

	def __str__(self):
		arm_str = 'Arm' if self.do_arm else 'Disarm'
		return f'{arm_str} at {self.hour:02d}:{self.minute:02d} on days: {self.active_days}, repeating every {self.repeat_every_days} days'