from abc import ABC, abstractmethod


class DetectorAPI(ABC):
    type_key: str

    @abstractmethod
    def __init__(self, detector_config):
        pass

    @abstractmethod
    def detect(self, tensor_input):
        pass