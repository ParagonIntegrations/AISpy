import datetime
import logging
from pathlib import Path
import numpy as np

def streaminfodict(	url: str,
				dimensions: tuple[int, int],
				detect: bool = True,
				detection_classes: int | list = 0,
				confidence_threshold: float = 0.7,
				record: bool = True,
				lite_aspect_ratio: bool = True,
				detectarea = np.array([
					[0,0],
					[1,0],
					[1,1],
					[0,1]])
				):
	return {
		'url': url,
		'dimensions': dimensions,
		'lite_aspect_ratio': lite_aspect_ratio,
		'detect': detect,
		'detection_classes': detection_classes,
		'confidence_threshold': confidence_threshold,
		'record': record,
		'detectarea': (detectarea * np.array([dimensions[0], dimensions[1]])).astype(int),
		'recordcounter': 0,
		'armed': 1,
	}


class Settings:
	base_dir = Path(__file__).resolve().parents[1]
	datadir =base_dir.joinpath('data')
	videodir = datadir.joinpath('media', 'videos')
	annotatedvideodir = datadir.joinpath('media', 'annotatedvideos')
	snapshot_dir = datadir.joinpath('media', 'snapshots')
	avg_inference_time = 0.02
	dbdir = datadir.joinpath('db')
	dbdir.mkdir(parents=True, exist_ok=True)
	db_file = dbdir.joinpath('FractalDB')
	log_dir = base_dir.joinpath('data','log')
	log_dir.mkdir(parents=True, exist_ok=True)
	log_name = str(log_dir.joinpath('Log.txt'))
	log_maxbytes = 10485760
	log_maxnum = 5
	file_loglevel = logging.WARNING
	console_loglevel = logging.INFO
	telegram_loglevel = logging.CRITICAL
	telegram_token = '6041241784:AAF0Tzx-2zJj6mMewDzwe0zlVUY2S1kiutk'
	fractal_token = '6902335739:AAHJ8oPdOnysjj8ic2rbLleZ968POjBmm3k'
	telegram_chat_id = '1769119635'
	detector_model_path = datadir.joinpath('yolov8m.pt')
	telegram_adminlist = ['1769119635']
	telegram_userlist = ['1769119635']


class UserSettings:
	streaminfo = {
		0: {'armed': True},
		# 1: streaminfodict('rtsp://admin:JKEPZZ@192.168.1.105/', (1920,1080), True, 0, 0.3, True, False),
		1: streaminfodict('rtsp://fractal:Nelis342256@192.168.1.110/Streaming/Channels/101', (1920, 1088), True, 0, 0.45, True),
		2: streaminfodict('rtsp://fractal:Nelis342256@192.168.1.110/Streaming/Channels/201', (1920, 1088), True, 0, 0.45, True),
		3: streaminfodict('rtsp://fractal:Nelis342256@192.168.1.110/Streaming/Channels/301', (1920, 1088), True, 0, 0.45,
						  True),
		4: streaminfodict('rtsp://fractal:Nelis342256@192.168.1.110/Streaming/Channels/401', (1920, 1088), True, 0, 0.45,
						  True),
		5: streaminfodict('rtsp://fractal:Nelis342256@192.168.1.110/Streaming/Channels/501', (1920, 1088), True, 0, 0.45,
						  True),
		6: streaminfodict('rtsp://fractal:Nelis342256@192.168.1.110/Streaming/Channels/601', (1920, 1088), True, 0, 0.45,
						  True),
		7: streaminfodict('rtsp://fractal:Nelis342256@192.168.1.110/Streaming/Channels/701', (1920, 1088), True, 0, 0.45,
						  True),
	}
	pre_record_time = datetime.timedelta(seconds=5)
	check_detection_time = datetime.timedelta(milliseconds=1000)
	max_clip_length = datetime.timedelta(minutes=30)
	record_fps = 30
	detections_for_event = 5
