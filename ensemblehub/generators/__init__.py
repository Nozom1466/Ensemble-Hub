"""
Generator modules for different inference backends
"""
from .base import BaseGenerator, GenOutput
from .hf_engine import HFGenerator
from .vllm import VLLMGenerator
from .pool import GeneratorPool

# Optional Ray-based vLLM
try:
    from .vllm_ray import VLLMRayGenerator
    __all__ = [
        "BaseGenerator",
        "GenOutput", 
        "HFGenerator",
        "VLLMGenerator",
        "VLLMRayGenerator",
        "GeneratorPool",
    ]
except ImportError:
    __all__ = [
        "BaseGenerator",
        "GenOutput", 
        "HFGenerator",
        "VLLMGenerator",
        "GeneratorPool",
    ]