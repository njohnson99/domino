from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Union

import meerkat as mk
import torch.nn as nn


class Slicer(ABC):
    @dataclass
    class Config:
        pass

    RESOURCES_REQUIRED = {"cpu": 1, "custom_resources": {"ram_gb": 4}}

    def __init__(self, n_slices: int):

        self.config = self.Config()
        self.config.n_slices = n_slices

    @abstractmethod
    def fit(
        self,
        model: nn.Module = None,
        data_dp: mk.DataPanel = None,
    ) -> Slicer:
        raise NotImplementedError()

    @abstractmethod
    def transform(self, data_dp: mk.DataPanel) -> mk.DataPanel:
        raise NotImplementedError()

    def to(self, device: Union[str, int]):

        if device != "cpu":
            raise ValueError(f"Slicer of type {type(self)} does not support GPU.")
        # by default this is a no-op, but subclasses can override