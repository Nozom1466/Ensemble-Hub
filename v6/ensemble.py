from __future__ import annotations

import logging
from typing import Dict, List
import torch

from v6.generator import BaseGenerator


# ---------------------------------------------------------------------------
# Logging / constants
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("ensemble_inference")

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
SYSTEM_PROMPT = "Solve the following math problem step by step. Write your reasoning clearly using LaTeX. Box the final answer using \\boxed{}."


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

    def render_dict(self) -> Dict[str, str]:
        """
        Render the full prompt to be fed into a language model.
        It includes the system message, user input, and accumulated assistant responses.
        """
        dicts = {
            "instruction": self.system,
            "input": self.question.strip(),
            "output": "".join(self.assistant_parts)
        }
        return dicts

    def render_list(self) -> list[dict[str, str]]:
        """
        Render the full prompt to be fed into a language model.
        It includes the system message, user input, and accumulated assistant responses.
        """
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self.question.strip()},
            {"role": "assistant", "content": "".join(self.assistant_parts)},
        ]

        return messages






# ---------------------------------------------------------------------------
# EnsembleReasoner: multi-model decoding loop with step-wise reward scoring
# ---------------------------------------------------------------------------

class EnsembleReasoner:
    def __init__(self, generators: List[BaseGenerator], scorers, max_rounds: int = 500,
                 score_threshold: float = 0.5, accumulate_context: bool = True):
        self.generators = generators
        self.scorers = scorers
        self.max_rounds = max_rounds
        self.score_threshold = score_threshold
        self.accumulate_context = accumulate_context


    def __call__(self, example) -> str:
        """
        Iteratively decode using multiple generators.
        In each round, the best candidate (with highest reward) is selected and appended.
        Generation stops early if
        1) reward is low,
        2) the chosen candidate emits EOS,
        3) every generator has emitted EOS at least once across rounds, or
        4) the chosen segment has appeared `max_repeat` times (to avoid infinite loops).
        """

        convo = ConversationTemplate(SYSTEM_PROMPT, example["input"])

        eos_flags = {g.name: False for g in self.generators}  # 记录各模型是否曾输出过 EOS
        segment_counter: Dict[str, int] = {}                  # 统计已选片段出现次数
        max_repeat = 3                                        # 片段允许重复次数阈值

        for rnd in range(1, self.max_rounds + 1):
            prompt = convo.render()

            # ─── 过滤长度超限的 generator ───────────────────────────────────────
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
            # ───────────────────────────────────────────────────────────────────

            dicts = convo.render_dict()

            # ─── 并行生成 ───────────────────────────────────────────────────────
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=len(available_gens)) as executor:
                outs = list(executor.map(lambda g: g.generate(dicts), available_gens))
            # ───────────────────────────────────────────────────────────────────

            filtered = [(g, o) for g, o in zip(available_gens, outs) if is_valid_segment(o.text)]
            if not filtered:
                logger.info("No valid outputs from generators; skipping this round.")
                continue
            available_gens, outs = zip(*filtered)
            segs = [o.text for o in outs]

            # 更新各模型 EOS 状态
            for g, o in zip(available_gens, outs):
                if o.ended_with_eos:
                    eos_flags[g.name] = True
            if all(eos_flags.values()):
                logger.info("Early stop: all models have emitted EOS at least once")
                break

            # 计算奖励分数
            scores = self.scorers.score(prompt, segs)

            for g, t, s in zip(available_gens, segs, scores):
                logger.info(f"→ {g.name} | {s:.2f} | {t.replace(chr(10), '\\n')}")

            best_idx = int(torch.tensor(scores).argmax())
            best_out = outs[best_idx]
            best_score = scores[best_idx]

            if best_score < self.score_threshold:
                logger.info("Stop: best score %.2f < threshold", best_score)
                continue

            # ─── 重复文本检测 ─────────────────────────────────────────────────
            segment_counter[best_out.text] = segment_counter.get(best_out.text, 0) + 1
            if segment_counter[best_out.text] >= max_repeat:
                logger.info("Early stop: segment repeated %d times → \"%s\"",
                            segment_counter[best_out.text], best_out.text)
                break
            # ──────────────────────────────────────────────────────────────────

            convo.add_assistant(best_out.text)
            # logger.info("Updated conversation:\n%s", convo.render())

            # 若本轮最佳候选已输出 EOS，也可直接终止
            if best_out.ended_with_eos:
                logger.info("Early stop: EOS token emitted by best model")
                break

        return "\n".join(convo.assistant_parts)


def is_valid_segment(text: str, min_len: int = 5) -> bool:
    import string
    import re
    stripped = text.strip()
    if len(stripped) < min_len:
        return False
    if not stripped or stripped.isspace():
        return False
    if all(c in string.punctuation + string.whitespace for c in stripped):
        return False
    cleaned = re.sub(r"\s+", "", stripped)
    if len(set(cleaned)) <= 2:
        return False
    if re.search(r"(.)\1{4,}", cleaned):
        return False
    return True

