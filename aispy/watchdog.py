import multiprocessing as mp
import threading
import time
from object_detector import ObjectDetector
from mediamanagers import SnapshotProcessor
from utils import mainlogger


class Watchdog(threading.Thread):

	def __init__(self, streaminfo: dict, snapshotrequests: mp.Queue = None,
				 snapshotreplies: mp.Queue = None):
		super().__init__()
		self.streaminfos = streaminfo
		self.snapshotqueue = mp.Queue()
		# Made by the app rather than here: the bot is on the other end of them and was
		# forked before this thread existed, so a queue created here could never reach it.
		self.snapshotrequests = snapshotrequests
		self.snapshotreplies = snapshotreplies
		self.updatetime = mp.Value('d', 0.0)
		self.detectorload = mp.Value('d', 0.0)
		self.processes = []

	def start_processes(self):
		mainlogger.info(f'Watchdog starting processes')
		snap = SnapshotProcessor(self.snapshotqueue)
		self.processes.append(snap)
		detect = ObjectDetector(
			self.streaminfos,
			self.snapshotqueue,
			self.updatetime,
			self.detectorload,
			self.snapshotrequests,
			self.snapshotreplies
		)
		self.processes.append(detect)

		for process in self.processes:
			process.start()

		while True:
			# mainlogger.info(f'{self.detectorload.value*100=:.0f}%')
			time.sleep(5)

	def run(self) -> None:
		self.start_processes()