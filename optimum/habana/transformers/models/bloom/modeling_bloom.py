import math
import os
import warnings
from typing import Optional, Tuple, Union

import torch
from torch.nn import CrossEntropyLoss
from torch.nn import functional as F
from transformers.modeling_outputs import BaseModelOutputWithPastAndCrossAttentions, CausalLMOutputWithCrossAttentions
from transformers.models.bloom.modeling_bloom import BloomForCausalLM, BloomMLP, BloomModel
from transformers.utils import logging


logger = logging.get_logger(__name__)


def build_alibi_slope_tensor(n_head: int) -> torch.Tensor:
    closest_power_of_2 = 2 ** math.floor(math.log2(n_head))
    base = torch.tensor(2 ** (-(2 ** -(math.log2(closest_power_of_2) - 3))), dtype=torch.float32)
    powers = torch.arange(1, 1 + closest_power_of_2, dtype=torch.int32)
    slopes = torch.pow(base, powers)

    if closest_power_of_2 != n_head:
        extra_base = torch.tensor(2 ** (-(2 ** -(math.log2(2 * closest_power_of_2) - 3))), dtype=torch.float32)
        num_remaining_heads = min(closest_power_of_2, n_head - closest_power_of_2)
        extra_powers = torch.arange(1, 1 + 2 * num_remaining_heads, 2, dtype=torch.int32)
        slopes = torch.cat([slopes, torch.pow(extra_base, extra_powers)], dim=0)
    return slopes


def gaudi_bloom_build_alibi_tensor(
    attention_mask: torch.Tensor, slopes: torch.Tensor, num_heads: int, dtype: torch.dtype
) -> torch.Tensor:
    """
    Link to paper: https://arxiv.org/abs/2108.12409 Alibi tensor is not causal as the original paper mentions, it
    relies on a translation invariance of softmax for quick implementation: with l being a tensor, and a fixed value
    `softmax(l+a) = softmax(l)`. Based on
    https://github.com/ofirpress/attention_with_linear_biases/blob/a35aaca144e0eb6b789dfcb46784c4b8e31b7983/fairseq/models/transformer.py#L742
    TODO @thomasw21 this doesn't work as nicely due to the masking strategy, and so masking varies slightly.

    Args:
    Returns tensor shaped (batch_size * num_heads, 1, max_seq_len)
        attention_mask (`torch.Tensor`):
            Token-wise attention mask, this should be of shape (batch_size, max_seq_len).
        num_heads (`int`, *required*):
            number of heads
        dtype (`torch.dtype`, *optional*, default=`torch.bfloat16`):
            dtype of the output tensor
    """
    batch_size = attention_mask.size()[0]
    max_seq_len = attention_mask.size()[1]
    alibi = slopes.unsqueeze(1).unsqueeze(1) * torch.arange(max_seq_len, device=attention_mask.device).unsqueeze(
        0
    ).unsqueeze(0).expand(num_heads, -1, -1)

    # Select the part of the tensor that corresponds to our tensor parallel index.
    tp_world_size = int(os.environ.get("WORLD_SIZE", 1))
    tp_index = int(os.environ.get("RANK", 0))
    alibi = alibi.reshape((tp_world_size, -1, *alibi.shape[1:]))[tp_index]

    alibi = alibi.repeat(batch_size, 1, 1)
    return alibi.to(dtype)


def gaudi_dropout_add(x: torch.Tensor, residual: torch.Tensor, prob: float, training: bool) -> torch.Tensor:
    """
    Dropout add function
    Args:
        x (`torch.tensor`, *required*):
            input tensor
        residual (`torch.tensor`, *required*):
            esidual tensor
        prob (`float`, *required*):
            dropout probability
        training (`bool`, *required*):
            training mode
    """
    out = F.dropout(x, p=prob, training=training) if training else x
    out = residual + out
    return out


def gaudi_bloom_attention_forward(
    self,
    hidden_states: torch.Tensor,
    residual: torch.Tensor,
    alibi: torch.Tensor,
    attention_mask: torch.Tensor,
    layer_past: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
    head_mask: Optional[torch.Tensor] = None,
    use_cache: bool = False,
    output_attentions: bool = False,
    token_idx=None,
):
    fused_qkv = self.query_key_value(hidden_states)  # [batch_size, seq_length, 3 x hidden_size]

    # 3 x [batch_size, seq_length, num_heads, head_dim]
    (query_layer, key_layer, value_layer) = self._split_heads(fused_qkv)

    batch_size, q_length, _, _ = query_layer.shape

    query_layer = query_layer.transpose(1, 2).reshape(batch_size * self.num_heads, q_length, self.head_dim)
    key_layer = (
        key_layer.permute(0, 2, 3, 1).reshape(batch_size * self.num_heads, self.head_dim, q_length).contiguous()
    )
    value_layer = value_layer.transpose(1, 2).reshape(batch_size * self.num_heads, q_length, self.head_dim)
    if layer_past is not None:
        past_key, past_value = layer_past
        # concatenate along seq_length dimension:
        #  - key: [batch_size * self.num_heads, head_dim, kv_length]
        #  - value: [batch_size * self.num_heads, kv_length, head_dim]
        if token_idx is not None:
            # HPU bug WA
            past_key.index_add_(2, token_idx - 1, key_layer - torch.index_select(past_key, 2, token_idx - 1))
            past_value.index_add_(1, token_idx - 1, value_layer - torch.index_select(past_value, 1, token_idx - 1))
            key_layer = past_key
            value_layer = past_value
        else:
            key_layer = torch.cat((past_key, key_layer), dim=2)
            value_layer = torch.cat((past_value, value_layer), dim=1)

    _, _, kv_length = key_layer.shape

    if use_cache is True:
        present = (key_layer, value_layer)
    else:
        present = None

    # [batch_size * num_heads, q_length, kv_length]
    # we use `torch.Tensor.baddbmm` instead of `torch.baddbmm` as the latter isn't supported by TorchScript v1.11
    matmul_result = alibi.baddbmm(
        batch1=query_layer,
        batch2=key_layer,
        beta=self.beta,
        alpha=self.inv_norm_factor,
    )

    # change view to [batch_size, num_heads, q_length, kv_length]
    attention_scores = matmul_result.view(batch_size, self.num_heads, q_length, kv_length)

    # cast attention scores to fp32, compute scaled softmax and cast back to initial dtype - [batch_size, num_heads, q_length, kv_length]
    input_dtype = attention_scores.dtype
    attn_weights = torch.masked_fill(attention_scores, attention_mask, torch.finfo(attention_scores.dtype).min)
    attention_probs = F.softmax(attn_weights, dim=-1, dtype=torch.float32).to(input_dtype)

    # [batch_size, num_heads, q_length, kv_length]
    if self.training:
        attention_probs = self.attention_dropout(attention_probs)

    if head_mask is not None:
        attention_probs = attention_probs * head_mask

    # change view [batch_size x num_heads, q_length, kv_length]
    attention_probs_reshaped = attention_probs.view(batch_size * self.num_heads, q_length, kv_length)

    # matmul: [batch_size * num_heads, q_length, head_dim]
    context_layer = torch.bmm(attention_probs_reshaped, value_layer)

    # change view [batch_size, num_heads, q_length, head_dim]
    context_layer = self._merge_heads(context_layer)

    # aggregate results across tp ranks. See here: https://github.com/pytorch/pytorch/issues/76232
    if self.pretraining_tp > 1 and self.slow_but_exact:
        slices = self.hidden_size / self.pretraining_tp
        output_tensor = torch.zeros_like(context_layer)
        for i in range(self.pretraining_tp):
            output_tensor = output_tensor + F.linear(
                context_layer[:, :, int(i * slices) : int((i + 1) * slices)],
                self.dense.weight[:, int(i * slices) : int((i + 1) * slices)],
            )
    else:
        output_tensor = self.dense(context_layer)

    output_tensor = gaudi_dropout_add(output_tensor, residual, self.hidden_dropout, self.training)

    outputs = (output_tensor, present)
    if output_attentions:
        outputs += (attention_probs,)

    return outputs


class GaudiBloomMLP(BloomMLP):
    def __init__(self, config):
        super().__init__(config)
        self.gelu_impl = torch.nn.GELU(approximate="tanh")


def gaudi_bloom_block_forward(
    self,
    hidden_states: torch.Tensor,
    alibi: torch.Tensor,
    attention_mask: torch.Tensor,
    layer_past: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
    head_mask: Optional[torch.Tensor] = None,
    use_cache: bool = False,
    output_attentions: bool = False,
    token_idx=None,
):
    # hidden_states: [batch_size, seq_length, hidden_size]

    # Layer norm at the beginning of the transformer layer.
    layernorm_output = self.input_layernorm(hidden_states)

    # Layer norm post the self attention.
    if self.apply_residual_connection_post_layernorm:
        residual = layernorm_output
    else:
        residual = hidden_states

    # Self attention.
    attn_outputs = self.self_attention(
        layernorm_output,
        residual,
        layer_past=layer_past,
        attention_mask=attention_mask,
        alibi=alibi,
        head_mask=head_mask,
        use_cache=use_cache,
        output_attentions=output_attentions,
        token_idx=token_idx,
    )

    attention_output = attn_outputs[0]

    outputs = attn_outputs[1:]

    layernorm_output = self.post_attention_layernorm(attention_output)

    # Get residual
    if self.apply_residual_connection_post_layernorm:
        residual = layernorm_output
    else:
        residual = attention_output

    # MLP.
    output = self.mlp(layernorm_output, residual)

    if use_cache:
        outputs = (output,) + outputs
    else:
        outputs = (output,) + outputs[1:]

    return outputs  # hidden_states, present, attentions


class GaudiBloomModel(BloomModel):
    def __init__(self, config):
        super().__init__(config)
        self.register_buffer("alibi_slope", build_alibi_slope_tensor(self.num_heads), persistent=False)

    def forward(
        self,
        input_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[Tuple[Tuple[torch.Tensor, torch.Tensor], ...]] = None,
        attention_mask: Optional[torch.Tensor] = None,
        head_mask: Optional[torch.LongTensor] = None,
        inputs_embeds: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        token_idx=None,
        **deprecated_arguments,
    ) -> Union[Tuple[torch.Tensor, ...], BaseModelOutputWithPastAndCrossAttentions]:
        if deprecated_arguments.pop("position_ids", False) is not False:
            # `position_ids` could have been `torch.Tensor` or `None` so defaulting pop to `False` allows to detect if users were passing explicitly `None`
            warnings.warn(
                "`position_ids` have no functionality in BLOOM and will be removed in v5.0.0. You can safely ignore"
                " passing `position_ids`.",
                FutureWarning,
            )
        if len(deprecated_arguments) > 0:
            raise ValueError(f"Got unexpected arguments: {deprecated_arguments}")

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            batch_size, seq_length = input_ids.shape
        elif inputs_embeds is not None:
            batch_size, seq_length, _ = inputs_embeds.shape
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        if past_key_values is None:
            past_key_values = tuple([None] * len(self.h))

        # Prepare head mask if needed
        # 1.0 in head_mask indicate we keep the head
        # attention_probs has shape batch_size x num_heads x N x N
        # head_mask has shape n_layer x batch x num_heads x N x N
        head_mask = self.get_head_mask(head_mask, self.config.n_layer)

        if inputs_embeds is None:
            inputs_embeds = self.word_embeddings(input_ids)

        hidden_states = self.word_embeddings_layernorm(inputs_embeds)

        presents = () if use_cache else None
        all_self_attentions = () if output_attentions else None
        all_hidden_states = () if output_hidden_states else None

        # Compute alibi tensor: check gaudi_bloom_build_alibi_tensor
        seq_length_with_past = seq_length
        past_key_values_length = 0
        if past_key_values[0] is not None:
            past_key_values_length = past_key_values[0][0].shape[2]
            seq_length_with_past = seq_length_with_past + past_key_values_length
        if attention_mask is None:
            attention_mask = torch.ones((batch_size, seq_length_with_past), device=hidden_states.device)
        else:
            attention_mask = attention_mask.to(hidden_states.device)

        alibi = gaudi_bloom_build_alibi_tensor(attention_mask, self.alibi_slope, self.num_heads, hidden_states.dtype)

        causal_mask = self._prepare_attn_mask(
            attention_mask,
            input_shape=(batch_size, seq_length),
            past_key_values_length=past_key_values_length,
        )

        for i, (block, layer_past) in enumerate(zip(self.h, past_key_values)):
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

            if self.gradient_checkpointing and self.training:
                if use_cache:
                    logger.warning(
                        "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`..."
                    )
                    use_cache = False

                def create_custom_forward(module):
                    def custom_forward(*inputs):
                        # None for past_key_value
                        return module(*inputs, use_cache=use_cache, output_attentions=output_attentions)

                    return custom_forward

                outputs = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(block),
                    hidden_states,
                    alibi,
                    causal_mask,
                    head_mask[i],
                )
            else:
                outputs = block(
                    hidden_states,
                    layer_past=layer_past,
                    attention_mask=causal_mask,
                    head_mask=head_mask[i],
                    use_cache=use_cache,
                    output_attentions=output_attentions,
                    alibi=alibi,
                    token_idx=token_idx,
                )

            hidden_states = outputs[0]
            if use_cache is True:
                presents = presents + (outputs[1],)

            if output_attentions:
                all_self_attentions = all_self_attentions + (outputs[2 if use_cache else 1],)

        # Add last hidden state
        hidden_states = self.ln_f(hidden_states)

        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        if not return_dict:
            return tuple(v for v in [hidden_states, presents, all_hidden_states, all_self_attentions] if v is not None)

        return BaseModelOutputWithPastAndCrossAttentions(
            last_hidden_state=hidden_states,
            past_key_values=presents,
            hidden_states=all_hidden_states,
            attentions=all_self_attentions,
        )


class GaudiBloomForCausalLM(BloomForCausalLM):
    def __init__(self, config):
        super().__init__(config)
        self.lm_head_chunks = []

    def split_lm_head(self):
        N = 2
        self.lm_head_chunks = [c.t() for c in self.lm_head.weight.chunk(N, dim=0)]

    def prepare_inputs_for_generation(
        self,
        input_ids: torch.LongTensor,
        past_key_values: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        token_idx: Optional[torch.Tensor] = None,
        **kwargs,
    ) -> dict:
        # only last token for input_ids if past is not None
        if past_key_values:
            if token_idx is not None:
                input_ids = torch.index_select(input_ids, 1, token_idx - 1)
            else:
                input_ids = input_ids[:, -1].unsqueeze(-1)

            # the cache may be in the stardard format (e.g. in contrastive search), convert to bloom's format if needed
            if past_key_values[0][0].shape[0] == input_ids.shape[0]:
                past_key_values = self._convert_to_bloom_cache(past_key_values)

        return {
            "input_ids": input_ids,
            "past_key_values": past_key_values,
            "use_cache": kwargs.get("use_cache"),
            "attention_mask": attention_mask,
            "token_idx": token_idx,
        }

    def forward(
        self,
        input_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[Tuple[Tuple[torch.Tensor, torch.Tensor], ...]] = None,
        attention_mask: Optional[torch.Tensor] = None,
        head_mask: Optional[torch.Tensor] = None,
        inputs_embeds: Optional[torch.Tensor] = None,
        labels: Optional[torch.Tensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        token_idx: Optional[bool] = None,
        **deprecated_arguments,
    ) -> Union[Tuple[torch.Tensor], CausalLMOutputWithCrossAttentions]:
        r"""
        labels (`torch.LongTensor` of shape `(batch_size, sequence_length)`, *optional*):
            Labels for language modeling. Note that the labels **are shifted** inside the model, i.e. you can set
            `labels = input_ids` Indices are selected in `[-100, 0, ..., config.vocab_size]` All labels set to `-100`
            are ignored (masked), the loss is only computed for labels in `[0, ..., config.vocab_size]`
        """
        if deprecated_arguments.pop("position_ids", False) is not False:
            # `position_ids` could have been `torch.Tensor` or `None` so defaulting pop to `False` allows to detect if users were passing explicitly `None`
            warnings.warn(
                "`position_ids` have no functionality in BLOOM and will be removed in v5.0.0. You can safely ignore"
                " passing `position_ids`.",
                FutureWarning,
            )
        if len(deprecated_arguments) > 0:
            raise ValueError(f"Got unexpected arguments: {deprecated_arguments}")

        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        transformer_outputs = self.transformer(
            input_ids,
            past_key_values=past_key_values,
            attention_mask=attention_mask,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            token_idx=token_idx,
        )
        hidden_states = transformer_outputs[0]

        if len(self.lm_head_chunks) > 0:
            lm_logits = torch.cat([torch.matmul(hidden_states, c) for c in self.lm_head_chunks], dim=-1)
        else:
            lm_logits = self.lm_head(hidden_states)

        loss = None
        if labels is not None:
            # move labels to correct device to enable model parallelism
            labels = labels.to(lm_logits.device)
            # Shift so that tokens < n predict n
            shift_logits = lm_logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            batch_size, seq_length, vocab_size = shift_logits.shape
            # Flatten the tokens
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(
                shift_logits.view(batch_size * seq_length, vocab_size), shift_labels.view(batch_size * seq_length)
            )

        if not return_dict:
            output = (lm_logits,) + transformer_outputs[1:]
            return ((loss,) + output) if loss is not None else output

        return CausalLMOutputWithCrossAttentions(
            loss=loss,
            logits=lm_logits,
            past_key_values=transformer_outputs.past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
        )
