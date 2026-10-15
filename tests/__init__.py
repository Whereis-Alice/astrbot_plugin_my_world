"""她的世界插件测试包（标准库 unittest，不依赖 AstrBot）。"""

import os

# 大量引擎 / 决策用例建立在一个"默认动作本来就开着"的世界上；这里显式保留
# 那种旧世界形态，生产默认仍然是"默认动作按需启用"。
# 新默认本身由 test_storage 的迁移用例和 defaults 的单测单独覆盖。
os.environ.setdefault("MY_WORLD_TEST_DEFAULT_ACTIONS", "1")
