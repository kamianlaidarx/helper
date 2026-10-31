"""测试用的拟人层随机源替身：一定回、不打错字、打字时间恒定。

拟人层默认带随机（有概率不回）和打字等待，任何断言「发了什么」的测试
都必须把它钉死，否则按概率偶发失败，还会真等好几秒。
"""


class AlwaysReply:
    def random(self):
        return 0.0

    def gauss(self, mu, sigma):
        return mu

    def randrange(self, stop):
        return 0

    def choice(self, pool):
        return pool[0]
