# coding=utf-8
# Copyright 2022 the HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import random
import subprocess
import time
from typing import Any, Dict

import numpy as np
import torch
from habana_frameworks.torch.hpu import memory_stats
from habana_frameworks.torch.hpu import random as hpu_random
from packaging import version
from transformers.utils import is_torch_available

from optimum.utils import logging

from .version import __version__


logger = logging.get_logger(__name__)


CURRENTLY_VALIDATED_SYNAPSE_VERSION = version.parse("1.9.0")


def to_device_dtype(my_input: Any, target_device: torch.device = None, target_dtype: torch.dtype = None):
    """
    Move a state_dict to the target device and convert it into target_dtype.

    Args:
        my_input : input to transform
        target_device (torch.device, optional): target_device to move the input on. Defaults to None.
        target_dtype (torch.dtype, optional): target dtype to convert the input into. Defaults to None.

    Returns:
        : transformed input
    """
    if isinstance(my_input, torch.Tensor):
        if target_device is None:
            target_device = my_input.device
        if target_dtype is None:
            target_dtype = my_input.dtype
        return my_input.to(device=target_device, dtype=target_dtype)
    elif isinstance(my_input, list):
        return [to_device_dtype(i, target_device, target_dtype) for i in my_input]
    elif isinstance(my_input, tuple):
        return tuple(to_device_dtype(i, target_device, target_dtype) for i in my_input)
    elif isinstance(my_input, dict):
        return {k: to_device_dtype(v, target_device, target_dtype) for k, v in my_input.items()}
    else:
        return my_input


def speed_metrics(
    split: str,
    start_time: float,
    num_samples: int = None,
    num_steps: int = None,
    start_time_after_warmup: float = None,
) -> Dict[str, float]:
    """
    Measure and return speed performance metrics.
    This function requires a time snapshot `start_time` before the operation to be measured starts and this function
    should be run immediately after the operation to be measured has completed.

    Args:
        split (str): name to prefix metric (like train, eval, test...)
        start_time (float): operation start time
        num_samples (int, optional): number of samples processed. Defaults to None.
        num_steps (int, optional): number of steps performed. Defaults to None.
        start_time_after_warmup (float, optional): time after warmup steps have been performed. Defaults to None.

    Returns:
        Dict[str, float]: dictionary with performance metrics.
    """

    runtime = time.time() - start_time
    result = {f"{split}_runtime": round(runtime, 4)}

    # Adjust runtime if there were warmup steps
    if start_time_after_warmup is not None:
        runtime = runtime + start_time - start_time_after_warmup

    # Compute throughputs
    if num_samples is not None:
        samples_per_second = num_samples / runtime
        result[f"{split}_samples_per_second"] = round(samples_per_second, 3)
    if num_steps is not None:
        steps_per_second = num_steps / runtime
        result[f"{split}_steps_per_second"] = round(steps_per_second, 3)

    return result


def to_gb_rounded(mem: float) -> float:
    """
    Rounds and converts to GB.

    Args:
        mem (float): memory in bytes

    Returns:
        float: memory in GB rounded to the second decimal
    """
    return np.round(mem / 1024**3, 2)


def get_hpu_memory_stats() -> Dict[str, float]:
    """
    Returns memory stats of HPU as a dictionary:
    - current memory allocated (GB)
    - maximum memory allocated (GB)
    - total memory available (GB)

    Returns:
        Dict[str, float]: memory stats.
    """
    mem_stats = memory_stats()

    mem_dict = {
        "memory_allocated (GB)": to_gb_rounded(mem_stats["InUse"]),
        "max_memory_allocated (GB)": to_gb_rounded(mem_stats["MaxInUse"]),
        "total_memory_available (GB)": to_gb_rounded(mem_stats["Limit"]),
    }

    return mem_dict


def set_seed(seed: int):
    """
    Helper function for reproducible behavior to set the seed in `random`, `numpy` and `torch`.
    Args:
        seed (`int`): The seed to set.
    """
    random.seed(seed)
    np.random.seed(seed)
    if is_torch_available():
        torch.manual_seed(seed)
        hpu_random.manual_seed_all(seed)


def check_synapse_version():
    """
    Checks whether the versions of SynapseAI and drivers have been validated for the current version of Optimum Habana.
    """
    # Change the logging format
    logging.enable_default_handler()
    logging.enable_explicit_format()

    # Check the version of habana_frameworks
    habana_frameworks_version_number = get_habana_frameworks_version()
    if (
        habana_frameworks_version_number.major != CURRENTLY_VALIDATED_SYNAPSE_VERSION.major
        or habana_frameworks_version_number.minor != CURRENTLY_VALIDATED_SYNAPSE_VERSION.minor
    ):
        logger.warning(
            f"optimum-habana v{__version__} has been validated for SynapseAI v{CURRENTLY_VALIDATED_SYNAPSE_VERSION} but habana-frameworks v{habana_frameworks_version_number} was found, this could lead to undefined behavior!"
        )

    # Check driver version
    driver_version = get_driver_version()
    # This check is needed to make sure an error is not raised while building the documentation
    # Because the doc is built on an instance that does not have `hl-smi`
    if driver_version is not None:
        if (
            driver_version.major != CURRENTLY_VALIDATED_SYNAPSE_VERSION.major
            or driver_version.minor != CURRENTLY_VALIDATED_SYNAPSE_VERSION.minor
        ):
            logger.warning(
                f"optimum-habana v{__version__} has been validated for SynapseAI v{CURRENTLY_VALIDATED_SYNAPSE_VERSION} but the driver version is v{driver_version}, this could lead to undefined behavior!"
            )
    else:
        logger.warning(
            "Could not run `hl-smi`, please follow the installation guide: https://docs.habana.ai/en/latest/Installation_Guide/index.html."
        )


def get_habana_frameworks_version():
    """
    Returns the installed version of SynapseAI.
    """
    output = subprocess.run(
        "pip list | grep habana-torch-plugin",
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return version.parse(output.stdout.split("\n")[0].split(" ")[-1])


def get_driver_version():
    """
    Returns the driver version.
    """
    output = subprocess.run(
        "hl-smi",
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if output.returncode == 0:
        return version.parse(output.stdout.split("\n")[2].replace(" ", "").split(":")[1][:-1].split("-")[0])
    return None
