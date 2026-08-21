import os
import time

from utils import mainlogger, optional_setting
from memory_managers import SharedFrameDeque
from recorder import SegmentRecorder
import cv2
import multiprocessing as mp
import threading

class Stream(mp.Process):
	def __init__(self, id, stream_info, fileannotatorqueue):
		super().__init__()
		mainlogger.debug(f'Stream {id} initializing')
		self.streamid = id
		self.streaminfo = stream_info
		self.framebuffer: SharedFrameDeque = self.streaminfo['framebuffer']
		self.video = None
		self.recorder = SegmentRecorder(id, stream_info, fileannotatorqueue)
		# Detection can run off the camera's substream: a quarter of the pixels is a
		# quarter of the decode, and the detector letterboxes down to 320x320 anyway.
		self.detect_url = self.streaminfo.get('detect_url') or self.streaminfo['url']
		# Frames are only appended this often. The buffer exists to keep one recent
		# frame ready for the detector, which wakes every check_detection_time - it is
		# no longer the pre-record ring, so appending at record_fps was copying
		# megabytes per frame that nothing would ever read.
		self.detect_fps = float(optional_setting('detect_fps', 5))

	def run(self):
		mainlogger.info(f'Stream {self.streamid} starting with pid {os.getpid()}')
		threading.Thread(target=self.recorder.run_ffmpeg, daemon=True).start()
		threading.Thread(target=self.recorder.collector, daemon=True).start()
		self.capture()

	def capture(self):
		"""Decode the detection stream into the frame buffer.

		Recording does not come from here any more - ffmpeg remuxes the camera's own
		stream straight to disk - so this only has to keep the buffer's newest frame
		fresh for the detector. Frames are still read at full rate, because not
		draining the RTSP receive queue just builds latency, but only appended at
		detect_fps.
		"""
		while True:
			try:
				mainlogger.info(f'Starting capture on stream {self.streamid}')
				self.video = cv2.VideoCapture(self.detect_url)
				interval = 1 / self.detect_fps
				nextframe = 0.0
				while True:
					check, frame = self.video.read()
					if not check:
						mainlogger.warning(
							f'Stream {self.streamid} returned no frame, reconnecting in 10 seconds')
						break
					now = time.monotonic()
					if now < nextframe:
						continue
					nextframe = now + interval
					if self.streaminfo['lite_aspect_ratio']:
						frame = frame.repeat(2, 1)
					self.framebuffer.append(frame)
			except Exception:
				mainlogger.exception(
					f'Exception on stream {self.streamid}, restarting in 10 seconds')
			finally:
				if self.video is not None:
					self.video.release()
					self.video = None
			time.sleep(10)
