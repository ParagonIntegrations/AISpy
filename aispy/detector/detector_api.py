from abc import ABC, abstractmethod
import supervision as sv

class DetectorAPI(ABC):
    type_key: str

    @abstractmethod
    def __init__(self, detector_config):
        pass

    @abstractmethod
    def detect(self, image, classes=None, conf=0.2, nms=True, iou=0.5, verbose=True) -> sv.Detections:
        pass