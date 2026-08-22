"""The catalogue of settings that live in the database.

Everything here is user policy: an admin can change it from the Telegram panel and it
takes effect without a redeploy. Settings that are *not* here stay in settings.py on
purpose - see settings.example.py for what is left and why.

Each spec carries enough metadata to build a settings menu out of, so a new tunable is
one entry here rather than a new handler in telegrambot.py. STREAM_FIELDS at the bottom
does the same job for the per-stream columns, so the stream editor is generated too.
"""

import datetime
import functools
import re
from pathlib import Path

import yaml

# Kinds understood by Spec. 'seconds' is stored as a float and handed out as a
# timedelta, because that is what the callers do arithmetic with. 'dimensions' is a
# (width, height) pair.
KINDS = ('int', 'float', 'bool', 'str', 'seconds', 'int_list', 'dimensions')

TRUE_WORDS = ('1', 'true', 'yes', 'on', 'y', 'enabled')
FALSE_WORDS = ('0', 'false', 'no', 'off', 'n', 'disabled')


class Spec:
	def __init__(self, name, kind, default, category, label, description='',
				 minimum=None, maximum=None, optional=False, unset_label='(unset)',
				 parser=None):
		assert kind in KINDS, f'unknown kind {kind}'
		self.name = name
		self.kind = kind
		self.category = category
		self.label = label
		self.description = description
		self.minimum = minimum
		self.maximum = maximum
		# An optional field stores None for 'leave this alone', which is not the same as
		# its default: an unset detect_url means 'use url', not 'use no url'.
		self.optional = optional
		self.unset_label = unset_label
		# For fields whose typed form is not their stored form, like class names.
		self.parser = parser
		self.default = self.coerce(default)

	# -- conversions ---------------------------------------------------------

	def coerce(self, value):
		"""Turn anything setting-shaped into this spec's Python type, or raise.

		Used on the way in from the legacy settings.py, from the database and from the
		admin panel alike, so a bad value fails at one place instead of at the reader.
		"""
		if self.optional and (value is None or (isinstance(value, str) and not value.strip())):
			return None
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
		elif self.kind == 'dimensions':
			value = self.coerce_dimensions(value)
		return self.check_range(value)

	def coerce_dimensions(self, value):
		"""'1920x1080', '1920, 1080' or any two-item sequence -> (1920, 1080)."""
		parts = [part for part in re.split(r'[^0-9]+', value) if part] \
			if isinstance(value, str) else list(value)
		if len(parts) != 2:
			raise ValueError(f'{self.label} wants a width and a height, like 1920x1080')
		width, height = (int(part) for part in parts)
		if width <= 0 or height <= 0:
			raise ValueError(f'{self.label} must be positive')
		return (width, height)

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
		if self.kind == 'dimensions' and value is not None:
			return list(value)
		return value

	def decode(self, stored):
		"""What json.loads gave back -> Python value."""
		return self.coerce(stored)

	def parse(self, text):
		"""Parse what an admin typed into the bot."""
		text = text.strip()
		if self.optional and not text:
			return None
		if self.parser is not None:
			return self.coerce(self.parser(text))
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
		if value is None:
			return self.unset_label
		if self.kind == 'bool':
			return 'on' if value else 'off'
		if self.kind == 'seconds':
			return f'{value.total_seconds():g}s'
		if self.kind == 'int_list':
			return ', '.join(str(item) for item in value) or '(empty)'
		if self.kind == 'dimensions':
			return f'{value[0]}x{value[1]}'
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
	Spec('stationary_time', 'seconds', 5.0, 'detection', 'Stationary time',
		 'How long a motion-only object has to hold still before it stops counting. A '
		 'car that parks goes quiet this long after it stops.',
		 minimum=0.1, maximum=3600.0),
	Spec('movement_threshold', 'float', 0.15, 'detection', 'Movement threshold',
		 'How far a motion-only object has to shift to count as moving, as a fraction '
		 'of its own width or height. Raise it if box jitter keeps waking things up.',
		 minimum=0.001, maximum=10.0),
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


# -- per-stream fields -------------------------------------------------------
#
# The stream editor in the admin panel is generated from this table, the same way the
# tunables menu is generated from SPECS. Two columns are deliberately absent: `armed` is
# runtime state that the arm/disarm buttons own, and `recordcounter` is the detector's
# own counter. `detect` and `record` are absent too - they are in the schema but nothing
# reads them, so a toggle for either would do nothing.

# Class ids as coco.yaml numbers them. Shipped with the model, so the file is read from
# next to it rather than from the container path detector_config hard-codes.
COCO_NAMES_PATH = Path(__file__).parent / 'detector' / 'models' / 'cfg' / 'coco.yaml'

# What a security camera is usually pointed at, offered as toggles. Everything else in
# coco.yaml is still reachable by typing its name or id.
COMMON_CLASSES = (0, 2, 7, 3, 1, 16, 15)


@functools.lru_cache(maxsize=1)
def class_names() -> dict:
	"""{id: name} for the model's classes, empty if coco.yaml cannot be read.

	Falling back to empty rather than raising: not being able to pretty-print a class
	name is no reason for the admin panel to refuse to open.
	"""
	try:
		return {int(key): str(value)
				for key, value in yaml.safe_load(COCO_NAMES_PATH.read_text())['names'].items()}
	except Exception:
		return {}


def class_label(class_id) -> str:
	name = class_names().get(int(class_id))
	return f'{name} ({class_id})' if name else str(class_id)


def format_classes(class_ids) -> str:
	if not class_ids:
		return '(everything)'
	return ', '.join(class_names().get(int(item), str(item)) for item in class_ids)


def resolve_classes(text) -> list:
	"""'dog, 2, person' -> [0, 2, 16], raising on anything unrecognised.

	Sorted rather than kept in the order they were typed, so a list built by typing and
	one built from the toggle buttons read the same on the way back out.
	"""
	names_by_id = class_names()
	ids_by_name = {name.lower(): class_id for class_id, name in names_by_id.items()}
	resolved = []
	for part in (part.strip() for part in text.replace(',', ' ').split()):
		if not part:
			continue
		if part.isdigit():
			class_id = int(part)
			# Without coco.yaml we cannot tell a valid id from a typo, so let it through.
			if names_by_id and class_id not in names_by_id:
				raise ValueError(f'There is no class {class_id}')
		elif part.lower() in ids_by_name:
			class_id = ids_by_name[part.lower()]
		else:
			raise ValueError(f'Unknown class {part!r}')
		if class_id not in resolved:
			resolved.append(class_id)
	return sorted(resolved)


class StreamField(Spec):
	"""One editable column of a stream.

	`requires_restart` is the interesting part: stream configuration is read once when
	the stream processes are forked, so almost every change here is inert until the app
	restarts. The name is the exception - it is only ever read to label a button.
	"""

	def __init__(self, name, kind, default, label, description='', minimum=None,
				 maximum=None, optional=False, unset_label='(unset)', parser=None,
				 requires_restart=True):
		super().__init__(name, kind, default, 'stream', label, description,
						 minimum=minimum, maximum=maximum, optional=optional,
						 unset_label=unset_label, parser=parser)
		self.requires_restart = requires_restart


STREAM_FIELDS = (
	StreamField('name', 'str', '', 'Name',
				'What this camera is called in the panel and on the arm/disarm buttons.',
				optional=True, unset_label='(none)', requires_restart=False),
	StreamField('url', 'str', '', 'URL',
				'The stream the clips are recorded from, usually the camera main stream.'),
	StreamField('detect_url', 'str', '', 'Detect URL',
				'Stream the detection frames are decoded from. A camera substream costs '
				'far less to decode. Leave it unset to detect on the recording URL.',
				optional=True, unset_label='(same as URL)'),
	StreamField('dimensions', 'dimensions', (1920, 1080), 'Dimensions',
				'Frame size of the recording stream, as WxH. The detect area is written '
				'in these coordinates and is rescaled when this changes.'),
	StreamField('detect_dimensions', 'dimensions', None, 'Detect dimensions',
				'Frames are scaled to this before detection. Smaller means fewer bytes '
				'down the pipe and a smaller frame buffer.',
				optional=True, unset_label='(same as Dimensions)'),
	StreamField('detection_classes', 'int_list', [0], 'Classes',
				'Which objects raise an event just by being there. Empty means '
				'everything the model knows apart from the motion-only classes.',
				parser=resolve_classes),
	StreamField('motion_classes', 'int_list', [], 'Motion classes',
				'Objects that only raise an event while they are moving, so a parked '
				'car is ignored and the same car pulling in is not. A separate group '
				'from Classes, not a subset of it.',
				parser=resolve_classes),
	StreamField('confidence_threshold', 'float', 0.5, 'Confidence',
				'How sure the model has to be before a detection counts.',
				minimum=0.0, maximum=1.0),
	StreamField('lite_aspect_ratio', 'bool', False, 'Lite aspect ratio',
				'For cameras whose substream is anamorphic - half width, stretched back '
				'out on playback. Tags clips with a 2:1 pixel aspect instead of '
				're-encoding them.'),
)

STREAM_FIELDS_BY_NAME = {field.name: field for field in STREAM_FIELDS}
