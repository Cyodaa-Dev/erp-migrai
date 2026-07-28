"""確認套件可安裝、可 import。"""

from erp_migrai import __version__


def test_version_is_exposed():
    assert __version__ == "0.0.1"
