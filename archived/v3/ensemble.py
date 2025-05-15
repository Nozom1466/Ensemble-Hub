from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import torch
import torch.nn.functional as F
from transformers import (
    AutoModel,
    AutoModelForCausalLM,
    AutoTokenizer,
    GenerationConfig,
    PreTrainedTokenizerBase,
)

# Optional vLLM backend -----------------------------------------------------
try:
    from vllm import LLM, SamplingParams  # type: ignore

    _VLLM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _VLLM_AVAILABLE = False

# ---------------------------------------------------------------------------
# Logging / constants
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("ensemble_inference")

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
EOS_TEXT = ""  # Most Qwen / Llama models use empty string as EOS
STEP_TOKEN = "<extra_0>"  # Token separator used by reward model
SYSTEM_PROMPT = "You are a helpful assistant."
STOP_TOKENS_TEXT = {".", "\n"}  # Stop decoding after these tokens


# ---------------------------------------------------------------------------
# Conversation Template
# ---------------------------------------------------------------------------

class ConversationTemplate:
    """
    A conversation template for constructing dialogue prompts.
    It includes a system prompt, a single user question, and accumulated assistant responses.
    """

    def __init__(self, system_prompt: str, initial_question: str):
        self.system = system_prompt
        self.question = initial_question
        self.assistant_parts: List[str] = []  # Collected assistant responses

    def add_assistant(self, content: str):
        """Append a new assistant response to the prompt context."""
        self.assistant_parts.append(content.strip())

    def render(self) -> str:
        """
        Render the full prompt to be fed into a language model.
        It includes the system message, user input, and accumulated assistant responses.
        """
        lines = [
            f"[SYSTEM] {self.system} [/SYSTEM]",
            f"<user>\n{self.question.strip()}\n</user>",
            f"<assistant>\n" + "\n".join(self.assistant_parts)
        ]
        return "".join(lines)


# ---------------------------------------------------------------------------
# Utility: trim text at the last occurrence of stop tokens
# ---------------------------------------------------------------------------

def _trim_text(txt: str) -> str:
    """Truncate the text after the last known stop token for cleaner outputs."""
    best_pos = -1
    best_tok = None
    for tok in STOP_TOKENS_TEXT:
        pos = txt.rfind(tok)
        if pos > best_pos:
            best_pos = pos
            best_tok = tok
    if best_pos != -1:
        return txt[: best_pos + len(best_tok)]
    return txt


# ---------------------------------------------------------------------------
# Utility: extract token-level reward scores from logits
# ---------------------------------------------------------------------------

def _step_rewards(logits: torch.Tensor, mask: torch.Tensor):
    """
    Compute step-wise probabilities using softmax over logits.
    Only consider positions where mask is non-zero (STEP_TOKEN positions).
    """
    probs = F.softmax(logits, dim=-1) * mask.unsqueeze(-1)
    arr: List[List[float]] = []
    for sample in probs:
        pos = sample[sample != 0].view(-1, 2)[:, 1]
        arr.append(pos.cpu().tolist())
    return arr


# ---------------------------------------------------------------------------
# Output container for model generation
# ---------------------------------------------------------------------------

@dataclass
class GenOutput:
    text: str
    ended_with_eos: bool  # Whether EOS token was generated


# ---------------------------------------------------------------------------
# Abstract base class for any generator (HF or vLLM)
# ---------------------------------------------------------------------------

class BaseGenerator:
    name: str

    def generate(self, prompt: str, **kw) -> GenOutput:
        """Abstract method for generating model outputs."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# HuggingFace Transformers-based Generator
# ---------------------------------------------------------------------------

class HFGenerator(BaseGenerator):
    def __init__(self, path: str, *, device: str = "auto", dtype: torch.dtype = torch.bfloat16):
        self.tokenizer = AutoTokenizer.from_pretrained(path, trust_remote_code=True)
        self.model = AutoModelForCausalLM.from_pretrained(
            path,
            torch_dtype=dtype,
            device_map=device,
            trust_remote_code=True
        ).eval()
        self.name = path
        self.device = next(self.model.parameters()).device if device == "auto" else torch.device(device)

        # Optional stop string list
        self.stop_strings = list(STOP_TOKENS_TEXT) + [
            self.tokenizer.decode([self.tokenizer.eos_token_id], skip_special_tokens=False)
        ]

    @torch.inference_mode()
    def generate(self, prompt: str, *, max_tokens=64, temperature=0.95, top_p=0.7) -> GenOutput:
        ids = self.tokenizer(prompt, return_tensors="pt").to(self.device)
        cfg = GenerationConfig(
            do_sample=True,
            temperature=temperature,
            top_p=top_p,
            max_new_tokens=max_tokens,
            pad_token_id=self.tokenizer.eos_token_id,
        )
        out = self.model.generate(**ids, generation_config=cfg, tokenizer=self.tokenizer)[0]
        ended = bool(self.tokenizer.eos_token_id in out)
        txt = self.tokenizer.decode(out[len(ids["input_ids"][0]):], skip_special_tokens=False)
        return GenOutput(_trim_text(txt) if not ended else txt, ended)


# ---------------------------------------------------------------------------
# vLLM-based Generator
# ---------------------------------------------------------------------------

class VLLMGenerator(BaseGenerator):
    def __init__(self, path: str):
        if not _VLLM_AVAILABLE:
            raise RuntimeError("vLLM is not installed.")
        self._llm = LLM(model=path)
        self._sp = SamplingParams(max_tokens=128, temperature=0.95, top_p=0.7, stop=list(STOP_TOKENS_TEXT))
        self.name = path
        self._eos_text = EOS_TEXT

    @torch.inference_mode()
    def generate(self, prompt: str, *, max_tokens=30, temperature=0.95, top_p=0.7) -> GenOutput:
        self._sp.max_tokens, self._sp.temperature, self._sp.top_p = max_tokens, temperature, top_p
        txt = self._llm.generate([prompt], self._sp)[0].outputs[0].text
        ended = txt.endswith(self._eos_text)
        return GenOutput(_trim_text(txt), ended)


# ---------------------------------------------------------------------------
# ModelPool: caches all loaded generators and reward models
# ---------------------------------------------------------------------------

class ModelPool:
    _gen_cache: Dict[Tuple[str, str], BaseGenerator] = {}
    _reward_cache: Dict[str, str] = {}

    @classmethod
    def get_generator(cls, path: str, engine: str = "hf", device: Optional[str] = None) -> BaseGenerator:
        """
        Load a generator model (e.g., HF or vLLM) to a specified device (e.g., 'cuda:0', 'cpu').
        """
        key = (engine, path)
        if key not in cls._gen_cache:
            logger.info("[Pool] loading %s (%s)", path, engine)

            resolved_device = device or "auto"
            logger.info(f"→ Assigned to device: {resolved_device}")

            if engine == "hf":
                cls._gen_cache[key] = HFGenerator(path, device=resolved_device)
            elif engine == "vllm":
                cls._gen_cache[key] = VLLMGenerator(path)  # vLLM usually uses global config
            else:
                raise ValueError(f"Unknown engine: {engine}")
        return cls._gen_cache[key]

    @classmethod
    def get_reward(cls, path: str, device: Optional[str] = None) -> "PRMScorer":
        """
        Load a reward model to a specified device (e.g., 'cuda:0', 'cpu').
        """
        if path not in cls._reward_cache:
            logger.info("[Pool] loading reward model %s", path)
            resolved_device = device or "auto"
            logger.info(f"→ Reward model assigned to device: {resolved_device}")
            cls._reward_cache[path] = PRMScorer(path, device=resolved_device)
        return cls._reward_cache[path]


# ---------------------------------------------------------------------------
# PRMScorer: reward model used for evaluating step-level outputs
# ---------------------------------------------------------------------------

class PRMScorer:
    def __init__(self, path: str, device: str = "auto"):
        self.tok = AutoTokenizer.from_pretrained(path, trust_remote_code=True)
        self.mod = AutoModel.from_pretrained(
            path,
            torch_dtype=torch.bfloat16,
            device_map=device,
            trust_remote_code=True
        ).eval()
        self.sep_id = self.tok.encode(STEP_TOKEN)[0]

    @torch.inference_mode()
    def score(self, question: str, answer: str) -> float:
        """Compute reward score from model output at STEP_TOKEN positions."""
        if not answer.endswith(STEP_TOKEN):
            answer += STEP_TOKEN
        msgs = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ]
        convo = self.tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=False)
        ids = self.tok(convo, return_tensors="pt").input_ids
        mask = ids == self.sep_id
        probs = _step_rewards(self.mod(ids).logits, mask)[0]
        return float(sum(probs) / len(probs) * 10.0) if probs else 0.0

    @torch.inference_mode()
    def score_batch_augmented(self, prompt: str, completions: List[str]) -> List[float]:
        """
        Efficiently score a batch of completions using the format:
        [prompt + STEP_TOKEN + completion + STEP_TOKEN]
        """
        inputs = [prompt + STEP_TOKEN + c + STEP_TOKEN for c in completions]
        enc = self.tok(inputs, return_tensors="pt", padding=True, truncation=True).to(self.mod.device)
        mask = enc["input_ids"] == self.sep_id
        logits = self.mod(**enc).logits
        probs = _step_rewards(logits, mask)
        return [float(sum(p) / len(p) * 10.0) if p else 0.0 for p in probs]


# ---------------------------------------------------------------------------
# EnsembleReasoner: multi-model decoding loop with step-wise reward scoring
# ---------------------------------------------------------------------------

class EnsembleReasoner:
    def __init__(self, generators: List[BaseGenerator], scorer: PRMScorer, max_rounds: int = 500,
                 score_threshold: float = 0.5, accumulate_context: bool = True):
        self.generators = generators
        self.scorer = scorer
        self.max_rounds = max_rounds
        self.score_threshold = score_threshold
        self.accumulate_context = accumulate_context

    def __call__(self, question: str) -> str:
        """
        Iteratively decode using multiple generators.
        In each round, the best candidate (with highest reward) is selected and appended.
        Generation stops early if reward is low or EOS is emitted.
        """
        convo = ConversationTemplate(SYSTEM_PROMPT, question)

        for rnd in range(1, self.max_rounds + 1):
            prompt = convo.render()

            # Filter out generators that exceed input length
            available_gens: List[BaseGenerator] = []
            for g in self.generators:
                tok = getattr(g, "tokenizer", None)
                if tok is not None:
                    length = tok(prompt, return_tensors="pt").input_ids.size(1)
                    if length > tok.model_max_length:
                        logger.info("Skip %s: prompt length %d > max %d",
                                    g.name, length, tok.model_max_length)
                        continue
                available_gens.append(g)

            if not available_gens:
                logger.error("No generators available for current prompt length; stopping early.")
                break

            # outs = [g.generate(prompt) for g in available_gens]
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=len(available_gens)) as executor:
                outs = list(executor.map(lambda g: g.generate(prompt), available_gens))

            segs = [o.text for o in outs]

            # Score each candidate using prompt + STEP_TOKEN + candidate + STEP_TOKEN
            # scores = []
            # for o in outs:
            #     augmented = prompt + STEP_TOKEN + o.text + STEP_TOKEN
            #     scores.append(self.scorer.score(question, augmented))
            completions = [o.text for o in outs]
            scores = self.scorer.score_batch_augmented(prompt, completions)

            for g, t, s in zip(available_gens, segs, scores):
                logger.info(f"→ {g.name} | {s:.2f} | {t.replace(chr(10), '\\n')}")

            best_idx = int(torch.tensor(scores).argmax())
            best_out = outs[best_idx]
            best_score = scores[best_idx]

            if best_score < self.score_threshold:
                logger.info("Stop: best score %.2f < threshold", best_score)
                continue

            convo.add_assistant(best_out.text)

            if best_out.ended_with_eos:
                logger.info("Early stop: EOS token emitted")
                break

        # Return the final composed assistant response
        return "\n".join(convo.assistant_parts)



