"""Local-only adapter for a trusted UnSmile tutorial pickle.

The notebook saves a PyTorch sequence classifier, not a tokenizer or policy
decision. Loading pickle executes Python code, so callers must explicitly opt
in and supply a file they created or inspected themselves.
"""

import pickle
from pathlib import Path

LABELS = ("여성/가족", "남성", "성소수자", "인종/국적", "연령", "지역",
          "종교", "기타 혐오", "악플/욕설", "clean")


class UnSmilePickle:
    def __init__(self, model, tokenizer, *, threshold: float = 0.5):
        if not 0 < threshold < 1:
            raise ValueError("Threshold must be between 0 and 1")
        configured = getattr(getattr(model, "config", None), "id2label", None)
        if not isinstance(configured, dict) or tuple(configured.get(i, configured.get(str(i))) for i in range(10)) != LABELS:
            raise ValueError("Model label order differs from the UnSmile tutorial")
        model.eval()
        self.model = model
        self.tokenizer = tokenizer
        self.threshold = threshold

    @classmethod
    def load(cls, path: Path, tokenizer_name: str, *, threshold: float = 0.5,
             trust_local_pickle: bool = False):
        if not trust_local_pickle:
            raise ValueError("Explicit trust_local_pickle is required")
        if not path.is_file() or not tokenizer_name.strip():
            raise ValueError("Model pickle and matching tokenizer are required")
        import torch
        from transformers import AutoTokenizer

        try:
            with path.open("rb") as stream:
                model = pickle.load(stream)  # Only an explicitly trusted local file.
        except (AttributeError, ModuleNotFoundError) as error:
            raise ValueError(
                "Model pickle requires the compatible PyTorch/Transformers versions used when it was saved"
            ) from error
        if not hasattr(model, "config") or not hasattr(model, "eval"):
            raise ValueError("Pickle does not contain a sequence classifier")
        model.to("cpu")
        tokenizer = AutoTokenizer.from_pretrained(tokenizer_name)
        return cls(model, tokenizer, threshold=threshold)

    def predict(self, text: str) -> dict:
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise ValueError("Text must contain 1..500 characters")
        import torch

        encoded = self.tokenizer(text, return_tensors="pt", truncation=True, max_length=128)
        with torch.inference_mode():
            logits = self.model(**encoded).logits
            probabilities = torch.sigmoid(logits).squeeze(0).tolist()
        if not isinstance(probabilities, list) or len(probabilities) != len(LABELS):
            raise ValueError("Model must return exactly ten label scores")
        scores = {label: round(float(score), 4) for label, score in zip(LABELS, probabilities)}
        matched = [label for label in LABELS[:-1] if scores[label] >= self.threshold]
        return {"selected": bool(matched), "matchedLabels": matched,
                "scores": scores, "threshold": self.threshold,
                "scope": "target-text-only", "policyVerdict": None}
