import copy
import logging
import os
import sys
import urllib
from typing import Literal

import cv2
import supervision as sv
import numpy as np
from pydantic import Field
from detector.detector_config import BaseDetectorConfig, BaseModelConfig
from detector.detector_api import DetectorAPI
from detector.utils.ops import xywh2xyxy, scale_boxes, LetterBox

try:
    from hide_warnings import hide_warnings  # noqa
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
        self.model_names = config.model.names

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
        if self.detector.init_runtime(core_mask=self.core_mask) != 0:
            logger.error(
                "Error initializing rknn runtime. Do you run docker in privileged mode?"
            )

    def __del__(self):
        self.detector.release()

    def preprocess(self, img):

        # Resize the image
        return [np.stack(self.pre_transform(img))]

        # shape = img.shape[:2]  # current shape [height, width]
        # new_shape = (self.model_height, self.model_width)
        # r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])
        # r = min(r, 1.0)
        # new_unpad = int(round(shape[1] * r)), int(round(shape[0] * r))
        # img = cv2.resize(img, new_shape, interpolation=cv2.INTER_LINEAR)
        # img = [np.stack([img])]
        # return img

    def pre_transform(self, img):
        same_shapes = False
        new_shape = (self.model_height, self.model_width)
        letterbox = LetterBox(new_shape, auto=same_shapes)
        return [letterbox(image=img)]

    def inference(self, tensor_input):
        inf_res = self.detector.inference(inputs=tensor_input)
        return inf_res

    def postprocess(self, inference_results, orig_image_size, classes, conf, nms, iou):
        res = self.process_yolov8(inference_results, orig_image_size, conf, classes)
        if nms:
            res = res.with_nms(threshold=iou)
        return res

    def process_yolov8(self, prediction, orig_image_size, conf_thres=0.25, classes=[0]):
        """
        Processes yolov8 output.

        Args:
        prediction: array with shape: (batch_size, num_classes + 4, num_boxes) the number of boxes depends on the model size
        and for a 320x320 model this will be 2100, a typical shape will be (1, 84, 2100) Only the first item of the batch
        will be used

        Returns:
        detection: a Supervision Detections object
        """

        # Checks
        assert 0 <= conf_thres <= 1, f"Invalid Confidence threshold {conf_thres}, valid values are between 0.0 and 1.0"
        if isinstance(prediction,
                      (list, tuple)):  # YOLOv8 model in validation model, output = (inference_out, loss_out)
            prediction = prediction[0]  # select only inference output

        num_classes = prediction.shape[1] - 4
        class_list = classes if classes else list(range(num_classes))

        prediction = np.transpose(prediction[0, :, :])  # Change to shape (2100, 84)
        conf_array = prediction[:, 4:].max(1)
        class_array = prediction[:, 4:].argmax(1)
        conf_filter = conf_array > conf_thres
        class_filter = np.isin(class_array, class_list)
        combined_filter = np.logical_and(conf_filter, class_filter)

        prediction = prediction[:, :6]
        prediction[:, 4] = conf_array
        prediction[:, 5] = class_array
        prediction = prediction[combined_filter, :]
        prediction[..., :4] = xywh2xyxy(prediction[..., :4])  # xywh to xyxy
        prediction[..., :4] = scale_boxes((self.model_height, self.model_width), prediction[..., :4], orig_image_size)

        return sv.Detections(
            xyxy=prediction[:, :4],
            confidence=prediction[:, 4],
            class_id=prediction[:, 5].astype(int)
        )

    def detect(self, image, classes=None, conf=0.2, nms=True, iou=0.5, verbose=True) -> sv.Detections:

        pre_image = self.preprocess(image)
        orig_image_size = image.shape[:2]
        inf_result = self.inference(pre_image)
        post_processes = self.postprocess(inf_result, orig_image_size, classes, conf, nms, iou)
        return post_processes
