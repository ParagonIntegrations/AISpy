from abc import ABC, abstractmethod
import supervision as sv

class DetectorAPI(ABC):
    type_key: str
    # Classes the model preferred to the one it was allowed to give a box, from the most
    # recent detect(). Empty on a backend that does not filter by class at all; the point
    # of it is that a class list narrower than the model's is the one filter whose losses
    # leave no box behind to notice them by.
    overridden: dict = {}

    @abstractmethod
    def __init__(self, detector_config):
        pass

    @abstractmethod
    def detect(self, image, classes=None, conf=0.2, nms=True, iou=0.5, verbose=True) -> sv.Detections:
        pass