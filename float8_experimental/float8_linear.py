# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD 3-Clause license found in the
# LICENSE file in the root directory of this source tree.
"""
A simple module swap UX for a float8 version of `torch.nn.Linear`.
"""

import dataclasses
import enum

from typing import Optional

import float8_experimental.config as config

import torch

from float8_experimental.float8_dynamic_utils import (
    cast_to_float8_e4m3_dynamic,
    cast_to_float8_e5m2_dynamic_bw,
    WeightWithDynamicFloat8CastTensor,
)

from float8_experimental.float8_tensor import (
    Float8Tensor,
    ScaledMMConfig,
    to_fp8_no_autograd,
)

from float8_experimental.float8_utils import (
    amax_history_to_scale,
    e4m3_dtype,
    e5m2_dtype,
    tensor_to_amax,
)


def _maybe_initialize_amaxes_scales_for_float8_cast(
    x,
    cur_amax,
    amax_history,
    scale,
    scale_fn_name,
    float8_dtype,
    is_initialized,
    reduce_amax,
):
    """
    If x is about to be cast to `float8` and the amax buffers are not initialized,
    initializes them inplace.
    """
    if is_initialized:
        return
    with torch.no_grad():
        # Note: we need to enable distributed reduction here in order
        # to match numerics between single GPU and multi GPU code for
        # activations and gradients
        new_amax = tensor_to_amax(x, reduce_amax=reduce_amax)
        cur_amax.fill_(new_amax)
        amax_history[0] = new_amax
        new_scale = amax_history_to_scale(
            amax_history, float8_dtype, x.dtype, scale_fn_name
        )
        scale.copy_(new_scale)


@torch._dynamo.allow_in_graph
class NoopFwToFloat8E5M2Bw(torch.autograd.Function):
    """
    Forward: no-op
    Backward: convert to float8_e5m2, initialize if needed
    """

    @staticmethod
    def forward(
        ctx,
        tensor,
        fp8_amax_dL_dY,
        fp8_amax_history_dL_dY,
        fp8_scale_dL_dY,
        scale_fn_name,
        is_amax_initialized,
        mm_config: ScaledMMConfig,
    ):
        ctx.save_for_backward(fp8_amax_dL_dY, fp8_amax_history_dL_dY, fp8_scale_dL_dY)
        ctx.scale_fn_name = scale_fn_name
        ctx.is_amax_initialized = is_amax_initialized
        ctx.mm_config = mm_config
        return tensor

    @staticmethod
    def backward(ctx, go):
        fp8_amax_dL_dY, fp8_amax_history_dL_dY, fp8_scale_dL_dY = ctx.saved_tensors
        scale_fn_name = ctx.scale_fn_name
        is_amax_initialized = ctx.is_amax_initialized

        _maybe_initialize_amaxes_scales_for_float8_cast(
            go,
            fp8_amax_dL_dY,
            fp8_amax_history_dL_dY,
            fp8_scale_dL_dY,
            scale_fn_name,
            e5m2_dtype,
            is_amax_initialized,
            reduce_amax=True,
        )

        fp8_amax_dL_dY.fill_(tensor_to_amax(go))

        res = to_fp8_no_autograd(
            go, fp8_scale_dL_dY, e5m2_dtype, mm_config=ctx.mm_config
        )
        empty_grads = None, None, None, None, None, None
        return res, *empty_grads


@dataclasses.dataclass
class DelayedScalingRecipe:
    # Controls the history length of amax buffers
    history_len: int

    # Controls the way to calculate current scale from amax history
    # TODO(future): add other functions as needed, hardcoded or user defined
    scale_fn_name: str

    def __init__(self, history_len: int = 16, scale_fn_name: str = "max"):
        self.history_len = history_len
        self.scale_fn_name = scale_fn_name
        assert (
            self.scale_fn_name == "max"
        ), f"{self.scale_fn_name} is not implemented yet. Only max is supported for now."


class TensorScalingType(enum.Enum):
    DELAYED = "delayed"
    DYNAMIC = "dynamic"

    def short_str(self):
        if self is TensorScalingType.DELAYED:
            return "del"
        else:
            assert self is TensorScalingType.DYNAMIC
            return "dyn"


class Float8Linear(torch.nn.Linear):
    """
    A wrapper around a `torch.nn.Linear` module which does fp8 compute, and tracks
    scales in way friendly to delayed scaling.
    """

    def __init__(self, *args, **kwargs):
        """
        Additional arguments on top of `torch.nn.Linear`'s arguments:
        * `delayed_scaling_recipe`: configuration for delayed scaling
        * `scaling_type_x`: delayed vs dynamic scaling for `x`
        * `scaling_type_w`: delayed vs dynamic scaling for `w`
        * `scaling_type_dL_dY`: delayed vs dynamic scaling for `dL_dY`
        """

        delayed_scaling_recipe = kwargs.pop(
            "delayed_scaling_recipe", DelayedScalingRecipe()
        )
        # Amax scales should always be kept as float32.
        self.always_float32_buffers = set()
        emulate = kwargs.pop("emulate", False)
        scaling_type_x = kwargs.pop("scaling_type_x", TensorScalingType.DYNAMIC)
        scaling_type_w = kwargs.pop("scaling_type_w", TensorScalingType.DYNAMIC)
        scaling_type_dL_dY = kwargs.pop("scaling_type_dL_dY", TensorScalingType.DYNAMIC)
        super().__init__(*args, **kwargs)

        # Defines the scaling behavior of x, w, dL_dY
        self.scaling_type_x = scaling_type_x
        self.scaling_type_w = scaling_type_w
        self.scaling_type_dL_dY = scaling_type_dL_dY
        # Convenience flag to skip code related to delayed scaling
        self.has_any_delayed_scaling = (
            self.scaling_type_x is TensorScalingType.DELAYED
            or self.scaling_type_w is TensorScalingType.DELAYED
            or self.scaling_type_dL_dY is TensorScalingType.DELAYED
        )

        # TODO(future): have a unique recipe per buffer instead of one per
        # module, saving implementing that until we need it.
        # TODO(future): serialization for recipes
        self.recipe = delayed_scaling_recipe

        self.create_buffers()

        # Defines the behavior of the matmul in the forward and backward pass
        self.forward_config = ScaledMMConfig(
            emulate, True if not emulate else False, False, config.pad_inner_dim
        )
        self.backward_config = ScaledMMConfig(
            emulate, False, False, config.pad_inner_dim
        )

        # Note: is_amax_initialized is not a buffer to avoid data dependent
        # control flow visible to dynamo
        # TODO(future PR): add serialization for this flag
        self.is_amax_initialized = not config.enable_amax_init

        # Syncing of amaxes and scales happens outside of this function. This
        # flag is here to enforce that the user does not forget to do this.
        self.amax_and_scale_synced = not config.enable_amax_init

        # This is needed to properly handle autocast in the amax/scale
        # update function for torch.float16
        self.last_seen_input_dtype = None

        # pre_forward and post_forward are currently broken with FSDP
        # and torch.compile, this option can disable them
        # Note that when using `config.enable_pre_and_post_forward = False`,
        # it's recommended to also set `config.enable_amax_init = False`.
        # Otherwise, the amax buffer would never be marked as initialized and
        # would be initialized in every iteration.
        self.enable_pre_and_post_forward = config.enable_pre_and_post_forward

    def create_buffers(self):
        # Default values for history buffers, see above TODO
        history_len = self.recipe.history_len
        device = self.weight.device
        # TODO(future PR): dtype values below don't have the other float8
        # flavors, fix it
        default_x = torch.finfo(torch.float8_e4m3fn).max
        default_w = torch.finfo(torch.float8_e4m3fn).max
        default_dl_dy = torch.finfo(torch.float8_e5m2).max

        # Note: for now, create all the buffers if any are needed, to postpone
        # the work to make the scale and amax syncing and history calculation
        # handle a heterogeneous setup. We can do that work later if benchmarks
        # show it is worth doing.
        if self.has_any_delayed_scaling:
            self.register_always_float32_buffer(
                "fp8_amax_x", torch.tensor([default_x], device=device)
            )
            self.register_always_float32_buffer(
                "fp8_amax_history_x", torch.zeros(history_len, device=device)
            )
            self.register_always_float32_buffer(
                "fp8_scale_x", torch.tensor([1.0], device=device)
            )
            self.register_always_float32_buffer(
                "fp8_amax_w", torch.tensor([default_w], device=device)
            )
            self.register_always_float32_buffer(
                "fp8_amax_history_w", torch.zeros(history_len, device=device)
            )
            self.register_always_float32_buffer(
                "fp8_scale_w", torch.tensor([1.0], device=device)
            )
            self.register_always_float32_buffer(
                "fp8_amax_dL_dY", torch.tensor([default_dl_dy], device=device)
            )
            self.register_always_float32_buffer(
                "fp8_amax_history_dL_dY", torch.zeros(history_len, device=device)
            )
            self.register_always_float32_buffer(
                "fp8_scale_dL_dY", torch.tensor([1.0], device=device)
            )

    def register_always_float32_buffer(
        self, name: str, tensor: Optional[torch.Tensor], persistent: bool = True
    ) -> None:
        self.register_buffer(name=name, tensor=tensor, persistent=persistent)
        self.always_float32_buffers.add(name)

    def _apply(self, fn, recurse=True):
        ret = super()._apply(fn, recurse)
        self.convert_amax_buffer_to_float32()
        return ret

    def convert_amax_buffer_to_float32(self):
        for key in self.always_float32_buffers:
            if self._buffers[key] is not None:
                self._buffers[key] = self._buffers[key].to(torch.float32)

    def cast_x_to_float8(
        self, x: torch.Tensor, is_amax_initialized: bool
    ) -> torch.Tensor:
        # Duplicate the autocast logic for F.linear, so that the output
        # of our module has the right original precision
        if torch.is_autocast_enabled():
            # For now, hardcode to GPU's autocast dtype
            # if we need CPU support in the future, we can add it
            autocast_dtype = torch.get_autocast_gpu_dtype()
            x = x.to(autocast_dtype)

        if self.scaling_type_x is TensorScalingType.DELAYED:
            scale_fn_name = self.recipe.scale_fn_name
            _maybe_initialize_amaxes_scales_for_float8_cast(
                x,
                self.fp8_amax_x,
                self.fp8_amax_history_x,
                self.fp8_scale_x,
                scale_fn_name,
                e4m3_dtype,
                is_amax_initialized,
                reduce_amax=True,
            )
            x_fp8 = Float8Tensor.to_float8(
                x,
                self.fp8_scale_x,
                e4m3_dtype,
                self.fp8_amax_x,
                self.forward_config,
            )
        else:
            assert self.scaling_type_x is TensorScalingType.DYNAMIC
            x_fp8 = cast_to_float8_e4m3_dynamic(x, self.forward_config)
        return x_fp8

    def cast_w_to_float8(
        self, w: torch.Tensor, is_amax_initialized: bool
    ) -> torch.Tensor:
        if self.scaling_type_w is TensorScalingType.DELAYED:
            scale_fn_name = self.recipe.scale_fn_name
            _maybe_initialize_amaxes_scales_for_float8_cast(
                w,
                self.fp8_amax_w,
                self.fp8_amax_history_w,
                self.fp8_scale_w,
                scale_fn_name,
                e4m3_dtype,
                is_amax_initialized,
                reduce_amax=False,
            )

            w_fp8 = Float8Tensor.to_float8(
                w,
                self.fp8_scale_w,
                e4m3_dtype,
                self.fp8_amax_w,
                self.forward_config,
            )
        else:
            assert self.scaling_type_w is TensorScalingType.DYNAMIC
            # TODO(future): also support FSDP integration in delayed scaling path
            if isinstance(self.weight, Float8Tensor):  # cast by FSDP
                w_fp8 = self.weight
            else:
                w_fp8 = cast_to_float8_e4m3_dynamic(self.weight, self.forward_config)
        return w_fp8

    def cast_y_to_float8_in_bw(self, y: torch.Tensor) -> torch.Tensor:
        if self.scaling_type_dL_dY is TensorScalingType.DELAYED:
            scale_fn_name = self.recipe.scale_fn_name
            y = NoopFwToFloat8E5M2Bw.apply(
                y,
                self.fp8_amax_dL_dY,
                self.fp8_amax_history_dL_dY,
                self.fp8_scale_dL_dY,
                scale_fn_name,
                self.is_amax_initialized,
                self.backward_config,
            )
        else:
            assert self.scaling_type_dL_dY is TensorScalingType.DYNAMIC
            y = cast_to_float8_e5m2_dynamic_bw(y, self.backward_config)
        return y

    def float8_pre_forward(self, x):
        if not self.enable_pre_and_post_forward:
            return
        if (
            self.is_amax_initialized
            and (not self.amax_and_scale_synced)
            and torch.is_grad_enabled()
        ):
            raise AssertionError(
                "amaxes and scales not synced, please call `sync_float8_amax_and_scale_history` before forward"
            )
        self.last_seen_input_dtype = x.dtype

    def float8_post_forward(self):
        if not self.enable_pre_and_post_forward:
            return
        # Ensure that calling forward again will fail until the user syncs
        # amaxes and scales
        self.is_amax_initialized = True
        self.amax_and_scale_synced = False

    def forward(self, input: torch.Tensor) -> torch.Tensor:
        if self.has_any_delayed_scaling:
            self.float8_pre_forward(input)

        x_fp8 = self.cast_x_to_float8(input, self.is_amax_initialized)
        w_fp8 = self.cast_w_to_float8(self.weight, self.is_amax_initialized)

        y = torch.matmul(x_fp8, w_fp8.t())

        # Cast gradY to float8_e5m2 during backward
        y = self.cast_y_to_float8_in_bw(y)

        if self.bias is not None:
            y = y + self.bias.to(y.dtype)

        if self.has_any_delayed_scaling:
            self.float8_post_forward()
        return y

    def scaling_repr(self):
        # add scaling settings without using too many characters
        # example: "x:del,w:del,dldy:dyn"
        return f"x:{self.scaling_type_x.short_str()},w:{self.scaling_type_w.short_str()},dldy:{self.scaling_type_dL_dY.short_str()}"

    def extra_repr(self):
        s = f'{super().extra_repr()}, scaling="{self.scaling_repr()}"'
        return s

    @classmethod
    def from_float(
        cls,
        mod,
        emulate: bool = False,
        scaling_type_x=TensorScalingType.DYNAMIC,
        scaling_type_w=TensorScalingType.DYNAMIC,
        scaling_type_dL_dY=TensorScalingType.DYNAMIC,
    ):
        """
        Create an nn.Linear with fp8 compute from a regular nn.Linear

        Args:
            mod (torch.nn.Linear): nn.Linear to convert
            emulate (bool): whether to emulate fp8 matmul logic in float32
        """
        with torch.device("meta"):
            new_mod = cls(
                mod.in_features,
                mod.out_features,
                bias=False,
                scaling_type_x=scaling_type_x,
                scaling_type_w=scaling_type_w,
                scaling_type_dL_dY=scaling_type_dL_dY,
                emulate=emulate,
            )
        if (
            scaling_type_w == TensorScalingType.DYNAMIC
            and config.enable_fsdp_fp8_all_gather
        ):
            new_mod.weight = torch.nn.Parameter(
                WeightWithDynamicFloat8CastTensor(mod.weight, new_mod.forward_config)
            )
        else:
            assert not config.enable_fsdp_fp8_all_gather, "unsupported"
            new_mod.weight = mod.weight
        new_mod.bias = mod.bias
        # need to create buffers again when moving from meta device to
        # real device
        new_mod.create_buffers()
        return new_mod
