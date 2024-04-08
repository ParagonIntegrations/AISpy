import os
import time
from datetime import datetime
from settings import UserSettings, Settings
from utils import mainlogger
from memory_managers import SharedFrameDeque
import cv2
import multiprocessing as mp
import threading

class Stream(mp.Process):
	def __init__(self, id, stream_info, fileannotatorqueue):
		super().__init__()
		mainlogger.debug(f'Stream {id} initializing')
		self.streamid = id
		self.streaminfo = stream_info
		self.fileannotatorqueue = fileannotatorqueue
		self.framebuffer: SharedFrameDeque = self.streaminfo['framebuffer']
		self.video = None
		self.recorddir = Settings.videodir.joinpath(str(self.streamid))
		self.recorddir.mkdir(parents=True, exist_ok=True)
		self.out: cv2.VideoWriter | None = None

	def run(self):
		mainlogger.info(f'Stream {self.streamid} starting with pid {os.getpid()}')
		recorder_worker = threading.Thread(target=self.recorder)
		recorder_worker.start()
		while True:
			try:
				mainlogger.info(f'Starting capture on stream {self.streamid}')
				self.video = cv2.VideoCapture(self.streaminfo['url'])
				now = datetime.now().timestamp()
				missed_frames = 0
				while True:
					check, frame = self.video.read()
					if self.streaminfo['lite_aspect_ratio']:
						frame = frame.repeat(2,1)
					if check == False:
						mainlogger.warning('Video Not Found. Please Enter a Valid Path (Full path of Video Should be Provided).')
						return
					# Place the correct number of frames on the buffer
					prev = now
					now = datetime.now().timestamp()
					dt = now-prev
					missed_frames += dt/(1/UserSettings.record_fps)
					frames_to_place = int(missed_frames//1)
					missed_frames -= frames_to_place
					for i in range(frames_to_place):
						self.framebuffer.append(frame)
			except:
				mainlogger.warning(f'Exception on stream {self.streamid} restarting in 10 seconds')
				time.sleep(10)

	def recorder(self):
		mainlogger.info(f'Recorder thread started for {self.streamid}')
		recording = False
		while True:
			try:
				while True:
					while self.streaminfo['recordflag'].value == 1:
						if len(self.framebuffer) > 0:
							frame = self.framebuffer.popleft()
						else:
							time.sleep(2)
							continue
						# Init the recording if it is not yet
						if self.out is None:
							recording = True
							mainlogger.info(f'Recording on {self.streamid} started')
							now = datetime.now()
							filename = str(self.recorddir.joinpath(f'{now.strftime("%Y%m%d_%H%M%S")}.mp4'))
							fourcc = cv2.VideoWriter_fourcc(*'mp4v')
							# fourcc = cv2.VideoWriter_fourcc(*'H264')
							self.out = cv2.VideoWriter(filename, fourcc, UserSettings.record_fps, self.streaminfo['dimensions'])
						self.out.write(frame)
						if datetime.now() >= now + UserSettings.max_clip_length:
							recording = False
							mainlogger.info(f'Recording segment on {self.streamid} done')
							if self.out is not None:
								self.out.release()
								self.out = None
							self.fileannotatorqueue.put((self.streamid, filename))
					if recording:
						recording = False
						mainlogger.info(f'Recording on {self.streamid} done')
						if self.out is not None:
							self.out.release()
							self.out = None
						self.fileannotatorqueue.put((self.streamid, filename))
					time.sleep(2)
			except:
				mainlogger.warning(f'Error on stream {self.streamid} recorder restarting in 10')
				time.sleep(10)

