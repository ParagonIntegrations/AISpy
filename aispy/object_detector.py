import os
import time
from datetime import datetime
import numpy as np
import supervision as sv
from supervision.draw.utils import draw_polygon
import multiprocessing as mp
from settings import UserSettings, Settings
from utils import mainlogger
from detector import create_detector
from detector.detector_api import DetectorAPI
from detector.detectors.rknn import RknnDetectorConfig

class ObjectDetector(mp.Process):

	def __init__(self, streaminfo: dict, fileinferencequeue: mp.Queue,
				 snapshotqueue: mp.Queue, fileannotatorsendqueue: mp.Queue, fileannotatorreceivequeue: mp.Queue,
				 updatetime: mp.Value, detectorload: mp.Value):
		super().__init__()
		self.streaminfos = streaminfo
		self.fileinferencequeue = fileinferencequeue
		self.snapshotqueue = snapshotqueue
		self.fileannotatorsendqueue = fileannotatorsendqueue
		self.fileannotatorreceivequeue = fileannotatorreceivequeue
		self.avginferencetime = Settings.avg_inference_time
		self.updatetime = updatetime
		self.detectorload = detectorload
		self.model: DetectorAPI | None = None
		self.boxannotator = sv.BoxAnnotator(
			thickness=2,
			text_thickness=2,
			text_scale=1,
			color=sv.Color.BLUE
		)


	def run(self):
		mainlogger.info(f'Starting detect process with pid {os.getpid()}')
		self.model = create_detector(RknnDetectorConfig(type_key='rknn'))
		while True:
			try:
				mainlogger.info(f'Starting detect process')
				while True:
					loopstarttime = datetime.now()
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
						recordcounter = min(recordcounter, UserSettings.detections_for_event*2)
						self.streaminfos[streamid]['recordcounter'] = recordcounter
						if recordcounter:
							mainlogger.debug(f'recordcounter {recordcounter}')
						# Re-check items with a recordcounter of between 1 and UserSettings.detections_for_event to make sure if recording should happen
						if 0 < recordcounter < UserSettings.detections_for_event and self.streaminfos[streamid]['recordflag'].value != 1:
							if motion_detections is None:
								framebuff.append((streamid, self.streaminfos[streamid]['framebuffer'][-1], motion_detections))
							else:
								# Append from the motion detector
								pass
						# Set the recordflag if needed
						if recordcounter >= UserSettings.detections_for_event and self.streaminfos[streamid]['recordflag'].value != 1:
							self.streaminfos[streamid]['recordflag'].value = 1
							mainlogger.info(f'Item found on Stream {streamid} setting recordflag')
							self.streaminfos[0]['alarm'].value = 1
							self.snapshotqueue.put((streamid, annotated_frame, f'Alarm Active on stream {streamid}'))
						# Clear the recordflag when the counter is decreasing and at 1 while recording
						if recordcounter == 1 and num_detections == 0 and self.streaminfos[streamid]['recordflag'].value == 1:
							self.streaminfos[streamid]['recordflag'].value = 0
							mainlogger.info(f'No more items on Stream {streamid}, clearing recordflag')
							if self.streaminfos[0]['armed'].value and self.streaminfos[streamid]['armed'].value:
								self.snapshotqueue.put((streamid, annotated_frame, f'Alarm Cleared on stream {streamid}'))

					# After all frames have been processed do other detection work if there is time left
					now = datetime.now()
					time_left = loopstarttime + UserSettings.check_detection_time - now
					time_left = time_left.total_seconds()
					# Magic value obtained by trial and error
					inferencestodo = int(time_left * 0.65 // self.avginferencetime)
					mainlogger.debug(f'Doing {inferencestodo} inferences on video')
					for i in range(inferencestodo):
						if self.fileannotatorsendqueue.qsize() > 0:
							try:
								packet = self.fileannotatorsendqueue.get_nowait()
							except:
								break
							if packet[1] == 'Done':
								mainlogger.debug(f'Received done packet from fileannotator')
								self.fileannotatorreceivequeue.put((None, 'Done'))
							else:
								inf_res = self.doinference(*packet)
								self.fileannotatorreceivequeue.put(inf_res)
					now = datetime.now()
					time_left = loopstarttime + UserSettings.check_detection_time - now
					time_left = time_left.total_seconds()
					# Update the updatetime
					self.updatetime.value = now.timestamp()
					self.detectorload.value = (self.detectorload.value*19 + (1-time_left/UserSettings.check_detection_time.total_seconds()))/20

					# Sleep if no more work is available
					if time_left > 0:
						mainlogger.debug(f'Sleeping for {time_left} seconds')
						time.sleep(time_left)
			except:
				mainlogger.exception(f'Problem in detector restarting in 10 seconds')
				time.sleep(10)


	def doinference(self, frame, streamid, double_check=True, motion_detections=None) -> tuple:
		starttime = datetime.now().timestamp()
		if motion_detections is None:
			confidence = self.streaminfos[streamid]['confidence_threshold']
			classes = self.streaminfos[streamid]['detection_classes']
			detections = self.model.detect(frame, classes=classes, conf=confidence,
										nms=True, iou=0.5, verbose=False)
		else:
			detections = motion_detections
		zone = sv.PolygonZone(self.streaminfos[streamid]['detectarea'],
							  self.streaminfos[streamid]['dimensions'])
		zone_detections = detections[zone.trigger(detections=detections)]
		# Zoom in and recheck if an object is found
		if zone_detections and double_check:
			verified = []
			for detection in zone_detections:
				x1, y1, x2, y2 = detection[0].astype(int)
				dx = x2 - x1
				dy = y2 - y1
				newx1 = int(max((x1 - dx*0.3), 0))
				newx2 = int(min((x2 + dx*0.3), self.streaminfos[streamid]['dimensions'][0]))
				newy1 = int(max((y1 - dy*0.3), 0))
				newy2 = int(min((y2 + dy*0.3), self.streaminfos[streamid]['dimensions'][1]))
				newframe: np.ndarray = frame[newy1:newy2,newx1:newx2]
				new_detections = self.model.detect(newframe, classes=classes, conf=confidence,
									nms=True, iou=0.5, verbose=False)
				# new_detections = sv.Detections.from_ultralytics(newresult)
				num_detections = len(new_detections.xyxy)
				if num_detections:
					verified.append(True)
				else:
					verified.append(False)
			zone_detections = zone_detections[verified]
		zone_annotated_frame = draw_polygon(frame, zone.polygon, color=sv.Color.GREEN)
		labels = [f'{self.model.model_names[class_id]} {conf: 0.2f}'
				  for class_id, conf in zip(zone_detections.class_id, detections.confidence)]
		annotated_frame = self.boxannotator.annotate(zone_annotated_frame, detections=zone_detections, labels=labels)
		num_detections = len(labels)
		inferencetime = datetime.now().timestamp() - starttime
		self.avginferencetime = (self.avginferencetime * 19 + inferencetime) / 20
		return (annotated_frame, num_detections)
