import subprocess
import threading
import time

import numpy as np

from utils import mainlogger, optional_setting

# Tried in this order; the first that actually delivers a frame is kept for the life
# of the process. Falling back one rung at a time means a board whose rkrga filter
# chain is not what we expect still gets hardware decode, and a board with no MPP at
# all still gets pictures.
DECODE_VARIANTS = ('rkmpp_rga', 'rkmpp', 'software')


class FfmpegDecoder:
	"""Decode a camera stream into raw BGR frames at a fixed size and frame rate.

	On the RK3588 this puts the decode on the VPU and the scale and colour conversion
	on the RGA 2D engine, so a detection frame costs the CPU nothing but the pipe
	read. It replaces cv2.VideoCapture, which decoded in software and handed back
	full-size frames the detector then had to letterbox down to 320x320 anyway.

	Scaling in the filter chain also subsumes the frame.repeat(2, 1) that anamorphic
	('lite_aspect_ratio') streams used to need: the frames arrive already at the
	dimensions the detect area is expressed in.
	"""

	def __init__(self, streamid, url, dimensions, fps):
		self.streamid = streamid
		self.url = url
		self.width, self.height = int(dimensions[0]), int(dimensions[1])
		self.fps = fps
		self.framesize = self.width * self.height * 3
		self.ffmpeg = optional_setting('ffmpeg_path', 'ffmpeg')
		self.variants = list(optional_setting('detect_decode_variants', DECODE_VARIANTS))
		self.variant = None

	def cmd(self, variant) -> list:
		args = [self.ffmpeg, '-hide_banner', '-loglevel', 'warning', '-nostdin']
		if variant == 'rkmpp_rga':
			# Frames stay in DRM prime buffers from the decoder through the scaler and
			# are only copied to system memory at the very end.
			args += ['-hwaccel', 'rkmpp', '-hwaccel_output_format', 'drm_prime']
			videofilter = (f'fps={self.fps},'
						   f'scale_rkrga=w={self.width}:h={self.height}:format=bgr24,'
						   f'hwdownload,format=bgr24')
		elif variant == 'rkmpp':
			# VPU decode, but let ffmpeg hand back software frames and scale them on
			# the CPU. Slower than the RGA path, still far cheaper than decoding here.
			args += ['-hwaccel', 'rkmpp']
			videofilter = f'fps={self.fps},scale={self.width}:{self.height}'
		else:
			videofilter = f'fps={self.fps},scale={self.width}:{self.height}'
		args += ['-rtsp_transport', 'tcp', '-i', self.url,
				 '-an', '-vf', videofilter,
				 '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1']
		return args

	def log_stderr(self, process):
		"""Drain ffmpeg's stderr; it blocks once the pipe fills."""
		for line in process.stderr:
			mainlogger.warning(
				f'Stream {self.streamid} decoder: {line.decode(errors="replace").strip()}')

	def read_frame(self, stream) -> bytes | None:
		"""Read exactly one frame's worth of bytes, or None at end of stream."""
		chunks = []
		remaining = self.framesize
		while remaining:
			chunk = stream.read(remaining)
			if not chunk:
				return None
			chunks.append(chunk)
			remaining -= len(chunk)
		return chunks[0] if len(chunks) == 1 else b''.join(chunks)

	def decode(self, variant, sink) -> bool:
		"""Run one ffmpeg until it stops. True if it ever delivered a frame."""
		delivered = False
		process = None
		try:
			cmd = self.cmd(variant)
			mainlogger.debug(f'Stream {self.streamid} decoder: {" ".join(cmd)}')
			process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
			threading.Thread(target=self.log_stderr, args=(process,), daemon=True).start()
			while True:
				buffer = self.read_frame(process.stdout)
				if buffer is None:
					break
				if not delivered:
					delivered = True
					self.variant = variant
					mainlogger.info(f'Stream {self.streamid} decoding with {variant}')
				sink(np.frombuffer(buffer, dtype=np.uint8).reshape(self.height, self.width, 3))
		except Exception:
			mainlogger.exception(f'Decoder failed on stream {self.streamid}')
		finally:
			if process is not None:
				process.kill()
				process.wait()
		return delivered

	def run(self, sink):
		"""Decode forever, handing every frame to `sink`.

		Once a variant has proved itself it is the only one retried, so a camera that
		drops out overnight does not walk back down the ladder onto software decode.
		"""
		mainlogger.info(f'Decoder starting for stream {self.streamid}')
		while True:
			for variant in ([self.variant] if self.variant else self.variants):
				if self.decode(variant, sink):
					break
				mainlogger.warning(
					f'Stream {self.streamid} delivered no frames with {variant} decode')
			time.sleep(10)
