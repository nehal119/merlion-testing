#
# Copyright (c) 2023 salesforce.com, inc.
# All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# For full license text, see the LICENSE file in the repo root or https://opensource.org/licenses/BSD-3-Clause
#
"""
Implementation of Transformer for time series data.
"""
import copy
import logging
import math

import numpy as np
import pandas as pd
from scipy.stats import norm

from typing import List, Optional, Tuple, Union
from abc import abstractmethod

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
except ImportError as e:
    err = (
        "Try installing Merlion with optional dependencies using `pip install salesforce-merlion[deep-learning]` or "
        "`pip install `salesforce-merlion[all]`"
    )
    raise ImportError(str(e) + ". " + err)


from merlion.models.base import NormalizingConfig
from merlion.models.deep_base import TorchModel
from merlion.models.forecast.deep_base import DeepForecasterConfig, DeepForecaster

from merlion.models.utils.nn_modules import FullAttention, AttentionLayer, DataEmbedding, ConvLayer

from merlion.models.utils.nn_modules.enc_dec_transformer import Encoder, EncoderLayer, Decoder, DecoderLayer

from merlion.utils.misc import initializer

logger = logging.getLogger(__name__)


class TransformerConfig(DeepForecasterConfig, NormalizingConfig):
    """
    Transformer for time series forecasting.
    Code adapted from https://github.com/thuml/Autoformer.
    """

    @initializer
    def __init__(
        self,
        n_past,
        max_forecast_steps: int = None,
        encoder_input_size: int = None,
        decoder_input_size: int = None,
        num_encoder_layers: int = 2,
        num_decoder_layers: int = 1,
        start_token_len: int = 0,
        factor: int = 3,
        model_dim: int = 512,
        embed: str = "timeF",
        dropout: float = 0.05,
        activation: str = "gelu",
        n_heads: int = 8,
        fcn_dim: int = 2048,
        distil: bool = True,
        **kwargs
    ):
        """
        :param n_past: # of past steps used for forecasting future.
        :param max_forecast_steps:  Max # of steps we would like to forecast for.
        :param encoder_input_size: Input size of encoder. If ``encoder_input_size = None``,
            then the model will automatically use ``config.dim``,  which is the dimension of the input data.
        :param decoder_input_size: Input size of decoder. If ``decoder_input_size = None``,
            then the model will automatically use ``config.dim``, which is the dimension of the input data.
        :param num_encoder_layers: Number of encoder layers.
        :param num_decoder_layers: Number of decoder layers.
        :param start_token_len: Length of start token for deep transformer encoder-decoder based models.
            The start token is similar to the special tokens for NLP models (e.g., bos, sep, eos tokens).
        :param factor: Attention factor.
        :param model_dim: Dimension of the model.
        :param embed: Time feature encoding type, options include ``timeF``, ``fixed`` and ``learned``.
        :param dropout: dropout rate.
        :param activation: Activation function, can be ``gelu``, ``relu``, ``sigmoid``, etc.
        :param n_heads: Number of heads of the model.
        :param fcn_dim: Hidden dimension of the MLP layer in the model.
        :param distil: whether to use distilling in the encoder of the model.
        """

        super().__init__(n_past=n_past, max_forecast_steps=max_forecast_steps, **kwargs)


class TransformerModel(TorchModel):
    """
    Implementaion of Transformer deep torch model.
    """

    def __init__(self, config: TransformerConfig):
        super().__init__(config)

        if config.dim is not None:
            config.encoder_input_size = config.dim if config.encoder_input_size is None else config.encoder_input_size
            config.decoder_input_size = (
                config.encoder_input_size if config.decoder_input_size is None else config.decoder_input_size
            )

        config.c_out = config.encoder_input_size

        self.n_past = config.n_past
        self.start_token_len = config.start_token_len
        self.max_forecast_steps = config.max_forecast_steps

        self.enc_embedding = DataEmbedding(
            config.encoder_input_size, config.model_dim, config.embed, config.ts_encoding, config.dropout
        )

        self.dec_embedding = DataEmbedding(
            config.decoder_input_size, config.model_dim, config.embed, config.ts_encoding, config.dropout
        )

        # Encoder
        self.encoder = Encoder(
            [
                EncoderLayer(
                    AttentionLayer(
                        FullAttention(False, config.factor, attention_dropout=config.dropout, output_attention=False),
                        config.model_dim,
                        config.n_heads,
                    ),
                    config.model_dim,
                    config.fcn_dim,
                    dropout=config.dropout,
                    activation=config.activation,
                )
                for l in range(config.num_encoder_layers)
            ],
            norm_layer=torch.nn.LayerNorm(config.model_dim),
        )

        # Decoder
        self.decoder = Decoder(
            [
                DecoderLayer(
                    AttentionLayer(
                        FullAttention(True, config.factor, attention_dropout=config.dropout, output_attention=False),
                        config.model_dim,
                        config.n_heads,
                    ),
                    AttentionLayer(
                        FullAttention(False, config.factor, attention_dropout=config.dropout, output_attention=False),
                        config.model_dim,
                        config.n_heads,
                    ),
                    config.model_dim,
                    config.fcn_dim,
                    dropout=config.dropout,
                    activation=config.activation,
                )
                for l in range(config.num_decoder_layers)
            ],
            norm_layer=torch.nn.LayerNorm(config.model_dim),
            projection=nn.Linear(config.model_dim, config.c_out, bias=True),
        )

    def forward(
        self,
        past,
        past_timestamp,
        future_timestamp,
        enc_self_mask=None,
        dec_self_mask=None,
        dec_enc_mask=None,
        **kwargs
    ):
        config = self.config

        start_token = past[:, (past.shape[1] - self.start_token_len) :]
        dec_inp = torch.zeros(
            past.shape[0], self.max_forecast_steps, config.decoder_input_size, dtype=torch.float, device=self.device
        )
        dec_inp = torch.cat([start_token, dec_inp], dim=1)

        future_timestamp = torch.cat(
            [past_timestamp[:, (past_timestamp.shape[1] - self.start_token_len) :], future_timestamp], dim=1
        )

        enc_out = self.enc_embedding(past, past_timestamp)
        enc_out, attns = self.encoder(enc_out, attn_mask=enc_self_mask)

        dec_out = self.dec_embedding(dec_inp, future_timestamp)
        dec_out = self.decoder(dec_out, enc_out, x_mask=dec_self_mask, cross_mask=dec_enc_mask)

        if self.config.target_seq_index is not None:
            return dec_out[:, -self.max_forecast_steps :, :1]
        else:
            return dec_out[:, -self.max_forecast_steps :, :]


class TransformerForecaster(DeepForecaster):
    """
    Implementaion of Transformer deep forecaster
    """

    config_class = TransformerConfig
    deep_model_class = TransformerModel

    def __init__(self, config: TransformerConfig):
        super().__init__(config)
