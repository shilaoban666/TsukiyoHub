"""内核：聊天 / 技能 / 检索增强生成 / 记忆。

内核**只依赖** ``domain.py``、``ports/`` 与自己的子模块，
不 import ``adapters``——换模型、换向量库、换数据库都不改内核代码。
"""

__all__ = ["chat", "skills", "rag", "memory"]