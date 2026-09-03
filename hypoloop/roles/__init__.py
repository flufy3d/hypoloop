"""三个角色。每个角色只负责拼自己的提示词，怎么跑是 loop.py 的事。"""

from . import challenger, hypothesizer, verifier

__all__ = ["hypothesizer", "challenger", "verifier"]
