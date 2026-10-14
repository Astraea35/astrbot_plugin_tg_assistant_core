"""Register the NAS Telegram platform without adding handlers to QQ."""

from astrbot.api.star import Context, Star, register

from .bridge_adapter import NasTelegramAdapter  # noqa: F401


@register(
    "astrbot_plugin_tg_assistant_core",
    "Astraea35",
    "Telegram NAS private assistant transport",
    "1.1.0",
)
class TGAssistantCorePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
