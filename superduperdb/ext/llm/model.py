import dataclasses as dc
import functools
import os
import typing
import typing as t

import torch
import transformers
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    TrainingArguments,
)
from typing_extensions import Optional

from superduperdb import logging
from superduperdb.backends.query_dataset import query_dataset_factory
from superduperdb.components.model import (
    Model,
    _TrainingConfiguration,
)
from superduperdb.ext.utils import ensure_initialized

if typing.TYPE_CHECKING:
    from superduperdb.backends.base.query import Select
    from superduperdb.base.datalayer import Datalayer
    from superduperdb.components.dataset import Dataset
    from superduperdb.components.metric import Metric


@dc.dataclass
class LLMTrainingArguments(TrainingArguments):
    """
    LLM Training Arguments.
    Inherits from :class:`transformers.TrainingArguments`.

    {training_arguments_doc}
        lora_r (`int`, *optional*, defaults to 8):
            Lora R dimension.

        lora_alpha (`int`, *optional*, defaults to 16):
            Lora alpha.

        lora_dropout (`float`, *optional*, defaults to 0.05):
            Lora dropout.

        lora_target_modules (`List[str]`, *optional*, defaults to None):
            Lora target modules. If None, will be automatically inferred.

        lora_weight_path (`str`, *optional*, defaults to ""):
            Lora weight path.

        lora_bias (`str`, *optional*, defaults to "none"):
            Lora bias.

        max_length (`int`, *optional*, defaults to 512):
            Maximum source sequence length during training.

    """

    lora_r: int = 8
    lora_alpha: int = 16
    lora_dropout: float = 0.05
    lora_target_modules: t.Optional[t.List[str]] = None
    lora_weight_path: str = ""
    lora_bias: str = "none"
    max_length: t.Optional[int] = 512

    __doc__ = __doc__.format(training_arguments_doc=TrainingArguments.__doc__)


@functools.wraps(LLMTrainingArguments)
def LLMTrainingConfiguration(identifier: str, **kwargs) -> _TrainingConfiguration:
    return _TrainingConfiguration(identifier=identifier, kwargs=kwargs)


@dc.dataclass
class LLM(Model):
    """
    LLM model based on `transformers` library.
    Parameters:
    : param identifier: model identifier
    : param model_name_or_path: model name or path
    : param bits: quantization bits, [4, 8], default is None
    : param model_kwargs: model kwargs,
        all the kwargs will pass to `transformers.AutoModelForCausalLM.from_pretrained`
    : param tokenizer_kwags: tokenizer kwargs,
        all the kwargs will pass to `transformers.AutoTokenizer.from_pretrained`
    """

    identifier: str = ""
    model_name_or_path: str = "facebook/opt-125m"
    bits: Optional[int] = None
    object: t.Optional[transformers.Trainer] = None
    model_kwargs: t.Dict = dc.field(default_factory=dict)
    tokenizer_kwags: t.Dict = dc.field(default_factory=dict)

    def __post_init__(self):
        self.identifier = self.identifier or self.model_name_or_path
        # overwrite model kwargs
        if self.bits is not None:
            self.model_kwargs["load_in_4bit"] = self.bits == 4
            self.model_kwargs["load_in_8bit"] = self.bits == 8
        super().__post_init__()

    def init_model_and_tokenizer(self):
        model = AutoModelForCausalLM.from_pretrained(
            self.model_name_or_path,
            **self.model_kwargs,
        )

        tokenizer = AutoTokenizer.from_pretrained(
            self.model_name_or_path,
            **self.tokenizer_kwags,
        )
        tokenizer.pad_token = tokenizer.pad_token or tokenizer.unk_token
        return model, tokenizer

    def create_trainer(
        self, train_dataset, eval_dataset, training_args, **kwargs
    ) -> transformers.Trainer:
        trainer = transformers.Trainer(
            model=self.model,
            tokenizer=self.tokenizer,
            args=training_args,
            train_dataset=train_dataset,
            eval_dataset=eval_dataset,
            **kwargs,
        )
        return trainer

    def init(self):
        self.model, self.tokenizer = self.init_model_and_tokenizer()

    def _fit(
        self,
        X: t.Any,
        y: t.Optional[t.Any] = None,
        configuration: t.Optional[_TrainingConfiguration] = None,
        data_prefetch: bool = False,
        db: t.Optional["Datalayer"] = None,
        metrics: t.Optional[t.Sequence["Metric"]] = None,
        select: t.Optional["Select"] = None,
        validation_sets: t.Optional[t.Sequence[t.Union[str, "Dataset"]]] = None,
        **kwargs,
    ):
        assert configuration is not None, "configuration must be provided"

        training_args = LLMTrainingArguments(**configuration.kwargs)  # type: ignore

        # get device map
        device_map: t.Union[None, str, t.Dict[str, int]] = None
        if os.environ.get("LOCAL_RANK") is not None:
            device_map = {"": int(os.environ.get("LOCAL_RANK", "0"))}
        elif torch.backends.mps.is_available():
            device_map = "mps"

        quantization_config = self._create_quantization_config(training_args)

        self.model_kwargs["quantization_config"] = quantization_config
        self.model_kwargs["device_map"] = device_map
        self.model, self.tokenizer = self.init_model_and_tokenizer()

        self.tokenizer.model_max_length = (
            training_args.max_length or self.tokenizer.model_max_length
        )
        self._prepare_lora_training(training_args)

        train_dataset, eval_dataset = self.get_datasets(
            X,
            y,
            db,
            select,
            data_prefetch=data_prefetch,
            eval=True,
            prefetch_size=kwargs.pop("prefetch_size", 10000),
        )

        # TODO: Defind callbacks about superduperdb side
        trainer = self.create_trainer(
            train_dataset,
            eval_dataset,
            compute_metrics=metrics,
            training_args=training_args,
            **kwargs,
        )
        trainer.model.config.use_cache = False
        trainer.train()
        trainer.save_state()

    @ensure_initialized
    def to_call(self, X: t.Any, **kwargs):
        """
        Overwrite `Model.to_call` method to support self.object=None.
        """
        return self._generate(X, **kwargs)

    def _generate(self, X: t.Any, adapter_name=None, **kwargs):
        """
        Private method for `Model.to_call` method.
        Support inference by multi-lora adapters.
        """
        if adapter_name is not None:
            try:
                self.model.set_adapter(adapter_name)
            except Exception as e:
                raise ValueError(
                    f"Adapter {adapter_name} is not found in the model, "
                    "please use add_adapter to add it."
                ) from e

        elif hasattr(self.model, "disable_adapter"):
            with self.model.disable_adapter():
                return self.generate(X, **kwargs)

        return self.generate(X, **kwargs)

    def generate(self, X: t.Any, **kwargs):
        """
        Generate text.
        Can overwrite this method to support more inference methods.
        """
        model_inputs = self.tokenizer(X, return_tensors="pt").to(self.model.device)
        kwargs.setdefault("pad_token_id", self.tokenizer.eos_token_id)
        outputs = self.model.generate(**model_inputs, **kwargs)
        texts = self.tokenizer.batch_decode(outputs)
        texts = [text.replace(self.tokenizer.eos_token, "") for text in texts]
        if isinstance(X, str):
            return texts[0]
        return texts

    @ensure_initialized
    def add_adapter(self, model_id, adapter_name: str):
        try:
            from peft import PeftModel
        except Exception as e:
            raise ImportError("Please install peft to use LoRA training") from e

        logging.info(f"Loading adapter {adapter_name} from {model_id}")
        if not isinstance(self.model, PeftModel):
            self.model = PeftModel.from_pretrained(
                self.model, model_id, adapter_name=adapter_name
            )
        else:
            self.model.load_adapter(model_id, adapter_name)

    def _create_quantization_config(self, config: LLMTrainingArguments):
        compute_dtype = (
            torch.float16
            if config.fp16
            else (torch.bfloat16 if config.bf16 else torch.float32)
        )
        if self.bits is not None:
            quantization_config = BitsAndBytesConfig(
                load_in_4bit=self.bits == 4,
                load_in_8bit=self.bits == 8,
                llm_int8_threshold=6.0,
                llm_int8_has_fp16_weight=False,
                bnb_4bit_compute_dtype=compute_dtype,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4",
            )
        else:
            quantization_config = None
        return quantization_config

    def _prepare_lora_training(self, config: LLMTrainingArguments):
        try:
            from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
        except Exception as e:
            raise ImportError("Please install peft to use LoRA training") from e

        lora_config = LoraConfig(
            r=config.lora_r,
            lora_alpha=config.lora_alpha,
            target_modules=config.lora_target_modules
            or self._get_lora_target_modules(),
            lora_dropout=config.lora_dropout,
            bias=config.lora_bias,
            task_type="CAUSAL_LM",
        )

        if self.bits:
            self.model = prepare_model_for_kbit_training(
                self.model,
                use_gradient_checkpointing=config.gradient_checkpointing,
            )

            if not self.ddp and torch.cuda.device_count() > 1:
                self.model.is_parallelizable = True
                self.model.model_parallel = True

        self.model = get_peft_model(self.model, lora_config)

        if config.gradient_checkpointing:
            self.model.enable_input_require_grads()

        if config.local_rank == 0:
            self.model.print_trainable_parameters()

    def _get_lora_target_modules(self):
        try:
            import bitsandbytes as bnb
        except Exception as e:
            raise ImportError("Please install bitsandbytes to use LoRA training") from e

        if self.bits == 4:
            cls = bnb.nn.Linear4bit
        elif self.bits == 8:
            cls = bnb.nn.Linear8bitLt
        else:
            cls = torch.nn.Linear

        lora_module_names = set()
        for name, module in self.model.named_modules():
            if isinstance(module, cls):
                names = name.split(".")
                lora_module_names.add(names[0] if len(names) == 1 else names[-1])

        lora_module_names.discard("lm_head")
        return list(lora_module_names)

    def get_datasets(
        self,
        X,
        y,
        db,
        select,
        data_prefetch: bool = False,
        eval: bool = False,
        prefetch_size: int = 10000,
    ):
        keys = [X]
        if y is not None:
            keys.append(y)

        train_dataset = query_dataset_factory(
            keys=keys,
            data_prefetch=data_prefetch,
            select=select,
            fold="train",
            db=db,
            transform=self.preprocess,
            prefetch_size=prefetch_size,
        )
        if eval:
            eval_dataset = query_dataset_factory(
                keys=keys,
                data_prefetch=data_prefetch,
                select=select,
                fold="valid",
                db=db,
                transform=self.preprocess,
                prefetch_size=prefetch_size,
            )
        else:
            eval_dataset = None

        def process_func(example):
            return self.tokenize(example, X, y)

        train_dataset = train_dataset.map(process_func)
        if eval_dataset is not None:
            eval_dataset = eval_dataset.map(process_func)
        return train_dataset, eval_dataset

    def tokenize(self, example, X, y):
        prompt = example[X]

        prompt = prompt + self.tokenizer.eos_token
        result = self.tokenizer(
            prompt,
            truncation=True,
            max_length=self.tokenizer.model_max_length,
            padding="max_length",
        )
        result["labels"] = result["input_ids"].copy()
        return result

    @property
    def ddp(self):
        return int(os.environ.get("WORLD_SIZE", 1)) != 1

    def post_create(self, db: "Datalayer") -> None:
        # TODO: Do not make sense to add this logic here,
        # Need a auto DataType to handle this
        from superduperdb.backends.ibis.data_backend import IbisDataBackend
        from superduperdb.backends.ibis.field_types import dtype

        if isinstance(db.databackend, IbisDataBackend) and self.encoder is None:
            self.encoder = dtype("str")

        # since then the `.add` clause is not necessary
        output_component = db.databackend.create_model_table_or_collection(self)

        if output_component is not None:
            db.add(output_component)