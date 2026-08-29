"""Choosing which class a detection box gets, out of the ones anyone asked for.

A YOLO head scores every class for every anchor, and the caller wants only a handful of
them. The obvious way to narrow that down is to take the winning class and then drop the
box if the winner is not on the list - and it is wrong in a way that is invisible from
the outside. A truck the model marginally prefers to call a bus scores well clear of the
threshold as a truck and is discarded whole, its own score never examined, because a
class nobody asked about happened to come first. Nothing downstream can tell that apart
from an empty frame: no box is produced, so the detect area, the tracker and the event
log all have nothing to say about the vehicle that was standing there the whole time.

So the classes nobody asked for are taken out of the running before the argmax rather
than used to veto the result of one. The box comes back as the best class the caller
actually wanted, and what it lost to is reported rather than thrown away, because a class
that keeps winning means the model and the class list disagree about what is out there.

Numpy in, numpy out, in the same spirit as movement_tracker: the awkward case here is a
score vector, and needing an NPU to ask about one would mean never asking.
"""

import numpy as np


def select_classes(scores, class_list) -> tuple:
	"""(confidence, class id, keep mask, what outscored the kept boxes).

	`scores` is one row per anchor and one column per class. `class_list` holds the class
	ids worth returning; the confidence threshold is the caller's business, applied to the
	confidences handed back.
	"""
	wanted = np.zeros(scores.shape[1], dtype=bool)
	wanted[class_list] = True
	# -1.0 rather than nothing at all: a row with no wanted class then peaks below any
	# valid threshold and falls out on its own, with no separate emptiness case to keep
	# in step with the one that does the real work.
	contenders = np.where(wanted, scores, -1.0)
	return contenders.max(1), contenders.argmax(1), wanted


def overridden_by(scores, wanted, kept) -> dict:
	"""Unasked-for classes that outscored the class a kept box was given.

	Highest score per class rather than a count of them: before NMS these are anchors and
	not objects, so a count measures how big the vehicle was rather than how often the
	model changed its mind about it.
	"""
	top = scores.argmax(1)
	report = {}
	for index in np.flatnonzero(kept & ~wanted[top]):
		class_id = int(top[index])
		report[class_id] = max(report.get(class_id, 0.0), float(scores[index, class_id]))
	return report
