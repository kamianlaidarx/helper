"""艾特文本的生图意图判定：纯规则，不连接外网。"""
import unittest

from wxbot_framework.image_intent import wants_image


class IntentTests(unittest.TestCase):
    def test_requests(self):
        for text in ('画一只戴墨镜的橘猫', '帮我画张夕阳', '想看你睡觉的照片', '想看你打游戏的样子',
                     '生成一张赛博朋克街道', '来张猫的图', 'draw a cat', 'imagine a castle',
                     '我想画个猫', '帮我生成一个头像', '画个海报'):
            with self.subTest(text=text):
                self.assertTrue(wants_image(text))

    def test_chats(self):
        for text in ('图里是什么', '这张照片里是谁', '这是什么图', '你好呀', '今天天气不错',
                     '前面那张图是谁发的', '我想看图', '给我看看你的代码', '生成一份报告',
                     '这画的是什么', '帮我看看这张照片', '', '   '):
            with self.subTest(text=text):
                self.assertFalse(wants_image(text))


if __name__ == '__main__':
    unittest.main()
