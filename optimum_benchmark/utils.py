from typing import Optional, List
from logging import getLogger
import subprocess
import importlib
import platform
import random
import signal
import time
import re
import os

from omegaconf import DictConfig
import numpy as np
import psutil
import torch

LOGGER = getLogger("utils")


def set_seed(seed: int) -> None:
    # TODO: Should be devided into multiple functions
    # each setting seeds for a backend
    random.seed(seed)
    np.random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)

    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


def bytes_to_mega_bytes(bytes: int) -> int:
    # Reference: https://en.wikipedia.org/wiki/Byte#Multiple-byte_units
    return int(bytes * 1e-6)


def get_cpu() -> Optional[str]:
    if platform.system() == "Windows":
        return platform.processor()

    elif platform.system() == "Darwin":
        os.environ["PATH"] = os.environ["PATH"] + os.pathsep + "/usr/sbin"
        command = "sysctl -n machdep.cpu.brand_string"
        return str(subprocess.check_output(command).strip())

    elif platform.system() == "Linux":
        command = "cat /proc/cpuinfo"
        all_info = subprocess.check_output(command, shell=True).decode().strip()
        for line in all_info.split("\n"):
            if "model name" in line:
                return re.sub(".*model name.*:", "", line, 1)
        return "Could not find device name"

    else:
        raise ValueError(f"Unknown system '{platform.system()}'")


def get_cpu_ram_mb():
    return bytes_to_mega_bytes(psutil.virtual_memory().total)


def check_no_process_is_running_on_cuda_device(device_ids: List[int]) -> None:
    """
    Raises a RuntimeError if any process is running on the given cuda device.
    """

    for device_id in device_ids:
        # get list of all PIDs running on nvidia devices
        pids = [
            int(pid)
            for pid in subprocess.check_output(
                ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"]
            )
            .decode()
            .strip()
            .split("\n")
            if pid != ""
        ]

        # get list of PIDs running on cuda device_id
        pids_on_device_id = set(
            [
                pid
                for pid in pids
                if subprocess.check_output(
                    [
                        "nvidia-smi",
                        f"--query-compute-apps=pid,used_memory",
                        f"--format=csv,noheader,nounits",
                        f"--id={device_id}",
                    ]
                )
                .decode()
                .startswith(f"{pid},")
            ]
        )

        # TODO: It would be safer to run each run of a sweep in a subprocess. Although we can trust PyTorch to clear GPU memory when asked,
        # it is not a safe assumption to make for all backends.
        if len(pids_on_device_id) > 1 or (
            len(pids_on_device_id) == 1 and os.getpid() not in pids_on_device_id
        ):
            raise RuntimeError(
                f"Expected no processes on device {device_id}, "
                f"found {len(pids_on_device_id)} processes "
                f"with PIDs {pids_on_device_id}."
            )


def check_only_this_process_is_running_on_cuda_device(
    device_ids: List[int], pid
) -> None:
    """
    Raises a RuntimeError if at any point in time, there is a process running
    on the given cuda device that is not the current process.
    """

    while True:
        # get list of all PIDs running on nvidia devices
        pids = [
            int(pid)
            for pid in subprocess.check_output(
                ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"]
            )
            .decode()
            .strip()
            .split("\n")
            if pid != ""
        ]

        for device_id in device_ids:
            # get list of PIDs running on cuda device_id
            pids_on_device_id = set(
                [
                    pid
                    for pid in pids
                    if subprocess.check_output(
                        [
                            "nvidia-smi",
                            f"--query-compute-apps=pid,used_memory",
                            f"--format=csv,noheader,nounits",
                            f"--id={device_id}",
                        ]
                    )
                    .decode()
                    .startswith(f"{pid},")
                ]
            )

            # check if there is a process running on device_id that is not the current process
            if len(pids_on_device_id) > 1:
                os.kill(pid, signal.SIGTERM)
                raise RuntimeError(
                    f"Expected only process {pid} on device {device_id}, "
                    f"found {len(pids_on_device_id)} processes "
                    f"with PIDs {pids_on_device_id}."
                )

        # sleep for 1 second
        time.sleep(1)


# TODO: move this to onnxruntime backend, the only place using it
def infer_device_id(device: str) -> int:
    """
    Infer the device id from the given device string.
    """

    if device == "cuda":
        return torch.cuda.current_device()
    elif torch.device(device).type == "cuda":
        return torch.device(device).index
    elif torch.device(device).type == "cpu":
        return -1
    else:
        raise ValueError(f"Unknown device '{device}'")


_NAME_TO_IMPORTPATH = {
    "pytorch": "optimum_benchmark.backends.pytorch",
    "openvino": "optimum_benchmark.backends.openvino",
    "neural_compressor": "optimum_benchmark.backends.neural_compressor",
    "onnxruntime": "optimum_benchmark.backends.onnxruntime",
    "inference": "optimum_benchmark.benchmarks.inference",
    "training": "optimum_benchmark.benchmarks.training",
}

_NAME_TO_CLASS_NAME = {
    "pytorch": "PyTorchConfig",
    "openvino": "OVConfig",
    "neural_compressor": "INCConfig",
    "onnxruntime": "ORTConfig",
    "inference": "InferenceConfig",
    "training": "TrainingConfig",
}


def name_to_dataclass(name: str):
    # We use a map name to import path to avoid importing everything here, especially every backend, to avoid to install all backends to run
    # optimum-benchmark.
    module = importlib.import_module(_NAME_TO_IMPORTPATH[name])
    dataclass_class = getattr(module, _NAME_TO_CLASS_NAME[name])
    return dataclass_class


def remap_to_correct_metadata(experiment: DictConfig):
    for key, value in experiment.items():
        if isinstance(value, DictConfig) and hasattr(value, "name"):
            experiment[key]._metadata.object_type = name_to_dataclass(
                experiment[key].name
            )
    return experiment


DIFFUSION_TASKS = [
    "stable-diffusion",
    "stable-diffusion-xl",
]


TEXT_GENERATION_TASKS = [
    "text-generation",
    "text2text-generation",
    "image-to-text",
    "automatic-speech-recognition",
]

# let's leave this here for now, it's a good list of tasks supported by transformers
ALL_TASKS = [
    "conversational",
    "feature-extraction",
    "fill-mask",
    "text-generation",
    "text2text-generation",
    "text-classification",
    "token-classification",
    "multiple-choice",
    "object-detection",
    "question-answering",
    "image-classification",
    "image-segmentation",
    "mask-generation",
    "masked-im",
    "semantic-segmentation",
    "automatic-speech-recognition",
    "audio-classification",
    "audio-frame-classification",
    "audio-xvector",
    "image-to-text",
    "stable-diffusion",
    "stable-diffusion-xl",
    "zero-shot-image-classification",
    "zero-shot-object-detection",
]
