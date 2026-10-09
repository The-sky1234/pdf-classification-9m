"""Classification分类引擎：Evidence→Gate→L1→L2→Verify，全Mock可测，真实模型可插拔。"""
from classifier.config import Thresholds
from classifier.pipeline import classify, ClassificationPipeline
from classifier.interfaces import EmbeddingBackend, RerankerBackend, LLMJudge

__all__ = ["Thresholds", "classify", "ClassificationPipeline",
           "EmbeddingBackend", "RerankerBackend", "LLMJudge"]
