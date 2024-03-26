from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict

class PixelFormatEnum(str, Enum):
    rgb = "rgb"
    bgr = "bgr"
    yuv = "yuv"


class InputTensorEnum(str, Enum):
    nchw = "nchw"
    nhwc = "nhwc"


class ModelTypeEnum(str, Enum):
    yolov8 = "yolov8"

class BaseModelConfig(BaseModel):
    path: Optional[str] = Field(None, title="Custom Object detection model path.")
    width: int = Field(default=320, title="Object detection model input width.")
    height: int = Field(default=320, title="Object detection model input height.")
    detection_model_type: ModelTypeEnum = Field(
        default=ModelTypeEnum.yolov8, title="Object Detection Model Type"
    )


class BaseDetectorConfig(BaseModel):
    # the type field must be defined in all subclasses
    type_key: str = Field(default="rknn", title="Detector Type")
    model: BaseModelConfig = Field(
        default=BaseModelConfig(), title="Detector specific model configuration."
    )
    model_config = ConfigDict(
        extra="allow", arbitrary_types_allowed=True, protected_namespaces=()
    )