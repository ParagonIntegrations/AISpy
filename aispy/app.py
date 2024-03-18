from datetime import datetime
from aispy.detector import create_detector
from aispy.detector.detectors.rknn import RknnDetectorConfig, RknnDetectorConfig


class AISpyApp:

	def __init__(self):
		pass

	def run(self):
		print(f'Starting app at {datetime.now()}')
		d_cfg = RknnDetectorConfig(type='rknn')
		detector = create_detector(d_cfg)

	def start(self):
		self.run()
