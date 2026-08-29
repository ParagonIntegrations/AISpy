"""What the moving/stationary split has to get right, without a camera or an NPU.

The tracker is the only part of the feature with no way to eyeball it: everything else
shows up in a snapshot, but 'why did the parked car alarm at 3am' is a question about
state that was thrown away hours ago. So the awkward cases live here instead.
"""

import os
import sys
import unittest

# aispy is run as `python3 aispy`, which puts this directory on the path; unittest
# discovery from the directory above imports it as a package, which does not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from movement_tracker import (MOVING, STATIONARY, UNKNOWN, MovementTracker, iou_matrix,
							  stationary_window)

CAR = 2
TRUCK = 7
PERSON = 0

STATIONARY_TIME = 5.0
THRESHOLD = 0.15


def box(x, y, width=200, height=120):
	return [x, y, x + width, y + height]


class TrackerTest(unittest.TestCase):

	def setUp(self):
		self.tracker = MovementTracker(started=0.0)

	def look(self, at, boxes, classes=None):
		"""One detector pass at time `at`, returning the state of each box."""
		classes = [CAR] * len(boxes) if classes is None else classes
		return list(self.tracker.update(boxes, classes, at, STATIONARY_TIME, THRESHOLD).states)

	def hold(self, since, until, boxes, classes=None, step=1.0):
		"""Nothing moves between two times, polled at the detector's real cadence.

		The cadence matters: an entry that goes unseen for stationary_time is forgotten,
		so a test that skips cycles is testing expiry by accident.
		"""
		states = []
		at = since
		while at <= until + 1e-9:
			states = self.look(at, boxes, classes)
			at += step
		return states

	# -- the two cases the feature exists for ---------------------------------

	def test_parked_car_goes_stationary_and_stays_there(self):
		"""The whole point: a car that never moves stops counting and never restarts."""
		self.assertEqual(self.look(10.0, [box(100, 200)]), [MOVING])
		# Still counts while it has not yet held the spot for stationary_time.
		self.assertEqual(self.hold(11.0, 14.0, [box(100, 200)]), [MOVING])
		self.assertEqual(self.hold(15.0, 16.0, [box(100, 200)]), [STATIONARY])
		for at in range(17, 400):
			self.assertEqual(self.look(float(at), [box(99, 200)]), [STATIONARY],
							 f'woke up again at {at}s')

	def test_parked_car_that_drives_off_counts_again(self):
		self.look(10.0, [box(100, 200)])
		self.assertEqual(self.hold(11.0, 16.0, [box(100, 200)]), [STATIONARY])
		# 40px on a 200px-wide box is 0.2 of its width, over the 0.15 threshold.
		self.assertEqual(self.look(17.0, [box(140, 200)]), [MOVING])

	# -- the ways it could go wrong -------------------------------------------

	def test_box_jitter_does_not_read_as_movement(self):
		"""A few pixels of wobble per cycle, forever, must never accumulate into an event."""
		self.look(10.0, [box(100, 200)])
		for index, at in enumerate(range(11, 300)):
			jitter = box(100 + (index % 5) - 2, 200 + (index % 3) - 1)
			state = self.look(float(at), [jitter])
			if at >= 16:
				self.assertEqual(state, [STATIONARY], f'jitter read as movement at {at}s')

	def test_slow_creep_accumulates_into_movement(self):
		"""Sub-threshold steps still trip it, because stillness is measured from an anchor.

		Measuring against the previous cycle instead would lose this entirely: no single
		step is over the threshold, so a car rolling down a driveway would stay quiet.
		"""
		self.look(10.0, [box(100, 200)])
		self.assertEqual(self.hold(11.0, 16.0, [box(100, 200)]), [STATIONARY])
		states = [self.look(float(at), [box(100 + 8 * step, 200)])[0]
				  for step, at in enumerate(range(17, 25), start=1)]
		self.assertIn(MOVING, states, 'a creeping car never registered')

	def test_fast_car_with_no_overlap_still_counts(self):
		"""A car crossing the frame in one cycle shares no pixels between looks.

		Novelty has to imply motion for this to work at all - matching alone would call
		it a new object every cycle and never have any history to judge it by.
		"""
		self.assertEqual(self.look(10.0, [box(0, 500)]), [MOVING])
		self.assertEqual(self.look(11.0, [box(600, 500)]), [MOVING])
		self.assertEqual(self.look(12.0, [box(1200, 500)]), [MOVING])

	def test_parked_car_flickering_out_of_detection_stays_quiet(self):
		"""The cost of 'novel means moving': a dropped cycle must not look novel.

		Stationary entries outlive their last sighting by a long way for exactly this.
		"""
		self.look(10.0, [box(100, 200)])
		self.assertEqual(self.hold(11.0, 16.0, [box(100, 200)]), [STATIONARY])
		self.assertEqual(self.hold(17.0, 24.0, []), [])
		self.assertEqual(self.look(25.0, [box(101, 201)]), [STATIONARY])

	def test_relabelled_vehicle_keeps_its_history(self):
		"""car <-> truck flips on the same parked vehicle must not read as an arrival."""
		self.look(10.0, [box(100, 200)], [CAR])
		self.assertEqual(self.hold(11.0, 16.0, [box(100, 200)], [CAR]), [STATIONARY])
		self.assertEqual(self.look(17.0, [box(101, 200)], [TRUCK]), [STATIONARY])

	def test_person_does_not_adopt_a_parked_cars_history(self):
		"""Cross-class matching is deliberately strict, or anything could inherit stillness."""
		self.look(10.0, [box(100, 200)], [CAR])
		self.assertEqual(self.hold(11.0, 16.0, [box(100, 200)], [CAR]), [STATIONARY])
		# Overlapping the car, but nothing like the same shape.
		walker = box(150, 210, width=40, height=100)
		self.assertEqual(self.look(17.0, [box(100, 200), walker], [CAR, PERSON]),
						 [STATIONARY, MOVING])

	def test_cold_start_is_silent(self):
		"""A restart with a car already parked in view must not alarm on it."""
		tracker = MovementTracker(started=100.0)
		states = [tracker.update([box(100, 200)], [CAR], at, STATIONARY_TIME, THRESHOLD).states[0]
				  for at in (100.5, 101.5, 102.5, 103.5, 104.5)]
		self.assertEqual(list(states), [UNKNOWN] * 5)
		self.assertEqual(list(tracker.update([box(100, 200)], [CAR], 105.5,
											 STATIONARY_TIME, THRESHOLD).states), [STATIONARY])

	def test_object_arriving_after_the_warm_up_still_counts(self):
		"""The cold-start hush is a window after startup, not a property of new objects."""
		tracker = MovementTracker(started=100.0)
		for at in (100.5, 101.5, 102.5, 103.5, 104.5, 105.5, 106.5):
			tracker.update([box(100, 200)], [CAR], at, STATIONARY_TIME, THRESHOLD)
		self.assertEqual(list(tracker.update([box(100, 200), box(800, 400)], [CAR, CAR],
											 107.5, STATIONARY_TIME, THRESHOLD).states),
						 [STATIONARY, MOVING])

	def test_two_cars_nose_to_tail_do_not_share_one_history(self):
		self.look(10.0, [box(100, 200)])
		self.assertEqual(self.hold(11.0, 16.0, [box(100, 200)]), [STATIONARY])
		# The parked one, plus one pulling in right behind it.
		self.assertEqual(self.look(17.0, [box(100, 200), box(310, 200)]),
						 [STATIONARY, MOVING])

	def test_a_stopping_car_keeps_counting_until_it_has_settled(self):
		"""A car that pauses at a gate must not be dropped for the length of the pause."""
		self.look(10.0, [box(0, 200)])
		self.assertEqual(self.look(11.0, [box(100, 200)]), [MOVING])
		self.assertEqual(self.hold(12.0, 15.0, [box(100, 200)]), [MOVING])
		self.assertEqual(self.look(16.0, [box(100, 200)]), [STATIONARY])

	# -- gaps in the looking, as opposed to gaps in the seeing -----------------

	def test_parked_car_is_silent_when_a_stream_comes_back_from_disarm(self):
		"""The overnight case: nothing is looked at while disarmed, so the memory expires.

		Without a re-warm every parked car is novel come morning, and novel means moving.
		The gap here is the real one - hours, not the seconds a flicker costs.
		"""
		self.look(10.0, [box(100, 200)])
		self.assertEqual(self.hold(11.0, 16.0, [box(100, 200)]), [STATIONARY])
		# ...disarmed all night, so update() is never called...
		morning = 16.0 + 8 * 3600
		self.assertEqual(self.hold(morning, morning + 4.0, [box(100, 200)]), [UNKNOWN])
		self.assertEqual(self.look(morning + 5.0, [box(100, 200)]), [STATIONARY])

	def test_a_gap_shorter_than_the_memory_does_not_re_warm(self):
		"""A brief stall must not cost a detection: the memory still covers that long.

		This is what makes disarming and immediately re-arming quiet in the first place,
		and it is why toggling the panel is no test of the case above.
		"""
		self.look(10.0, [box(100, 200)])
		self.assertEqual(self.hold(11.0, 16.0, [box(100, 200)]), [STATIONARY])
		resume = 16.0 + stationary_window(STATIONARY_TIME) - 1.0
		self.assertEqual(self.look(resume, [box(100, 200)]), [STATIONARY])
		# Still awake to a genuine arrival, rather than hushed by a re-warm.
		self.assertEqual(self.look(resume + 1.0, [box(100, 200), box(800, 400)]),
						 [STATIONARY, MOVING])

	def test_a_car_arriving_during_the_re_warm_is_the_known_cost(self):
		"""Documenting the trade, not endorsing it: the re-warm is a hush, and it hushes.

		Same cost as a cold start, for the same reason - there is nothing to judge a novel
		box against - and the alternative is alarming on the parked car every morning.
		"""
		self.look(10.0, [box(100, 200)])
		self.hold(11.0, 16.0, [box(100, 200)])
		morning = 16.0 + 8 * 3600
		self.assertEqual(self.look(morning, [box(0, 500)]), [UNKNOWN])

	# -- saying why, not just what --------------------------------------------

	def reasons(self, at, boxes, classes=None):
		classes = [CAR] * len(boxes) if classes is None else classes
		return list(self.tracker.update(boxes, classes, at, STATIONARY_TIME, THRESHOLD).reasons)

	def test_every_route_to_a_state_names_itself(self):
		"""The three ways to reach MOVING have three different fixes, so they must differ."""
		self.assertEqual(self.reasons(10.0, [box(100, 200)]), ['novel'])
		# Sub-threshold, and not yet still for long enough to have settled. 20px on a
		# 200px box is 0.10 of its width, under the 0.15 threshold.
		self.assertEqual(self.reasons(11.0, [box(120, 200)]), ['held 0.10'])
		self.assertEqual(self.hold(12.0, 16.0, [box(100, 200)]), [STATIONARY])
		self.assertEqual(self.reasons(17.0, [box(100, 200)]), ['settled 0.00'])
		# 40px on a 200px box is 0.20 of its width, over the 0.15 threshold.
		self.assertEqual(self.reasons(18.0, [box(140, 200)]), ['moved 0.20'])

	def test_the_warm_up_hush_is_distinguishable_from_a_real_novel_box(self):
		"""Both come back UNKNOWN-or-MOVING with no history; only the reason separates them."""
		tracker = MovementTracker(started=100.0)
		warm = tracker.update([box(100, 200)], [CAR], 100.5, STATIONARY_TIME, THRESHOLD)
		self.assertEqual(list(warm.reasons), ['warm-up'])
		later = tracker.update([box(100, 200), box(800, 400)], [CAR, CAR], 107.5,
							   STATIONARY_TIME, THRESHOLD)
		self.assertEqual(list(later.reasons)[1], 'novel')

	def test_reasons_survive_the_masking_the_caller_does(self):
		"""object_detector filters twice, and a reason out of step with its state is a lie."""
		movement = self.tracker.update([box(100, 200), box(800, 400)], [CAR, CAR], 10.0,
									   STATIONARY_TIME, THRESHOLD)
		import numpy as np
		kept = movement.select(np.array([False, True]))
		self.assertEqual(len(kept.states), 1)
		self.assertEqual(list(kept.reasons), ['novel'])

	# -- housekeeping ---------------------------------------------------------

	def test_entries_are_capped_dropping_stationary_ones_first(self):
		tracker = MovementTracker(started=0.0, max_entries=4)
		parked = [box(400 * index, 100) for index in range(4)]
		at = 10.0
		while at <= 16.0:
			tracker.update(parked, [CAR] * 4, at, STATIONARY_TIME, THRESHOLD)
			at += 1.0
		self.assertTrue(all(entry.state == STATIONARY for entry in tracker.entries))
		arriving = [box(400 * index, 900) for index in range(4)]
		states = list(tracker.update(parked + arriving, [CAR] * 8, 17.0,
									 STATIONARY_TIME, THRESHOLD).states)
		self.assertEqual(states, [STATIONARY] * 4 + [MOVING] * 4)
		self.assertEqual(len(tracker.entries), 4)
		# The arrivals are the ones worth remembering: they are still being decided.
		self.assertTrue(all(entry.state == MOVING for entry in tracker.entries))

	def test_empty_frames_are_handled(self):
		self.assertEqual(self.look(10.0, []), [])
		self.assertEqual(self.tracker.entries, [])

	def test_degenerate_box_does_not_divide_by_zero(self):
		"""A zero-area box is not a real detection, but it must not take the process down.

		It never matches anything - a box with no area overlaps nothing, itself included -
		so it stays novel and therefore moving. That is the safe way to be wrong about
		something the model should not have produced.
		"""
		self.assertEqual(self.look(10.0, [[50, 50, 50, 50]]), [MOVING])
		self.assertEqual(self.look(11.0, [[50, 50, 50, 50]]), [MOVING])


class PeekTest(unittest.TestCase):
	"""Reading the memory for a snapshot, which must not count as having looked.

	Snapshots are taken by pressing a button, on any stream, armed or not. That makes them
	the one caller that can reach the tracker at a time nothing else does, so the thing to
	pin down is that they leave it exactly as they found it.
	"""

	def setUp(self):
		self.tracker = MovementTracker(started=0.0)

	def look(self, at, boxes, classes=None):
		classes = [CAR] * len(boxes) if classes is None else classes
		return list(self.tracker.update(boxes, classes, at, STATIONARY_TIME, THRESHOLD).states)

	def peek(self, at, boxes, classes=None):
		classes = [CAR] * len(boxes) if classes is None else classes
		return list(self.tracker.peek(boxes, classes, at, STATIONARY_TIME).states)

	def settle(self, at, boxes):
		"""Watch `boxes` hold still long enough to be called stationary."""
		while at <= 10.0 + STATIONARY_TIME * 2:
			self.look(at, boxes)
			at += 1.0

	def test_reports_what_the_memory_already_knows(self):
		self.settle(10.0, [box(100, 200)])
		self.assertEqual(self.peek(20.0, [box(100, 200)]), [STATIONARY])

	def test_object_nothing_knows_about_is_unknown(self):
		"""Not MOVING: novelty means motion when the tracker is watching every cycle, and
		a snapshot of a stream nobody is inferencing is the case where it does not."""
		self.settle(10.0, [box(100, 200)])
		self.assertEqual(self.peek(20.0, [box(700, 200)]), [UNKNOWN])

	def test_forgets_at_the_same_time_update_would(self):
		"""A car remembered from hours ago is not evidence about the car in front of us."""
		self.settle(10.0, [box(100, 200)])
		stale = 20.0 + stationary_window(STATIONARY_TIME) + 1.0
		self.assertEqual(self.peek(stale, [box(100, 200)]), [UNKNOWN])

	def test_creates_nothing_to_be_found_later(self):
		"""Peeking at an object must not leave an entry behind for the next cycle to match
		against, or a snapshot would be enough to make an arriving car look established."""
		self.assertEqual(self.peek(10.0, [box(100, 200)]), [UNKNOWN])
		self.assertEqual(self.look(10.0, [box(100, 200)]), [MOVING])

	def test_does_not_count_as_having_looked(self):
		"""The case this exists for: a disarmed camera being snapshotted through the night.

		Nothing is inferencing it, so by morning the memory is stale and every parked car
		is novel - which is why a gap that long restarts the warm-up. Snapshots in the gap
		must not fill it in, or the first armed cycle alarms on the whole driveway.
		"""
		self.settle(10.0, [box(100, 200)])
		morning = 20.0
		while morning < 20.0 + stationary_window(STATIONARY_TIME) * 2:
			self.peek(morning, [box(100, 200)])
			morning += 60.0
		self.assertEqual(self.look(morning, [box(100, 200)]), [UNKNOWN])


class IouTest(unittest.TestCase):

	def test_identical_boxes(self):
		self.assertAlmostEqual(float(iou_matrix([box(0, 0)], [box(0, 0)])[0][0]), 1.0)

	def test_disjoint_boxes(self):
		self.assertEqual(float(iou_matrix([box(0, 0)], [box(900, 900)])[0][0]), 0.0)

	def test_half_overlap(self):
		overlap = float(iou_matrix([[0, 0, 100, 100]], [[50, 0, 150, 100]])[0][0])
		self.assertAlmostEqual(overlap, 5000 / 15000, places=5)

	def test_empty_sides(self):
		self.assertEqual(iou_matrix([], [box(0, 0)]).shape, (0, 1))
		self.assertEqual(iou_matrix([box(0, 0)], []).shape, (1, 0))


if __name__ == '__main__':
	unittest.main()
