"""Per-device settings. Copy to aispy/settings.py and fill in.

Only bootstrap, secrets and host layout live here now. Everything an admin is expected
to change - the Telegram user lists, the streams, the auto arm/disarm schedule and the
detection and recording tunables - lives in the database and is edited from the bot's
/admin panel. See settings_spec.py for the catalogue: SPECS for the tunables and
STREAM_FIELDS for the per-stream ones.

Streams and timers can be added, edited and deleted from the panel, so a new device can
be set up entirely from Telegram and the UserSettings block below is only needed if you
would rather seed one from a file. Stream edits are the one thing that is not live: the
stream processes read their configuration once, when they are forked, so the panel says
a restart is pending and offers the button until the app has come back up.

Upgrading an existing install: leave your current settings.py exactly as it is. The
first run creates the new tables and copies the moved settings across, after which the
leftover attributes here are simply ignored. They are safe to delete once that has
happened.

What is kept here, and why:

  * db_file          - the pointer to the database cannot live in the database.
  * log_*            - utils.py builds the logger at import time, before anything has
                       opened the database. It is also what would report a failure to
                       open it.
  * *_token          - secrets, kept out of a database file that is bind-mounted into
                       ./data and readable by anything with the volume.
  * telegram_superadminlist
                     - the lockout escape hatch. An admin can remove every admin from
                       the panel; recovering from that should not need hand-edited SQL.
                       It is also what stops the bot granting itself more than it was
                       given.
  * paths and binaries
                     - these describe the container's mounts and the board's hardware,
                       not user policy. An admin editing them can only break the
                       install.
"""

import logging
from pathlib import Path


class Settings:
	# -- bootstrap -----------------------------------------------------------
	db_file = '/opt/aispy/data/aispy.db'

	# -- logging -------------------------------------------------------------
	log_name = '/opt/aispy/data/aispy.log'
	log_maxbytes = 10 * 1024 * 1024
	log_maxnum = 5
	file_loglevel = logging.INFO
	console_loglevel = logging.INFO
	telegram_loglevel = logging.ERROR
	# Where log records above telegram_loglevel are shipped.
	telegram_chat_id = 0

	# -- secrets -------------------------------------------------------------
	# The bot's API token. telegram_token is the one the log handler posts with; they
	# are usually the same bot.
	fractal_token = ''
	telegram_token = ''

	# -- lockout escape hatch ------------------------------------------------
	# Always admins, and cannot be removed from the panel.
	telegram_superadminlist = []

	# -- host layout ---------------------------------------------------------
	videodir = Path('/opt/aispy/data/video')
	snapshot_dir = Path('/opt/aispy/data/snapshots')
	# Defaults to videodir's sibling 'cache' if left out.
	# cachedir = Path('/opt/aispy/data/cache')

	# -- binaries and hardware -----------------------------------------------
	ffmpeg_path = 'ffmpeg'
	ffprobe_path = 'ffprobe'
	# Decode ladder, tried in order; see decoder.DECODE_VARIANTS. Leave it out unless
	# this board needs to pin one.
	# detect_decode_variants = ('rkmpp_rga', 'software', 'rkmpp')


class UserSettings:
	"""Kept for the one-time migration of an existing install.

	A fresh install can leave this empty and configure everything from /admin. If you
	would rather seed a new device from a file, fill these in for the first run and they
	will be copied into the database:

		streaminfo = {
			0: {'armed': 1},
			1: {
				'armed': 1,
				'url': 'rtsp://user:pass@192.168.1.10:554/stream1',
				'detect_url': 'rtsp://user:pass@192.168.1.10:554/stream2',
				'dimensions': (1920, 1080),
				'detect_dimensions': (960, 540),
				'detectarea': [[0, 0], [1920, 0], [1920, 1080], [0, 1080]],
				'detection_classes': [0],
				'motion_classes': [2, 5, 7],
				'confidence_threshold': 0.5,
				'lite_aspect_ratio': False,
			},

	'detectarea' is the only stream field the panel will not edit: there is no way to
	draw a polygon on a phone keyboard, and a stream added from Telegram gets the whole
	frame. Write one here, or into the database, and the panel leaves it alone - it is
	rescaled if you change 'dimensions' and never otherwise touched.

	'detection_classes' and 'motion_classes' are two separate groups, not a set and a
	subset of it. A class in the first raises an event just by being there; one in the
	second only while it is moving, so a parked car is ignored and the same car pulling
	in is not. The example above alarms on a person standing still, and on a car, bus or
	truck only while it moves. A class cannot be in both - putting it in one takes it out
	of the other - and an empty group holds nothing rather than everything, so a stream
	with both empty is looked at and never alarms.

	'detect' and 'record' are accepted for an existing install but nothing reads them.
	Whether a stream is watched is the per-stream arm/disarm button, and the recorder
	always records.
		}

		auto_arm_disarm_list = [AutoArm(hour=22, do_arm=True),
								AutoArm(hour=6, do_arm=False)]

		telegram_adminlist = []
		telegram_userlist = []
		telegram_alarmlist = []
		telegram_notify_arm_disarm_list = []
	"""
