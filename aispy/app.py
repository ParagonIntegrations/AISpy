import threading
import time

import numpy as np
from streams import Stream
from settings import UserSettings, Settings
from utils import mainlogger
from db_driver import DBDriver
from telegrambot import Telegrambot
from watchdog import Watchdog
import multiprocessing as mp
from memory_managers import SharedFrameDeque


class FractalApp:

	def __init__(self):
		mainlogger.info(f'Fractal Initializing')
		self.db = DBDriver(Settings.db_file)
		self.streams = {}
		# self.recordflags = {}
		self.streaminfos = self.db.load_state()
		self.fileinferencequeue = None
		self.dbupdatequeue = None
		self.process_outputs = {}
		self.telegrambot = None
		self.init_shared_state_objects()

	def init_shared_state_objects(self):
		self.fileinferencequeue = mp.Queue()
		self.dbupdatequeue = mp.Queue()
		self.motionmanager = mp.Manager()
		# TODO use these in the process
		self.process_outputs['detector'] = {}
		self.process_outputs['detector']['updatetime'] = mp.Value('d', 0.0)
		self.process_outputs['detector']['load'] = mp.Value('d', 0.0)

		self.streaminfos[0]['alarm'] = mp.Value('i', 0)
		for streamid in UserSettings.streaminfo.keys():
			self.streaminfos[streamid]['armed'] = mp.Value('i', self.streaminfos[streamid]['armed'])
			if streamid == 0:
				continue
			# self.recordflags[streamid] = mp.Value('i', 0)
			self.streaminfos[streamid]['recordflag'] = mp.Value('i', 0)
			self.streaminfos[streamid]['framebuffer'] = SharedFrameDeque(
				max_items=int(UserSettings.pre_record_time.total_seconds() * UserSettings.record_fps),
				itemshape=(self.streaminfos[streamid]['dimensions'][1], self.streaminfos[streamid]['dimensions'][0], 3),
				datatype=np.uint8
			)
			self.streaminfos[streamid]['motionlist'] = self.motionmanager.list([None])

	def start_telegrambot(self):
		self.telegrambot = Telegrambot(self.streaminfos, self.dbupdatequeue)
		self.telegrambot.start()

	def supervise_telegrambot(self, interval=10):
		"""Restart the bot if its process ever exits.

		Nothing else notices that it is gone: the rest of the app keeps running happily
		without any Telegram control or notifications.
		"""
		while True:
			time.sleep(interval)
			if not self.telegrambot.is_alive():
				mainlogger.warning(
					f'Telegrambot exited with code {self.telegrambot.exitcode}, restarting')
				self.telegrambot.join()
				self.start_telegrambot()

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
		# Create the streams
		for streamid in UserSettings.streaminfo.keys():
			if streamid == 0:
				continue
			stream = Stream(streamid, self.streaminfos[streamid], self.fileinferencequeue)
			self.streams[streamid] = stream
		# Start the streams
		for stream in self.streams.values():
			stream.start()

		# Start the telegram server and keep it alive
		self.start_telegrambot()
		threading.Thread(target=self.supervise_telegrambot, daemon=True).start()

		# Create and start the detector watchdog
		watchdog = Watchdog(self.streaminfos, self.fileinferencequeue)
		watchdog.start()

		# Start the dbupdater
		self.dbupdater()


if __name__ == "__main__":

	app = FractalApp()
	app.run()