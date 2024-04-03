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
			print(f'{len(results)=}')
			# for res in results:
			# 	print(f'{res=}')
			# print(f'End of results')



	def start(self):
		self.run()



from streams import Stream
from settings import UserSettings, Settings
from utils import mainlogger
from db_driver import DBDriver
from telegrambot import Telegrambot
from watchdog import Watchdog
import multiprocessing as mp


class FractalApp:

	def __init__(self):
		mainlogger.info(f'Fractal Initializing')
		self.db = DBDriver(Settings.db_file)
		self.streams = {}
		self.detectqueues = {}
		self.recordflags = {}
		self.streaminfos = self.db.load_state()
		self.fileinferencequeue = mp.Queue()
		self.dbupdatequeue = mp.Queue()
		for streamid in UserSettings.streaminfo.keys():
			self.detectqueues[streamid] = mp.Queue(maxsize=1)
			self.recordflags[streamid] = mp.Value('i', 0)
			self.streaminfos[streamid]['armed'] = mp.Value('i', self.streaminfos[streamid]['armed'])

	def dbupdater(self):
		while True:
			self.dbupdatequeue.get()
			statedict = {}
			for streamid in self.streaminfos.keys():
				statedict[streamid] = {}
				for k,v in self.streaminfos[streamid].items():
					if k == 'armed':
						statedict[streamid][k] = v.value
					else:
						statedict[streamid][k] = v
			self.db.save_state(statedict)

	def run(self):
		# print([name for name in logging.root.manager.loggerDict])
		# return
		# Create the streams
		for streamid in UserSettings.streaminfo.keys():
			if streamid == 0:
				continue
			stream = Stream(streamid, self.streaminfos[streamid], self.detectqueues[streamid], self.recordflags[streamid], self.fileinferencequeue)
			self.streams[streamid] = stream
		# Start the streams
		for stream in self.streams.values():
			stream.start()

		# Start the telegram server
		t = Telegrambot(self.streaminfos, self.dbupdatequeue)
		t.start()

		# Create and start the detector watchdog
		watchdog = Watchdog(self.streaminfos, self.detectqueues, self.recordflags, self.fileinferencequeue)
		watchdog.start()

		# Start the dbupdater
		self.dbupdater()


if __name__ == "__main__":

	app = FractalApp()
	# app.telegramui()
	app.run()