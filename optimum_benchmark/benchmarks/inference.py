from dataclasses import dataclass, field
from typing import List, Dict
from logging import getLogger

from pandas import DataFrame
import statistics

from optimum_benchmark.backends.base import Backend
from optimum_benchmark.generators.input_generator import InputGenerator
from optimum_benchmark.benchmarks.base import Benchmark, BenchmarkConfig
from optimum_benchmark.trackers.memory import memory_tracker_class_for_backend
from optimum_benchmark.trackers.latency import latency_tracker_class_for_backend


LOGGER = getLogger("inference")


@dataclass
class InferenceConfig(BenchmarkConfig):
    name: str = "inference"
    _target_: str = "optimum_benchmark.benchmarks.inference.InferenceBenchmark"

    # benchmark options
    memory: bool = False
    warmup_runs: int = 10

    benchmark_duration: int = 10  # TODO: deprecate this and use `benchmark.duration`

    # input options
    input_shapes: Dict = field(
        default_factory=lambda: {
            # used with all tasks
            "batch_size": 2,
            # used with text input tasks
            "sequence_length": 16,
            # used with multiple choice tasks where input
            # is of shape (batch_size, num_choices, sequence_length)
            "num_choices": 1,
            # used with audio input tasks
            "feature_size": 80,
            "nb_max_frames": 3000,
            "audio_sequence_length": 16000,
        }
    )

    # generation options
    new_tokens: int = 100  # TODO: deprecate this and use `benchamrk.generation_options`

    # diffusion options
    # TODO: add `benchmark.diffusion_options` for multiple images per prompt


class InferenceBenchmark(Benchmark):
    def __init__(self):
        # initialize inference results
        self.forward_peak_memory: int = 0
        self.forward_latencies: List[float] = []
        self.generate_latencies: List[float] = []

    def configure(self, config: InferenceConfig):
        super().configure(config)

        self.memory = config.memory

        self.warmup_runs = config.warmup_runs
        self.benchmark_duration = config.benchmark_duration

        self.input_shapes = config.input_shapes
        self.new_tokens = config.new_tokens

    def run(self, backend: Backend) -> None:
        LOGGER.info("Running inference benchmark")

        self.can_generate = backend.is_text_generation_model()
        self.input_shapes.update(backend.model_shapes)

        self.input_generator = InputGenerator(
            task=backend.task,
            input_shapes=self.input_shapes,
            pretrained_config=backend.pretrained_config,
        )

        if self.memory:
            # if requested, run memory tracking
            self.run_memory_tracking(backend)

        # run forward pass tracking
        self.run_forward_tracking(backend)

        if self.can_generate:
            # if possible, run generation pass tracking
            self.run_generate_tracking(backend)

    def run_memory_tracking(self, backend: Backend) -> None:
        memory_input = self.input_generator.generate(
            mode="forward",
        )

        # TODO: handle this in backend using prepare_for_inference
        for key, value in memory_input.items():
            if key == "prompt":
                continue
            memory_input[key] = value.to(backend.device)

        # for backends that require compilation with static shapes
        backend.prepare_for_inference(input_shapes=self.input_shapes)

        LOGGER.info("\t+ Tracking forward pass peak memory")
        memory_tracker = memory_tracker_class_for_backend[backend.config.name](backend)
        with memory_tracker.track(interval=self.benchmark_duration // 100):
            _ = backend.forward(memory_input)

        self.forward_peak_memory = memory_tracker.get_peak_memory()
        LOGGER.info(f"\t+ Forward pass peak memory: {self.forward_peak_memory} (MB)")

    def run_forward_tracking(self, backend: Backend) -> None:
        forward_input = self.input_generator.generate(
            mode="forward",
        )

        # TODO: handle this in backend using prepare_for_inference
        for key, value in forward_input.items():
            if key == "prompt":
                continue
            forward_input[key] = value.to(backend.device)

        # for backends that require compilation with static shapes
        backend.prepare_for_inference(input_shapes=self.input_shapes)

        LOGGER.info("\t+ Warming up the forward pass")
        for _ in range(self.warmup_runs):
            _ = backend.forward(forward_input)

        LOGGER.info("\t+ Tracking forward pass latency and throughput")
        latency_tracker = latency_tracker_class_for_backend[backend.config.name](
            backend
        )
        while sum(self.forward_latencies) < self.benchmark_duration:
            with latency_tracker.track():
                _ = backend.forward(forward_input)
            self.forward_latencies = latency_tracker.get_latencies()

        LOGGER.info(f"\t+ Forward pass latency: {self.forward_latency:.2e} (s)")
        LOGGER.info(
            f"\t+ Forward pass throughput: {self.forward_throughput:.2f} (samples/s)"
        )

    def run_generate_tracking(self, backend: Backend) -> None:
        generate_input = self.input_generator.generate(
            mode="forward",
        )

        # TODO: handle this in backend using prepare_for_inference
        for key, value in generate_input.items():
            if key == "prompt":
                continue
            generate_input[key] = value.to(backend.device)

        LOGGER.info("\t+ Warming up the generation pass")
        _ = backend.generate(
            input=generate_input,
            max_new_tokens=self.new_tokens,
            min_new_tokens=self.new_tokens,
            do_sample=False,
            use_cache=True,
            pad_token_id=0,
            num_beams=1,
        )

        LOGGER.info("\t+ Tracking generation latency and throughput")
        latency_tracker = latency_tracker_class_for_backend[backend.config.name](
            backend
        )
        while sum(self.generate_latencies) < self.benchmark_duration:
            with latency_tracker.track():
                _ = backend.generate(
                    generate_input,
                    max_new_tokens=self.new_tokens,
                    min_new_tokens=self.new_tokens,
                    do_sample=False,
                    use_cache=True,
                    pad_token_id=0,
                    num_beams=1,
                )
            self.generate_latencies = latency_tracker.get_latencies()

        LOGGER.info(f"\t+ Generation pass latency: {self.generate_latency:.2e} (s)")

        LOGGER.info(
            f"\t+ Generation pass throughput: {self.generate_throughput:.2f} (tokens/s)"
        )

    # Metrics
    @property
    def forward_latency(self) -> float:
        return significant_figures(statistics.mean(self.forward_latencies))

    @property
    def forward_throughput(self) -> float:
        return significant_figures(self.input_shapes.batch_size / self.forward_latency)

    @property
    def generate_latency(self) -> float:
        return significant_figures(statistics.mean(self.generate_latencies))

    @property
    def generate_throughput(self) -> float:
        return significant_figures(
            self.new_tokens * self.input_shapes.batch_size / self.generate_latency
        )

    def get_results_df(self) -> DataFrame:
        results_dict = dict()

        if self.memory:
            results_dict["forward.peak_memory(MB)"] = self.forward_peak_memory

        results_dict["forward.latency(s)"] = self.forward_latency
        results_dict["forward.throughput(samples/s)"] = self.forward_throughput

        if self.can_generate:
            results_dict["generate.latency(s)"] = self.generate_latency
            results_dict["generate.throughput(tokens/s)"] = self.generate_throughput

        return DataFrame(results_dict, index=[0])

    def save(self) -> None:
        LOGGER.info("Saving inference results")
        results_df = self.get_results_df()
        results_df.to_csv("inference_results.csv")


def significant_figures(x):
    return float(f"{x:.3g}")
