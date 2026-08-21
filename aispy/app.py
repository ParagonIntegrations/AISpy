import threading
import time

import numpy as np
from streams import Stream
from settings import UserSettings, Settings
from utils import mainlogger, optional_setting
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
		self.dbupdatequeue = None
		self.process_outputs = {}
		self.telegrambot = None
		self.init_shared_state_objects()

	def init_shared_state_objects(self):
		self.dbupdatequeue = mp.Queue()
		self.motionmanager = mp.Manager()
		# TODO use these in the process
		self.process_outputs['detector'] = {}
		self.process_outputs['detector']['updatetime'] = mp.Value('d', 0.0)
		self.process_outputs['detector']['load'] = mp.Value('d', 0.0)

		self.streaminfos[0]['alarm'] = mp.Value('i', 0)
		# Owned here, not by the bot: the auto arm/disarm schedule is replayed once per
		# app start, so a Telegrambot process restart cannot undo a manual arm/disarm.
		self.streaminfos[0]['timer_state_applied'] = mp.Value('i', 0)
		for streamid in UserSettings.streaminfo.keys():
			self.streaminfos[streamid]['armed'] = mp.Value('i', self.streaminfos[streamid]['armed'])
			if streamid == 0:
				continue
			# self.recordflags[streamid] = mp.Value('i', 0)
			self.streaminfos[streamid]['recordflag'] = mp.Value('i', 0)
			self.init_detect_geometry(streamid)
			# The pre-record window lives on disk as ffmpeg segments now, so this only
			# has to hold a few recent frames for the detector instead of
			# pre_record_time * record_fps of them.
			detect_dimensions = self.streaminfos[streamid]['detect_dimensions']
			self.streaminfos[streamid]['framebuffer'] = SharedFrameDeque(
				max_items=int(optional_setting('detect_buffer_frames', 8)),
				itemshape=(detect_dimensions[1], detect_dimensions[0], 3),
				datatype=np.uint8
			)
			self.streaminfos[streamid]['motionlist'] = self.motionmanager.list([None])

	def init_detect_geometry(self, streamid):
		"""Work out the frame size and detect area the detector will actually see.

		The decoder scales whatever it is given to 'detect_dimensions', so this is a
		free choice rather than a property of the source: smaller means fewer bytes
		down the pipe and a smaller frame buffer. The detect area is written against
		'dimensions', so it is scaled into those coordinates here.
		"""
		streaminfo = self.streaminfos[streamid]
		dimensions = tuple(streaminfo['dimensions'])
		detect_dimensions = tuple(streaminfo.get('detect_dimensions') or dimensions)
		streaminfo['detect_dimensions'] = detect_dimensions
		if detect_dimensions == dimensions:
			streaminfo['detect_detectarea'] = streaminfo['detectarea']
			return
		scale = np.array([detect_dimensions[0] / dimensions[0],
						  detect_dimensions[1] / dimensions[1]])
		streaminfo['detect_detectarea'] = (streaminfo['detectarea'] * scale).astype(int)
		mainlogger.info(
			f'Stream {streamid} detecting on {detect_dimensions} and recording {dimensions}')

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
			stream = Stream(streamid, self.streaminfos[streamid])
			self.streams[streamid] = stream
		# Start the streams
		for stream in self.streams.values():
			stream.start()

		# Start the telegram server and keep it alive
		self.start_telegrambot()
		threading.Thread(target=self.supervise_telegrambot, daemon=True).start()

		# Create and start the detector watchdog
		watchdog = Watchdog(self.streaminfos)
		watchdog.start()

		# Start the dbupdater
		self.dbupdater()


if __name__ == "__main__":

	app = FractalApp()
	app.run()