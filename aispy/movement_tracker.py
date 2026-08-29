"""Deciding whether a detected object is moving or just sitting there.

A parked car is a car every time the model looks at it, so a camera pointed at a
driveway alarms forever on nothing. Telling the two apart needs memory the detector
does not otherwise have: it wakes up, looks at one frame, and forgets.

This keeps that memory. Boxes are matched between cycles by overlap, and each one is
measured against an *anchor* - where it was when it last moved - rather than against
the previous cycle. Comparing to the previous cycle loses a slow creep to rounding,
one sub-threshold step at a time; comparing to an anchor lets the creep accumulate
until it trips.

Three states, of which only MOVING counts as an event:

  UNKNOWN     no idea yet. Only handed out during the warm-up after a start, so a
              restart with a car already parked in view is silent rather than noisy.
  MOVING      shifted more than the threshold within the last stationary_time.
  STATIONARY  has held the same spot for stationary_time.

Novelty implies motion: a box matching nothing in memory is MOVING, which is what
makes a car crossing the frame in a single cycle - no overlap at all between one look
and the next - register instead of vanishing into UNKNOWN forever. The obvious way for
that to go wrong is a parked car dropping below the confidence threshold for a cycle
and coming back looking brand new, so entries that reached STATIONARY are remembered
far longer than active ones. The flickering car re-matches its own entry, finds its
anchor where it left it, and stays quiet.

The other way it goes wrong is a gap in the looking rather than in the seeing. A stream
that is disarmed overnight is never handed to update() at all, so by morning the memory
has expired and every parked car is novel - and the warm-up that covers exactly this at
startup has long since passed. Any gap longer than the stationary memory therefore
restarts the warm-up: past that point the memory that suppresses parked objects is gone,
so resuming is a start, whatever the clock says.

Each state comes back with the reason for it - 'novel', 'moved 0.31', 'held 0.04' - so
an event can be explained after the fact. The three routes to MOVING have three
different fixes, and the state alone does not say which one was taken.

No supervision or model types on purpose: this takes arrays and hands back arrays, so
it can be exercised without a camera or an NPU.
"""

from typing import NamedTuple

import numpy as np

UNKNOWN = 0
MOVING = 1
STATIONARY = 2

STATE_NAMES = {UNKNOWN: 'unknown', MOVING: 'moving', STATIONARY: 'still'}


class Movement(NamedTuple):
	"""One state and one reason per box, in the order the boxes were given.

	They travel together because the caller filters boxes twice - by detect area, then by
	the zoomed-in recheck - and a reason that has drifted out of step with its state is
	worse than no reason at all.
	"""

	states: np.ndarray
	reasons: np.ndarray

	def select(self, mask) -> 'Movement':
		return Movement(self.states[mask], self.reasons[mask])


# Overlap needed to call two boxes in consecutive cycles the same object.
MATCH_IOU = 0.3
# Overlap needed to match across a class change. Models flip car/truck on the same
# vehicle between frames, and without this that reads as the parked car disappearing
# and a brand new - therefore moving - one arriving in exactly its place. Set well
# above MATCH_IOU so it takes near-identical boxes, not a person walking past a car.
CROSS_CLASS_IOU = 0.6
# How long an entry outlives its last sighting, as a multiple of stationary_time.
# Anything still being argued about is cheap to rebuild; a STATIONARY entry is the
# only thing standing between a flickering parked car and a false alarm, so it is
# kept for much longer.
ACTIVE_MEMORY = 1.0
STATIONARY_MEMORY = 12.0
# ...but not less than this, so a short stationary_time cannot make the memory that
# suppresses parked cars uselessly brief.
STATIONARY_MEMORY_FLOOR = 60.0


def stationary_window(stationary_time) -> float:
	"""How long a STATIONARY entry outlives its last sighting.

	Also how long a gap in updating can be before the tracker has to admit it no longer
	knows what is out there: the two are the same number because it is the STATIONARY
	entries that do the suppressing, and once they are gone there is nothing left to
	suppress with.
	"""
	return max(stationary_time * STATIONARY_MEMORY, STATIONARY_MEMORY_FLOOR)


class _Entry:
	__slots__ = ('class_id', 'box', 'anchor', 'anchor_time', 'last_seen', 'state')

	def __init__(self, class_id, box, now, state):
		self.class_id = int(class_id)
		self.box = box
		# Where it was when it last moved, and when that was. Stillness is measured
		# from here, so a drift too small to trip the threshold in one cycle still
		# trips it once enough of them have added up.
		self.anchor = box
		self.anchor_time = now
		self.last_seen = now
		self.state = state


def iou_matrix(boxes, others) -> np.ndarray:
	"""Intersection over union of every box against every other, as (len, len)."""
	if not len(boxes) or not len(others):
		return np.zeros((len(boxes), len(others)), dtype=np.float32)
	boxes = np.asarray(boxes, dtype=np.float32)
	others = np.asarray(others, dtype=np.float32)
	x1 = np.maximum(boxes[:, None, 0], others[None, :, 0])
	y1 = np.maximum(boxes[:, None, 1], others[None, :, 1])
	x2 = np.minimum(boxes[:, None, 2], others[None, :, 2])
	y2 = np.minimum(boxes[:, None, 3], others[None, :, 3])
	overlap = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
	area = np.clip(boxes[:, 2] - boxes[:, 0], 0, None) * np.clip(boxes[:, 3] - boxes[:, 1], 0, None)
	other_area = (np.clip(others[:, 2] - others[:, 0], 0, None)
				  * np.clip(others[:, 3] - others[:, 1], 0, None))
	union = area[:, None] + other_area[None, :] - overlap
	return np.where(union > 0, overlap / np.maximum(union, 1e-9), 0.0).astype(np.float32)


class MovementTracker:
	"""One stream's worth of memory. Not thread safe, and not meant to be shared.

	`max_entries` is a guard against a busy street, where the model returns dozens of
	boxes a cycle and every one of them wants remembering. Stationary entries are given
	up first when it fills: they are the cheapest to be wrong about, since the worst
	case is one spurious event from a car that was already being ignored.
	"""

	def __init__(self, started=None, max_entries=200):
		self.entries: list[_Entry] = []
		self.started = started
		# When update() was last called, as opposed to when anything was last seen. A
		# disarmed stream is never looked at, and that is invisible to last_seen.
		self.last_update = None
		self.max_entries = max_entries

	def update(self, boxes, class_ids, now, stationary_time, movement_threshold) -> Movement:
		"""Classify this cycle's boxes, and fold them into the memory.

		Returns one state and one reason per box, in the order they were given, so the
		caller can carry both alongside the detections through whatever filtering it does
		next.
		"""
		if self.started is None:
			self.started = now
		elif (self.last_update is not None
			  and now - self.last_update > stationary_window(stationary_time)):
			# Nobody has been looking for long enough that _expire is about to throw away
			# every entry that could vouch for a parked car. Resuming from that is a start
			# in every sense that matters here, so warm up again rather than alarm on
			# whatever has been sitting in the driveway all night.
			self.started = now
		self.last_update = now
		boxes = np.asarray(boxes, dtype=np.float32).reshape(-1, 4)
		class_ids = (np.zeros(len(boxes), dtype=int) if class_ids is None
					 else np.asarray(class_ids).reshape(-1).astype(int))
		self._expire(now, stationary_time)

		states = np.full(len(boxes), UNKNOWN, dtype=np.int8)
		reasons = np.empty(len(boxes), dtype=object)
		matched = self._match(boxes, class_ids)
		# Nothing is known about an object during the first stationary_time of a stream:
		# there has not been time to watch anything hold still, so calling a novel box
		# moving here would alarm on whatever happened to be parked in view at startup.
		warming_up = (now - self.started) < stationary_time
		for index, box in enumerate(boxes):
			entry = matched.get(index)
			if entry is None:
				entry = _Entry(class_ids[index], box, now,
							   UNKNOWN if warming_up else MOVING)
				self.entries.append(entry)
				reasons[index] = 'warm-up' if warming_up else 'novel'
			else:
				reasons[index] = self._advance(entry, box, class_ids[index], now,
											   stationary_time, movement_threshold)
			states[index] = entry.state
		self._enforce_cap()
		return Movement(states, reasons)

	def peek(self, boxes, class_ids, now, stationary_time) -> Movement:
		"""What the memory already says about these boxes, writing nothing back.

		A snapshot taken from the bot is a look nobody asked for: it happens whenever a
		button is pressed, on a stream that may not even be armed. Folding it in through
		update() would let it stand in for the regular cycles - it moves anchors, it
		creates entries, and above all it counts as having looked, so a disarmed camera
		polled by snapshots would never take the warm-up that stops every parked car
		reading as novel when it is armed again. So this reads and does not write.

		Nothing is matched against an entry that the next update() would throw away
		anyway, which is what makes an untracked stream honestly answer 'unknown' rather
		than reporting where a car was standing hours ago.
		"""
		boxes = np.asarray(boxes, dtype=np.float32).reshape(-1, 4)
		class_ids = (np.zeros(len(boxes), dtype=int) if class_ids is None
					 else np.asarray(class_ids).reshape(-1).astype(int))
		states = np.full(len(boxes), UNKNOWN, dtype=np.int8)
		reasons = np.empty(len(boxes), dtype=object)
		matched = self._match(boxes, class_ids, self._live_entries(now, stationary_time))
		for index in range(len(boxes)):
			entry = matched.get(index)
			states[index] = UNKNOWN if entry is None else entry.state
			reasons[index] = ('not being tracked' if entry is None
							  else f'remembered {STATE_NAMES[entry.state]}')
		return Movement(states, reasons)

	# -- internals -----------------------------------------------------------

	def _live_entries(self, now, stationary_time) -> list:
		active_cutoff = now - stationary_time * ACTIVE_MEMORY
		stationary_cutoff = now - stationary_window(stationary_time)
		return [
			entry for entry in self.entries
			if entry.last_seen >= (stationary_cutoff if entry.state == STATIONARY
								   else active_cutoff)]

	def _expire(self, now, stationary_time) -> None:
		self.entries = self._live_entries(now, stationary_time)

	def _match(self, boxes, class_ids, entries=None) -> dict:
		"""Greedy best-overlap pairing of this cycle's boxes to remembered entries.

		Same class at MATCH_IOU first, then a second pass at CROSS_CLASS_IOU that will
		cross a class boundary, so a relabelled vehicle keeps its history.
		"""
		entries = self.entries if entries is None else entries
		if not len(boxes) or not entries:
			return {}
		overlaps = iou_matrix(boxes, [entry.box for entry in entries])
		entry_classes = np.array([entry.class_id for entry in entries])
		same_class = class_ids[:, None] == entry_classes[None, :]
		matched = {}
		taken = set()
		for allowed, floor in ((same_class, MATCH_IOU),
							   (np.ones_like(same_class), CROSS_CLASS_IOU)):
			scores = np.where(allowed, overlaps, 0.0)
			# Best pair first, then strike out that box and that entry and go again, so
			# two cars nose to tail cannot both claim the same history.
			order = np.argsort(scores, axis=None)[::-1]
			for flat in order:
				box_index, entry_index = np.unravel_index(flat, scores.shape)
				if scores[box_index, entry_index] < floor:
					break
				if box_index in matched or entry_index in taken:
					continue
				matched[int(box_index)] = entries[int(entry_index)]
				taken.add(int(entry_index))
		return matched

	@staticmethod
	def _advance(entry, box, class_id, now, stationary_time, movement_threshold) -> str:
		"""Re-judge a box against its own history, and say what the judgement rested on.

		The reason carries the measured shift even when nothing changed, because the
		number is what movement_threshold is tuned against and 'held 0.14' is the
		difference between a threshold that is about right and one that is about to fire.
		"""
		anchor = entry.anchor
		width = max(anchor[2] - anchor[0], 1.0)
		height = max(anchor[3] - anchor[1], 1.0)
		shift = max(abs((box[0] + box[2]) - (anchor[0] + anchor[2])) / 2 / width,
					abs((box[1] + box[3]) - (anchor[1] + anchor[3])) / 2 / height)
		entry.class_id = int(class_id)
		entry.box = box
		entry.last_seen = now
		if shift > movement_threshold:
			entry.state = MOVING
			entry.anchor = box
			entry.anchor_time = now
			return f'moved {shift:.2f}'
		if now - entry.anchor_time >= stationary_time:
			entry.state = STATIONARY
			return f'settled {shift:.2f}'
		# Otherwise it holds whatever it was: a moving object keeps counting until it
		# has been still long enough to have earned STATIONARY, which is what stops an
		# arriving car from being dropped the moment it pauses at a gate.
		return f'held {shift:.2f}'

	def _enforce_cap(self) -> None:
		if len(self.entries) <= self.max_entries:
			return
		self.entries.sort(key=lambda entry: (entry.state == STATIONARY, -entry.last_seen))
		del self.entries[self.max_entries:]
