import multiprocessing as mp
from datetime import datetime

import numpy as np
import imutils
from memory_managers import SharedFrameDeque
import supervision as sv
import cv2


class MotionDetector:
	def __init__(self, streaminfo):
		self.streaminfo = streaminfo
		self.avg_frame = np.zeros((streaminfo['dimensions'][1], streaminfo['dimensions'][0]), np.float32)
		self.calibrating = True
		self.blur_radius = 1
		self.mask = np.zeros((streaminfo['dimensions'][1], streaminfo['dimensions'][0]), bool)
		self.motion_threshold = 30
		self.contour_area = 10
		self.lightning_threshold = 0.8
		self.motion_frame_count = 0
		self.motion_frames_conf = 5
		self.frame_alpha = 0.01

	def detect(self, frame) -> sv.Detections:
		grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
		# Improve contrast
		contrast_improved_frame = grey

		# Mask the frame
		contrast_improved_frame[self.mask] = [0]

		# Add some blur
		blurred_frame = cv2.GaussianBlur(contrast_improved_frame, (21, 21), 0)

		# compare to average
		frameDelta = cv2.absdiff(blurred_frame, cv2.convertScaleAbs(self.avg_frame))

		# compute the threshold image for the current frame
		thresh = cv2.threshold(
			frameDelta, self.motion_threshold, 255, cv2.THRESH_BINARY
		)[1]

		# dilate the thresholded image to fill in holes, then find contours
		# on thresholded image
		thresh_dilated = cv2.dilate(thresh, None, iterations=1)
		cnts = cv2.findContours(
			thresh_dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
		)
		cnts = imutils.grab_contours(cnts)

		motion_boxes = np.zeros(shape=(len(cnts), 4), dtype=np.uint32)
		motion_boxes_mask = np.zeros(shape=(len(cnts),), dtype=bool)
		# loop over the contours
		total_contour_area = 0
		for i in range(len(cnts)):
			c = cnts[i]
			# if the contour is big enough, count it as motion
			contour_area = cv2.contourArea(c)
			total_contour_area += contour_area
			if contour_area > self.contour_area:
				motion_boxes_mask[i] = True
				x, y, w, h = cv2.boundingRect(c)
				motion_boxes[i] = [int(x), int(y), int((x + w)), int((y + h))]

		motion_boxes = motion_boxes[motion_boxes_mask]
		pct_motion = total_contour_area / (streaminfo['dimensions'][1]* streaminfo['dimensions'][0])

		# once the motion is less than 5% and the number of contours is < 4, assume its calibrated
		if pct_motion < 0.05 and len(motion_boxes) <= 4:
			self.calibrating = False

		# if calibrating or the motion contours are > 80% of the image area (lightning, ir, ptz) recalibrate
		if self.calibrating or pct_motion > self.lightning_threshold:
			self.calibrating = True

		if motion_boxes.shape[0] > 0:
			self.motion_frame_count += 1
			if self.motion_frame_count >= self.motion_frames_conf:
				# only average in the current frame if the difference persists for a bit
				cv2.accumulateWeighted(
					blurred_frame,
					self.avg_frame,
					0.2 if self.calibrating else self.frame_alpha,
				)
		else:
			# when no motion, just keep averaging the frames together
			cv2.accumulateWeighted(
				blurred_frame,
				self.avg_frame,
				0.2 if self.calibrating else self.frame_alpha,
			)
			self.motion_frame_count = 0

		return sv.Detections(
			xyxy=motion_boxes
		)

	def run(self):
		while True:
			start = datetime.now()
			# Get new frame
			frame = self.streaminfo['framebuffer'][-1]
			detections = self.detect(frame)
			self.streaminfo['motionlist'][0] = detections
			end = datetime.now()
			delta = (end - start).total_seconds()
			print(f'Detection took {delta} seconds')




if __name__ == '__main__':
	shape = (1080, 1920, 3)
	type = np.uint8
	img = np.full(shape=(1080, 1920, 3), fill_value=128, dtype=type)
	motionman = mp.Manager()
	streaminfo = {}
	streaminfo['framebuffer'] = SharedFrameDeque(
				max_items=10,
				itemshape=shape,
				datatype=type
			)
	streaminfo['framebuffer'].append(img)
	streaminfo['motionlist'] = motionman.list()
	streaminfo['motionlist'].append(None)

	md = MotionDetector(streaminfo)
	md.run()