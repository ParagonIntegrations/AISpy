import os
import queue
import time
from collections import deque
from datetime import datetime
import numpy as np
import supervision as sv
from supervision.draw.utils import draw_polygon
import multiprocessing as mp
from settings_store import get_store
from utils import mainlogger
from detector import create_detector
from detector.detector_api import DetectorAPI
from detector.detectors.rknn import RknnDetectorConfig
from movement_tracker import MovementTracker, Movement, MOVING, STATE_NAMES

# Detector cycles of per-object detail kept per stream, dumped to the log when an event
# fires. The alarm snapshot only ever shows the last cycle; the run-up to it is what says
# whether an object was moving all along or merely looked new for one look.
EVIDENCE_HISTORY = 20


class ObjectDetector(mp.Process):

	def __init__(self, streaminfo: dict, snapshotqueue: mp.Queue,
				 updatetime: mp.Value, detectorload: mp.Value,
				 snapshotrequests: mp.Queue = None, snapshotreplies: mp.Queue = None):
		super().__init__()
		self.streaminfos = streaminfo
		self.snapshotqueue = snapshotqueue
		# Snapshots asked for from the bot are answered here rather than there, because
		# this process is the one holding the model: the NPU has a single loaded copy of
		# it, and a second one in the bot would be a second copy of the weights to keep a
		# button press company.
		self.snapshotrequests = snapshotrequests
		self.snapshotreplies = snapshotreplies
		self.settings = get_store()
		self.avginferencetime = self.settings.get('avg_inference_time')
		self.updatetime = updatetime
		self.detectorload = detectorload
		self.model: DetectorAPI | None = None
		self.zones = {}
		self.trackers = {}
		self.evidence = {}
		# Refreshed at the top of every pass, like the other live settings.
		self.stationary_time = 5.0
		self.movement_threshold = 0.15
		self.boxannotator = sv.BoxAnnotator(
			thickness=2,
			text_thickness=2,
			text_scale=1,
			color=sv.Color.BLUE
		)
		# Objects that were found but did not count, drawn so a notification shows why
		# nothing fired rather than looking like the detector missed the car entirely.
		self.suppressedannotator = sv.BoxAnnotator(
			thickness=2,
			text_thickness=2,
			text_scale=1,
			color=sv.Color(128, 128, 128)
		)


	def run(self):
		mainlogger.info(f'Starting detect process with pid {os.getpid()}')
		self.model = create_detector(RknnDetectorConfig(type_key='rknn'))
		while True:
			try:
				mainlogger.info(f'Starting detect process')
				while True:
					loopstarttime = datetime.now()
					# Re-read every pass: both are live, so retuning them from the admin
					# panel takes effect without restarting the detector.
					detections_for_event = self.settings.get('detections_for_event')
					check_detection_time = self.settings.get('check_detection_time')
					self.stationary_time = self.settings.get('stationary_time').total_seconds()
					self.movement_threshold = self.settings.get('movement_threshold')
					# Get one frame from each camera for processing, This happens as per the settings
					mainlogger.debug(f'Checking all streams for objects')
					framebuff: list[tuple] = []
					for streamid in self.streaminfos.keys():
						if streamid == 0:
							continue
						framebuff.append((streamid, self.streaminfos[streamid]['framebuffer'][-1], None))
					# # Workaround for stream 4
					# id = 4
					# fr = self.streaminfos[id]['framebuffer'][-1]
					# # mainlogger.info(f'Shape')
					# # mainlogger.info(f'{fr.shape}')
					# cutframe = fr[272:816,384:1344,:]
					# # mainlogger.info(f'{cutframe.shape}')
					# # self.snapshotqueue.put((4,cutframe,f'Test'))
					# framebuff.append((id, cutframe, None))
					mainlogger.debug(f'Got frames from {len(framebuff)} streams')

					while framebuff:
						item = framebuff.pop()
						streamid = item[0]
						frame = item[1]
						motion_detections = item[2]
						if self.streaminfos[0]['armed'].value and self.streaminfos[streamid]['armed'].value:
							annotated_frame, num_detections, found = self.doinference(frame, streamid, motion_detections=motion_detections)
							self.record_evidence(streamid, found)
						else:
							annotated_frame, num_detections = frame, 0
						recordcounter = self.streaminfos[streamid]['recordcounter']
						if num_detections >= 1:
							recordcounter += 1
						else:
							recordcounter -= 1
						recordcounter = max(0, recordcounter)
						recordcounter = min(recordcounter, detections_for_event*2)
						self.streaminfos[streamid]['recordcounter'] = recordcounter
						if recordcounter:
							mainlogger.debug(f'recordcounter {recordcounter}')
						# Re-check items with a recordcounter of between 1 and detections_for_event to make sure if recording should happen
						if 0 < recordcounter < detections_for_event and self.streaminfos[streamid]['recordflag'].value != 1:
							if motion_detections is None:
								framebuff.append((streamid, self.streaminfos[streamid]['framebuffer'][-1], motion_detections))
							else:
								# Append from the motion detector
								pass
						# Set the recordflag if needed
						if recordcounter >= detections_for_event and self.streaminfos[streamid]['recordflag'].value != 1:
							self.streaminfos[streamid]['recordflag'].value = 1
							mainlogger.info(f'Item found on Stream {streamid} setting recordflag')
							mainlogger.info(f'What raised Stream {streamid}:\n'
											f'{self.evidence_report(streamid)}')
							self.streaminfos[0]['alarm'].value = 1
							self.snapshotqueue.put((streamid, annotated_frame,
													f'Alarm Active on {self.settings.stream_name(streamid)}'))
						# Clear the recordflag when the counter is decreasing and at 1 while recording
						if recordcounter == 1 and num_detections == 0 and self.streaminfos[streamid]['recordflag'].value == 1:
							self.streaminfos[streamid]['recordflag'].value = 0
							mainlogger.info(f'No more items on Stream {streamid}, clearing recordflag')
							if self.streaminfos[0]['armed'].value and self.streaminfos[streamid]['armed'].value:
								self.snapshotqueue.put((streamid, annotated_frame,
														f'Alarm Cleared on {self.settings.stream_name(streamid)}'))

					# Whatever is left of the interval is now simply idle. It used to
					# be spent re-inferencing recorded clips to annotate them.
					now = datetime.now()
					time_left = loopstarttime + check_detection_time - now
					time_left = time_left.total_seconds()
					# Update the updatetime
					self.updatetime.value = now.timestamp()
					self.detectorload.value = (self.detectorload.value*19 + (1-time_left/check_detection_time.total_seconds()))/20

					# Whatever is left of the cycle goes to the bot's snapshot requests, and
					# then to sleeping off the remainder.
					mainlogger.debug(f'Idle for {time_left} seconds')
					self.serve_snapshots(time.monotonic() + time_left)
			except:
				mainlogger.exception(f'Problem in detector restarting in 10 seconds')
				time.sleep(10)


	def zone_for(self, streamid, frame) -> sv.PolygonZone:
		"""The detect area in the coordinate space of the frames we are handed.

		Cached because building a PolygonZone rasterises a mask as big as the whole
		frame, which is not work to repeat on every single inference.
		"""
		height, width = frame.shape[:2]
		key = (streamid, width, height)
		if key not in self.zones:
			streaminfo = self.streaminfos[streamid]
			polygon = streaminfo.get('detect_detectarea', streaminfo['detectarea'])
			self.zones[key] = sv.PolygonZone(polygon, (width, height))
		return self.zones[key]

	def tracker_for(self, streamid) -> MovementTracker:
		"""This stream's movement memory, started the first time it is asked for.

		Started lazily rather than in __init__ so the warm-up runs from the first frame
		the stream actually delivers, not from whenever the process happened to fork.
		"""
		if streamid not in self.trackers:
			self.trackers[streamid] = MovementTracker(started=time.monotonic())
		return self.trackers[streamid]

	def classes_for(self, streamid) -> tuple:
		"""(classes to ask the model for, the ones that only count while moving).

		The two configured lists are separate groups rather than a set and a subset, so
		the model has to be asked for the union of them. An empty group means no class
		is in it, so a stream with both empty is watching for nothing at all.
		"""
		streaminfo = self.streaminfos[streamid]
		presence = set(streaminfo.get('detection_classes') or [])
		motion = set(streaminfo.get('motion_classes') or [])
		return sorted(presence | motion), motion

	def counting_mask(self, detections, states, motion_classes) -> np.ndarray:
		"""Which detections are allowed to raise an event.

		Anything outside the motion-only group counts on sight, exactly as it always
		did. Anything inside it counts only while the tracker says it is moving, so a
		parked car is found, drawn, and ignored.
		"""
		if not len(detections):
			return np.zeros(0, dtype=bool)
		return np.array([class_id not in motion_classes or state == MOVING
						 for class_id, state in zip(detections.class_id, states)],
						dtype=bool)

	def doinference(self, frame, streamid, double_check=True, motion_detections=None) -> tuple:
		"""(annotated frame, how many counted, what this cycle saw).

		The descriptions are what the event log is built from, so they cover the ignored
		objects too: 'the car was there and was called still' and 'the car was never found
		at all' look identical in a count and need different fixes. A cycle that found
		nothing says which stage emptied it rather than going quiet, for the same reason.
		"""
		starttime = datetime.now().timestamp()
		confidence = self.streaminfos[streamid]['confidence_threshold']
		classes, motion_classes = self.classes_for(streamid)
		if not classes:
			# Neither group holds anything, so there is nothing to look for. The model
			# reads an empty class list as 'all of them', so this has to be caught here
			# rather than handed down as a filter that filters nothing.
			return frame, 0, ['no classes selected for this stream']
		if motion_detections is None:
			detections = self.model.detect(frame, classes=classes, conf=confidence,
										nms=True, iou=0.5, verbose=False)
		else:
			detections = motion_detections
		# Movement is judged on the whole frame, before the detect area is applied: a car
		# approaching from outside the zone has already been seen moving by the time it
		# crosses in, instead of arriving as an unknown and wasting a cycle.
		movement = self.tracker_for(streamid).update(
			detections.xyxy, detections.class_id, time.monotonic(),
			self.stationary_time, self.movement_threshold)
		# Kept for the evidence line: once everything has been filtered away there is no
		# way back to how many there were to begin with, and 'the model saw nothing' and
		# 'the model saw it and the zoom-in threw it out' are the same empty list.
		on_frame = len(detections)
		overridden = self.model.overridden if motion_detections is None else {}
		zone = self.zone_for(streamid, frame)
		zone_mask = zone.trigger(detections=detections)
		zone_detections = detections[zone_mask]
		movement = movement.select(zone_mask)
		in_zone = len(zone_detections)
		# Zoom in and recheck if an object is found
		if len(zone_detections) and double_check:
			verified = []
			for detection in zone_detections:
				x1, y1, x2, y2 = detection[0].astype(int)
				dx = x2 - x1
				dy = y2 - y1
				newx1 = int(max((x1 - dx*0.3), 0))
				newx2 = int(min((x2 + dx*0.3), frame.shape[1]))
				newy1 = int(max((y1 - dy*0.3), 0))
				newy2 = int(min((y2 + dy*0.3), frame.shape[0]))
				newframe: np.ndarray = frame[newy1:newy2,newx1:newx2]
				new_detections = self.model.detect(newframe, classes=classes, conf=confidence,
									nms=True, iou=0.5, verbose=False)
				# new_detections = sv.Detections.from_ultralytics(newresult)
				num_detections = len(new_detections.xyxy)
				if num_detections:
					verified.append(True)
				else:
					verified.append(False)
			verified = np.array(verified, dtype=bool)
			zone_detections = zone_detections[verified]
			movement = movement.select(verified)
		counting = self.counting_mask(zone_detections, movement.states, motion_classes)
		annotated_frame = draw_polygon(frame, zone.polygon, color=sv.Color.GREEN)
		# The suppressed boxes go on first, so a car that did not count cannot draw over
		# the person standing next to it who did.
		suppressed = zone_detections[~counting]
		if len(suppressed):
			annotated_frame = self.suppressedannotator.annotate(
				annotated_frame, detections=suppressed,
				labels=self.labels_for(suppressed, movement.states[~counting], motion_classes))
		counted = zone_detections[counting]
		annotated_frame = self.boxannotator.annotate(
			annotated_frame, detections=counted,
			labels=self.labels_for(counted, movement.states[counting], motion_classes))
		num_detections = len(counted)
		inferencetime = datetime.now().timestamp() - starttime
		self.avginferencetime = (self.avginferencetime * 19 + inferencetime) / 20
		found = self.describe(zone_detections, movement, motion_classes, counting)
		if not found:
			found = [self.nothing_found(on_frame, in_zone)]
		if overridden:
			found.append(self.contested(overridden))
		return annotated_frame, num_detections, found

	@staticmethod
	def nothing_found(on_frame, in_zone) -> str:
		"""Which stage emptied the frame, for the cycles that produced nothing.

		Every stage here ends in the same empty list, and they have nothing in common: a
		model that never saw the truck, a detect area drawn short of where it parks, and a
		zoom-in recheck that will not confirm it are three different faults with three
		different fixes, and the run-up to an event is exactly where the difference
		matters.
		"""
		if not on_frame:
			return 'nothing found on the frame at all'
		if not in_zone:
			return f'nothing in the detect area ({on_frame} found, all outside it)'
		return (f'nothing in the detect area ({in_zone} of {on_frame} inside it, '
				f'all rejected by the zoom-in recheck)')

	def contested(self, overridden) -> str:
		"""Classes the model preferred to the one it was allowed to use.

		The detector now scores only the classes this stream asked for, so these boxes are
		kept rather than discarded - but a class that keeps winning is worth saying out
		loud, because it means the model and the class list disagree about what is out
		there, and the disagreement used to be silent.
		"""
		preferred = ', '.join(f'{self.model.model_names[class_id]} {score:0.2f}'
							  for class_id, score in sorted(overridden.items()))
		return f'model would rather have called something {preferred}'

	def describe(self, detections, movement: Movement, motion_classes, counting) -> list:
		"""One line per object found in the zone: what it was, and what was made of it.

		Box size and position are in here because the shift in a reason is a fraction of
		the box's own width, not a number of pixels: 0.20 on a 40px box is eight pixels of
		wobble on something far away, and 0.20 on a 600px box is a car pulling in.
		"""
		lines = []
		for index, class_id in enumerate(detections.class_id):
			x1, y1, x2, y2 = detections.xyxy[index]
			gate = 'motion-only' if class_id in motion_classes else 'on-sight'
			lines.append(
				f'{self.model.model_names[class_id]} {detections.confidence[index]:0.2f} '
				f'{"COUNTED" if counting[index] else "ignored"} [{gate}] '
				f'{STATE_NAMES[int(movement.states[index])]} ({movement.reasons[index]}) '
				f'{int(x2 - x1)}x{int(y2 - y1)}px at {int(x1)},{int(y1)}')
		return lines

	def record_evidence(self, streamid, found) -> None:
		"""Keep the last few cycles of detail, so an event can be explained afterwards.

		A ring rather than a debug log line: turning DEBUG on to catch the next false alarm
		means logging every cycle of every stream until it happens, and the run-up is only
		ever interesting for the cycles that actually led somewhere.
		"""
		if streamid not in self.evidence:
			self.evidence[streamid] = deque(maxlen=EVIDENCE_HISTORY)
		self.evidence[streamid].append((datetime.now().strftime('%H:%M:%S'), found))

	def evidence_report(self, streamid) -> str:
		"""The kept cycles as text, and a fresh start for whatever happens next.

		Cleared as it is read: the question is always what led to *this* event, and a
		second event minutes later re-reading the first one's run-up would be answering a
		question nobody asked.
		"""
		lines = []
		for stamp, found in self.evidence.pop(streamid, ()):
			if found:
				lines.extend(f'  {stamp}  {line}' for line in found)
			else:
				lines.append(f'  {stamp}  nothing recorded')
		return '\n'.join(lines) or '  no detection history'

	# -- snapshots on request -------------------------------------------------

	def serve_snapshots(self, until) -> None:
		"""Answer the bot's snapshot requests until the next cycle is due.

		This is what the tail of a cycle is for: the detector wakes on a fixed interval and
		is then idle for whatever is left of it, so a snapshot costs one inference in time
		that was going to be spent sleeping. Requests that arrive with nothing left of the
		cycle are answered anyway rather than held over - somebody is waiting on a chat
		message for it, and a detector too busy to have idle time is exactly when there is
		something out there worth looking at.
		"""
		if self.snapshotrequests is None:
			if until > time.monotonic():
				time.sleep(until - time.monotonic())
			return
		while True:
			timeout = until - time.monotonic()
			try:
				if timeout > 0:
					request = self.snapshotrequests.get(timeout=timeout)
				else:
					request = self.snapshotrequests.get_nowait()
			except queue.Empty:
				return
			self.answer_snapshot(*request)

	def answer_snapshot(self, requestid, streamid) -> None:
		"""One request, answered with a frame or with nothing.

		Always answered, one way or the other: the bot is holding a request open against a
		timeout, and the plain unannotated frame it falls back to is much better sent now
		than in ten seconds' time.
		"""
		try:
			frame = self.streaminfos[streamid]['framebuffer'][-1]
			self.snapshotreplies.put((requestid, self.annotate_snapshot(streamid, frame)))
		except Exception:
			mainlogger.exception(f'Could not annotate a snapshot of stream {streamid}')
			self.snapshotreplies.put((requestid, None))

	def annotate_snapshot(self, streamid, frame) -> np.ndarray:
		"""A frame with a box on each of the objects this stream is watching for.

		Only those: the model is asked for the stream's two class groups and nothing else,
		so a snapshot carries the objects the alarm is about rather than every chair and
		potted plant in view.

		Coloured the way an alarm snapshot is coloured - blue for what would raise an
		event, grey for what would not, whether that is because it is outside the detect
		area or because it is a motion-only class that is not moving. The point of a
		snapshot on a quiet camera is usually 'why has this not gone off', and that is an
		answer to it rather than a second vocabulary to learn.

		No zoomed-in recheck, unlike a detection cycle: that exists to keep a doubtful box
		from raising an alarm, and here nothing is being raised. Showing the box and
		letting whoever pressed the button judge it costs one inference instead of one per
		object.
		"""
		confidence = self.streaminfos[streamid]['confidence_threshold']
		classes, motion_classes = self.classes_for(streamid)
		if not classes:
			# Nothing is being watched for on this stream, so there is nothing to draw and
			# no class list to hand the model - it reads an empty one as 'all of them'.
			return frame
		detections = self.model.detect(frame, classes=classes, conf=confidence,
									   nms=True, iou=0.5, verbose=False)
		# peek, not update: a snapshot is a look nobody scheduled, and letting it write to
		# the movement memory would let button presses stand in for the detection cycles a
		# disarmed stream is deliberately not getting.
		movement = self.tracker_for(streamid).peek(
			detections.xyxy, detections.class_id, time.monotonic(), self.stationary_time)
		zone = self.zone_for(streamid, frame)
		counting = (self.counting_mask(detections, movement.states, motion_classes)
					& zone.trigger(detections=detections))
		annotated_frame = draw_polygon(frame, zone.polygon, color=sv.Color.GREEN)
		# Suppressed first, so a parked car cannot draw over the person walking past it.
		suppressed = detections[~counting]
		if len(suppressed):
			annotated_frame = self.suppressedannotator.annotate(
				annotated_frame, detections=suppressed,
				labels=self.labels_for(suppressed, movement.states[~counting], motion_classes))
		counted = detections[counting]
		if len(counted):
			annotated_frame = self.boxannotator.annotate(
				annotated_frame, detections=counted,
				labels=self.labels_for(counted, movement.states[counting], motion_classes))
		return annotated_frame

	def labels_for(self, detections, states, motion_classes) -> list:
		"""'person 0.82', or 'car 0.79 (still)' for a motion-only class.

		The state is only worth printing for classes it actually decides anything for -
		on the rest it would just be noise about a measurement nothing consulted.
		"""
		labels = []
		for class_id, conf, state in zip(detections.class_id, detections.confidence, states):
			label = f'{self.model.model_names[class_id]} {conf: 0.2f}'
			if class_id in motion_classes:
				label = f'{label} ({STATE_NAMES[int(state)]})'
			labels.append(label)
		return labels
