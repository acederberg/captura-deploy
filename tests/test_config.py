# --------------------------------------------------------------------------- #
from captura_pipelines.config import PATTERN_REGISTRY


def test_pattern_registryf():
    p = PATTERN_REGISTRY
    m = p.match("https://registry.acederberg.io")
    assert m is None, "Must not contain protocol."

    m = p.match("registry.acederberg.io")
    assert m is not None
    assert m.group("host") == "registry.acederberg.io"
    assert m.group("port") is None
