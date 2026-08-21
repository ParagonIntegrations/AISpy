"""The catalogue of settings that live in the database.

Everything here is user policy: an admin can change it from the Telegram panel and it
takes effect without a redeploy. Settings that are *not* here stay in settings.py on
purpose - see settings.example.py for what is left and why.

Each spec carries enough metadata to build a settings menu out of, so a new tunable is
one entry here rather than a new handler in telegrambot.py.
"""

import datetime

# Kinds understood by Spec. 'seconds' is stored as a float and handed out as a
# timedelta, because that is what the callers do arithmetic with.
KINDS = ('int', 'float', 'bool', 'str', 'seconds', 'int_list')

TRUE_WORDS = ('1', 'true', 'yes', 'on', 'y', 'enabled')
FALSE_WORDS = ('0', 'false', 'no', 'off', 'n', 'disabled')


class Spec:
	def __init__(self, name, kind, default, category, label, description='',
				 minimum=None, maximum=None):
		assert kind in KINDS, f'unknown kind {kind}'
		self.name = name
		self.kind = kind
		self.category = category
		self.label = label
		self.description = description
		self.minimum = minimum
		self.maximum = maximum
		self.default = self.coerce(default)

	# -- conversions ---------------------------------------------------------

	def coerce(self, value):
		"""Turn anything setting-shaped into this spec's Python type, or raise.

		Used on the way in from the legacy settings.py, from the database and from the
		admin panel alike, so a bad value fails at one place instead of at the reader.
		"""
		if self.kind == 'int':
			value = int(value)
		elif self.kind == 'float':
			value = float(value)
		elif self.kind == 'bool':
			value = bool(value)
		elif self.kind == 'str':
			value = str(value)
		elif self.kind == 'seconds':
			if not isinstance(value, datetime.timedelta):
				value = datetime.timedelta(seconds=float(value))
		elif self.kind == 'int_list':
			value = [int(item) for item in value]
		return self.check_range(value)

	def check_range(self, value):
		if self.minimum is None and self.maximum is None:
			return value
		measure = value.total_seconds() if self.kind == 'seconds' else value
		if self.minimum is not None and measure < self.minimum:
			raise ValueError(f'{self.label} must be at least {self.minimum}')
		if self.maximum is not None and measure > self.maximum:
			raise ValueError(f'{self.label} must be at most {self.maximum}')
		return value

	def encode(self, value):
		"""Python value -> something json.dumps can store."""
		if self.kind == 'seconds':
			return value.total_seconds()
		if self.kind == 'bool':
			return bool(value)
		return value

	def decode(self, stored):
		"""What json.loads gave back -> Python value."""
		return self.coerce(stored)

	def parse(self, text):
		"""Parse what an admin typed into the bot."""
		text = text.strip()
		if self.kind == 'bool':
			lowered = text.lower()
			if lowered in TRUE_WORDS:
				return True
			if lowered in FALSE_WORDS:
				return False
			raise ValueError(f'{self.label} wants on or off, not {text!r}')
		if self.kind == 'int_list':
			return self.coerce([part for part in text.replace(',', ' ').split()])
		return self.coerce(text)

	def format(self, value) -> str:
		"""Render a value for a Telegram message."""
		if self.kind == 'bool':
			return 'on' if value else 'off'
		if self.kind == 'seconds':
			return f'{value.total_seconds():g}s'
		if self.kind == 'int_list':
			return ', '.join(str(item) for item in value) or '(empty)'
		if self.kind == 'str':
			return value or '(unset)'
		return str(value)


SPECS = (
	# -- detection -----------------------------------------------------------
	Spec('detect_fps', 'float', 5.0, 'detection', 'Detect FPS',
		 'Frames per second pulled from each camera for detection. Only affects decode '
		 'cost; the detector itself runs on the detection interval.',
		 minimum=0.1, maximum=30.0),
	Spec('detect_buffer_frames', 'int', 8, 'detection', 'Detect buffer frames',
		 'Recent frames kept in shared memory per stream. The detector only ever reads '
		 'the newest one, so this is a jitter cushion, not a pre-record buffer.',
		 minimum=1, maximum=120),
	Spec('check_detection_time', 'seconds', 1.0, 'detection', 'Detection interval',
		 'How often every stream is checked for objects.',
		 minimum=0.1, maximum=600.0),
	Spec('detections_for_event', 'int', 3, 'detection', 'Detections for event',
		 'Consecutive detections needed before a stream starts recording and the alarm '
		 'countdown begins.',
		 minimum=1, maximum=100),
	Spec('avg_inference_time', 'float', 0.1, 'detection', 'Seed inference time',
		 'Starting value for the rolling average of inference time, in seconds.',
		 minimum=0.0, maximum=60.0),

	# -- recording -----------------------------------------------------------
	Spec('pre_record_time', 'seconds', 10.0, 'recording', 'Pre-record time',
		 'How much footage from before the trigger is kept in each clip. Segments are '
		 'held on disk for at least this long.',
		 minimum=0.0, maximum=3600.0),
	Spec('max_clip_length', 'seconds', 300.0, 'recording', 'Max clip length',
		 'A running event is cut into a new clip once it reaches this length.',
		 minimum=10.0, maximum=86400.0),
	Spec('segment_time', 'int', 2, 'recording', 'Segment length',
		 'Requested length of each cached segment, in seconds. Copy-mode ffmpeg can only '
		 'cut on a keyframe, so a segment lasts this long or one camera GOP, whichever '
		 'is longer.',
		 minimum=1, maximum=60),
	Spec('segment_retention_margin', 'seconds', 30.0, 'recording', 'Segment retention margin',
		 'Extra time cached segments are kept beyond the pre-record window, covering the '
		 'gap between the detector raising recordflag and the collector noticing.',
		 minimum=0.0, maximum=3600.0),
	Spec('snapshot_jpeg_quality', 'int', 90, 'recording', 'Snapshot JPEG quality',
		 'Quality of the snapshots sent to Telegram.',
		 minimum=1, maximum=100),

	# -- alarm ---------------------------------------------------------------
	Spec('alarm_relay_ip', 'str', '', 'alarm', 'Alarm relay IP',
		 'Address of the Tasmota relay that fires the siren. Empty disables it.'),
	Spec('alarm_countdown', 'seconds', 30.0, 'alarm', 'Alarm countdown',
		 'How long alarm users have to cancel before the siren fires.',
		 minimum=0.0, maximum=3600.0),

	# -- system --------------------------------------------------------------
	Spec('timers_enabled', 'bool', True, 'system', 'Auto arm/disarm timers',
		 'Master switch for the whole auto arm/disarm schedule.'),
)

SPECS_BY_NAME = {spec.name: spec for spec in SPECS}

# Menu order for the admin panel.
CATEGORIES = ('detection', 'recording', 'alarm', 'system')

CATEGORY_LABELS = {
	'detection': 'Detection',
	'recording': 'Recording',
	'alarm': 'Alarm',
	'system': 'System',
}


def specs_in(category) -> list:
	return [spec for spec in SPECS if spec.category == category]
