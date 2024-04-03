import pathlib
import time
from datetime import datetime
import cv2
import multiprocessing as mp
from settings import UserSettings, Settings
from utils import mainlogger, send_photo_telegram

class FileAnnotator(mp.Process):
	def __init__(self, sendqueue: mp.Queue, receivequeue: mp.Queue, ordersqueue: mp.Queue, streaminfos):
		super().__init__()
		self.sendqueue = sendqueue
		self.receivequeue = receivequeue
		self.ordersqueue = ordersqueue
		self.streaminfos = streaminfos

	def run(self):
		mainlogger.info(f'Fileannotator starting')
		working = False
		while True:
			try:
				if not working:
					order = self.ordersqueue.get()
					working = True
					streamid = order[0]
					infilepath = pathlib.Path(order[1])
					infilename = infilepath.name
					mainlogger.info(f'Starting inference on {infilename} from stream {streamid}')
					outfilepath = Settings.annotatedvideodir.joinpath(str(streamid))
					outfilepath.mkdir(parents=True,exist_ok=True)
					outfilename = str(outfilepath.joinpath(infilename))
					fourcc = cv2.VideoWriter_fourcc(*'mp4v')
					out = cv2.VideoWriter(outfilename, fourcc, UserSettings.record_fps, self.streaminfos[streamid]['dimensions'])
					cap = cv2.VideoCapture(order[1])
				if working:
					while cap.isOpened():
						check, frame = cap.read()
						if check:
							if self.sendqueue.qsize() <= 100:
								self.sendqueue.put((frame, streamid))
							else:
								break
						else:
							mainlogger.debug(f'All video frames placed on queue')
							self.sendqueue.put((None, 'Done'))
							cap.release()
					qsize = self.receivequeue.qsize()
					for i in range(qsize):
						try:
							packet = self.receivequeue.get_nowait()
						except:
							break
						if packet[1] == 'Done':
							out.release()
							working = False
							mainlogger.info(f'Inference on {infilename} from stream {streamid} done')
						else:
							out.write(packet[0])
					time.sleep(0.1)
			except:
				mainlogger.exception(f'Problem with fileannotator restarting in 10')
				self.ordersqueue.put(order)
				working = False
				time.sleep(10)

class SnapshotProcessor(mp.Process):
	def __init__(self, snapshotqueue: mp.Queue):
		super().__init__()
		self.snapshotqueue = snapshotqueue

	def run(self):
		mainlogger.info(f'Starting snapshot process')
		while True:
			try:
				item = self.snapshotqueue.get()
				streamid = item[0]
				frame = item[1]
				caption = item[2]
				snapshot_dir = Settings.snapshot_dir.joinpath(f'{streamid}')
				snapshot_dir.mkdir(parents=True, exist_ok=True)
				datetimestr = datetime.now().strftime("%Y%m%d_%H%M%S")
				if caption == None: caption = datetimestr
				snapshot_filename = str(snapshot_dir.joinpath(f'{datetimestr}.png'))
				cv2.imwrite(snapshot_filename,frame)
				# Send the photo to telegram
				send_photo_telegram(snapshot_filename, Settings.telegram_chat_id, Settings.fractal_token, caption)
			except:
				mainlogger.warning(f'Problem in snapshot processor restarting in 10')
				self.snapshotqueue.put(item)
				time.sleep(10)