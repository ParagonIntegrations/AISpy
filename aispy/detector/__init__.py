
# from .detector_config import InputTensorEnum, ModelConfig, PixelFormatEnum
from .detector_types import DetectorConfig, DetectorTypeEnum, api_types

def create_detector(detector_configuration: DetectorConfig):
    # if detector_config.type == DetectorTypeEnum.cpu:
    #     logger.warning(
    #         "CPU detectors are not recommended and should only be used for testing or for trial purposes."
    #     )
    api = api_types.get(detector_configuration.type_key)
    if not api:
        raise ValueError(detector_configuration.type_key)
    return api(detector_configuration)