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

from movement_tracker import MOVING, STATIONARY, UNKNOWN, MovementTracker, iou_matrix

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
		return list(self.tracker.update(boxes, classes, at, STATIONARY_TIME, THRESHOLD))

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
		states = [tracker.update([box(100, 200)], [CAR], at, STATIONARY_TIME, THRESHOLD)[0]
				  for at in (100.5, 101.5, 102.5, 103.5, 104.5)]
		self.assertEqual(list(states), [UNKNOWN] * 5)
		self.assertEqual(list(tracker.update([box(100, 200)], [CAR], 105.5,
											 STATIONARY_TIME, THRESHOLD)), [STATIONARY])

	def test_object_arriving_after_the_warm_up_still_counts(self):
		"""The cold-start hush is a window after startup, not a property of new objects."""
		tracker = MovementTracker(started=100.0)
		for at in (100.5, 101.5, 102.5, 103.5, 104.5, 105.5, 106.5):
			tracker.update([box(100, 200)], [CAR], at, STATIONARY_TIME, THRESHOLD)
		self.assertEqual(list(tracker.update([box(100, 200), box(800, 400)], [CAR, CAR],
											 107.5, STATIONARY_TIME, THRESHOLD)),
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
									 STATIONARY_TIME, THRESHOLD))
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
