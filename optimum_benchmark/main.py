import os
import platform
from typing import Type, Dict
from logging import getLogger
from dataclasses import dataclass, MISSING, field

import hydra
from hydra.utils import get_class
from optimum.exporters import TasksManager
from omegaconf import DictConfig, OmegaConf
from hydra.core.config_store import ConfigStore
from diffusers import __version__ as diffusers_version
from accelerate import __version__ as accelerate_version
from optimum.version import __version__ as optimum_version
from transformers import __version__ as transformers_version

from .import_utils import (
    is_torch_available,
    is_onnxruntime_available,
    is_openvino_available,
    is_neural_compressor_available,
)
from optimum_benchmark.backends.base import Backend
from optimum_benchmark.benchmarks.base import Benchmark
from optimum_benchmark.backends.base import Backend, BackendConfig
from optimum_benchmark.benchmarks.training import TrainingConfig
from optimum_benchmark.benchmarks.inference import InferenceConfig
from optimum_benchmark.benchmarks.base import Benchmark, BenchmarkConfig
from .utils import remap_to_correct_metadata, get_cpu, get_cpu_ram_mb

LOGGER = getLogger("main")

OmegaConf.register_new_resolver(
    "infer_task",
    # TODO: find a better way for this; it doesn't 
    # always work because it relies on hub metadata
    lambda model, revision: TasksManager.infer_task_from_model(
        model=model,
        revision=revision,
    ),
)


@dataclass
class ExperimentConfig:
    # BACKEND CONFIGURATION
    backend: BackendConfig = MISSING

    # BENCHMARK CONFIGURATION
    benchmark: BenchmarkConfig = MISSING

    # EXPERIMENT CONFIGURATION
    experiment_name: str = MISSING
    # Model name or path (bert-base-uncased, google/vit-base-patch16-224, ...)
    model: str = MISSING
    # Device name or path (cpu, cuda, cuda:0, ...)
    device: str = MISSING
    # Task name (text-classification, image-classification, ...)
    task: str = "${infer_task:${model}, ${hub_kwargs.revision}}"

    # ADDITIONAL MODEL CONFIGURATION: Model revision, use_auth_token, trust_remote_code
    hub_kwargs: Dict = field(
        default_factory=lambda: {
            "revision": "main",
            "cache_dir": None,
            "force_download": False,
            "local_files_only": False,
        }
    )

    # ENVIRONMENT CONFIGURATION
    environment: Dict = field(
        default_factory=lambda: {
            "optimum_version": optimum_version,
            "transformers_version": transformers_version,
            "accelerate_version": accelerate_version,
            "diffusers_version": diffusers_version,
            "python_version": platform.python_version(),
            "system": platform.system(),
            "cpu": get_cpu(),
            "cpu_count": os.cpu_count(),
            "cpu_ram_mb": get_cpu_ram_mb(),
        }
    )


# Register configurations
cs = ConfigStore.instance()
cs.store(name="experiment", node=ExperimentConfig)

if is_torch_available():
    from optimum_benchmark.backends.pytorch import PyTorchConfig

    cs.store(group="backend", name="pytorch", node=PyTorchConfig)

if is_onnxruntime_available():
    from optimum_benchmark.backends.onnxruntime import ORTConfig

    cs.store(group="backend", name="onnxruntime", node=ORTConfig)

if is_openvino_available():
    from optimum_benchmark.backends.openvino import OVConfig

    cs.store(group="backend", name="openvino", node=OVConfig)

if is_neural_compressor_available():
    from optimum_benchmark.backends.neural_compressor import INCConfig

    cs.store(group="backend", name="neural_compressor", node=INCConfig)

cs.store(group="benchmark", name="inference", node=InferenceConfig)
cs.store(group="benchmark", name="training", node=TrainingConfig)


@hydra.main(version_base=None)
def run_experiment(experiment: DictConfig) -> None:
    # By default, Hydra populates the metadata object_type with the ones from ExperimentConfig but the object_type should really be
    # one of the subclass (e.g. PyTorchBackendConfig instead of BackendConfig). This is required to call `to_object`.
    experiment = remap_to_correct_metadata(experiment)

    # This is required to trigger __post_init__. Reference: https://github.com/omry/omegaconf/issues/377
    experiment = OmegaConf.to_object(experiment)
    experiment = OmegaConf.create(experiment)

    # Save the config
    OmegaConf.save(experiment, "hydra_config.yaml", resolve=True)

    # Allocate requested benchmark
    benchmark_factory: Type[Benchmark] = get_class(experiment.benchmark._target_)
    benchmark: Benchmark = benchmark_factory()
    benchmark.configure(experiment.benchmark)

    # Allocate requested backend
    backend_factory: Type[Backend] = get_class(experiment.backend._target_)
    backend: Backend = backend_factory(
        experiment.model,
        experiment.task,
        experiment.device,
        experiment.hub_kwargs,
    )

    try:
        backend.configure(experiment.backend)

        benchmark.run(backend)
        # Save the benchmark results
        benchmark.save()

        backend.clean()
    except Exception as e:
        LOGGER.error("Error during benchmarking: %s", e)
        backend.clean()
        raise e
