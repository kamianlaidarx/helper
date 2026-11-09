"""艾特文本的生图意图判定：纯规则，不连接外网。"""
import unittest

from wxbot_framework.image_intent import is_portrait_request, wants_image


class IntentTests(unittest.TestCase):
    def test_requests(self):
        for text in ('画一只戴墨镜的橘猫', '帮我画张夕阳', '想看你睡觉的照片', '想看你打游戏的样子',
                     '生成一张赛博朋克街道', 'draw a cat', 'imagine a castle',
                     '我想画个猫', '帮我生成一个头像', '画个海报'):
            with self.subTest(text=text):
                self.assertTrue(wants_image(text))

    def test_chats(self):
        # 没有具体描述的一律当聊天："来张图""要张图""想看张照片"都交给模型追问。
        for text in ('图里是什么', '这张照片里是谁', '这是什么图', '你好呀', '今天天气不错',
                     '前面那张图是谁发的', '我想看图', '给我看看你的代码', '生成一份报告',
                     '这画的是什么', '帮我看看这张照片', '来张图', '来张猫的图',
                     '要张图', '想要一张图', '想看照片', '想看张照片', '', '   '):
            with self.subTest(text=text):
                self.assertFalse(wants_image(text))

    def test_portrait_requests_are_recognised(self):
        # 「画像」既像生图又像人物画像：image_gen 见到就让给 mention_chat。
        for text in ('@小红的画像', '画一张 @小红 的画像', '画个像 @小红', '给他画个画像照'):
            with self.subTest(text=text):
                self.assertTrue(is_portrait_request(text))

    def test_ordinary_images_are_not_portraits(self):
        for text in ('一只戴墨镜的橘猫', '帮我画张夕阳', '生成一张赛博朋克街道', '来张图', '', '   '):
            with self.subTest(text=text):
                self.assertFalse(is_portrait_request(text))


if __name__ == '__main__':
    unittest.main()
