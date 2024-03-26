import time
from datetime import datetime

import cv2

from aispy.detector import create_detector
from aispy.detector.detectors.rknn import RknnDetectorConfig, RknnDetectorConfig


class AISpyApp:

	def __init__(self):
		pass

	def run(self):
		print(f'Starting app at {datetime.now()}')
		d_cfg = RknnDetectorConfig(type_key='rknn')
		detector = create_detector(d_cfg)
		# video = cv2.VideoCapture('rtsp://fractal:Nelis342256@192.168.1.110/Streaming/Channels/101')
		video = cv2.VideoCapture('rtsp://admin:JKEPZZ@192.168.1.116/')
		while True:
			check, frame = video.read()
			if not check:
				print(f'Problem with stream')
				break
			# frame = frame.repeat(2, 1)
			results = detector.detect(frame)
			# for res in results:
			# 	print(f'{res=}')
			# print(f'End of results')



	def start(self):
		self.run()
