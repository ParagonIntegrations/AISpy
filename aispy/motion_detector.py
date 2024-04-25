import multiprocessing as mp
from datetime import datetime

import numpy as np

from memory_managers import SharedFrameDeque
import supervision as sv


class MotionDetector(mp.Process):
	def __init__(self, streaminfo):
		super().__init__()
		self.streaminfo = streaminfo
		self.avg_frame = np.zeros((streaminfo['dimensions'][1], streaminfo['dimensions'][0], 3), np.float32)
		self.calibrating = True

	def detect(self, frame) -> sv.Detections:
		pass

	def run(self):
		while True:
			start = datetime.now()
			# Get new frame
			frame = self.streaminfo['framebuffer'][-1]
			detections = self.detect(frame)
			self.streaminfo['motionlist'][0] = detections
			end = datetime.now()
			delta = (end - start).total_seconds()
			print(f'Detection took {delta} seconds')




if __name__ == '__main__':
	shape = (1080, 1920, 3)
	type = np.uint8
	img = np.full(shape=(1080, 1920, 3), fill_value=128, dtype=type)
	motionman = mp.Manager()
	streaminfo = {}
	streaminfo['framebuffer'] = SharedFrameDeque(
				max_items=10,
				itemshape=shape,
				datatype=type
			)
	streaminfo['framebuffer'].append(img)
	streaminfo['motionlist'] = motionman.list()
	streaminfo['motionlist'].append(None)

	md = MotionDetector(streaminfo)
	md.run()