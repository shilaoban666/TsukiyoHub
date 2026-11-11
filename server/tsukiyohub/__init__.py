"""TsukiyoHub —— 与业务无关的聊天 / 技能 / RAG 内核服务。"""

__version__ = "0.1.0"

#: 默认机器人名称（可在 .env 里用 TSUKIYO_BOT_NAME 覆盖，不绑定任何公司）
DEFAULT_BOT_NAME = "Tsukiyo"

__all__ = ["__version__", "DEFAULT_BOT_NAME"]