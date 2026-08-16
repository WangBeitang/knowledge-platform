"""pytest 公共夹具：测试环境配置隔离，不触碰真实数据库。"""

import os

import pytest

os.environ.setdefault("APP_ENV", "test")


@pytest.fixture
def anyio_backend():
    return "asyncio"
