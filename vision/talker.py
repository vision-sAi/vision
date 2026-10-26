"""CUDA-graph decode loop for the Qwen3-TTS talker.

The library drives the talker through transformers' generate(): every 80 ms frame is one forward of the
1.9B talker plus a 15-step generate() of the 175M code predictor, and most of the wall time is Python
and kernel-launch overhead rather than GPU work. This keeps the model's weights and forward code but
runs the decoding itself: prefill once eagerly, then per frame one CUDA-graph replay for the talker step
and one for the whole code-predictor chain (sampling included), on static KV caches with a static
attention mask and Gumbel-max sampling, so nothing on the GPU has to synchronise until the end-of-speech
check. Same sampling rules as the library (repetition penalty, suppressed tokens, min/max tokens,
temperature, top-k, top-p), so the speech is the same; it just arrives faster."""
from __future__ import annotations

import types
from typing import Callable

import torch
from transformers import StaticCache

NEG = float("-inf")


def _gumbel_sample(logits: torch.Tensor, temperature: float, top_k: int, top_p: float) -> torch.Tensor:
    """One token per row from softmax(logits / T) after top-k / top-p, without a sync (argmax of
    logits + Gumbel noise draws from exactly that distribution)."""
    logits = logits.float() / max(float(temperature), 1e-5)
    if top_k and top_k < logits.shape[-1]:
        kth = torch.topk(logits, top_k).values[..., -1:]
        logits = logits.masked_fill(logits < kth, NEG)
    if top_p < 1.0:
        sorted_logits, order = torch.sort(logits, descending=True)
        probs = sorted_logits.softmax(-1)
        drop = probs.cumsum(-1) - probs > top_p
        logits = logits.masked_fill(drop.scatter(-1, order, drop), NEG)
    noise = -torch.log(-torch.log(torch.rand_like(logits).clamp_(1e-20, 1.0)))
    return (logits + noise).argmax(-1)


class FastTalker:
    """Drop-in for `talker.generate(...)` as Qwen3TTSForConditionalGeneration.generate calls it."""

    def __init__(self, core, max_len: int = 1536):
        self.talker = core.talker
        self.cp = core.talker.code_predictor
        tc = core.config.talker_config
        self.groups = tc.num_code_groups  # codes per frame (16)
        self.eos = tc.codec_eos_token_id
        self.max_len = max_len
        p = next(self.talker.parameters())
        self.device, self.dtype = p.device, p.dtype
        d = tc.hidden_size
        dev = self.device
        self.on_frame: Callable[[torch.Tensor], None] | None = None  # gets each [groups] frame as it is made
        # static state the graphs read and write
        self.cache = StaticCache(self.talker.config, max_len)
        self.cp_cache = StaticCache(self.cp.config, self.groups)
        self.s_codes = torch.zeros(1, self.groups, dtype=torch.long, device=dev)  # previous frame
        self.s_extra = torch.zeros(1, 1, d, dtype=self.dtype, device=dev)  # text hidden / pad for this step
        self.s_pos = torch.zeros(1, dtype=torch.long, device=dev)
        self.s_delta = torch.zeros(1, dtype=torch.long, device=dev)  # rope delta from the prefill
        self.s_mask = torch.zeros(1, 1, 1, max_len, dtype=torch.bool, device=dev)
        self.s_tok = torch.zeros(1, 1, dtype=torch.long, device=dev)  # first code of the frame being filled
        self.s_out = torch.zeros(1, self.groups - 1, dtype=torch.long, device=dev)  # the other codes
        tri = torch.tril(torch.ones(self.groups, self.groups, dtype=torch.bool, device=dev))
        self.cp_mask0 = tri[:2].reshape(1, 1, 2, self.groups)
        self.cp_masks = [tri[s + 1].reshape(1, 1, 1, self.groups) for s in range(1, self.groups - 1)]
        self.cp_pos0 = torch.arange(2, device=dev)
        self.cp_pos = [torch.tensor([s + 1], device=dev) for s in range(1, self.groups - 1)]
        self.o_hidden = torch.zeros(1, 1, d, dtype=self.dtype, device=dev)  # talker step output
        self.o_logits = None
        self.g_talker = self.g_pred = None
        self._sub = None  # sub-talker sampling settings the predictor graph was captured with
        self._suppress: tuple[tuple, torch.Tensor] | None = None

    # -- the two pieces of work that become graphs
    def _talker_step(self):
        tables = self.cp.get_input_embeddings()
        emb = self.talker.get_input_embeddings()(self.s_codes[:, :1])
        for i in range(self.groups - 1):
            emb = emb + tables[i](self.s_codes[:, i + 1 : i + 2])
        pos = (self.s_pos + self.s_delta).reshape(1, 1, 1).expand(3, 1, 1)
        out = self.talker.model(
            inputs_embeds=emb + self.s_extra, attention_mask=self.s_mask, position_ids=pos,
            past_key_values=self.cache, use_cache=True, cache_position=self.s_pos,
        )
        h = out.last_hidden_state
        return h, self.talker.codec_head(h)

    def _predictor_chain(self, temperature: float, top_k: int, top_p: float, do_sample: bool):
        pick = (lambda lg: _gumbel_sample(lg, temperature, top_k, top_p)) if do_sample else (lambda lg: lg.argmax(-1))
        x = torch.cat((self.o_hidden, self.talker.get_input_embeddings()(self.s_tok)), dim=1)
        out = self.cp(
            inputs_embeds=x, attention_mask={"full_attention": self.cp_mask0},
            past_key_values=self.cp_cache, use_cache=True, cache_position=self.cp_pos0,
        )
        tok = pick(out.logits[:, -1])
        self.s_out[:, 0].copy_(tok)
        for s in range(1, self.groups - 1):
            out = self.cp(
                input_ids=tok.reshape(1, 1), attention_mask={"full_attention": self.cp_masks[s - 1]},
                past_key_values=self.cp_cache, use_cache=True, cache_position=self.cp_pos[s - 1], generation_steps=s,
            )
            tok = pick(out.logits[:, -1])
            self.s_out[:, s].copy_(tok)

    def _capture(self, sub):
        """Warm up on a side stream, then record both graphs (each with its own memory pool)."""
        stream = torch.cuda.Stream()
        stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(stream), torch.inference_mode():
            for _ in range(2):
                self._talker_step()
        torch.cuda.current_stream().wait_stream(stream)
        self.g_talker = torch.cuda.CUDAGraph()
        with torch.cuda.graph(self.g_talker), torch.inference_mode():
            h, logits = self._talker_step()
        self.o_hidden, self.o_logits = h, logits
        stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(stream), torch.inference_mode():
            for _ in range(2):
                self._predictor_chain(*sub)
        torch.cuda.current_stream().wait_stream(stream)
        self.g_pred = torch.cuda.CUDAGraph()
        with torch.cuda.graph(self.g_pred), torch.inference_mode():
            self._predictor_chain(*sub)
        self._sub = sub

    # -- the loop
    def _first_code(self, logits, history, k, min_new_tokens, suppress, eos, do_sample, temperature, top_k, top_p, penalty):
        """The frame's first code, with transformers' logits processors applied in its order."""
        logits = logits.float().clone()
        if k and penalty != 1.0:
            seen = history[:k].reshape(1, -1)
            score = logits.gather(1, seen)
            logits.scatter_(1, seen, torch.where(score < 0, score * penalty, score / penalty))
        if k < min_new_tokens:
            logits[:, eos] = NEG
        if suppress is not None:
            logits[:, suppress] = NEG
        if not do_sample:
            return logits.argmax(-1)
        return _gumbel_sample(logits, temperature, top_k, top_p)

    @torch.inference_mode()
    def generate(
        self, inputs_embeds=None, attention_mask=None, trailing_text_hidden=None, tts_pad_embed=None,
        max_new_tokens: int = 2048, min_new_tokens: int = 2, do_sample: bool = True, top_k: int = 50,
        top_p: float = 1.0, temperature: float = 0.9, subtalker_dosample: bool = True, subtalker_top_k: int = 50,
        subtalker_top_p: float = 1.0, subtalker_temperature: float = 0.9, eos_token_id=None,
        repetition_penalty: float = 1.05, suppress_tokens=None, **_ignored,
    ):
        if inputs_embeds.shape[0] != 1:
            raise ValueError("FastTalker decodes one sequence at a time")
        sub = (float(subtalker_temperature), int(subtalker_top_k), float(subtalker_top_p), bool(subtalker_dosample))
        if self.g_pred is None or sub != self._sub:
            self._capture(sub)
        dev = self.device
        L = inputs_embeds.shape[1]
        budget = min(int(max_new_tokens), self.max_len - L - 1)
        if budget < 1:
            raise ValueError(f"prompt of {L} tokens leaves no room in a {self.max_len}-token cache")
        eos = self.eos if eos_token_id is None else int(eos_token_id)
        if suppress_tokens:
            key = tuple(suppress_tokens)
            if self._suppress is None or self._suppress[0] != key:
                self._suppress = (key, torch.tensor(key, dtype=torch.long, device=dev))
            suppress = self._suppress[1]
        else:
            suppress = None
        # prefill: the library's own forward, into the static cache
        self.s_mask.zero_()
        self.s_mask[..., :L] = True
        out = self.talker(
            inputs_embeds=inputs_embeds, attention_mask=attention_mask, past_key_values=self.cache, use_cache=True,
            cache_position=torch.arange(L, device=dev), trailing_text_hidden=trailing_text_hidden,
            tts_pad_embed=tts_pad_embed, output_hidden_states=False,
        )
        logits = out.logits[:, -1]
        self.o_hidden.copy_(out.past_hidden)
        self.s_delta.copy_(self.talker.rope_deltas.reshape(-1)[:1])
        history = torch.zeros(budget, dtype=torch.long, device=dev)
        n_text = trailing_text_hidden.shape[1]
        frames: list[torch.Tensor] = []
        for k in range(budget):
            tok = self._first_code(logits, history, k, min_new_tokens, suppress, eos, do_sample, temperature, top_k, top_p, repetition_penalty)
            if int(tok) == eos:  # the one sync per frame
                break
            history[k] = tok
            self.s_tok.copy_(tok.reshape(1, 1))
            self.g_pred.replay()
            frame = torch.cat((tok.reshape(1), self.s_out.reshape(-1))).clone()
            frames.append(frame)
            if self.on_frame is not None:
                self.on_frame(frame)
            # the frame's embeddings plus the next text hidden state feed the next talker step
            self.s_codes.copy_(frame.reshape(1, -1))
            self.s_extra.copy_(trailing_text_hidden[:, k : k + 1] if k < n_text else tts_pad_embed)
            pos = L + k
            self.s_pos.fill_(pos)
            self.s_mask[..., pos] = True
            self.g_talker.replay()
            logits = self.o_logits[:, -1]
        if not frames:  # nothing but end-of-speech: hand back one EOS frame so the caller trims to zero
            frames = [torch.full((self.groups,), eos, dtype=torch.long, device=dev)]
        # what Qwen3TTSForConditionalGeneration.generate reads: per step (hidden states tuple, codes)
        return types.SimpleNamespace(hidden_states=[((self.o_hidden,), f.reshape(1, -1)) for f in frames])


def install(core, max_len: int = 1536) -> FastTalker:
    """Route the library's talker.generate through a FastTalker on this model instance."""
    fast = FastTalker(core, max_len)
    core.talker.generate = fast.generate
    return fast
