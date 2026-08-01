import os
import sys
import unittest

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot


class AiChannelTests(unittest.TestCase):
    def test_greets_in_arabic(self) -> None:
        reply = bot._build_ai_reply("مرحبا")
        self.assertIn("مرحبا", reply)

    def test_detects_configured_ai_channel(self) -> None:
        self.assertTrue(bot._is_ai_channel_target(bot.AI_CHANNEL_ID))
        self.assertFalse(bot._is_ai_channel_target(999999999999999999))

    def test_responds_to_general_help(self) -> None:
        reply = bot._build_ai_reply("ساعدني")
        self.assertIn("أقدر", reply)


if __name__ == "__main__":
    unittest.main()
