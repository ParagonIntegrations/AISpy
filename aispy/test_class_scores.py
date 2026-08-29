"""What the class list has to get right, without a camera or an NPU.

These are score vectors, which is the one part of a detection nobody ever sees: by the
time a miss reaches a snapshot or a log there is no box to look at and no way to tell
'the model saw nothing' from 'the model saw it and called it something nobody asked
about'. So the awkward cases live here.
"""

import os
import sys
import unittest

import numpy as np

# aispy is run as `python3 aispy`, which puts this directory on the path; unittest
# discovery from the directory above imports it as a package, which does not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from class_scores import overridden_by, select_classes

PERSON = 0
CAR = 2
MOTORCYCLE = 3
BUS = 5
TRUCK = 7

# What stream 7 watches for: a person counts on sight, the vehicles only while moving.
YARD = [PERSON, CAR, MOTORCYCLE, TRUCK]
THRESHOLD = 0.45


def anchors(*rows) -> np.ndarray:
	"""Score vectors as {class: score} dicts, since 76 of the 80 are always zero."""
	scores = np.zeros((len(rows), 80), dtype=np.float32)
	for index, row in enumerate(rows):
		for class_id, score in row.items():
			scores[index, class_id] = score
	return scores


class SelectClassesTest(unittest.TestCase):

	def keep(self, scores, class_list=None, threshold=THRESHOLD):
		"""(class id, confidence) for each anchor that survives, as the caller sees it."""
		conf, class_ids, _ = select_classes(scores, YARD if class_list is None else class_list)
		return [(int(class_ids[index]), round(float(conf[index]), 2))
				for index in np.flatnonzero(conf > threshold)]

	def test_a_wanted_class_survives_an_unwanted_one_outscoring_it(self):
		"""The whole bug: a truck is a truck even when the model slightly prefers 'bus'.

		Filtering on the winning class dropped this box entirely - not below threshold,
		outranked - and produced exactly the same empty result as an empty frame.
		"""
		scores = anchors({TRUCK: 0.46, BUS: 0.62})
		self.assertEqual(self.keep(scores), [(TRUCK, 0.46)])

	def test_the_box_is_still_dropped_when_no_wanted_class_clears_the_threshold(self):
		"""Ignoring unwanted classes is not the same as counting them for the wanted ones."""
		scores = anchors({TRUCK: 0.11, BUS: 0.93})
		self.assertEqual(self.keep(scores), [])

	def test_an_anchor_scoring_nothing_at_all_is_dropped(self):
		"""Most of 2100 anchors are empty sky, and -1.0 has to fall out on its own."""
		self.assertEqual(self.keep(anchors({})), [])

	def test_the_best_wanted_class_wins_when_several_are_asked_for(self):
		"""car and truck are both selected here, so the higher of the two is the answer."""
		self.assertEqual(self.keep(anchors({CAR: 0.55, TRUCK: 0.44})), [(CAR, 0.55)])
		self.assertEqual(self.keep(anchors({CAR: 0.44, TRUCK: 0.55})), [(TRUCK, 0.55)])

	def test_one_box_per_anchor_rather_than_one_per_wanted_class(self):
		"""Saving the vehicle under both its names would put two boxes on one truck.

		Class-aware NMS keeps one of each, the tracker cannot give two boxes the same
		history, so the second arrives with none - novel, therefore moving, therefore an
		alarm on a parked vehicle. Which is the fault this all started with.
		"""
		self.assertEqual(len(self.keep(anchors({CAR: 0.55, TRUCK: 0.52}))), 1)

	def test_an_empty_class_list_is_the_callers_problem_not_a_wildcard(self):
		"""Guarded upstream in both callers; here it means what it says and keeps nothing."""
		self.assertEqual(self.keep(anchors({TRUCK: 0.99}), class_list=[]), [])

	def test_unwanted_classes_cannot_drag_a_neighbour_down_with_them(self):
		"""Several anchors at once, since that is how the head actually reports."""
		scores = anchors({TRUCK: 0.46, BUS: 0.62}, {PERSON: 0.81}, {BUS: 0.77}, {})
		self.assertEqual(self.keep(scores), [(TRUCK, 0.46), (PERSON, 0.81)])


class OverriddenByTest(unittest.TestCase):

	def report(self, scores, class_list=None, threshold=THRESHOLD):
		conf, _, wanted = select_classes(scores, YARD if class_list is None else class_list)
		# Rounded because the scores are float32 and these are written as decimals.
		return {class_id: round(score, 2) for class_id, score
				in overridden_by(scores, wanted, conf > threshold).items()}

	def test_it_names_what_the_model_would_rather_have_said(self):
		"""The seven silent minutes, had anything been watching: 'bus' every cycle."""
		self.assertEqual(self.report(anchors({TRUCK: 0.46, BUS: 0.62})), {BUS: 0.62})

	def test_agreement_is_silent(self):
		"""Reported on every cycle, so a clean frame has to stay quiet or it is noise."""
		self.assertEqual(self.report(anchors({TRUCK: 0.88, BUS: 0.12})), {})

	def test_a_dropped_box_is_not_reported(self):
		"""A bus that is simply a bus is not a disagreement about a box - there is no box."""
		self.assertEqual(self.report(anchors({BUS: 0.91})), {})

	def test_the_highest_score_is_kept_rather_than_a_count(self):
		"""One vehicle covers many anchors, so a count would measure its size."""
		scores = anchors({TRUCK: 0.46, BUS: 0.62}, {TRUCK: 0.51, BUS: 0.70},
						 {TRUCK: 0.47, BUS: 0.55})
		self.assertEqual(self.report(scores), {BUS: 0.70})


if __name__ == '__main__':
	unittest.main()
