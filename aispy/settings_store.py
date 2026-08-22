"""SQLite-backed settings, shared by every process.

Why this exists: the admin panel used to edit settings by appending to
Settings.telegram_adminlist. Telegrambot is an mp.Process, so that mutated the bot's
own copy of the module and nothing else - the parent, the snapshot process and the bot
itself after its next supervisor restart all carried on with the old list. Settings that
an admin can change have to live somewhere all the processes can see, which means the
database.

Each process gets its own connection (sqlite3 connections are not fork-safe, though they
are shared across the threads of one process) and its own read cache. The cache is dropped when PRAGMA data_version reports that another connection
has committed, so a change made in the bot reaches the detector without a restart. What
is *not* live is anything consumed once at startup - stream URLs and geometry are read
when the stream processes are forked, so editing those still needs a restart.

Bootstrap settings - the database path itself, logging, and the API tokens - cannot live
here and stay in settings.py. See settings.example.py.
"""

import json
import logging
import os
import sqlite3
import time

import numpy as np

from autoarm import AutoArm
from settings_spec import SPECS, SPECS_BY_NAME, STREAM_FIELDS, STREAM_FIELDS_BY_NAME

# Logging by name rather than by importing utils: utils builds the logger from settings.py
# at import time and imports back into everything, and this module is a dependency of it.
logger = logging.getLogger('Main Logger')

SCHEMA_VERSION = 4

ROLE_ADMIN = 'admin'
ROLE_USER = 'user'
ROLE_ALARM = 'alarm'
ROLE_NOTIFY_ARM_DISARM = 'notify_arm_disarm'

ROLES = (ROLE_ADMIN, ROLE_USER, ROLE_ALARM, ROLE_NOTIFY_ARM_DISARM)

ROLE_LABELS = {
	ROLE_ADMIN: 'Admin',
	ROLE_USER: 'User',
	ROLE_ALARM: 'Alarm User',
	ROLE_NOTIFY_ARM_DISARM: 'Arm/Disarm Notification',
}

# Where each role's members used to be listed in settings.py.
LEGACY_ROLE_ATTRS = {
	ROLE_ADMIN: 'telegram_adminlist',
	ROLE_USER: 'telegram_userlist',
	ROLE_ALARM: 'telegram_alarmlist',
	ROLE_NOTIFY_ARM_DISARM: 'telegram_notify_arm_disarm_list',
}

# Stream columns that are plain scalars, stored as themselves.
STREAM_SCALAR_COLUMNS = ('name', 'url', 'detect_url', 'confidence_threshold', 'detect',
						 'record', 'lite_aspect_ratio', 'recordcounter')
# Stream columns held as JSON because they are tuples or lists.
STREAM_JSON_COLUMNS = ('dimensions', 'detect_dimensions', 'detection_classes',
					   'motion_classes', 'detectarea')

STREAM_COLUMNS = STREAM_SCALAR_COLUMNS + STREAM_JSON_COLUMNS

# The two class groups, each pointing at the one it must not overlap with.
CLASS_GROUPS = {'detection_classes': 'motion_classes',
				'motion_classes': 'detection_classes'}

_MISSING = object()


class SettingsStore:
	"""Read/write access to everything an admin is allowed to change.

	Safe to construct before a fork: the connection is opened lazily and reopened if the
	pid changes, so children never share the parent's handle.
	"""

	def __init__(self, dbfile, refresh_interval=2.0):
		self.dbfile = str(dbfile)
		# How stale a cached read may be, in seconds. Only bounds how often the cheap
		# data_version check runs; the reload itself only happens on a real change.
		self.refresh_interval = refresh_interval
		self._conn = None
		self._pid = None
		self._data_version = None
		self._checked_at = None
		self._values = {}
		self._roles = {role: [] for role in ROLES}
		self._timer_rows = []
		# AutoArm instances have to survive across polls: check_action() only fires once
		# now has passed next_time, so rebuilding them every second would mean never
		# firing at all. Keyed by config, so an edited timer is rebuilt and an untouched
		# one is not.
		self._timers = {}

	# -- connection ----------------------------------------------------------

	@property
	def conn(self) -> sqlite3.Connection:
		if self._conn is None or self._pid != os.getpid():
			# Deliberately not closing the inherited handle: it belongs to the parent,
			# which is still using it.
			self._connect()
		return self._conn

	def __getstate__(self):
		# A sqlite3 connection cannot cross a process boundary. Dropping it here means the
		# store survives being pickled into a spawned process, which then reopens lazily.
		state = self.__dict__.copy()
		state['_conn'] = None
		state['_pid'] = None
		return state

	def _connect(self) -> None:
		# isolation_level=None keeps us in autocommit, so reads never sit inside an open
		# transaction. A stale read snapshot would hide other processes' writes from
		# data_version, which is the whole propagation mechanism.
		#
		# check_same_thread=False because a process is not a thread: Telegrambot runs its
		# local logic on a daemon thread and the Application on the main one, and both
		# read settings. Whichever got there first would own the connection and the other
		# would raise ProgrammingError on every access - which, with the read cache
		# absorbing anything inside the refresh interval, showed up as handlers failing
		# intermittently rather than as an obvious break. Sharing one connection across
		# the threads of a process is safe: sqlite3.threadsafety is 3 here (SQLite built
		# serialized), so it does its own locking, and the caches below are only ever
		# touched by single dict and list operations, which the GIL already makes atomic.
		self._conn = sqlite3.connect(self.dbfile, timeout=30, isolation_level=None,
									 check_same_thread=False)
		self._conn.row_factory = sqlite3.Row
		# WAL so the bot can write settings while the detector is reading them.
		self._conn.execute('PRAGMA journal_mode=WAL')
		self._conn.execute('PRAGMA busy_timeout=30000')
		self._pid = os.getpid()
		self._data_version = None
		self._checked_at = None

	# -- schema --------------------------------------------------------------

	def prepare(self) -> None:
		"""Create or upgrade the schema and seed it from settings.py. Parent only."""
		conn = self.conn
		fresh = conn.execute(
			"SELECT name FROM sqlite_master WHERE type='table' AND name='streaminfos'"
		).fetchone() is None
		conn.execute('BEGIN IMMEDIATE')
		try:
			self._create_tables()
			version = self._schema_version()
			if version < 2:
				self._upgrade_to_v2(seed_streams=not fresh)
			if version < 3:
				self._upgrade_to_v3()
			if version < 4:
				self._upgrade_to_v4()
			# The master arm/disarm switch has to exist before anything can read it. An
			# install seeded from settings.py always had it; one configured entirely from
			# the panel would not, and FractalApp goes straight to streaminfos[0].
			conn.execute('INSERT OR IGNORE INTO streaminfos(streamid, armed) VALUES(0, 1)')
			self._set_meta('schema_version', SCHEMA_VERSION)
			conn.execute('COMMIT')
		except Exception:
			conn.execute('ROLLBACK')
			raise
		self.reload()

	def _create_tables(self) -> None:
		conn = self.conn
		conn.execute('''
			CREATE TABLE IF NOT EXISTS schema_meta(
				key TEXT PRIMARY KEY NOT NULL,
				value TEXT NOT NULL
			)
		''')
		conn.execute('''
			CREATE TABLE IF NOT EXISTS settings(
				name TEXT PRIMARY KEY NOT NULL,
				value TEXT NOT NULL
			)
		''')
		conn.execute('''
			CREATE TABLE IF NOT EXISTS telegram_roles(
				user_id INTEGER NOT NULL,
				role TEXT NOT NULL,
				PRIMARY KEY (user_id, role)
			)
		''')
		conn.execute('''
			CREATE TABLE IF NOT EXISTS autoarm_timers(
				id INTEGER PRIMARY KEY,
				hour INTEGER NOT NULL,
				minute INTEGER NOT NULL DEFAULT 0,
				repeat_every_days INTEGER NOT NULL DEFAULT 1,
				active_days TEXT NOT NULL DEFAULT '[0, 1, 2, 3, 4, 5, 6]',
				do_arm INTEGER NOT NULL DEFAULT 1,
				enabled INTEGER NOT NULL DEFAULT 1
			)
		''')
		# Matches what db_driver used to create, so an existing database is left alone
		# and only picks up the ALTER TABLEs below.
		conn.execute('''
			CREATE TABLE IF NOT EXISTS streaminfos(
				id INTEGER PRIMARY KEY NOT NULL,
				streamid INTEGER NOT NULL,
				armed INTEGER,
				url TEXT,
				dimensions TEXT,
				detect INTEGER,
				detection_classes TEXT,
				confidence_threshold REAL,
				record INTEGER,
				detectarea TEXT,
				recordcounter INTEGER
			)
		''')

	def _schema_version(self) -> int:
		row = self.conn.execute(
			'SELECT value FROM schema_meta WHERE key = ?', ('schema_version',)).fetchone()
		return int(row['value']) if row else 1

	def _get_meta(self, key, default=None):
		row = self.conn.execute(
			'SELECT value FROM schema_meta WHERE key = ?', (key,)).fetchone()
		return default if row is None else json.loads(row['value'])

	def _set_meta(self, key, value) -> None:
		self.conn.execute('''
			INSERT INTO schema_meta(key, value) VALUES(?, ?)
			ON CONFLICT(key) DO UPDATE SET value = excluded.value
		''', (key, json.dumps(value)))

	def _upgrade_to_v2(self, seed_streams) -> None:
		"""Add the columns the stream panel needs and copy settings.py into the database.

		`seed_streams` is False for a database being created from scratch, where the
		stream rows are inserted whole rather than back-filled.
		"""
		conn = self.conn
		existing = {row['name'] for row in conn.execute('PRAGMA table_info(streaminfos)')}
		for column, decl in (('detect_url', 'TEXT'),
							 ('detect_dimensions', 'TEXT'),
							 ('lite_aspect_ratio', 'INTEGER')):
			if column not in existing:
				conn.execute(f'ALTER TABLE streaminfos ADD COLUMN {column} {decl}')
		# db_driver never enforced this, and an upsert by streamid needs it. Duplicates
		# would only come from a database that was seeded twice; keep the first row.
		conn.execute('''
			DELETE FROM streaminfos WHERE id NOT IN (
				SELECT MIN(id) FROM streaminfos GROUP BY streamid
			)
		''')
		conn.execute(
			'CREATE UNIQUE INDEX IF NOT EXISTS streaminfos_streamid ON streaminfos(streamid)')
		self._migrate_from_file()

	def _upgrade_to_v3(self) -> None:
		"""Give streams a name, so the panel can list cameras rather than numbers."""
		existing = {row['name'] for row in self.conn.execute('PRAGMA table_info(streaminfos)')}
		if 'name' not in existing:
			self.conn.execute('ALTER TABLE streaminfos ADD COLUMN name TEXT')

	def _upgrade_to_v4(self) -> None:
		"""Split the detection classes into ones that count on sight and ones that
		only count while moving.

		Left empty on an existing stream, which is the same behaviour it had before:
		every class it detects still counts on sight.
		"""
		existing = {row['name'] for row in self.conn.execute('PRAGMA table_info(streaminfos)')}
		if 'motion_classes' not in existing:
			self.conn.execute('ALTER TABLE streaminfos ADD COLUMN motion_classes TEXT')

	# -- migration off settings.py -------------------------------------------

	def _legacy_sources(self) -> list:
		"""UserSettings then Settings, matching how optional_setting resolved names."""
		try:
			import settings as legacy
		except Exception:
			logger.warning('No settings.py to migrate from, starting with defaults')
			return []
		return [source for source in (getattr(legacy, 'UserSettings', None),
									  getattr(legacy, 'Settings', None))
				if source is not None]

	@staticmethod
	def _legacy_get(sources, name):
		for source in sources:
			if hasattr(source, name):
				return getattr(source, name)
		return _MISSING

	def _migrate_from_file(self) -> None:
		sources = self._legacy_sources()
		if not sources:
			return
		logger.info('Migrating settings from settings.py into the database')
		self._migrate_values(sources)
		self._migrate_roles(sources)
		self._migrate_timers(sources)
		self._migrate_streams(sources)

	def _migrate_values(self, sources) -> None:
		for spec in SPECS:
			value = self._legacy_get(sources, spec.name)
			if value is _MISSING:
				# Left absent rather than written as the default, so the default stays
				# free to change in settings_spec.py.
				continue
			try:
				self._write_value(spec, spec.coerce(value))
			except Exception:
				logger.exception(f'Could not migrate setting {spec.name}, using the default')

	def _migrate_roles(self, sources) -> None:
		for role, attr in LEGACY_ROLE_ATTRS.items():
			members = self._legacy_get(sources, attr)
			if members is _MISSING:
				continue
			for user_id in members:
				self.conn.execute(
					'INSERT OR IGNORE INTO telegram_roles(user_id, role) VALUES(?, ?)',
					(int(user_id), role))

	def _migrate_timers(self, sources) -> None:
		timers = self._legacy_get(sources, 'auto_arm_disarm_list')
		if timers is _MISSING:
			return
		for timer in timers:
			self.conn.execute('''
				INSERT INTO autoarm_timers(
					hour, minute, repeat_every_days, active_days, do_arm, enabled
				) VALUES(?, ?, ?, ?, ?, 1)
			''', (int(timer.hour), int(timer.minute), int(timer.repeat_every_days),
				  json.dumps([int(day) for day in timer.active_days]),
				  1 if timer.do_arm else 0))

	def _migrate_streams(self, sources) -> None:
		streams = self._legacy_get(sources, 'streaminfo')
		if streams is _MISSING:
			return
		for streamid, streaminfo in streams.items():
			# armed is the one field the database was already authoritative for, so an
			# existing row keeps whatever state it was last saved in.
			self.conn.execute('''
				INSERT OR IGNORE INTO streaminfos(streamid, armed) VALUES(?, ?)
			''', (int(streamid), int(streaminfo.get('armed', 0))))
			if int(streamid) == 0:
				continue
			self._write_stream(int(streamid), streaminfo)

	# -- cache ---------------------------------------------------------------

	def _maybe_refresh(self) -> None:
		now = time.monotonic()
		if self._checked_at is not None and now - self._checked_at < self.refresh_interval:
			return
		self._checked_at = now
		# data_version only moves when a *different* connection commits, which is exactly
		# the case we cannot otherwise see. Our own writes update the cache in place.
		version = self.conn.execute('PRAGMA data_version').fetchone()[0]
		if version == self._data_version:
			return
		self._data_version = version
		self._load()

	def reload(self) -> None:
		"""Drop the cache. Called after this process writes, and after prepare()."""
		self._checked_at = time.monotonic()
		self._data_version = self.conn.execute('PRAGMA data_version').fetchone()[0]
		self._load()

	def _load(self) -> None:
		values = {}
		for row in self.conn.execute('SELECT name, value FROM settings'):
			spec = SPECS_BY_NAME.get(row['name'])
			if spec is None:
				continue
			try:
				values[row['name']] = spec.decode(json.loads(row['value']))
			except Exception:
				logger.warning(f'Ignoring unusable stored value for {row["name"]}')
		self._values = values

		roles = {role: [] for role in ROLES}
		for row in self.conn.execute(
				'SELECT user_id, role FROM telegram_roles ORDER BY rowid'):
			roles.setdefault(row['role'], []).append(row['user_id'])
		self._roles = roles

		self._timer_rows = [dict(row) for row in self.conn.execute(
			'SELECT * FROM autoarm_timers ORDER BY hour, minute, id')]

	# -- scalar settings -----------------------------------------------------

	def get(self, name):
		"""The current value of a setting, falling back to its spec default."""
		spec = SPECS_BY_NAME[name]
		self._maybe_refresh()
		return self._values.get(name, spec.default)

	def set(self, name, value):
		"""Store a setting. Accepts the Python type or anything coercible to it."""
		spec = SPECS_BY_NAME[name]
		value = spec.coerce(value)
		self._write_value(spec, value)
		self._values[name] = value
		return value

	def set_from_text(self, name, text):
		"""Store a setting from what an admin typed."""
		return self.set(name, SPECS_BY_NAME[name].parse(text))

	def _write_value(self, spec, value) -> None:
		self.conn.execute('''
			INSERT INTO settings(name, value) VALUES(?, ?)
			ON CONFLICT(name) DO UPDATE SET value = excluded.value
		''', (spec.name, json.dumps(spec.encode(value))))

	# -- telegram roles ------------------------------------------------------

	def superadmins(self) -> list:
		"""The file-only escape hatch.

		Kept out of the database on purpose: an admin can remove every admin from the
		panel, and recovering from that should not need hand-edited SQL. It is also what
		stops the bot being able to grant itself more than it was given.
		"""
		from utils import optional_setting
		return [int(user_id) for user_id in optional_setting('telegram_superadminlist', [])]

	def members(self, role) -> list:
		"""Everyone holding a role, in the order they were added."""
		self._maybe_refresh()
		return list(self._roles.get(role, []))

	def admins(self) -> list:
		superadmins = self.superadmins()
		return superadmins + [user_id for user_id in self.members(ROLE_ADMIN)
							  if user_id not in superadmins]

	def is_admin(self, user_id) -> bool:
		return user_id in self.admins()

	def is_user(self, user_id) -> bool:
		return self.is_admin(user_id) or user_id in self.members(ROLE_USER)

	def is_alarm_user(self, user_id) -> bool:
		return user_id in self.members(ROLE_ALARM)

	def alarm_chat_ids(self) -> list:
		return self.members(ROLE_ALARM)

	def arm_disarm_chat_ids(self) -> list:
		return self.members(ROLE_NOTIFY_ARM_DISARM)

	def add_member(self, role, user_id) -> None:
		user_id = int(user_id)
		self.conn.execute('INSERT OR IGNORE INTO telegram_roles(user_id, role) VALUES(?, ?)',
						  (user_id, role))
		if user_id not in self._roles.setdefault(role, []):
			self._roles[role].append(user_id)

	def remove_member(self, role, user_id) -> None:
		user_id = int(user_id)
		self.conn.execute('DELETE FROM telegram_roles WHERE user_id = ? AND role = ?',
						  (user_id, role))
		if user_id in self._roles.get(role, []):
			self._roles[role].remove(user_id)

	# -- auto arm/disarm timers ----------------------------------------------

	def timer_rows(self) -> list:
		"""Every timer as stored, enabled or not. For the admin panel."""
		self._maybe_refresh()
		return [dict(row) for row in self._timer_rows]

	def timer_row(self, timer_id):
		for row in self.timer_rows():
			if row['id'] == timer_id:
				return row
		return None

	def autoarm_timers(self) -> list:
		"""The enabled timers as live AutoArm objects.

		Instances are cached against their configuration so that repeated polling gets
		the same objects back: AutoArm.check_action() fires on next_time having passed,
		and a freshly built timer never has one in the past.
		"""
		self._maybe_refresh()
		live = {}
		for row in self._timer_rows:
			if not row['enabled']:
				continue
			key = (row['id'], row['hour'], row['minute'], row['repeat_every_days'],
				   row['active_days'], row['do_arm'])
			timer = self._timers.get(key)
			if timer is None:
				timer = AutoArm(hour=row['hour'], minute=row['minute'],
								repeat_every_days=row['repeat_every_days'],
								active_days=json.loads(row['active_days']),
								do_arm=bool(row['do_arm']))
			live[key] = timer
		self._timers = live
		return list(live.values())

	@staticmethod
	def check_time(hour, minute):
		if not 0 <= int(hour) <= 23 or not 0 <= int(minute) <= 59:
			raise ValueError('Time must be between 00:00 and 23:59')
		return int(hour), int(minute)

	@staticmethod
	def check_days(active_days) -> list:
		active_days = sorted({int(day) for day in active_days})
		# Told apart because the panel reaches the first one by turning the last day off,
		# where 'must be 0 to 6' says nothing about what went wrong.
		if not active_days:
			raise ValueError('A timer needs at least one day')
		if any(day not in range(7) for day in active_days):
			raise ValueError('Days must be 0 (Monday) to 6 (Sunday)')
		return active_days

	def add_timer(self, hour, minute=0, repeat_every_days=1, active_days=None,
				  do_arm=True, enabled=True) -> int:
		active_days = self.check_days(range(7) if active_days is None else active_days)
		hour, minute = self.check_time(hour, minute)
		cursor = self.conn.execute('''
			INSERT INTO autoarm_timers(
				hour, minute, repeat_every_days, active_days, do_arm, enabled
			) VALUES(?, ?, ?, ?, ?, ?)
		''', (hour, minute, max(1, int(repeat_every_days)),
			  json.dumps(active_days), 1 if do_arm else 0, 1 if enabled else 0))
		self.reload()
		return cursor.lastrowid

	def update_timer(self, timer_id, **fields) -> None:
		"""Change one or more fields of an existing timer.

		Editing rather than delete-and-recreate keeps the row id, so whichever message
		the admin is looking at still addresses the same timer. The AutoArm instance is
		rebuilt on the next poll because the cache key covers every field here, and a
		rebuilt timer takes its next_time from now - so moving a timer earlier in the day
		does not make it fire retroactively.
		"""
		unknown = set(fields) - {'hour', 'minute', 'repeat_every_days', 'active_days',
								 'do_arm', 'enabled'}
		if unknown:
			raise ValueError(f'Not a timer field: {", ".join(sorted(unknown))}')
		if not fields:
			return
		if 'hour' in fields or 'minute' in fields:
			row = self.timer_row(timer_id)
			if row is None:
				raise ValueError('That timer no longer exists')
			fields['hour'], fields['minute'] = self.check_time(
				fields.get('hour', row['hour']), fields.get('minute', row['minute']))
		if 'active_days' in fields:
			fields['active_days'] = json.dumps(self.check_days(fields['active_days']))
		if 'repeat_every_days' in fields:
			fields['repeat_every_days'] = max(1, int(fields['repeat_every_days']))
		if 'do_arm' in fields:
			fields['do_arm'] = 1 if fields['do_arm'] else 0
		if 'enabled' in fields:
			fields['enabled'] = 1 if fields['enabled'] else 0
		assignments = ', '.join(f'{column} = ?' for column in fields)
		self.conn.execute(f'UPDATE autoarm_timers SET {assignments} WHERE id = ?',
						  list(fields.values()) + [int(timer_id)])
		self.reload()

	def remove_timer(self, timer_id) -> None:
		self.conn.execute('DELETE FROM autoarm_timers WHERE id = ?', (int(timer_id),))
		self.reload()

	def set_timer_enabled(self, timer_id, enabled) -> None:
		self.update_timer(timer_id, enabled=enabled)

	# -- streams -------------------------------------------------------------

	def load_streams(self) -> dict:
		"""Every stream as the dict the rest of the app passes around.

		Read once at startup and forked into the stream processes, so a change here needs
		a restart to take effect - unlike the scalar settings above.
		"""
		streams = {}
		for row in self.conn.execute('SELECT * FROM streaminfos ORDER BY streamid'):
			streams[row['streamid']] = self._stream_from_row(row)
		if not streams:
			logger.warning('No streams configured')
		return streams

	def _stream_from_row(self, row) -> dict:
		streaminfo = {'armed': int(row['armed'] or 0)}
		# Stream 0 is the master arm/disarm pseudo-stream; it has no camera behind it.
		if row['streamid'] == 0:
			return streaminfo
		for column in STREAM_SCALAR_COLUMNS:
			if row[column] is not None:
				streaminfo[column] = row[column]
		for column in STREAM_JSON_COLUMNS:
			if row[column] is None:
				continue
			value = json.loads(row[column])
			# The detect area is indexed and scaled as an array of points.
			streaminfo[column] = np.array(value) if column == 'detectarea' else value
		streaminfo['lite_aspect_ratio'] = bool(streaminfo.get('lite_aspect_ratio'))
		streaminfo.setdefault('recordcounter', 0)
		return streaminfo

	def _write_stream(self, streamid, streaminfo) -> None:
		"""Write the configuration columns of one stream, leaving `armed` alone."""
		assignments = []
		params = []
		for column in STREAM_COLUMNS:
			if column not in streaminfo:
				continue
			assignments.append(f'{column} = ?')
			params.append(self._encode_stream_field(column, streaminfo[column]))
		if not assignments:
			return
		params.append(int(streamid))
		self.conn.execute(
			f'UPDATE streaminfos SET {", ".join(assignments)} WHERE streamid = ?', params)

	@staticmethod
	def _encode_stream_field(column, value):
		# An optional field that is not set is a NULL, not an encoded None: detect_url
		# unset means 'use url', and json.dumps would turn that into the string 'null'.
		if value is None:
			return None
		if column in STREAM_JSON_COLUMNS:
			if isinstance(value, np.ndarray):
				value = value.tolist()
			return json.dumps(list(value))
		if column == 'lite_aspect_ratio':
			return 1 if value else 0
		return value

	def save_stream(self, streamid, streaminfo) -> None:
		self.conn.execute('INSERT OR IGNORE INTO streaminfos(streamid, armed) VALUES(?, ?)',
						  (int(streamid), int(streaminfo.get('armed', 0))))
		self._write_stream(int(streamid), streaminfo)

	def next_streamid(self) -> int:
		row = self.conn.execute('SELECT MAX(streamid) AS top FROM streaminfos').fetchone()
		return int(row['top'] or 0) + 1

	# -- streams, as the admin panel sees them -------------------------------

	def stream_rows(self) -> list:
		"""Every real stream, as dicts of decoded field values. Stream 0 is not one.

		Read straight from the connection rather than from the cache the scalar settings
		use: this is only ever called while drawing a menu, and being a poll behind would
		show an admin their own edit not having happened.
		"""
		return [self._stream_fields(row) for row in self.conn.execute(
			'SELECT * FROM streaminfos WHERE streamid != 0 ORDER BY streamid')]

	def stream_row(self, streamid):
		row = self.conn.execute('SELECT * FROM streaminfos WHERE streamid = ? AND streamid != 0',
								(int(streamid),)).fetchone()
		return None if row is None else self._stream_fields(row)

	def _stream_fields(self, row) -> dict:
		"""One row as {streamid, armed, <every field in STREAM_FIELDS>}.

		A column that has never been written, or that holds something the field can no
		longer make sense of, comes back as the field default rather than raising: a bad
		value in one column should not make the stream uneditable.
		"""
		fields = {'streamid': row['streamid'], 'armed': int(row['armed'] or 0)}
		for field in STREAM_FIELDS:
			stored = row[field.name]
			if stored is None:
				fields[field.name] = field.default
				continue
			if field.name in STREAM_JSON_COLUMNS:
				stored = json.loads(stored)
			try:
				fields[field.name] = field.coerce(stored)
			except Exception:
				logger.warning(
					f'Stream {row["streamid"]} has an unusable {field.name}, showing the default')
				fields[field.name] = field.default
		return fields

	def stream_name(self, streamid) -> str:
		"""What to call a stream on a button. Cheap enough to call per keyboard."""
		if int(streamid) == 0:
			return 'All'
		row = self.conn.execute('SELECT name FROM streaminfos WHERE streamid = ?',
								(int(streamid),)).fetchone()
		name = (row['name'] or '').strip() if row is not None else ''
		return name or f'Stream {streamid}'

	def add_stream(self, url, dimensions=None, name=None) -> int:
		"""Create a stream from a URL, with every other field at its default.

		The detect area is the whole frame, which is the only sensible thing to write
		without being able to see the picture. The panel never touches it again, so a
		polygon put there by hand survives everything the admin can do from Telegram.
		"""
		field = STREAM_FIELDS_BY_NAME['url']
		url = field.coerce(url)
		if not url:
			raise ValueError('A stream needs a URL')
		dimensions = STREAM_FIELDS_BY_NAME['dimensions'].coerce(
			dimensions if dimensions is not None else STREAM_FIELDS_BY_NAME['dimensions'].default)
		streamid = self.next_streamid()
		# Armed by default: the master switch still gates it, so a new camera comes up in
		# whatever state the system as a whole is in rather than silently watching nothing.
		self.conn.execute('INSERT INTO streaminfos(streamid, armed) VALUES(?, 1)', (streamid,))
		values = {each.name: each.default for each in STREAM_FIELDS}
		values.update({'url': url, 'dimensions': dimensions,
					   'name': STREAM_FIELDS_BY_NAME['name'].coerce(name)})
		values['detectarea'] = self.full_frame(dimensions)
		self._write_stream(streamid, values)
		self.mark_streams_dirty()
		return streamid

	@staticmethod
	def full_frame(dimensions) -> list:
		width, height = dimensions
		return [[0, 0], [width, 0], [width, height], [0, height]]

	def set_stream_field(self, streamid, name, value):
		"""Write one field of one stream, and say whether that needs a restart."""
		field = STREAM_FIELDS_BY_NAME[name]
		value = field.coerce(value)
		if name == 'url' and not value:
			raise ValueError('A stream needs a URL')
		row = self.stream_row(streamid)
		if row is None:
			raise ValueError('That stream no longer exists')
		changes = {name: value}
		if name == 'dimensions' and tuple(value) != tuple(row['dimensions']):
			changes['detectarea'] = self.rescaled_detectarea(streamid, row['dimensions'], value)
		# A class belongs to one group or the other, never both: a class that counts on
		# sight cannot also be one that has to be moving first. Enforced here rather than
		# in the panel so it holds for a list that was typed as well as one toggled.
		if name in CLASS_GROUPS:
			other = CLASS_GROUPS[name]
			overlap = set(value) & set(row[other])
			if overlap:
				changes[other] = [class_id for class_id in row[other] if class_id not in overlap]
		self._write_stream(int(streamid), changes)
		if field.requires_restart:
			self.mark_streams_dirty()
		return value

	def rescaled_detectarea(self, streamid, old_dimensions, new_dimensions) -> list:
		"""Move the detect area into the new coordinate space.

		The area is written against `dimensions`, so leaving it alone when those change
		would silently point it at the wrong part of the picture. A whole-frame area
		stays a whole-frame area and a hand-drawn one keeps its shape, which is the same
		thing app.init_detect_geometry does when scaling into detect_dimensions.
		"""
		row = self.conn.execute('SELECT detectarea FROM streaminfos WHERE streamid = ?',
								(int(streamid),)).fetchone()
		if row is None or row['detectarea'] is None:
			return self.full_frame(new_dimensions)
		try:
			points = np.array(json.loads(row['detectarea']), dtype=float)
			scale = np.array([new_dimensions[0] / old_dimensions[0],
							  new_dimensions[1] / old_dimensions[1]])
			return (points * scale).astype(int).tolist()
		except Exception:
			logger.exception(f'Could not rescale the detect area of stream {streamid}')
			return self.full_frame(new_dimensions)

	def remove_stream(self, streamid) -> None:
		if int(streamid) == 0:
			raise ValueError('Stream 0 is the master arm/disarm switch and cannot be removed')
		self.conn.execute('DELETE FROM streaminfos WHERE streamid = ?', (int(streamid),))
		self.mark_streams_dirty()

	# -- pending restart -----------------------------------------------------
	#
	# Stream configuration is read once, when FractalApp forks the stream processes, so
	# an edit made in the panel is inert until the app comes back up. The flag is set by
	# every write above and cleared once those processes have been built from the new
	# rows, which is the only moment the database and what is running are known to agree.

	def streams_dirty(self) -> bool:
		return bool(self._get_meta('streams_dirty', False))

	def mark_streams_dirty(self) -> None:
		self._set_meta('streams_dirty', True)

	def clear_streams_dirty(self) -> None:
		self._set_meta('streams_dirty', False)

	# -- armed state ---------------------------------------------------------

	def save_armed_state(self, statedict) -> None:
		"""Persist the arm/disarm flags. Hot path: called on every button press."""
		logger.info('Saving state to db')
		for streamid, armed in statedict.items():
			self.conn.execute('UPDATE streaminfos SET armed = ? WHERE streamid = ?',
							  (int(armed), int(streamid)))


_store = None


def get_store(dbfile=None) -> SettingsStore:
	"""The per-process store singleton.

	`dbfile` is only needed by whoever gets there first; everyone else, including forked
	children, picks up the path that was already set.
	"""
	global _store
	if _store is None:
		if dbfile is None:
			from settings import Settings
			dbfile = Settings.db_file
		_store = SettingsStore(dbfile)
	return _store
