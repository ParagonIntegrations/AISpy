import time
from datetime import datetime
import cv2
import multiprocessing as mp
from settings import Settings
from utils import mainlogger, optional_setting, send_photo_telegram

class SnapshotProcessor(mp.Process):
	def __init__(self, snapshotqueue: mp.Queue):
		super().__init__()
		self.snapshotqueue = snapshotqueue

	def run(self):
		mainlogger.info(f'Starting snapshot process')
		while True:
			item = None
			try:
				item = self.snapshotqueue.get()
				streamid = item[0]
				frame = item[1]
				caption = item[2]
				snapshot_dir = Settings.snapshot_dir.joinpath(f'{streamid}')
				snapshot_dir.mkdir(parents=True, exist_ok=True)
				datetimestr = datetime.now().strftime("%Y%m%d_%H%M%S")
				if caption == None: caption = datetimestr
				# JPEG rather than PNG: encoding a full frame as PNG costs an order of
				# magnitude more CPU, and Telegram recompresses photos either way.
				quality = int(optional_setting('snapshot_jpeg_quality', 90))
				snapshot_filename = str(snapshot_dir.joinpath(f'{datetimestr}.jpg'))
				cv2.imwrite(snapshot_filename, frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
				# Send the photo to telegram
				send_photo_telegram(snapshot_filename, Settings.telegram_alarmlist, Settings.fractal_token, caption)
			except:
				mainlogger.warning(f'Problem in snapshot processor restarting in 10')
				if item is not None:
					self.snapshotqueue.put(item)
				time.sleep(10)