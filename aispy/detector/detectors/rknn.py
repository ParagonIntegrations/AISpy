import logging
import os
import urllib
from typing import Literal

import cv2
import numpy as np
from pydantic import Field
from aispy.detector.detector_config import BaseDetectorConfig, BaseModelConfig
from aispy.detector.detector_api import DetectorAPI

try:
    from hide_warnings import hide_warnings # noqa
except:
    def hide_warnings(func):
        pass

logger = logging.getLogger(__name__)

DETECTOR_KEY = "rknn"

supported_socs = ["rk3562", "rk3566", "rk3568", "rk3588"]

yolov8_suffix = {
    "default-yolov8n": "n",
    "default-yolov8s": "s",
    "default-yolov8m": "m",
    "default-yolov8l": "l",
    "default-yolov8x": "x",
}


class RknnDetectorConfig(BaseDetectorConfig):
    type_key: Literal[DETECTOR_KEY]
    core_mask: int = Field(default=7, ge=0, le=7, title="Core mask for NPU.")


class Rknn(DetectorAPI):
    type_key = DETECTOR_KEY

    def __init__(self, config: RknnDetectorConfig):

        try:
            with open("/proc/device-tree/compatible") as file:
                soc = file.read().split(",")[-1].strip("\x00")
        except FileNotFoundError:
            logger.error("Make sure to run docker in privileged mode.")
            raise Exception("Make sure to run docker in privileged mode.")

        soc = 'rk3588'

        if soc not in supported_socs:
            logger.error(
                "Your SoC is not supported. Your SoC is: {}. Currently these SoCs are supported: {}.".format(
                    soc, supported_socs
                )
            )
            raise Exception(
                "Your SoC is not supported. Your SoC is: {}. Currently these SoCs are supported: {}.".format(
                    soc, supported_socs
                )
            )

        if not os.path.isfile("/usr/lib/librknnrt.so"):
            if "rk356" in soc:
                os.rename("/usr/lib/librknnrt_rk356x.so", "/usr/lib/librknnrt.so")
            elif "rk3588" in soc:
                os.rename("/usr/lib/librknnrt_rk3588.so", "/usr/lib/librknnrt.so")



        # Import this on class instantiation to avoid errors on other systems
        from rknnlite.api import RKNNLite
        self.core_mask = config.core_mask
        self.model_height = config.model.height
        self.model_width = config.model.width
        self.model_path = config.model.path or "default-yolov8n"

        if self.model_path in yolov8_suffix:
            if self.model_path == "default-yolov8n":
                self.model_path = f"/models/rknn/default-yolov8n-{soc}.rknn"
            else:
                model_suffix = yolov8_suffix[self.model_path]
                self.model_path = (
                    f"/config/model_cache/rknn/default-yolov8{model_suffix}-{soc}.rknn"
                )

                os.makedirs("/config/model_cache/rknn", exist_ok=True)
                if not os.path.isfile(self.model_path):
                    logger.info(
                        f"Downloading yolov8{model_suffix} model."
                    )
                    urllib.request.urlretrieve(
                        f"https://github.com/MarcA711/rknn-models/releases/download/v1.6.0-yolov8-default/default-yolov8{model_suffix}-{soc}.rknn",
                        self.model_path)

        self.detector = RKNNLite(verbose=False)
        if self.detector.load_rknn(self.model_path) != 0:
            logger.error("Error initializing rknn model.")
            pass
        if self.detector.init_runtime(core_mask=self.core_mask) != 0:
            logger.error(
                "Error initializing rknn runtime. Do you run docker in privileged mode?"
            )
            pass
    def __del__(self):
        self.detector.release()

    def preprocess(self, img):

        # Resize the image
        shape = img.shape[:2]  # current shape [height, width]
        new_shape = (self.model_height, self.model_width)
        r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])
        r = min(r, 1.0)
        new_unpad = int(round(shape[1] * r)), int(round(shape[0] * r))
        img = cv2.resize(img, new_shape, interpolation=cv2.INTER_LINEAR)
        img = [np.stack([img])]

        return img

    def inference(self, tensor_input):
        # print(f'{tensor_input[0].shape=}')
        return self.detector.inference(inputs=tensor_input)


    def postprocess(self, results):
        return self.process_yolov8(results)

    def process_yolov8(self, results):
        """
        Processes yolov8 output.

        Args:
        results: array with shape: (1, 84, n, 1) where n depends on yolov8 model size (for 320x320 model n=2100)

        Returns:
        detections: array with shape (20, 6) with 20 rows of (class, confidence, y_min, x_min, y_max, x_max)
        """
        results = np.array(results)
        results = np.transpose(results[0, 0, :, :])  # array shape (2100, 84)
        # results = results[0]
        # results = np.transpose(results[0, :, :, 0])  # array shape (2100, 84)
        scores = np.max(
            results[:, 4:], axis=1
        )  # array shape (2100,); max confidence of each row

        # remove lines with score scores < 0.4
        filtered_arg = np.argwhere(scores > 0.4)
        results = results[filtered_arg[:, 0]]
        scores = scores[filtered_arg[:, 0]]

        num_detections = len(scores)

        if num_detections == 0:
            return np.zeros((20, 6), np.float32)

        if num_detections > 20:
            top_arg = np.argpartition(scores, -20)[-20:]
            results = results[top_arg]
            scores = scores[top_arg]
            num_detections = 20

        classes = np.argmax(results[:, 4:], axis=1)

        boxes = np.transpose(
            np.vstack(
                (
                    (results[:, 0] - 0.5 * results[:, 2]) / self.model_width,
                    (results[:, 1] - 0.5 * results[:, 3]) / self.model_height,
                    (results[:, 0] + 0.5 * results[:, 2]) / self.model_width,
                    (results[:, 1] + 0.5 * results[:, 3]) / self.model_height,
                )
            )
        )

        detections = np.zeros((20, 6), np.float32)
        detections[:num_detections, 0] = classes
        detections[:num_detections, 1] = scores
        detections[:num_detections, 2:] = boxes
        return detections

    def detect(self, image):
        # print(f'Input {image.shape=}')
        pre_image = self.preprocess(image)
        inf_res = self.inference(pre_image)
        # print(f'{inf_res[0].shape=}')
        post_processes = self.postprocess(inf_res)
        # print(f'{post_processes.shape=}')
        return post_processes