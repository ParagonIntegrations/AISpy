import subprocess
import time
from datetime import datetime, timedelta

from settings import UserSettings, Settings
from utils import mainlogger, optional_setting

# Segment files are named for their wall-clock start. The pre-record and retention
# windows are worked out from those names, so they have to stay parseable.
SEGMENT_NAME_FORMAT = '%Y%m%d_%H%M%S'

# Bitstream filter that can rewrite a stream's pixel aspect ratio without re-encoding
METADATA_BSF = {'h264': 'h264_metadata', 'hevc': 'hevc_metadata'}


class SegmentRecorder:
	"""Record a camera by remuxing its stream to disk instead of re-encoding it.

	ffmpeg reads the RTSP stream and writes it out in short segments with '-c copy',
	so no frame is ever decoded or encoded. Recording costs practically no CPU and
	keeps the camera's own quality instead of running everything through a software
	mp4v encode. The rolling directory of segments doubles as the pre-record buffer,
	replacing the SharedFrameDeque ring that used to hold pre_record_time *
	record_fps raw frames in shared memory for every camera.

	An event is the span where the detector holds recordflag at 1. When it clears,
	the segments covering [event start - pre_record_time, event end] are concatenated
	into a single clip - '-c copy' again - and handed to the FileAnnotator.
	"""

	def __init__(self, streamid, streaminfo, fileannotatorqueue):
		self.streamid = streamid
		self.streaminfo = streaminfo
		self.fileannotatorqueue = fileannotatorqueue
		self.ffmpeg = optional_setting('ffmpeg_path', 'ffmpeg')
		self.ffprobe = optional_setting('ffprobe_path', 'ffprobe')
		# Requested segment length. '-c copy' can only cut on a keyframe, so a segment
		# really lasts this long or one camera GOP, whichever is longer.
		self.segment_time = int(optional_setting('segment_time', 2))
		# Cache held beyond the pre-record window, so nothing is pruned in the gap
		# between the detector raising recordflag and the collector noticing.
		self.retention_margin = timedelta(
			seconds=int(optional_setting('segment_retention_margin', 30)))
		self.cachedir = optional_setting(
			'cachedir', Settings.videodir.parent.joinpath('cache')).joinpath(str(streamid))
		self.recorddir = Settings.videodir.joinpath(str(streamid))
		self.codec = None

	# -- the ffmpeg segmenter ------------------------------------------------

	def ffmpeg_cmd(self) -> list:
		return [
			self.ffmpeg, '-hide_banner', '-loglevel', 'warning', '-nostdin',
			'-rtsp_transport', 'tcp',
			# Cameras with sloppy PTS otherwise drift the segments away from wall
			# clock, which is what the pre-record window is measured against.
			'-use_wallclock_as_timestamps', '1',
			'-i', self.streaminfo['url'],
			'-an', '-c', 'copy',
			'-f', 'segment',
			'-segment_time', str(self.segment_time),
			'-segment_format', 'mp4',
			'-reset_timestamps', '1',
			'-strftime', '1',
			str(self.cachedir.joinpath(f'{SEGMENT_NAME_FORMAT}.mp4')),
		]

	def run_ffmpeg(self):
		"""Keep the segmenter alive for the life of the process."""
		mainlogger.info(f'Segment recorder starting for stream {self.streamid}')
		while True:
			try:
				self.cachedir.mkdir(parents=True, exist_ok=True)
				cmd = self.ffmpeg_cmd()
				mainlogger.debug(f'Stream {self.streamid} recorder: {" ".join(cmd)}')
				process = subprocess.Popen(
					cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
				# ffmpeg blocks once the stderr pipe fills, so it has to be drained
				# for as long as it runs.
				for line in process.stderr:
					mainlogger.warning(
						f'Stream {self.streamid} ffmpeg: {line.decode(errors="replace").strip()}')
				returncode = process.wait()
				mainlogger.warning(
					f'Stream {self.streamid} segment recorder exited with '
					f'{returncode}, restarting in 10 seconds')
			except Exception:
				mainlogger.exception(
					f'Segment recorder failed on stream {self.streamid}, restarting in 10 seconds')
			time.sleep(10)

	# -- the segment cache ---------------------------------------------------

	def segments(self) -> list:
		"""Every cached segment as (start time, path), oldest first."""
		found = []
		if not self.cachedir.is_dir():
			return found
		for path in self.cachedir.glob('*.mp4'):
			try:
				start = datetime.strptime(path.stem, SEGMENT_NAME_FORMAT)
			except ValueError:
				continue
			found.append((start, path))
		found.sort()
		return found

	def segments_covering(self, segments: list, since: datetime) -> list:
		"""The paths needed to cover from `since` up to the newest segment.

		A segment covers from its own start until the next one's start, so the one
		to begin at is the last that starts at or before `since`. Starting at the
		first segment after `since` would clip the front off the pre-record window.
		"""
		start_index = 0
		for index, (start, _) in enumerate(segments):
			if start <= since:
				start_index = index
		return [path for _, path in segments[start_index:]]

	def prune(self, keep: set):
		"""Drop segments that are past the retention window and not part of a clip."""
		cutoff = datetime.now() - UserSettings.pre_record_time - self.retention_margin
		# The newest segment is the one ffmpeg is still writing, so it is never ours
		# to delete.
		for start, path in self.segments()[:-1]:
			if path in keep or start >= cutoff:
				continue
			try:
				path.unlink()
			except OSError:
				mainlogger.debug(f'Could not prune segment {path}')

	# -- turning segments into clips -----------------------------------------

	def video_codec(self, path) -> str | None:
		"""Probe the recorded codec once, so the aspect ratio can be tagged."""
		if self.codec is None:
			try:
				result = subprocess.run(
					[self.ffprobe, '-v', 'error', '-select_streams', 'v:0',
					 '-show_entries', 'stream=codec_name',
					 '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
					capture_output=True, timeout=30)
				self.codec = result.stdout.decode(errors='replace').strip() or None
			except Exception:
				mainlogger.exception(f'Could not probe codec for stream {self.streamid}')
		return self.codec

	def aspect_args(self, sample_path) -> list:
		"""Tag anamorphic streams with their display aspect, without re-encoding.

		The capture side widens these frames with frame.repeat(2, 1) before the
		detector sees them. A remuxed clip keeps the camera's own narrow frames, so
		the 2:1 pixel aspect is written into the SPS instead and players undo it.
		"""
		if not self.streaminfo.get('lite_aspect_ratio'):
			return []
		codec = self.video_codec(sample_path)
		bsf = METADATA_BSF.get(codec)
		if bsf is None:
			mainlogger.warning(
				f'Stream {self.streamid} is {codec}: cannot tag its pixel aspect '
				f'without re-encoding, clips will look squashed')
			return []
		return ['-bsf:v', f'{bsf}=sample_aspect_ratio=2/1']

	def assemble(self, paths: list, clip_start: datetime) -> str | None:
		"""Concatenate segments into one clip without touching the frames."""
		self.recorddir.mkdir(parents=True, exist_ok=True)
		stamp = clip_start.strftime(SEGMENT_NAME_FORMAT)
		outfilename = str(self.recorddir.joinpath(f'{stamp}.mp4'))
		listfile = self.cachedir.joinpath(f'concat_{stamp}.txt')
		listfile.write_text(''.join(f"file '{path.as_posix()}'\n" for path in paths))
		cmd = [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
			   '-f', 'concat', '-safe', '0', '-i', str(listfile),
			   '-c', 'copy', '-movflags', '+faststart']
		cmd += self.aspect_args(paths[0])
		cmd.append(outfilename)
		try:
			result = subprocess.run(cmd, capture_output=True, timeout=300)
		except Exception:
			mainlogger.exception(f'Could not assemble clip for stream {self.streamid}')
			return None
		finally:
			try:
				listfile.unlink()
			except OSError:
				pass
		if result.returncode != 0:
			mainlogger.error(
				f'Assembling {outfilename} failed: '
				f'{result.stderr.decode(errors="replace").strip()}')
			return None
		return outfilename

	def flush(self, paths: list, clip_start: datetime):
		if not paths:
			mainlogger.warning(
				f'Recording segment on {self.streamid} had no complete segments to keep')
			return
		outfilename = self.assemble(paths, clip_start)
		if outfilename is not None:
			mainlogger.info(
				f'Recording segment on {self.streamid} done, '
				f'{len(paths)} segments -> {outfilename}')
			self.fileannotatorqueue.put((self.streamid, outfilename))

	# -- the event collector -------------------------------------------------

	def tail_complete(self, complete: list, stop_time: datetime) -> bool:
		"""Has the segment that was being written at `stop_time` been closed yet?

		It has once a completed segment starts at or after that instant. Waiting for it
		is what stops a clip ending a segment short of the event. Bounded by a timeout so
		a stalled segmenter cannot hold an event open indefinitely.
		"""
		if complete and complete[-1][0] >= stop_time:
			return True
		return datetime.now() - stop_time > timedelta(seconds=2 * self.segment_time + 10)

	def collector(self, interval=0.5):
		"""Watch recordflag and turn each event into a clip.

		The state machine is driven off what is on disk rather than off frames arriving,
		so an ffmpeg restart costs at most the segment it was part way through.
		"""
		mainlogger.info(f'Recorder collector started for {self.streamid}')
		recording = False
		clip_start = None
		clip_from = None
		draining_since = None
		kept = []
		while True:
			try:
				flag = self.streaminfo['recordflag'].value == 1
				segments = self.segments()
				# ffmpeg is still writing the newest segment: it has no moov atom yet and
				# concat would reject it.
				complete = segments[:-1]

				if flag and not recording:
					recording = True
					clip_start = datetime.now()
					clip_from = clip_start - UserSettings.pre_record_time
					draining_since = None
					mainlogger.info(f'Recording on {self.streamid} started')
				elif flag and draining_since is not None:
					# Detected again before the tail closed, so this stays one clip.
					draining_since = None

				if recording:
					# Recomputed every pass rather than accumulated: a segment that was
					# still being written when the detector fired is neither pre-roll nor
					# "started after the trigger", and would otherwise fall through the gap.
					kept = self.segments_covering(complete, clip_from)

					if not flag and draining_since is None:
						draining_since = datetime.now()

					if draining_since is not None and self.tail_complete(complete, draining_since):
						recording = False
						mainlogger.info(f'Recording on {self.streamid} done')
						self.flush(kept, clip_start)
						clip_start = clip_from = draining_since = None
						kept = []
					elif datetime.now() - clip_start >= UserSettings.max_clip_length:
						self.flush(kept, clip_start)
						# Everything flushed is free to prune. The segment ffmpeg is part way
						# through opens the next clip, so nothing is dropped or duplicated
						# across the boundary.
						clip_start = datetime.now()
						clip_from = segments[-1][0] if segments else clip_start
						kept = []

				self.prune(keep=set(kept))
			except Exception:
				mainlogger.exception(
					f'Recorder collector failed on stream {self.streamid}, restarting in 10 seconds')
				recording, clip_start, clip_from, draining_since, kept = False, None, None, None, []
				time.sleep(10)
			time.sleep(interval)
