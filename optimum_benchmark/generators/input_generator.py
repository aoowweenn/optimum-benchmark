from typing import Dict, List, Union, Optional, TYPE_CHECKING
from logging import getLogger

if TYPE_CHECKING:
    from transformers import PretrainedConfig
    import torch

from optimum_benchmark.generators.model_type_generator import (
    SUPPURTED_MODEL_TYPES,
    ModelTypeGenerator,
)
from optimum_benchmark.generators.task_generator import (
    TASKS_TO_GENERATORS,
    TaskGenerator,
)


LOGGER = getLogger("dummy_dataset")


class InputGenerator:
    model_type_generator: Optional[ModelTypeGenerator] = None
    task_generator: Optional[TaskGenerator] = None

    def __init__(
        self,
        task: str,
        input_shapes: Dict[str, int],
        # for model_type_generator
        pretrained_config: Optional["PretrainedConfig"] = None,
    ):
        if pretrained_config is not None:
            model_type = pretrained_config.model_type
            if ModelTypeGenerator.check_model_type_support(model_type):
                LOGGER.info(f"Using {model_type} model type generator")
                self.model_type_generator = ModelTypeGenerator(
                    task=task,
                    model_type=model_type,
                    shapes=input_shapes,
                    pretrained_config=pretrained_config,
                )
        elif task in TASKS_TO_GENERATORS:
            LOGGER.info(f"Using {task} task generator")
            self.task_generator = TASKS_TO_GENERATORS[task](
                shapes=input_shapes,
                with_labels=False,
            )
        else:
            raise NotImplementedError(
                f"Neither task {task} nor model type {model_type} is supported. \n"
                f"Available tasks: {list(TASKS_TO_GENERATORS.keys())}. \n"
                "If you want to add support for this task, please submit a PR or a feature request to optimum-benchmark. \n"
                f"Available model types: {SUPPURTED_MODEL_TYPES}. \n"
                "If you want to add support for this model type, please submit a PR or a feature request to optimum."
            )

    # TODO: we can drop the torch dependency here by returning a dict of numpy arrays
    # and then converting them to torch tensors in backend.prepare_for_inference
    def generate(self, mode: str) -> Dict[str, Union["torch.Tensor", List[str]]]:
        if self.model_type_generator is not None:
            dummy_input = self.model_type_generator.generate()
        elif self.task_generator is not None:
            dummy_input = self.task_generator.generate()

        if mode == "generate":
            if "input_ids" in dummy_input:
                # text input
                dummy_input = {
                    "input_ids": dummy_input["input_ids"],
                }
            elif "pixel_values" in dummy_input:
                # image input
                dummy_input = {
                    "pixel_values": dummy_input["pixel_values"],
                }
            elif "input_values" in dummy_input:
                # speech input
                dummy_input = {
                    "input_values": dummy_input["input_values"],
                }
            elif "input_features" in dummy_input:
                # waveform input
                dummy_input = {
                    "input_features": dummy_input["input_features"],
                }

        return dummy_input
