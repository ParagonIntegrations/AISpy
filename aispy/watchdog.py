import multiprocessing as mp
import threading
import time
from object_detector import ObjectDetector
from mediamanager import FileAnnotator, SnapshotProcessor
from utils import mainlogger


class Watchdog(threading.Thread):

	def __init__(self, streaminfo: dict, streamqueues: dict, recordflags: dict, fileinferencequeue: mp.Queue):
		super().__init__()
		self.streaminfos = streaminfo
		self.streamqueues = streamqueues
		self.recordflags = recordflags
		self.fileinferencequeue = fileinferencequeue
		self.snapshotqueue = mp.Queue()
		self.fileannotatorsendqueue = mp.Queue()
		self.fileannotatorreceivequeue = mp.Queue()
		self.updatetime = mp.Value('d', 0.0)
		self.detectorload = mp.Value('d', 0.0)
		self.processes = []

	def start_processes(self):
		mainlogger.info(f'Watchdog starting processes')
		snap = SnapshotProcessor(self.snapshotqueue)
		self.processes.append(snap)
		fileanno = FileAnnotator(
			self.fileannotatorsendqueue,
			self.fileannotatorreceivequeue,
			self.fileinferencequeue,
			self.streaminfos
		)
		self.processes.append(fileanno)
		detect = ObjectDetector(
			self.streaminfos,
			self.streamqueues,
			self.recordflags,
			self.fileinferencequeue,
			self.snapshotqueue,
			self.fileannotatorsendqueue,
			self.fileannotatorreceivequeue,
			self.updatetime,
			self.detectorload
		)
		self.processes.append(detect)

		for process in self.processes:
			process.start()

		while True:
			print(f'{self.detectorload.value*100=:.0f}%')
			time.sleep(5)

	def run(self) -> None:
		self.start_processes()