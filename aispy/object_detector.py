import os
import time
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
from movement_tracker import MovementTracker, MOVING, STATE_NAMES

class ObjectDetector(mp.Process):

	def __init__(self, streaminfo: dict, snapshotqueue: mp.Queue,
				 updatetime: mp.Value, detectorload: mp.Value):
		super().__init__()
		self.streaminfos = streaminfo
		self.snapshotqueue = snapshotqueue
		self.settings = get_store()
		self.avginferencetime = self.settings.get('avg_inference_time')
		self.updatetime = updatetime
		self.detectorload = detectorload
		self.model: DetectorAPI | None = None
		self.zones = {}
		self.trackers = {}
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
							annotated_frame, num_detections = self.doinference(frame, streamid, motion_detections=motion_detections)
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

					# Sleep if no more work is available
					if time_left > 0:
						mainlogger.debug(f'Sleeping for {time_left} seconds')
						time.sleep(time_left)
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
		starttime = datetime.now().timestamp()
		confidence = self.streaminfos[streamid]['confidence_threshold']
		classes, motion_classes = self.classes_for(streamid)
		if not classes:
			# Neither group holds anything, so there is nothing to look for. The model
			# reads an empty class list as 'all of them', so this has to be caught here
			# rather than handed down as a filter that filters nothing.
			return frame, 0
		if motion_detections is None:
			detections = self.model.detect(frame, classes=classes, conf=confidence,
										nms=True, iou=0.5, verbose=False)
		else:
			detections = motion_detections
		# Movement is judged on the whole frame, before the detect area is applied: a car
		# approaching from outside the zone has already been seen moving by the time it
		# crosses in, instead of arriving as an unknown and wasting a cycle.
		states = self.tracker_for(streamid).update(
			detections.xyxy, detections.class_id, time.monotonic(),
			self.stationary_time, self.movement_threshold)
		zone = self.zone_for(streamid, frame)
		zone_mask = zone.trigger(detections=detections)
		zone_detections = detections[zone_mask]
		states = states[zone_mask]
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
			states = states[verified]
		counting = self.counting_mask(zone_detections, states, motion_classes)
		annotated_frame = draw_polygon(frame, zone.polygon, color=sv.Color.GREEN)
		# The suppressed boxes go on first, so a car that did not count cannot draw over
		# the person standing next to it who did.
		suppressed = zone_detections[~counting]
		if len(suppressed):
			annotated_frame = self.suppressedannotator.annotate(
				annotated_frame, detections=suppressed,
				labels=self.labels_for(suppressed, states[~counting], motion_classes))
		counted = zone_detections[counting]
		annotated_frame = self.boxannotator.annotate(
			annotated_frame, detections=counted,
			labels=self.labels_for(counted, states[counting], motion_classes))
		num_detections = len(counted)
		inferencetime = datetime.now().timestamp() - starttime
		self.avginferencetime = (self.avginferencetime * 19 + inferencetime) / 20
		return (annotated_frame, num_detections)

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
