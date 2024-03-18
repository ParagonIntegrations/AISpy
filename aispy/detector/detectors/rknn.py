from typing import Literal
from pydantic import Field
from aispy.detector.detector_config import BaseDetectorConfig, BaseModelConfig
from aispy.detector.detector_api import DetectorAPI

try:
    from hide_warnings import hide_warnings
except:  # noqa
    def hide_warnings(func):
        pass



DETECTOR_KEY = "rknn"


class RknnDetectorConfig(BaseDetectorConfig):
    type: Literal[DETECTOR_KEY]
    core_mask: int = Field(default=0, ge=0, le=7, title="Core mask for NPU.")


class Rknn(DetectorAPI):
    type_key = DETECTOR_KEY

    def __init__(self, config: RknnDetectorConfig):
        # Import this on class instantiation to avoid errors on other systems
        from rknnlite.api import RKNNLite
        self.core_mask = config.core_mask
        self.model_height = config.model.height
        self.model_width = config.model.width
        self.model_path = config.model.path

        self.detector = RKNNLite(verbose=False)
        if self.detector.load_rknn(self.model_path) != 0:
            # logger.error("Error initializing rknn model.")
            pass
        if self.detector.init_runtime(core_mask=self.core_mask) != 0:
            # logger.error(
            #     "Error initializing rknn runtime. Do you run docker in privileged mode?"
            # )
            pass
    def __del__(self):
        self.detector.release()

    def postprocess(self, results):
        print(f'{results=}')
        return results

    @hide_warnings
    def inference(self, tensor_input):
        return self.detector.inference(inputs=tensor_input)

    def detect(self, tensor_input):
        output = self.inference(
            [
                tensor_input,
            ]
        )
        return self.postprocess(output[0])