import os

from settings_store import get_store
from utils import mainlogger
from memory_managers import SharedFrameDeque
from recorder import SegmentRecorder
from decoder import FfmpegDecoder
import multiprocessing as mp
import threading

class Stream(mp.Process):
	def __init__(self, id, stream_info):
		super().__init__()
		mainlogger.debug(f'Stream {id} initializing')
		self.streamid = id
		self.streaminfo = stream_info
		self.framebuffer: SharedFrameDeque = self.streaminfo['framebuffer']
		self.recorder = SegmentRecorder(id, stream_info)
		# Detection can run off the camera's substream, and ffmpeg scales whatever it
		# gets to detect_dimensions anyway, so this is only about how many pixels have
		# to be decoded.
		detect_url = self.streaminfo.get('detect_url') or self.streaminfo['url']
		# Frames arrive at this rate. The buffer only has to keep one recent frame
		# ready for the detector, which wakes every check_detection_time - it is not
		# the pre-record ring any more, so decoding at record_fps was work nothing
		# would ever look at.
		self.decoder = FfmpegDecoder(
			id, detect_url,
			self.streaminfo['detect_dimensions'],
			get_store().get('detect_fps'))

	def run(self):
		mainlogger.info(f'Stream {self.streamid} starting with pid {os.getpid()}')
		threading.Thread(target=self.recorder.run_ffmpeg, daemon=True).start()
		threading.Thread(target=self.recorder.collector, daemon=True).start()
		self.decoder.run(self.framebuffer.append)
